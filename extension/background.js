/* 后台：抓包转发 + 取数任务执行。

两个职责：
1. 把页面里的接口记录推给本地桥（抓包，用于发现新接口）
2. 按任务定义，在对应域名的页面上下文里重放接口，把导出文件回传给本地桥

为什么要"在页面上下文里发请求"：接口依赖 Cookie 与风控参数（fp、X-Tts-Oec-Bsid 等），
这些只有那个域名的页面里才有。扩展负责带登录态，本地程序负责算账。
*/

const BRIDGE = "http://127.0.0.1:8765";
const MAX_BUFFER = 500;

let buffer = [];
let stats = { captured: 0, pushed: 0, lastError: "" };
let enabled = true;

chrome.storage.local.get({ enabled: true, stats: stats }, (s) => {
  enabled = s.enabled;
  stats = s.stats || stats;
});

// ---------------- 抓包转发 ----------------

function flush() {
  if (!enabled || buffer.length === 0) return;
  const batch = buffer.slice(0, 50);
  fetch(`${BRIDGE}/capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch }),
  }).then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    buffer = buffer.slice(batch.length);
    stats.pushed += batch.length;
    stats.lastError = "";
    chrome.storage.local.set({ stats });
  }).catch((e) => {
    stats.lastError = String(e.message || e);
    chrome.storage.local.set({ stats });
  });
}

// ---------------- 抓包记录入口 ----------------

function recordCapture(payload) {
  if (!payload) return;
  buffer.push(payload);
  stats.captured += 1;
  if (buffer.length > MAX_BUFFER) buffer.splice(0, buffer.length - MAX_BUFFER);
  chrome.storage.local.set({ stats });
  if (payload.url) learnParams(payload.url);
  flush();
}

/** 把 webRequest 的 requestBody 转成可读文本 */
function summarizeRequestBody(rb) {
  if (!rb) return null;
  try {
    if (rb.formData) {
      const parts = [];
      for (const [k, vs] of Object.entries(rb.formData)) {
        parts.push(`${k}=${(vs || []).join(",").slice(0, 200)}`);
      }
      return parts.join("&").slice(0, 4000);
    }
    if (rb.raw && rb.raw.length) {
      const chunks = rb.raw.map((r) => {
        if (!r.bytes) return "";
        const dec = new TextDecoder("utf-8", { fatal: false });
        return dec.decode(new Uint8Array(r.bytes));
      });
      return chunks.join("").slice(0, 4000);
    }
  } catch (e) { /* 二进制或读不出来就算了 */ }
  return null;
}

// 页面里的 fetch/XHR 抓不到的东西都在这一层：表单提交、iframe、Service Worker 发的请求。
// 广告导出很可能就是这类，所以必须补上。
if (chrome.webRequest && chrome.webRequest.onBeforeRequest) {
  chrome.webRequest.onBeforeRequest.addListener(
    (details) => {
      if (!enabled) return;
      const u = details.url || "";
      if (!/export|download|task|insight|stat\//i.test(u)) return;
      recordCapture({
        kind: "webrequest",
        url: u,
        method: details.method,
        status: null,
        contentType: "",
        ms: 0,
        reqBody: null,
        respSample: null,
        page: details.initiator || "",
        at: new Date().toISOString(),
      });
    },
    { urls: ["*://*.tiktok.com/*", "*://*.tiktokglobalshop.com/*"] }
  );
}

// ---------------- 抓包转发结束 ----------------
// 页面每个请求都带一长串公共参数（locale/seller_id/fp/X-Tts-Oec-Bsid…）。
// 我们不伪造，直接从抓到的真实请求里学一份最新的，重放时带上。

const LEARN_KEYS = [
  "locale", "language", "oec_seller_id", "seller_id", "aid", "app_name", "fp",
  "device_platform", "cookie_enabled", "screen_width", "screen_height",
  "browser_language", "browser_platform", "browser_name", "browser_version",
  "browser_online", "timezone_name", "X-Tts-Oec-Bsid", "aadvid", "shop_region",
  "user_language", "device_id",
];

async function learnParams(url) {
  let u;
  try { u = new URL(url); } catch (e) { return; }
  const host = u.hostname;
  const fresh = {};
  for (const k of LEARN_KEYS) {
    const v = u.searchParams.get(k);
    if (v) fresh[k] = v;
  }
  if (Object.keys(fresh).length === 0) return;
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  params[host] = { ...(params[host] || {}), ...fresh, _at: Date.now() };
  await chrome.storage.local.set({ params });
}

async function buildUrl(host, path, extra = {}) {
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  const p = params[host] || {};
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) {
    if (k.startsWith("_")) continue;
    qs.set(k, v);
  }
  for (const [k, v] of Object.entries(extra)) qs.set(k, v);
  return `https://${host}${path}?${qs.toString()}`;
}

function pickHost(params, hint) {
  return Object.keys(params).find((h) => h.includes(hint));
}

const PAGE_HINTS = {
  seller: "https://seller-my.tiktok.com/",
  affiliate: "https://affiliate.tiktok.com/affiliate/platform/homepage",
};

const PARAM_TTL_MS = 6 * 3600 * 1000;   // 参数超过 6 小时视为过期

function isFresh(p) {
  return !!(p && p._at && Date.now() - p._at < PARAM_TTL_MS);
}

/** 在已打开的标签里找一个属于该域名族的页面（用于就地刷新，不用新开页） */
async function findTab(hint) {
  try {
    const tabs = await chrome.tabs.query({
      url: ["*://*.tiktok.com/*", "*://*.tiktokglobalshop.com/*"],
    });
    for (const tb of tabs) {
      try {
        const h = new URL(tb.url).hostname;
        if (h.includes(hint)) return { host: h, tabId: tb.id };
      } catch (e) { /* 忽略坏 URL */ }
    }
  } catch (e) { /* 无权限或查询失败 */ }
  return null;
}

/**
 * 确保某个后台域的公共参数可用。三种获取途径，按成本从低到高：
 *   1. 已学到的参数且未过期 → 直接用
 *   2. 已经有该域的标签页 → 刷新它，让页面重新发请求（就地自愈）
 *   3. 都没有 → 后台开一个该域页面
 * 这样不管用户当前停在哪个页面，都能拿到可用的登录态与风控参数。
 */
async function ensureHost(hint, log, force = false) {
  const read = async () => (await chrome.storage.local.get({ params: {} })).params || {};
  let params = await read();
  let host = pickHost(params, hint);
  if (host && isFresh(params[host]) && !force) return host;
  if (host && force) log("参数可能已过期，尝试刷新页面重新获取…");

  const tab = await findTab(hint);
  if (tab) {
    log(`复用已打开的页面刷新参数：${tab.host}`);
    try { await chrome.tabs.reload(tab.tabId); } catch (e) { /* 忽略 */ }
    for (let i = 0; i < 12; i++) {
      await sleep(2000);
      params = await read();
      host = pickHost(params, hint);
      if (host && isFresh(params[host])) { log(`参数已刷新：${host}`); return host; }
    }
    if (host) return host;
  }

  const url = PAGE_HINTS[hint];
  if (url) {
    log(`后台打开页面获取参数：${url}`);
    try { await chrome.tabs.create({ url, active: false }); } catch (e) { log(`打开失败：${e.message}`); }
    for (let i = 0; i < 16; i++) {
      await sleep(2500);
      params = await read();
      host = pickHost(params, hint);
      if (host) { log(`已学到 ${host} 的参数`); return host; }
    }
  }

  const label = hint === "affiliate" ? "达人联盟后台" : "卖家后台";
  throw new Error(`拿不到${label}的参数：请手动打开一次${label}页面后重试`);
}

// ---------------- 请求封装 ----------------

async function apiPost(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  let json;
  try { json = JSON.parse(text); } catch (e) { json = { raw: text.slice(0, 300) }; }
  if (!resp.ok) throw new Error(`HTTP ${resp.status} ${text.slice(0, 200)}`);
  if (json && json.code !== undefined && json.code !== 0) {
    throw new Error(`接口返回 code=${json.code} ${json.message || json.msg || ""}`);
  }
  return json;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function uploadToBridge(name, arrayBuffer) {
  // 分块 base64，避免一次拼串把内存打爆
  const bytes = new Uint8Array(arrayBuffer);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  const b64 = btoa(bin);
  const resp = await fetch(`${BRIDGE}/upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, b64 }),
  });
  const j = await resp.json();
  if (!j.ok) throw new Error(j.error || "上传失败");
  return j;
}

// ---------------- 任务：订单 ----------------

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}_${p(d.getMinutes())}`;
}

async function runOrders({ startMs, endMs }, log) {
  const host = await ensureHost("seller", log);

  log("提交订单导出任务…");
  const fileName = encodeURIComponent(`全部 笔订单-${stamp()}.csv`);
  const created = await apiPost(
    await buildUrl(host, "/api/fulfillment/order/export"),
    {
      search_condition: {
        condition_list: { time_order_created: { value: [String(startMs), String(endMs)] } },
      },
      sort_info: "6",
      file_name: fileName,
      file_type: 2,
    }
  );
  const taskId = created?.data?.export_task_id;
  if (!taskId) throw new Error("导出任务创建失败：没有拿到 export_task_id");
  log(`任务已创建：${taskId}`);

  // 轮询导出记录，等文件生成
  let fileKey = created?.data?.file_key || "";
  for (let i = 0; i < 40; i++) {
    await sleep(4000);
    const rec = await apiPost(await buildUrl(host, "/api/fulfillment/order/export_record/get"), {});
    const list = rec?.data?.export_records || [];
    const hit = list.find((x) => String(x.export_task_id) === String(taskId));
    if (hit && hit.file_key) {
      fileKey = hit.file_key;
      log(`文件已生成：${decodeURIComponent(hit.file_name || "")}`);
      break;
    }
    log(`等待中…（${(i + 1) * 4}s）`);
  }
  if (!fileKey) throw new Error("等了约 3 分钟仍未见导出文件，可能平台还在生成");

  log("获取下载地址…");
  const dl = await apiPost(
    await buildUrl(host, "/api/fulfillment/order/download"),
    { file_key: fileKey, export_task_id: taskId }
  );
  const url = dl?.data?.download_url;
  if (!url) throw new Error("没有拿到 download_url");

  log("下载并回传…");
  const fileResp = await fetch(url, { credentials: "include" });
  if (!fileResp.ok) throw new Error(`下载失败 HTTP ${fileResp.status}`);
  const buf = await fileResp.arrayBuffer();
  const saved = await uploadToBridge(`全部 笔订单-${stamp()}.csv`, buf);
  log(`已保存：${saved.path}`);
  return { saved: saved.path, bytes: buf.byteLength };
}

// ---------------- 工具：在响应里找下载地址 ----------------

/** 递归找一个像下载地址的字符串。不同接口字段名各不相同，与其逐个猜，不如扫一遍。 */
function findUrl(obj, depth = 0) {
  if (!obj || depth > 6) return null;
  if (typeof obj === "string") {
    if (/^https?:\/\//.test(obj) && /\.(xlsx|xls|csv|zip)/i.test(obj)) return obj;
    return null;
  }
  if (Array.isArray(obj)) {
    for (const v of obj) {
      const hit = findUrl(v, depth + 1);
      if (hit) return hit;
    }
    return null;
  }
  if (typeof obj === "object") {
    for (const v of Object.values(obj)) {
      const hit = findUrl(v, depth + 1);
      if (hit) return hit;
    }
  }
  return null;
}

async function downloadAndUpload(url, name, log) {
  log(`下载中…`);
  const resp = await fetch(url, { credentials: "include" });
  if (!resp.ok) throw new Error(`下载失败 HTTP ${resp.status}`);
  const buf = await resp.arrayBuffer();
  if (buf.byteLength < 512) throw new Error("下载到的文件太小，可能还没生成好");
  const saved = await uploadToBridge(name, buf);
  log(`已保存：${saved.path}`);
  return { saved: saved.path, bytes: buf.byteLength };
}

function dstr(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// ---------------- 任务：商品 SKU 统计 ----------------

function shiftDate(s, days) {
  const d = new Date(s + "T00:00:00");
  d.setDate(d.getDate() + days);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 拉一次指定区间的全部 SKU 页面。失败直接抛，由上层决定换尺寸还是换日期。 */
async function pullSkuPages(host, start, endExclusive, size, maxPages, log) {
  const all = [];
  for (let page = 0; page < maxPages; page++) {
    const body = {
      request: {
        time_descriptor: { start, end: endExclusive },
        search: {},
        filter: {},
        list_control: {
          rules: [{ direction: 2, field: "sku_gmv" }],
          pagination: { size, page },
        },
      },
    };
    const j = await apiPost(await buildUrl(host, "/api/v2/insights/seller/ttp/product/sku/list"), body);
    const items = j?.data?.items || [];
    all.push(...items);
    if (items.length < size) return all;      // 末页
  }
  return all;
}

async function runSkuStats({ startDate, endDateExclusive }, log, opts = {}) {
  // 映射不能缓存：链接一编辑，SKU 归属就可能变，一天变一次也正常。
  // 缓存下来的旧映射会让新商品落进「未识别商品」，那种错很难被发现。
  const { skuCache: prev } = await chrome.storage.local.get({ skuCache: null });

  const host = await ensureHost("seller", log);

  // 分页尺寸不能贪：真实页面用的是 10，给 5000 会被后端拒绝。
  const SIZES = [{ size: 100, maxPages: 60 }, { size: 10, maxPages: 400 }];

  // 商品分析（洞察）数据是 T+1 的。用户选到"今天"时，结束日期指向了还没聚合的日期，
  // 后端会回 code=98001021 call downstream server error —— 这不是参数错误，是没数据。
  // 所以报这个码就把结束日期往前撤一天重试，最多撤 5 天，并明确告诉用户实际截止到哪天。
  let endEx = endDateExclusive;
  let all = null;
  let usedEnd = endEx;
  let sizeNote = "";

  for (let back = 0; back <= 7 && !all; back++) {
    for (const { size, maxPages } of SIZES) {
      try {
        log(`拉取商品SKU（区间 ${startDate} ~ ${shiftDate(endEx, -1)}，每页 ${size} 条）…`);
        const got = await pullSkuPages(host, startDate, endEx, size, maxPages, log);
        all = got;
        usedEnd = endEx;
        sizeNote = `每页 ${size} 条`;
        break;
      } catch (e) {
        const msg = String(e.message || e);
        if (back === 0 && size === SIZES[0].size) {
          const { params = {} } = await chrome.storage.local.get({ params: {} });
          const p0 = params[host] || {};
          log(`参数自检：fp=${p0.fp ? "有" : "缺"} bsid=${p0["X-Tts-Oec-Bsid"] ? "有" : "缺"} seller=${p0.oec_seller_id || "缺"}`);
        }
        if (/98001021|downstream/.test(msg)) {
          // 数据未就绪，换日期比换分页尺寸有用，直接跳出内层
          log(`该截止日期暂无聚合数据（结束 ${shiftDate(endEx, -1)}），往前撤一天`);
          if (back === 2) log("提示：可在店铺后台「商品分析」页用同样日期看是否有数据，若那里也是空，就是平台还没结算到那天");
          break;
        }
        log(`每页 ${size} 时失败：${msg}`);
      }
    }
    if (!all) {
      endEx = shiftDate(endEx, -1);
      if (back === 1) {
        // 撤了一次日期还不行，说明可能不是日期问题：把风控参数重新学一遍再试
        log("撤日期仍未成功，尝试刷新登录态与风控参数…");
        try { await ensureHost("seller", log, true); } catch (e) { log(`刷新参数失败：${e.message}`); }
      }
    }
  }

  if (!all) throw new Error("商品SKU取数失败：连续 7 个截止日期都取不到数据，请确认店铺后台「商品分析」页能正常打开");

  if (!all.length) throw new Error("商品SKU返回 0 条：该区间可能没有任何商品数据");

  const bytes = new TextEncoder().encode(JSON.stringify({ data: { items: all } }));
  const name = `sku_list_${startDate}_${shiftDate(usedEnd, -1)}.json`;
  const saved = await uploadToBridge(name, bytes.buffer);
  await chrome.storage.local.set({ skuCache: { at: Date.now(), file: saved.path, items: all.length } });

  const realEnd = shiftDate(usedEnd, -1);
  if (realEnd !== shiftDate(endDateExclusive, -1)) {
    log(`注意：平台数据截至 ${realEnd}（T+1 延迟），不是你选的 ${shiftDate(endDateExclusive, -1)}`);
  }
  if (prev && prev.items != null && prev.items !== all.length) {
    const delta = all.length - prev.items;
    log(`映射有变化：${prev.items} → ${all.length} 条（${delta > 0 ? "+" : ""}${delta}）`);
  }
  log(`已保存：${saved.path}（${all.length} 条 SKU，${sizeNote}）`);
  return { saved: saved.path, items: all.length, realEnd };
}

// ---------------- 任务：联盟佣金 ----------------

async function runAffiliate({ startDate, endDateExclusive, tzOffset }, log) {
  const host = await ensureHost("affiliate", log);

  const startSec = Math.floor(new Date(startDate + "T00:00:00").getTime() / 1000);
  const endSec = Math.floor(new Date(endDateExclusive + "T00:00:00").getTime() / 1000) - 1;
  const tz = tzOffset ?? -new Date().getTimezoneOffset() * 60;

  log("提交联盟佣金导出…");
  const body = {
    module_type: 104,
    transaction_product_list_param: {
      time_descriptor: {
        granularity_type: 1,
        timezone_offset: tz,
        start_time: startSec,
        end_time: endSec,
        time_option_type: 1,
      },
      metric_types: [1, 3, 2, 4, 9, 20, 13, 14, 19, 6, 12, 11, 16, 17, 18, 8, 7, 10, 5],
      sorter: { sort_type: 1, order_type: 1 },
      filter: {},
    },
    locale: "zh-CN",
  };
  const created = await apiPost(
    await buildUrl(host, "/api/v1/oec/affiliate/compass/export_task/create"),
    body
  );
  const taskId = created?.data?.task_id || "";
  log(`任务已创建：${taskId || "(无 task_id)"}`);

  // 轮询任务列表，找到可下载的地址
  for (let i = 0; i < 40; i++) {
    await sleep(5000);
    const list = await apiPost(
      await buildUrl(host, "/api/v1/oec/affiliate/compass/export_task/list"),
      { module_type: 104, locale: "zh-CN" }
    );
    const url = findUrl(list);
    if (url) {
      return downloadAndUpload(url, `Transaction_Analysis_${startDate}_${endDateExclusive}.xlsx`, log);
    }
    log(`等待任务完成…（${(i + 1) * 5}s）`);
  }
  throw new Error("等了约 3 分钟仍没有可下载地址（接口结构可能变了）");
}

// ---------------- 任务：广告（只拉已完成的任务） ----------------

const ADS_DONE_KEY = "adsDownloaded";

async function getAdDone() {
  const { [ADS_DONE_KEY]: v } = await chrome.storage.local.get({ [ADS_DONE_KEY]: [] });
  return Array.isArray(v) ? v : [];
}

async function markAdDone(ids) {
  const cur = await getAdDone();
  const next = Array.from(new Set([...cur, ...ids])).slice(-200);
  await chrome.storage.local.set({ [ADS_DONE_KEY]: next });
}

/** 拉一次广告导出任务列表，把所有「已完成且没下过」的任务下载回来。 */
async function pullAdsTasks(host, log) {
  const r = await apiPost(
    await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/download_task/query"),
    { download_scene: "offline_export" }
  );
  const tasks = r?.offline_tasks || [];
  const done = await getAdDone();

  const ready = tasks.filter((x) => x && x.url && x.download_id && !done.includes(x.download_id));
  const running = tasks.filter((x) => x && x.task_status && x.task_status !== "FINISHED");

  const saved = [];
  for (const tk of ready) {
    const name = (tk.file_name || "creative data").replace(/[\\/:*?"<>|]/g, "_") + ".xlsx";
    log(`下载广告导出：${name}`);
    const one = await downloadAndUpload(tk.url, name, log);
    saved.push({ ...one, id: tk.download_id });
    await markAdDone([tk.download_id]);
  }
  return { saved, running: running.length, total: tasks.length, ready: ready.length };
}

async function runAds(range, log, opts = {}) {
  const quick = !!opts.quick;
  const host = await ensureHost("seller", log);

  log("查询广告导出任务…");
  const maxRounds = quick ? 1 : 20;      // 非快查模式最多等 5 分钟
  for (let i = 0; i < maxRounds; i++) {
    const r = await pullAdsTasks(host, log);
    if (r.saved.length) {
      log(`已下载 ${r.saved.length} 个广告文件`);
      return { saved: r.saved };
    }
    if (quick) {
      log(r.running
        ? `广告任务进行中（${r.running} 个），完成后会自动下载，不用再管`
        : "暂无已完成的广告导出任务（需要先在广告页面点一次导出）");
      return { skipped: true };
    }
    log(`等待广告任务完成…（${(i + 1) * 15}s，进行中 ${r.running} 个）`);
    await sleep(15000);
  }
  log("广告任务还没完成。它会在后台继续等待，好了自动下载，不用再点任何东西");
  return { pending: true };
}

// ---------------- 广告任务后台守望 ----------------
// 创建导出这一步走的是页面上的表单/SDK，三层监听都抓不到它的接口，
// 所以换个思路：不接管"创建"，只盯着"结果"。
// 用户点一次导出后，这里每 3 分钟查一次，任务一完成就自动下载落盘。

async function pollAdsQuiet() {
  try {
    const { params = {} } = await chrome.storage.local.get({ params: {} });
    const host = pickHost(params, "seller-") || pickHost(params, "seller");
    if (!host) return;
    const log = (m) => appendTaskLog(`[ads-poll] ${m}`);
    const r = await pullAdsTasks(host, log);
    if (r.saved.length) {
      log(`自动下载完成 ${r.saved.length} 个文件`);
    }
  } catch (e) {
    // 静默：这里失败不打扰用户，用户主动点「广告」时才会报错
  }
}

try {
  if (chrome.alarms) {
    chrome.alarms.create("ads-poll", { periodInMinutes: 3, delayInMinutes: 2 });
    chrome.alarms.onAlarm.addListener((a) => {
      if (a.name === "ads-poll") pollAdsQuiet();
    });
  }
} catch (e) {
  console.warn("广告守望注册失败：", e);
}

// ---------------- 定时自动取数 ----------------
// 扩展不能像 cron 那样精确定时，标准做法是：每 15 分钟醒一次，
// 检查"现在是否到了该跑的时间点、今天是否已经跑过"，满足就执行。

const DEFAULT_SCHEDULE = {
  weekly: { enabled: false, weekday: 1, hour: 9 },   // 1 = 周一
  monthly: { enabled: false, day: 1, hour: 9 },
  lastRun: { weekly: "", monthly: "" },
};

async function getSchedule() {
  const { schedule } = await chrome.storage.local.get({ schedule: DEFAULT_SCHEDULE });
  return { ...DEFAULT_SCHEDULE, ...schedule, lastRun: { ...DEFAULT_SCHEDULE.lastRun, ...(schedule?.lastRun || {}) } };
}

function rangeFor(mode, now) {
  const fmt = (d) => {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  if (mode === "weekly") {
    // 上周一 ~ 上周日
    const dow = (now.getDay() + 6) % 7;              // 周一=0
    const thisMonday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - dow);
    const from = new Date(thisMonday.getTime() - 7 * 86400000);
    const to = new Date(thisMonday.getTime() - 86400000);
    return { startDate: fmt(from), endDateExclusive: fmt(thisMonday), to: fmt(to) };
  }
  // 上月 1 日 ~ 月末
  const firstThis = new Date(now.getFullYear(), now.getMonth(), 1);
  const from = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  const to = new Date(firstThis.getTime() - 86400000);
  return { startDate: fmt(from), endDateExclusive: fmt(firstThis), to: fmt(to) };
}

function toRangeObj(r) {
  const start = new Date(r.startDate + "T00:00:00");
  const endEx = new Date(r.endDateExclusive + "T00:00:00");
  const endLast = new Date(endEx.getTime() - 1);
  return {
    startMs: start.getTime(),
    endMs: new Date(endLast.getFullYear(), endLast.getMonth(), endLast.getDate(), 23, 59, 59, 999).getTime(),
    startDate: r.startDate,
    endDateExclusive: r.endDateExclusive,
    tzOffset: -new Date().getTimezoneOffset() * 60,
  };
}

async function checkSchedule() {
  const s = await getSchedule();
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const dow = (now.getDay() + 6) % 7;

  const jobs = [];
  if (s.weekly.enabled && dow === s.weekly.weekday && now.getHours() >= s.weekly.hour
      && s.lastRun.weekly !== today) {
    jobs.push(["weekly", rangeFor("weekly", now)]);
  }
  if (s.monthly.enabled && now.getDate() === s.monthly.day && now.getHours() >= s.monthly.hour
      && s.lastRun.monthly !== today) {
    jobs.push(["monthly", rangeFor("monthly", now)]);
  }
  if (!jobs.length) return null;

  for (const [mode, r] of jobs) {
    const log = (m) => appendTaskLog(`[auto-${mode}] ${m}`);
    log(`开始自动取数：${r.startDate} ~ ${r.to}`);
    try {
      await runTaskThenCalc("all", toRangeObj(r), log);
      s.lastRun[mode] = today;
      await chrome.storage.local.set({ schedule: s });
      log(`完成`);
      fetch(`${BRIDGE}/log`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ msg: `[auto-${mode}] 解锁：自动取数完成 ${r.startDate} ~ ${r.to}` }),
      }).catch(() => {});
    } catch (e) {
      log(`失败：${e.message}`);
    }
  }
  return jobs.map((j) => j[0]).join(",");
}

// 定时器注册必须包起来：缺 alarms 权限时这里是会抛异常的，
// 一旦抛出去，后面的消息路由就全注册不上，扩展表现为"完全没反应"。
try {
  if (chrome.alarms) {
    chrome.alarms.create("jingli-scheduler", { periodInMinutes: 15, delayInMinutes: 1 });
    chrome.alarms.onAlarm.addListener((a) => {
      if (a.name === "jingli-scheduler") checkSchedule();
    });
  } else {
    console.warn("alarms 不可用，定时功能失效");
  }
} catch (e) {
  console.warn("定时器注册失败：", e);
}


// ---------------- 深度抓包（调试协议） ----------------
// 有些请求既不走页面 fetch/XHR，也不在 webRequest 视野里（由签名 SDK 在 worker 里发）。
// chrome.debugger 直接挂在标签页的网络层，能看到该页发出的全部请求。
// 代价：浏览器顶部会出现"正在调试"提示条，抓完自动断开。

let deepTimer = null;
let deepKeepAlive = null;

async function deepCapture(seconds, log) {
  // 只挂当前标签页是不够的：用户很可能在另一个标签页点导出，
  // 那样什么都看不到。这里把所有 tiktok 域名的标签页都挂上。
  const tabs = await chrome.tabs.query({
    url: ["*://*.tiktok.com/*", "*://*.tiktokglobalshop.com/*"],
  });
  if (!tabs.length) throw new Error("没有找到 tiktok 标签页，请先打开店铺后台");

  const ids = new Set();
  for (const tb of tabs) {
    try {
      await chrome.debugger.attach({ tabId: tb.id }, "1.3");
      await chrome.debugger.sendCommand({ tabId: tb.id }, "Network.enable", {});
      ids.add(tb.id);
    } catch (e) {
      // 已经挂着调试器的标签页会失败，跳过即可
    }
  }
  if (!ids.size) throw new Error("没能挂上任何标签页的调试器（可能已有其它调试会话）");
  log(`已挂载 ${ids.size} 个标签页，${seconds} 秒内请完成要记录的操作`);

  // 顺手扫一遍页面上的按钮文案：广告导出的创建接口抓不到，
  // 那就改成"模拟点击导出按钮"。先要知道那个按钮到底叫什么。
  for (const tb of tabs) {
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tb.id },
        func: () => {
          const out = [];
          const seen = new Set();
          const nodes = document.querySelectorAll("button, [role=button], a, [class*=btn], [class*=Button]");
          for (const el of nodes) {
            const txt = (el.innerText || "").trim().replace(/\s+/g, " ");
            if (!txt || txt.length > 24 || seen.has(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 20 || r.height < 10) continue;
            seen.add(txt);
            out.push(txt);
          }
          return out.slice(0, 60);
        },
      });
      const list = (res && res.result) || [];
      const hit = list.filter((x) => /导出|导出记录|Export|Eksport|Xuất|ส่งออก/i.test(x));
      log(`页面按钮扫描（${(tb.url || "").split("?")[0].slice(-40)}）：共 ${list.length} 个`);
      if (hit.length) log(`  含"导出"的按钮：${hit.join(" | ")}`);
      log(`  全部：${list.slice(0, 25).join(" / ")}`);
    } catch (e) {
      // 页面可能还没加载完或不允许注入，忽略
    }
  }

  const onEvent = (source, method, params) => {
    if (!ids.has(source.tabId) || method !== "Network.requestWillBeSent") return;
    const req = params.request || {};
    const url = req.url || "";
    // GET/OPTIONS 是静态资源和预检，记下来只会淹没真正有用的信息
    if (req.method === "GET" || req.method === "OPTIONS") return;
    recordCapture({
      kind: "debugger",
      url,
      method: req.method,
      status: null,
      contentType: "",
      ms: 0,
      reqBody: (req.postData || "").slice(0, 4000) || null,
      respSample: null,
      page: "",
      at: new Date().toISOString(),
    });
  };

  chrome.debugger.onEvent.addListener(onEvent);

  // MV3 的 service worker 闲置会被回收，抓包期间要持续喂它一下
  deepKeepAlive = setInterval(() => {
    chrome.runtime.getPlatformInfo().catch(() => {});
  }, 20000);

  const stop = async (reason) => {
    chrome.debugger.onEvent.removeListener(onEvent);
    if (deepTimer) { clearTimeout(deepTimer); deepTimer = null; }
    if (deepKeepAlive) { clearInterval(deepKeepAlive); deepKeepAlive = null; }
    for (const id of ids) {
      try { await chrome.debugger.detach({ tabId: id }); } catch (e) { /* 已断开 */ }
    }
    log(`深度抓包结束（${reason}），已记录 ${ids.size} 个标签页的请求`);
  };

  deepTimer = setTimeout(() => stop(`${seconds} 秒到`), seconds * 1000);
  return { ok: true, tabs: ids.size, seconds };
}


// 长任务保活：面板中途关掉后，MV3 的后台进程可能被判定空闲而回收，
// 任务就断在半路。每 20 秒做一次极轻的 API 调用，让它别睡。
let taskKeepAlive = null;

function startKeepAlive() {
  if (taskKeepAlive) return;
  taskKeepAlive = setInterval(() => {
    try { chrome.runtime.getPlatformInfo().catch(() => {}); } catch (e) { /* 忽略 */ }
  }, 20000);
}

function stopKeepAlive() {
  if (taskKeepAlive) { clearInterval(taskKeepAlive); taskKeepAlive = null; }
}

// ---------------- 核算 ----------------
// 取数完成后让本地桥跑一次核算（Python 子进程），点一下就能看到净利，
// 不用再切到命令行。核算是同步阻塞的，通常十几秒到一两分钟。

async function calcNow(range, log, opts = {}) {
  const { settings = {} } = await chrome.storage.local.get({ settings: {} });
  const country = opts.country || settings.country || "马来西亚";
  const from = range.startDate;
  const to = range.to || (range.endDateExclusive ? shiftDate(range.endDateExclusive, -1) : "");

  log(`开始核算：${country} ${from} ~ ${to}`);
  let resp;
  try {
    resp = await fetch(`${BRIDGE}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ country, from, to }),
    });
  } catch (e) {
    throw new Error(`连不上本地桥（${e.message}），请确认 scripts\\start_bridge.bat 在跑`);
  }

  let j;
  try { j = await resp.json(); } catch (e) { throw new Error(`核算接口返回异常（HTTP ${resp.status}）`); }
  if (!j.ok) throw new Error(j.error || "核算失败");

  log(`报表：${j.file}`);
  log(`GMV ${j.gmv}｜净利 ${j.net_local}（人民币 ${j.net_cny}）｜净利率 ${(j.net_margin * 100).toFixed(2)}%`);
  log(`商品 ${j.products}｜订单 ${j.orders}｜异常 ${j.issues} 条`);
  await chrome.storage.local.set({ lastCalc: { at: Date.now(), ...j } });
  return j;
}

async function runTaskThenCalc(task, range, log, opts = {}) {
  const r = await runTask(task, range, log, opts);
  try {
    return { fetch: r, calc: await calcNow(range, log, opts) };
  } catch (e) {
    log(`核算失败：${e.message}（取数已成功，数据在 data/exports/_inbox）`);
    return { fetch: r, calcError: String(e.message || e) };
  }
}

// ---------------- 消息路由 ----------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;

  if (msg.type === "capture") {
    recordCapture(msg.payload);
    sendResponse({ ok: true, buffered: buffer.length });
    return true;
  }

  if (msg.type === "state") {
    chrome.storage.local.get({ stats, params: {}, running: null, lastResult: null, lastCalc: null }, (s) => {
      sendResponse({
        enabled,
        buffered: buffer.length,
        stats: s.stats,
        learnedHosts: Object.keys(s.params || {}),
        running: s.running,
        lastResult: s.lastResult,
        lastCalc: s.lastCalc,
      });
    });
    return true;
  }

  if (msg.type === "toggle") {
    enabled = !!msg.enabled;
    chrome.storage.local.set({ enabled });
    if (enabled) flush();
    sendResponse({ ok: true, enabled });
    return true;
  }

  if (msg.type === "deepCapture") {
    const log = (m) => appendTaskLog(`[debug] ${m}`);
    deepCapture(msg.seconds || 120, log)
      .then((r) => sendResponse({ ok: true, result: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }

  if (msg.type === "getLog") {
    chrome.storage.local.get({ [LOG_KEY]: [] }, (s) => sendResponse({ ok: true, logs: s[LOG_KEY] }));
    return true;
  }

  if (msg.type === "clearLog") {
    chrome.storage.local.set({ [LOG_KEY]: [] }, () => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "getSchedule") {
    getSchedule().then((s) => sendResponse({ ok: true, schedule: s }));
    return true;
  }

  if (msg.type === "setSchedule") {
    (async () => {
      const cur = await getSchedule();
      const next = { ...cur, ...msg.schedule };
      await chrome.storage.local.set({ schedule: next });
      sendResponse({ ok: true, schedule: next });
    })();
    return true;
  }

  if (msg.type === "checkScheduleNow") {
    checkSchedule()
      .then((r) => sendResponse({ ok: true, ran: r }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true;
  }

  if (msg.type === "runTask") {
    // 任务可能跑好几分钟（广告要等导出、SKU 要撤日期重试）。
    // 等它结束再 sendResponse，面板一关或扩展一重载消息通道就断了，
    // 用户看到的是"没有响应"——所以先回执，任务在后台继续，结果写 storage 让面板自己取。
    // 注意：这里必须 return true（异步响应），否则扩展的后台进程会在回执之后
    // 被浏览器判定为"空闲"并终止，任务根本没机会跑。
    const finish = (obj) => { try { sendResponse(obj); } catch (e) { /* 面板已关 */ } };

    (async () => {
      const task = msg.task;
      const log = (m) => appendTaskLog(`[${task}] ${m}`);
      startKeepAlive();
      await chrome.storage.local.set({ running: { task, at: Date.now() } });
      try {
        log(`开始（${msg.range?.startDate} ~ ${msg.range?.to || msg.range?.endDateExclusive}）`);
        const withCalc = msg.withCalc !== false && task !== "ads";
        const result = withCalc
          ? await runTaskThenCalc(task, msg.range || {}, log, { force: !!msg.force })
          : await runTask(task, msg.range || {}, log, { force: !!msg.force });
        await chrome.storage.local.set({
          lastResult: { task, ok: true, at: Date.now(), result },
        });
        log("完成");
        finish({ ok: true, started: true, done: true, result });
      } catch (e) {
        const err = String(e.message || e);
        log(`失败：${err}`);
        await chrome.storage.local.set({ lastResult: { task, ok: false, at: Date.now(), error: err } });
        finish({ ok: false, started: true, error: err });
      } finally {
        stopKeepAlive();
        // 清掉 running 前留一点余量，避免面板刚好在中间轮询到空
        setTimeout(() => chrome.storage.local.set({ running: null }), 1500);
      }
    })();
    return true;
  }

  if (msg.type === "calcNow") {
    const finish = (obj) => { try { sendResponse(obj); } catch (e) { /* 面板已关 */ } };
    (async () => {
      const log = (m) => appendTaskLog(`[calc] ${m}`);
      startKeepAlive();
      await chrome.storage.local.set({ running: { task: "calc", at: Date.now() } });
      try {
        const j = await calcNow(msg.range || {}, log, { country: msg.country });
        await chrome.storage.local.set({ lastResult: { task: "calc", ok: true, at: Date.now() } });
        finish({ ok: true, started: true, done: true, result: j });
      } catch (e) {
        const err = String(e.message || e);
        log(`失败：${err}`);
        await chrome.storage.local.set({ lastResult: { task: "calc", ok: false, at: Date.now(), error: err } });
        finish({ ok: false, started: true, error: err });
      } finally {
        stopKeepAlive();
        setTimeout(() => chrome.storage.local.set({ running: null }), 1500);
      }
    })();
    return true;
  }

  return false;
});

setInterval(flush, 3000);

// 启动心跳：用来确认后台脚本已经换到新版本
(function announce() {
  try {
    fetch(`${BRIDGE}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: `[扩展] 后台启动 v${chrome.runtime.getManifest().version}，webRequest=${!!(chrome.webRequest && chrome.webRequest.onBeforeRequest)}，alarms=${!!chrome.alarms}` }),
    }).catch(() => {});
  } catch (e) { /* 忽略 */ }
})();
