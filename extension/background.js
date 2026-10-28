/* 后台：抓包转发 + 取数任务执行。

两个职责：
1. 把页面里的接口记录推给本地桥（抓包，用于发现新接口）
2. 按任务定义，在对应域名的页面上下文里重放接口，把导出文件回传给本地桥

为什么要"在页面上下文里发请求"：接口依赖 Cookie 与风控参数（fp、X-Tts-Oec-Bsid 等），
这些只有那个域名的页面里才有。扩展负责带登录态，本地程序负责算账。
*/

const BRIDGE = "http://127.0.0.1:8765";

// 后台脚本里的异常默认只出现在开发者工具的 console 里，用户看不到，
// 排查时等于黑箱。这里把所有未捕获错误直接送到本地桥的日志。
function reportCrash(tag, detail) {
  try {
    fetch(`${BRIDGE}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: `[崩溃] ${tag}：${detail}` }),
    }).catch(() => {});
  } catch (e) { /* 连桥都发不出去，只能放弃 */ }
}

try {
  self.addEventListener("error", (e) => {
    reportCrash("未捕获异常", `${e.message || ""} @${e.filename || ""}:${e.lineno || ""}`);
  });
  self.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    reportCrash("未处理的 Promise 拒绝", (r && (r.stack || r.message)) || String(r));
  });
} catch (e) { /* 环境不支持，忽略 */ }

// ---------------- 日志 ----------------
// 这两个函数（连同 LOG_KEY）曾经只有引用没有定义——v0.3.6 加日志落盘时
// 一次字符串替换没匹配上，之后所有任务在写第一行日志时就抛
// ReferenceError: appendTaskLog is not defined，
// 而且抛错位置在异步链的最外层，表现为「点了没反应、日志空白」。

const HEADER_KEY = "headerParams";
const LOG_KEY = "taskLogs";
const LOG_MAX = 300;

/** 只送到本地桥（黑窗口 + task.log），不落扩展存储。 */
function bridgeLog(msg) {
  try {
    fetch(`${BRIDGE}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg: String(msg) }),
    }).catch(() => {});
  } catch (e) { /* 桥没开 */ }
}

