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
        reqBody: summarizeRequestBody(details.requestBody),
        respSample: null,
        page: details.initiator || "",
        at: new Date().toISOString(),
      });
    },
    { urls: ["*://*.tiktok.com/*", "*://*.tiktokglobalshop.com/*"] },
    ["requestBody"]
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

async function runSkuStats({ startDate, endDateExclusive }, log) {
  const host = await ensureHost("seller", log);

  log("提交商品SKU导出…");
  const body = {
    request: {
      list_control: {
        rules: [{ direction: 2, field: "sku_gmv" }],
        pagination: { size: 10000, page: 0 },
      },
      filter: {},
      time_descriptor: { start: startDate, end: endDateExclusive },
      search: {},
    },
  };
  const r = await apiPost(
    await buildUrl(host, "/api/v2/insights/seller/ttp/product/sku/list/export"),
    body
  );
  const file = r?.data?.file;
  if (!file) throw new Error("没拿到文件路径（data.file 为空）");
  log(`文件路径：${file}`);

  const name = `product_sku_list-${startDate}_${endDateExclusive}.xlsx`;
  const direct = file.startsWith("http") ? file : `https://${host}${file}`;

  for (let i = 0; i < 40; i++) {
    // 路子一：查导出任务列表，里面通常带可下载地址
    try {
      const tl = await apiPost(
        await buildUrl(host, "/api/v2/insights/seller/shop/export/task/list"),
        { sub_modules: [80] }
      );
      let url = findUrl(tl);
      const n = tl?.total_files ?? 0;
      if (i === 0 || url) log(`任务列表：total_files=${n}${url ? "，已拿到下载地址" : ""}`);
      if (url) {
        if (url.startsWith("/")) url = `https://${host}${url}`;
        return downloadAndUpload(url, name, log);
      }
    } catch (e) {
      log(`查任务列表出错：${e.message}`);
    }

    // 路子二：直接取文件路径
    try {
      const resp = await fetch(direct, { credentials: "include" });
      const ct = resp.headers.get("content-type") || "";
      if (resp.ok && !ct.includes("json")) {
        const buf = await resp.arrayBuffer();
        if (buf.byteLength > 512) {
          const saved = await uploadToBridge(name, buf);
          log(`已保存：${saved.path}`);
          return { saved: saved.path, bytes: buf.byteLength };
        }
      } else if (i % 4 === 0) {
        let body = "";
        try { body = (await resp.text()).slice(0, 150); } catch (e) { /* 忽略 */ }
        log(`直取返回 ${resp.status} ${ct.split(";")[0]} ${body.replace(/\s+/g, " ")}`);
      }
    } catch (e) {
      if (i % 4 === 0) log(`直取出错：${e.message}`);
    }

    log(`等待文件生成…（${(i + 1) * 5}s）`);
    await sleep(5000);
  }
  throw new Error("等了约 3 分钟仍取不到文件，日志里有每次返回的状态供排查");
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

async function runAds(range, log, opts = {}) {
  const quick = !!opts.quick;
  const host = await ensureHost("seller", log);

  log("查询广告导出任务…");
  const maxRounds = quick ? 1 : 60;
  for (let i = 0; i < maxRounds; i++) {
    const r = await apiPost(
      await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/download_task/query"),
      { download_scene: "offline_export" }
    );
    const tasks = r?.offline_tasks || [];
    const done = tasks.find((t) => t && t.url);
    if (done) {
      log(`发现已完成任务：${done.file_name || done.download_id}`);
      return downloadAndUpload(done.url, `${done.file_name || "creative data"}.xlsx`, log);
    }
    const running = tasks.find((t) => t && t.task_status && t.task_status !== "FINISHED");
    if (quick) {
      log(running ? `广告任务进行中（${running.task_status}），稍后可单独点「广告」拉取` : "暂无已完成的广告导出任务");
      return { skipped: true };
    }
    log(`任务状态：${running ? running.task_status : "无进行中的任务"}（${(i + 1) * 10}s）`);
    await sleep(10000);
  }
  throw new Error("十分钟内没有等到已完成的广告导出任务（广告导出需要先手工点一次）");
}

// ---------------- 任务调度 ----------------

const TASK_IMPL = {
  orders: runOrders,
  sku: runSkuStats,
  affiliate: runAffiliate,
  ads: runAds,
};
const TASK_HINT = { orders: "seller", sku: "seller", affiliate: "affiliate", ads: "seller" };

async function runTask(task, range, log, opts = {}) {
  if (task === "all") {
    const out = {};
    for (const t of ["orders", "sku", "affiliate", "ads"]) {
      try {
        log(`—— 开始：${t} ——`);
        out[t] = await runTask(t, range, log, t === "ads" ? { quick: true } : {});
      } catch (e) {
        log(`—— ${t} 失败：${e.message}`);
        out[t] = { error: String(e.message || e) };
      }
    }
    return out;
  }

  const impl = TASK_IMPL[task];
  if (!impl) throw new Error(`暂不支持的取数任务：${task}`);

  try {
    return await impl(range, log, opts);
  } catch (e) {
    const msg = String(e.message || e);
    // 登录态或风控参数过期是最常见的失败原因：自愈一次再试，别直接甩给用户
    if (/40[123]|风控|登录|code=|参数|学不到/.test(msg)) {
      log("可能登录态或风控参数失效，自动刷新页面后重试一次…");
      try {
        await ensureHost(TASK_HINT[task], log, true);
      } catch (e2) {
        throw new Error(`${msg}（自愈失败：${e2.message}）`);
      }
      return await impl(range, log, opts);
    }
    throw e;
  }
}

function bridgeLog(msg) {
  fetch(`${BRIDGE}/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msg: String(msg) }),
  }).catch(() => {});
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
    const log = (m) => bridgeLog(`[auto-${mode}] ${m}`);
    log(`开始自动取数：${r.startDate} ~ ${r.to}`);
    try {
      await runTask("all", toRangeObj(r), log);
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

chrome.alarms.create("jingli-scheduler", { periodInMinutes: 15, delayInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "jingli-scheduler") checkSchedule();
});

// ---------------- 消息路由 ----------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;

  if (msg.type === "capture") {
    recordCapture(msg.payload);
    sendResponse({ ok: true, buffered: buffer.length });
    return true;
  }

  if (msg.type === "state") {
    chrome.storage.local.get({ stats, params: {} }, (s) => {
      sendResponse({
        enabled,
        buffered: buffer.length,
        stats: s.stats,
        learnedHosts: Object.keys(s.params || {}),
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
    const log = (m) => {
      bridgeLog(`[${msg.task}] ${m}`);
      // 面板没打开时 sendMessage 会抛，别让日志影响任务本身
      try {
        const p = chrome.runtime.sendMessage({ type: "taskLog", msg: m });
        if (p && typeof p.catch === "function") p.catch(() => {});
      } catch (e) { /* 无接收方，忽略 */ }
    };
    runTask(msg.task, msg.range || {}, log)
      .then((r) => {
        bridgeLog(`[${msg.task}] 完成`);
        sendResponse({ ok: true, result: r });
      })
      .catch((e) => {
        bridgeLog(`[${msg.task}] 失败：${e.message}`);
        sendResponse({ ok: false, error: String(e.message || e) });
      });
    return true;  // 异步响应
  }

  return false;
});

setInterval(flush, 3000);