/** 任务日志：桥 + 扩展存储（面板重开可恢复）+ 实时推给面板。 */
async function appendTaskLog(msg) {
  const line = `${new Date().toLocaleTimeString("zh-CN", { hour12: false })}  ${msg}`;
  bridgeLog(msg);

  try {
    const cur = await skGet({ [LOG_KEY]: [] });
    const arr = Array.isArray(cur[LOG_KEY]) ? cur[LOG_KEY] : [];
    arr.push(line);
    if (arr.length > LOG_MAX) arr.splice(0, arr.length - LOG_MAX);
    await skSet({ [LOG_KEY]: arr });
  } catch (e) { /* 存储失败不影响任务 */ }

  try {
    const p = chrome.runtime.sendMessage({ type: "taskLog", msg });
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (e) { /* 面板没开 */ }
}

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
  if (payload.url) {
    learnParams(payload.url);
    try {
      learnHeaders(new URL(payload.url).hostname, payload.reqHeaders);
    } catch (e) { /* URL 不合法就跳过 */ }
  }
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

/** 学一份请求头。真实请求里带的 x-* 头（风控、追踪、会话标识）常常是
 *  接口判定的必要条件，缺了会返回含糊的 downstream server error。 */
async function learnHeaders(host, reqHeaders) {
  if (!host || !reqHeaders) return;
  const clean = {};
  for (const [k, v] of Object.entries(reqHeaders)) {
    if (!k || v == null) continue;
    const key = k.toLowerCase();
    if (key === "content-type" || key === "cookie" || key === "authorization") continue;
    clean[key] = String(v);
  }
  if (!Object.keys(clean).length) return;
  const cur = await skGet({ [HEADER_KEY]: {} });
  const all = cur[HEADER_KEY] || {};
  all[host] = { ...(all[host] || {}), ...clean, _at: Date.now() };
  await skSet({ [HEADER_KEY]: all });
}

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
  let host = "";
  try { host = new URL(url).hostname; } catch (e) { /* 忽略 */ }
  const { [HEADER_KEY]: hp = {} } = await skGet({ [HEADER_KEY]: {} });
  const learned = (hp[host] || {});
  const headers = { "Content-Type": "application/json" };
  for (const [k, v] of Object.entries(learned)) {
    if (k.startsWith("_")) continue;
    headers[k] = v;
  }

  const resp = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
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

/** storage 读写统一走回调包装。
 *  老版 Chromium 的 chrome.storage 只有回调形式，直接 await 会拿到 undefined，
 *  写法上不报错但行为不对——这里统一收口。 */
function skGet(defaults) {
  return new Promise((resolve) => {
    try {
      chrome.storage.local.get(defaults, (v) => resolve(v || defaults));
    } catch (e) {
      resolve(defaults);
    }
  });
}

function skSet(obj) {
  return new Promise((resolve) => {
    try {
      chrome.storage.local.set(obj, () => resolve(true));
    } catch (e) {
      resolve(false);
    }
  });
}

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


// ---------------- 在页面上下文里发请求 ----------------
// 这是取数能通的关键。同样一个请求：
//   · 页面自己发 → Referer 是店铺域名、Origin 同源 → 服务端正常返回
//   · 扩展后台发 → Origin 是 chrome-extension://、Referer 也是扩展 → 风控直接拒
// 实测同一份 body，页面 status 200，后台 fetch 回 code=98001021，
// 差别只在发起方。所以取数一律在目标页面的上下文里执行。

async function findTabFor(host) {
  const tabs = await chrome.tabs.query({ url: [`*://${host}/*`] });
  const tab = (tabs || []).find((x) => x && x.id);
  return tab || null;
}

/** 在页面里跑一段函数，拿到 JSON。页面没开时返回 null，由调用方决定回退。 */
async function runInPage(host, func, args) {
  let tab = await findTabFor(host);
  if (!tab) {
    // 参数是新鲜的，ensureHost 不会开页，但页面上下文取数必须有 tab
    try { await chrome.tabs.create({ url: `https://${host}/`, active: false }); } catch (e) { /* 忽略 */ }
    for (let i = 0; i < 10 && !tab; i++) {
      await sleep(2000);
      tab = await findTabFor(host);
    }
  }
  if (!tab) return { missingTab: true };
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
    return { result: res && res.result };
  } catch (e) {
    return { error: String(e && e.message || e) };
  }
}

/** 分页拉全量商品 SKU（在页面上下文里循环，一次调用拿完）。 */
async function pagePullSku(host, start, endExclusive, size, maxPages) {
  const url = await buildUrl(host, "/api/v2/insights/seller/ttp/product/sku/list");

  const r = await runInPage(host, async (u, s, e, sz, mp) => {
    const all = [];
    for (let page = 0; page < mp; page++) {
      const body = {
        request: {
          time_descriptor: { start: s, end: e },
          search: {},
          filter: {},
          list_control: {
            rules: [{ direction: 2, field: "sku_gmv" }],
            pagination: { size: sz, page },
          },
        },
      };
      let j;
      try {
        const resp = await fetch(u, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        j = JSON.parse(await resp.text());
      } catch (err) {
        return { error: `第 ${page + 1} 页请求失败：${(err && err.message) || err}` };
      }
      if (!j || j.code !== 0) {
        return { error: `code=${j && j.code} ${(j && j.message) || ""}`.trim(), page: page + 1 };
      }
      const items = (j.data && j.data.items) || [];
      all.push(...items);
      if (items.length < sz) return { items: all, pages: page + 1 };
    }
    return { items: all, pages: mp, truncated: true };
  }, [url, start, endExclusive, size, maxPages]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  if (r.error) throw new Error(r.error);
  if (!r.result) throw new Error("页面执行没有返回结果");
  if (r.result.error) throw new Error(r.result.error);
  return r.result;
}

/** 把日期区间切成若干短段（end 为开区间）。 */
function splitDateRange(startStr, endExclusive, chunkDays) {
  const out = [];
  const fmtD = (d) => {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  let cur = new Date(startStr + "T00:00:00");
  const end = new Date(endExclusive + "T00:00:00");
  if (isNaN(cur.getTime()) || isNaN(end.getTime())) return out;

  while (cur < end) {
    const next = new Date(cur.getTime() + chunkDays * 86400000);
    const stop = next > end ? end : next;
    out.push({ start: fmtD(cur), end: fmtD(stop) });
    cur = stop;
  }
  return out;
}

/**
 * 按区间取 SKU→商品 映射，整段失败就切成短段。
 *
 * 这个接口按商品维度聚合，跨度一大下游必超时（实测 16 天 → 98001021，
 * 1 天 → 200）。但映射必须跟着统计区间走：链接改过之后，同一个 SKU
 * 在不同时期属于不同商品，用「近 7 天」会拿到现在的归属而不是当时的。
 * 所以不缩短区间，改成切段。
 */
/**
 * 走「导出任务」接口取 SKU 列表（用户手工导出走的就是这条路）。
 *
 * 为什么不只靠 list 接口：list 按商品维度聚合，区间一大下游就超时。
 * 导出任务则是异步生成文件，对长区间是支持的——用户手工导 15 天一直成功。
 * 之前这条路走不通是因为下载环节跨源；现在整段都在页面上下文里执行，
 * 创建、轮询、下载用的都是页面的登录态与同源身份。
 */
/**
 * 从「商品管理」的 SKU 列表接口取全量映射。
 *
 * 这是商品管理页自己调用的接口（`/api/v1/product/stock/sku/list`），返回的每个 SKU
 * 直接带 sku_id / product_id / seller_sku / product_title，而且**不按时间区间过滤**，
 * 覆盖全部商品——包括当期没销量、没被分析接口收录的那些。
 *
 * 为什么它比「导出链接资料表」更好：
 *   · 不用生成文件、不用下载、不用解析 xlsx
 *   · 分页可控，拿到的就是结构化 JSON
 *   · 还额外带 status 与库存，能区分在售/下架
 */
async function pagePullSkuFromProductList(host, log) {
  const url = await buildUrl(host, "/api/v1/product/stock/sku/list");

  const r = await runInPage(host, async (u) => {
    const all = [];
    for (let page = 1; page <= 80; page++) {
      let j;
      try {
        const resp = await fetch(u, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ page, size: 200, is_need_toggle_status: true }),
        });
        j = JSON.parse(await resp.text());
      } catch (err) {
        return { error: `第 ${page} 页请求失败：${(err && err.message) || err}` };
      }
      if (!j || j.code !== 0) {
        return { error: `第 ${page} 页 code=${j && j.code} ${(j && j.message) || ""}`.trim() };
      }
      const skus = j.skus || [];
      all.push(...skus);
      if (skus.length < 200) return { skus: all, pages: page };
    }
    return { skus: all, pages: 80, truncated: true };
  }, [url]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  if (r.error) throw new Error(r.error);
  const out = r.result || {};
  if (out.error) throw new Error(out.error);
  if (!out.skus || !out.skus.length) throw new Error("商品管理接口返回 0 个 SKU");

  log(`商品管理接口：${out.pages} 页，${out.skus.length} 个 SKU`);
  return out.skus;
}

async function pagePullSkuByExport(host, start, endExclusive) {
  const exportUrl = await buildUrl(host, "/api/v2/insights/seller/ttp/product/sku/list/export");

  const r = await runInPage(host, async (expUrl, s, e) => {
    const body = {
      request: {
        time_descriptor: { start: s, end: e },
        search: {},
        filter: {},
        // 页面自己用的是「总条数」一次导全（抓包里是 236）。
        // 这里给一个足够大的值，导出接口对 size 不敏感，不像 list 会超时。
        list_control: { rules: [{ direction: 2, field: "sku_gmv" }], pagination: { size: 10000, page: 0 } },
      },
    };

    let j;
    try {
      const resp = await fetch(expUrl, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      j = JSON.parse(await resp.text());
    } catch (err) {
      return { error: `创建导出失败：${(err && err.message) || err}` };
    }
    if (!j || j.code !== 0) return { error: `创建导出 code=${j && j.code} ${(j && j.message) || ""}`.trim() };

    const file = j.data && j.data.file;
    if (!file) return { error: "创建成功但没返回文件路径" };

    const fileUrl = file.startsWith("http") ? file : `${location.origin}${file}`;

    // 文件是异步生成的，轮询到能取到二进制为止
    for (let i = 0; i < 40; i++) {
      await new Promise((r2) => setTimeout(r2, 2000));
      try {
        const fr = await fetch(fileUrl, { credentials: "include" });
        const ct = fr.headers.get("content-type") || "";
        if (fr.ok && !ct.includes("json")) {
          const buf = await fr.arrayBuffer();
          if (buf.byteLength > 512) {
            const bytes = new Uint8Array(buf);
            let bin = "";
            const CH = 0x8000;
            for (let k = 0; k < bytes.length; k += CH) {
              bin += String.fromCharCode.apply(null, bytes.subarray(k, k + CH));
            }
            return { b64: btoa(bin), bytes: buf.byteLength };
          }
        }
      } catch (err) { /* 还没生成好，继续等 */ }
    }
    return { error: "等了两分多钟仍取不到导出文件" };
  }, [exportUrl, start, endExclusive]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  if (r.error) throw new Error(r.error);
  const out = r.result || {};
  if (out.error) throw new Error(out.error);
  if (!out.b64) throw new Error("导出接口没有返回文件内容");

  const bin = atob(out.b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return uploadToBridge(`sku_export_${start}_${endExclusive}.xlsx`, bytes.buffer);
}

async function pagePullSkuRanged(host, start, endExclusive, log) {
  const MAX_TRIES = [3, 1];     // 先按 3 天切，仍失败就按天

  for (const chunk of MAX_TRIES) {
    const segs = splitDateRange(start, endExclusive, chunk);
    if (!segs.length) return [];

    // 只有一段就直接打，不用走分段循环
    if (segs.length === 1) {
      const r = await pagePullSku(host, segs[0].start, segs[0].end, 10, 400);
      log(`区间 ${segs[0].start} ~ ${segs[0].end}：${(r.items || []).length} 条`);
      return r.items || [];
    }

    log(`整段跨度 ${segs.length * chunk} 天左右，按 ${chunk} 天切 ${segs.length} 段取…`);
    const bySku = new Map();     // sku_id → item，同一 SKU 只留第一条
    const rebound = new Set();   // 区间内改绑过的 SKU
    let failed = 0;

    for (const s of segs) {
      try {
        const r = await pagePullSku(host, s.start, s.end, 10, 400);
        for (const it of r.items || []) {
          const meta = (it && it.meta) || {};
          const sid = String(meta.sku_id || meta.skuId || meta.seller_sku || "") || JSON.stringify(it).slice(0, 40);
          const old = bySku.get(sid);
          if (!old) {
            bySku.set(sid, it);
          } else {
            const pOld = String((old.meta || {}).product_id || (old.meta || {}).productId || "");
            const pNew = String(meta.product_id || meta.productId || "");
            if (pOld && pNew && pOld !== pNew) rebound.add(sid);
          }
        }
      } catch (e) {
        failed += 1;
        log(`${s.start} ~ ${s.end} 失败：${(e && e.message) || e}`);
      }
    }

    if (bySku.size) {
      log(`分段取到 ${bySku.size} 条（失败 ${failed} 段）`);
      if (rebound.size) log(`注意：区间内有 ${rebound.size} 个 SKU 改绑过商品`);
      return Array.from(bySku.values());
    }
    log(`按 ${chunk} 天切段仍全部失败，换更细的粒度…`);
  }
  return [];
}

async function runSkuStats({ startDate, endDateExclusive }, log, opts = {}) {
  const host = await ensureHost("seller", log);
  const today = shiftDate(new Date().toISOString().slice(0, 10), 0);
  const done = [];

  // ① 商品管理全量：覆盖所有链接，包括当期没有销量的。
  //    这一份是「当前归属」，先把底铺全。
  try {
    log("① 取商品管理全量 SKU（含当期无销量的链接）…");
    const skus = await pagePullSkuFromProductList(host, log);
    const bytes = new TextEncoder().encode(JSON.stringify({ items: skus }));
    const saved = await uploadToBridge(`sku_list_商品管理_${today}.json`, bytes.buffer);
    log(`   已保存：${saved.path}（${skus.length} 个 SKU）`);
    done.push(`商品管理 ${skus.length} 条`);
  } catch (e) {
    log(`   商品管理接口失败：${(e && e.message) || e}`);
  }

  // ② 区间内 SKU：只补①里没有的 SKU（例如已删除、但区间内仍有订单的链接）。
  //    归属冲突时以①为准——①是 SKU ID 全量表，属于主来源。
  try {
    log("② 取区间内 SKU（归属按所选区间）…");
    const saved = await pagePullSkuByExport(host, startDate, endDateExclusive);
    log(`   已保存：${saved.path}（${Math.round((saved.bytes || 0) / 1024)} KB）`);
    done.push("区间导出");
  } catch (e) {
    log(`   区间导出失败：${(e && e.message) || e}（沿用①的全量归属）`);
  }

  // ③ 兜底：list 接口分段
  if (!done.length) {
    const all = await pagePullSkuRanged(host, startDate, endDateExclusive, log);
    if (all.length) {
      const bytes = new TextEncoder().encode(JSON.stringify({ data: { items: all } }));
      const saved = await uploadToBridge(`sku_list_${startDate}_${shiftDate(endDateExclusive, -1)}.json`, bytes.buffer);
      log(`③ 已保存：${saved.path}（${all.length} 条）`);
      done.push(`list 分段 ${all.length} 条`);
    }
  }

  if (!done.length) {
    throw new Error(
      "商品SKU三条路都没取到，本轮沿用现有主表（主工作簿「链接资料」表已自动作为来源）",
    );
  }
  await skSet({ skuCache: { at: Date.now(), items: null } });
  log(`映射取数完成：${done.join(" + ")}`);
  return { via: done.join(" + ") };
}

// ---------------- 任务：联盟佣金 ----------------

/**
 * 探测联盟「商品维度」数据接口。
 *
 * 背景：导出任务的 list 接口只返回 task_id / file_name / status，**没有下载地址**，
 * 抓包也找不到任何 download 接口。所以导出这条路走不通时，改取数据接口。
 * 已知 `/transaction/detail_list/get` 是数据接口，但抓到的请求都是 module_type=103
 * （达人维度），商品维度（导出用的是 104）的参数键名未知。
 *
 * 这里把几种可能的参数形状各试一次，只读、只打日志，不改任何数据。
 * 目的是拿到「哪种形状能返回商品行」以及「商品行的字段名」，下一轮就能定死映射。
 */
async function pageProbeAffiliate(host, s, e, tz, log) {
  const url = await buildUrl(host, "/api/v1/oec/affiliate/compass/transaction/detail_list/get");
  const METRICS = [1, 3, 2, 4, 9, 20, 13, 14, 19, 6, 12, 11, 16, 17, 18, 8, 7, 10, 5];
  const SHAPES = [
    { name: "m104/type2/product_list_params", module_type: 104, detail_list_type: 2, key: "product_list_params" },
    { name: "m104/type2/transaction_product_list_params", module_type: 104, detail_list_type: 2, key: "transaction_product_list_params" },
    { name: "m104/type1/product_list_params", module_type: 104, detail_list_type: 1, key: "product_list_params" },
    { name: "m103/type2/product_list_params", module_type: 103, detail_list_type: 2, key: "product_list_params" },
  ];

  const r = await runInPage(host, async (u, startS, endS, tzOffset, metrics, shapes) => {
    const out = [];
    const td = () => ({
      granularity_type: 1, timezone_offset: tzOffset,
      start_time: startS, end_time: endS, time_option_type: 1,
    });

    /** 在响应里找第一个含商品ID 的对象，用来确认「这就是商品维度」。 */
    function findProductRow(node, depth) {
      if (!node || depth > 6) return null;
      if (Array.isArray(node)) {
        for (const it of node) {
          const hit = findProductRow(it, depth + 1);
          if (hit) return hit;
        }
        return null;
      }
      if (typeof node === "object") {
        const keys = Object.keys(node);
        const hasPid = keys.some((k) => /^(product_?id|productId)$/i.test(k));
        if (hasPid) return node;
        for (const k of keys) {
          const hit = findProductRow(node[k], depth + 1);
          if (hit) return hit;
        }
      }
      return null;
    }

    for (const sh of shapes) {
      const body = {
        params: {
          detail_list_type: sh.detail_list_type,
          [sh.key]: [{
            time_descriptor: td(),
            metric_types: metrics,
            page_param: { page_no: 1, page_size: 20 },
            sorter: { sort_type: 2, order_type: 1 },
            filter: {},
          }],
        },
        module_type: sh.module_type,
        locale: "zh-CN",
      };
      try {
        const resp = await fetch(u, {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = JSON.parse(await resp.text());
        const row = (j && j.code === 0) ? findProductRow(j.data, 0) : null;
        out.push({
          name: sh.name,
          code: j && j.code,
          msg: String((j && j.message) || "").slice(0, 60),
          dataKeys: j && j.data ? Object.keys(j.data).slice(0, 6) : [],
          foundProductRow: !!row,
          rowKeys: row ? Object.keys(row).slice(0, 24) : [],
          sample: row ? JSON.stringify(row).slice(0, 300) : "",
        });
        if (row) return out;              // 找到商品行就停，不再试后面的形状
      } catch (err) {
        out.push({ name: sh.name, error: String((err && err.message) || err).slice(0, 80) });
      }
    }
    return out;
  }, [url, Math.floor(new Date(s + "T00:00:00").getTime() / 1000),
      Math.floor(new Date(e + "T00:00:00").getTime() / 1000) - 1, tz, METRICS, SHAPES]);

  if (r.missingTab) { log("  探测失败：没有开着的联盟页面"); return null; }
  if (r.error) { log(`  探测异常：${r.error}`); return null; }
  const rows = (r.result || []);
  for (const x of rows) {
    if (x.error) { log(`  · ${x.name} → 请求异常 ${x.error}`); continue; }
    log(`  · ${x.name} → code=${x.code} ${x.msg}｜data键=${(x.dataKeys || []).join(",") || "无"}｜含商品行=${x.foundProductRow}`);
    if (x.foundProductRow) {
      log(`    商品行字段：${(x.rowKeys || []).join(", ")}`);
      log(`    样例：${x.sample}`);
    }
  }
  return rows.find((x) => x.foundProductRow) || null;
}

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

  // 轮询任务列表，找到可下载的地址。
  // 实测 list 接口只返回 task_id / file_name / status，**始终没有下载地址**，
  // 抓包里也找不到任何 download 接口。所以这里只等 30 秒探一下，
  // 拿不到就转去探测数据接口，不把整轮拖成几分钟。
  for (let i = 0; i < 6; i++) {
    await sleep(5000);
    const list = await apiPost(
      await buildUrl(host, "/api/v1/oec/affiliate/compass/export_task/list"),
      { module_type: 104, locale: "zh-CN" }
    );
    const url = findUrl(list);
    if (url) {
      return downloadAndUpload(url, `Transaction_Analysis_${startDate}_${endDateExclusive}.xlsx`, log);
    }
    if (i === 0 || i === 5) {
      const n = (list?.data?.export_tasks || []).length;
      log(`任务列表 ${n} 条，暂无下载地址（第 ${i + 1} 次）`);
    }
  }

  // 导出拿不到就改取数据接口。先探测哪种参数形状能返回商品行。
  log("导出接口给不出下载地址，改探测交易明细接口…");
  const hit = await pageProbeAffiliate(host, startDate, endDateExclusive, tz, log);
  if (hit) {
    throw new Error(
      `联盟数据接口已定位（${hit.name}），但字段映射还没接上，本轮用上一期文件。` +
      `日志里已打印商品行字段，下一步即可接通`
    );
  }
  throw new Error("联盟佣金本轮未取到（导出无下载地址、数据接口未定位），沿用上一期文件，报表会标注");
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

  // 同一个文件名往往对应多条任务记录（重复点导出、或平台保留历史），
  // 全下会得到 11 份一模一样的 968KB 文件。按文件名去重，只取最新一条。
  const byName = new Map();
  for (const x of tasks) {
    if (!x || !x.url || !x.download_id || done.includes(x.download_id)) continue;
    const key = x.file_name || x.download_id;
    byName.set(key, x);            // 后出现的覆盖前面的，即取最新
  }
  const ready = Array.from(byName.values());
  const running = tasks.filter((x) => x && x.task_status && x.task_status !== "FINISHED");

  const saved = [];
  for (const tk of ready) {
    const key = tk.file_name || tk.download_id;
    const name = (tk.file_name || "creative data").replace(/[\\/:*?"<>|]/g, "_") + ".xlsx";
    log(`下载广告导出：${name}`);
    const one = await downloadAndUpload(tk.url, name, log);
    saved.push({ ...one, id: tk.download_id });
    // 同名的所有任务记录一起标记，否则下次又会挨个下一遍
    await markAdDone(tasks.filter((x) => (x.file_name || x.download_id) === key).map((x) => x.download_id));
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
    chrome.alarms.create("pending-watch", { periodInMinutes: 1, delayInMinutes: 1 });
    chrome.alarms.onAlarm.addListener((a) => {
      if (a.name === "pending-watch") consumePending().catch(() => {});
    });
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

/** 真正执行一个任务：写运行标记、保活、跑、落结果。两条触发路径共用。 */
async function executeTask(task, range, log, opts = {}) {
  try {
    startKeepAlive();
    await skSet({ running: { task, at: Date.now() } });
    log(`开始（${range?.startDate} ~ ${range?.to || range?.endDateExclusive}）`);
    const withCalc = opts.withCalc !== false && task !== "ads";
    const result = withCalc
      ? await runTaskThenCalc(task, range || {}, log, opts)
      : await runTask(task, range || {}, log, opts);
    await skSet({ lastResult: { task, ok: true, at: Date.now(), result } });
    log("完成");
    return { ok: true, result };
  } catch (e) {
    const err = String((e && (e.message || e)) || e);
    log(`失败：${err}`);
    await skSet({ lastResult: { task, ok: false, at: Date.now(), error: err } });
    return { ok: false, error: err };
  } finally {
    stopKeepAlive();
    setTimeout(() => skSet({ running: null }), 1500);
  }
}

// ---------------- 存储领取 ----------------
// 面板发消息这条路偶尔会断（面板上下文失效、后台刚被唤醒等）。
// 所以点按钮时同时把任务写进 storage，这里定时来领——
// 只要有一条路通，任务就能跑起来。

const PENDING_KEY = "pendingTask";

async function consumePending() {
  // 两条来源：面板写的本地存储，以及面板通过桥中转的任务。
  // 前者依赖扩展自己的 storage，后者依赖普通 HTTP，互为备份。
  let p = (await skGet({ pendingTask: null })).pendingTask;

  if (!p || !p.task) {
    try {
      const resp = await fetch(`${BRIDGE}/pending`);
      const j = await resp.json();
      if (j && j.job && j.job.task) {
        p = { ...j.job, at: j.job.at || Date.now() };
        appendTaskLog(`[${p.task}] 从本地桥领取任务`);
      }
    } catch (e) { /* 桥没开，忽略 */ }
  }

  if (!p || !p.task || Date.now() - (p.at || 0) > 10 * 60 * 1000) return null;

  const { running } = await skGet({ running: null });
  if (running) return null;                 // 已经在跑了，别重复执行

  await skSet({ pendingTask: null });
  const log = (m) => appendTaskLog(`[${p.task}] ${m}`);
  log("从待办队列领取任务");
  return executeTask(p.task, p.range || {}, log, { force: !!p.force, withCalc: p.withCalc });
}

// ---------------- 任务调度 ----------------
// 这个调度器在一次重构里被误删，只留下调用（runTaskThenCalc / executeTask），
// 后果是所有取数任务在第一步就抛 ReferenceError: runTask is not defined。
// 现在补回来：按任务名分派、一键全跑、失败时自愈重试一次。

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
    // 广告排在最后：它只查已完成任务，不会阻塞前面的取数；
    // 剩下三个是算净利必需的，任何一个失败都不影响其余继续跑。
    for (const t of ["orders", "sku", "affiliate", "ads"]) {
      try {
        log(`—— 开始：${t} ——`);
        out[t] = await runTask(
          t, range, log,
          t === "ads" ? { quick: true } : { force: !!opts.force },
        );
      } catch (e) {
        log(`—— ${t} 失败：${e.message}`);
        out[t] = { error: String(e.message || e) };
      }
    }
    return out;
  }

  const impl = TASK_IMPL[task];
  if (!impl) throw new Error(`不支持的取数任务：${task}`);

  try {
    return await impl(range, log, opts);
  } catch (e) {
    const msg = String(e.message || e);
    // 登录态或风控参数失效是最常见的失败原因：刷新一次参数再试，别直接甩给用户
    if (/40[123]|风控|登录|code=|参数|学不到/.test(msg)) {
      log("可能登录态或风控参数失效，刷新页面后重试一次…");
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

  if (msg.type === "ping") {
    let v = "?";
    try { v = chrome.runtime.getManifest().version; } catch (e) { /* 忽略 */ }
    sendResponse({ ok: true, version: v, at: Date.now() });
    return true;
  }

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
      try {
        const log = (m) => appendTaskLog(`[${task}] ${m}`);
        await skSet({ pendingTask: null });          // 消息到了，待办作废
        const r = await executeTask(task, msg.range || {}, log, {
          force: !!msg.force, withCalc: msg.withCalc,
        });
        finish({ ok: r.ok, started: true, done: true, result: r.result, error: r.error });
      } catch (e) {
        const detail = (e && (e.stack || e.message)) || String(e);
        appendTaskLog(`[${task}] 启动失败：${detail.split("\n")[0]}`);
        reportCrash("runTask", detail.split("\n").slice(0, 3).join(" / "));
        finish({ ok: false, started: false, error: String(e && e.message || e) });
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

// 后台每次被唤醒都先看看有没有待办任务
setTimeout(() => { consumePending().catch(() => {}); }, 1200);

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
