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

const LAST_REQ_KEY = "lastRequestByPath";

/** 记住页面最近一次发往某个接口的请求（完整 URL + body）。

 * 为什么记：平台对这条接口的判定很严——请求体结构、参数、签名差一点就回
 * 98001021。与其猜，不如**原样复刻页面自己发过的那一条**，只改必要的地方。
 */
const EXT_MARK = "#jl-ext";   // fragment 不会发给平台，只用来识别「这请求是扩展自己发的」

async function rememberRequest(url, body) {
  try {
    // 扩展自己发的请求不能当作「页面那条」记下来，否则会自己覆盖自己
    if (String(url).includes(EXT_MARK)) return;
    const u = new URL(url);
    const { [LAST_REQ_KEY]: all = {} } = await chrome.storage.local.get({ [LAST_REQ_KEY]: {} });
    const key = `${u.hostname}|${u.pathname}`;
    all[key] = { url: String(url), body: body === undefined ? null : body, at: Date.now() };
    const keys = Object.keys(all)
      .sort((a, b) => (all[b].at || 0) - (all[a].at || 0)).slice(0, 40);
    const trimmed = {};
    for (const k of keys) trimmed[k] = all[k];
    await chrome.storage.local.set({ [LAST_REQ_KEY]: trimmed });
  } catch (e) { /* 忽略 */ }
}

async function lastRequestFor(host, path) {
  try {
    const { [LAST_REQ_KEY]: all = {} } = await chrome.storage.local.get({ [LAST_REQ_KEY]: {} });
    const hit = all[`${host}|${path}`];
    // 只认 30 分钟内的那条：参数和登录态都可能变了
    if (!hit || !hit.at || Date.now() - hit.at > 30 * 60 * 1000) return null;
    return hit;
  } catch (e) {
    return null;
  }
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
    rememberRequest(payload.url, payload.reqBody);
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
      return parts.join("&").slice(0, 20000);
    }
    if (rb.raw && rb.raw.length) {
      const chunks = rb.raw.map((r) => {
        if (!r.bytes) return "";
        const dec = new TextDecoder("utf-8", { fatal: false });
        return dec.decode(new Uint8Array(r.bytes));
      });
      return chunks.join("").slice(0, 20000);
    }
  } catch (e) { /* 二进制或读不出来就算了 */ }
  return null;
}

// 抓广告导出任务时打开的观察窗口（null = 不观察）
let adsWatch = null;

// 页面里的 fetch/XHR 抓不到的东西都在这一层：表单提交、iframe、Service Worker 发的请求。
// 广告导出很可能就是这类，所以必须补上。
//
// 关键：extraInfoSpec 必须带 "requestBody"，否则 details.requestBody 是 undefined，
// 记下来的只有 URL 没有请求体——而我们需要的恰恰是请求体（导出接口传了什么参数）。
//
// 域名范围：广告接口实测挂在**店铺主域**下（seller-my.tiktok.com/oec_ads/…），
// *.tiktok.com 就覆盖得到。之前想过加 <all_urls> 抓更全，但那样 Chrome 会要求用户
// 单独批准新权限，不批准就把扩展整个禁用（表现为开关变灰、抓包全无）——
// 得不偿失，这里保持明确域名。
const CAPTURE_URLS = [
  "*://*.tiktok.com/*",
  "*://*.tiktokglobalshop.com/*",
  "*://*.tiktokv.com/*",
  "*://*.byteoversea.com/*",
];

if (chrome.webRequest && chrome.webRequest.onBeforeRequest) {
  const onBefore = (details) => {
    if (!enabled) return;
    const u = details.url || "";
    const kw = /export|download|task|insight|stat\//i.test(u);
    const watching = !!adsWatch;
    // 平时只收关键词命中的；抓广告任务期间，所有非 GET 请求都收
    if (!kw && !(watching && details.method !== "GET")) return;

    const reqBody = summarizeRequestBody(details.requestBody);
    const rec = {
      kind: "webrequest",
      url: u,
      method: details.method,
      status: null,
      contentType: details.type || "",
      ms: 0,
      reqBody,
      respSample: null,
      page: details.initiator || "",
      at: new Date().toISOString(),
    };
    recordCapture(rec);

    // 抓广告导出任务时，命中的请求单独回传一份，
    // 免得事后在几十万条日常流量里翻。
    // GET 也要记：有些导出接口是用 GET 带参数触发的。
    if (adsWatch && isAdsUrl(u) && /export|download|task|stat|generate|create/i.test(u)) {
      rec.kind = "ads-export";
      adsWatch.recs.push(rec);
      adsWatch.log(`★ 捕获广告请求：${details.method} ${u.split("?")[0]}`);
      pushAdsCapture(rec);
    }
  };

  // 注册失败不能让整个扩展挂掉：顶层抛错会让 background script 直接死，
  // 扩展被 Chrome 判为「崩溃」，开关变灰，什么都不工作。
  let ok = false;
  try {
    chrome.webRequest.onBeforeRequest.addListener(onBefore, { urls: CAPTURE_URLS }, ["requestBody"]);
    ok = true;
  } catch (e) {
    console.warn("webRequest 监听注册失败（降级为不带请求体）", e);
    try {
      chrome.webRequest.onBeforeRequest.addListener(onBefore, { urls: CAPTURE_URLS });
      ok = true;
    } catch (e2) {
      console.warn("webRequest 监听彻底不可用", e2);
    }
  }
  if (!ok) reportCrash("webRequest 监听注册失败", "抓包功能不可用");
}

/** 广告相关的域名/路径判定。
 *
 *  注意 `oec_ads` 这一项——抓包实测广告接口挂在**店铺主域**下
 *  （seller-my.tiktok.com/oec_ads/shopping/v1/oec/stat/…），
 *  并不是在 ads.tiktok.com 域。漏了它就等于整个广告域都识别不出来。
 */
function isAdsUrl(u) {
  return /oec_ads|ads\.tiktok\.com|\/ads\/|advertis|ad_?center|\/stat\//i.test(u || "");
}

/**
 * 扫描所有店铺标签页上的按钮文案，找含「导出」的。
 *
 * 为什么需要它：TikTok Shop 里有好几个「导出」——订单导出、商品分析导出、
 * 广告数据导出，还有「导出记录」页面上的「下载」。点错地方就只会触发
 * download_task/query，永远抓不到创建接口。扫一遍把候选摆出来，
 * 用户一眼就知道该点哪个。
 */
async function scanExportButtons() {
  const tabs = await chrome.tabs.query({});
  const out = [];
  for (const tb of tabs) {
    const u = tb.url || "";
    if (!/tiktok\.com|tiktokglobalshop\.com/i.test(u)) continue;
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tb.id },
        func: () => {
          const found = [];
          const seen = new Set();
          const nodes = document.querySelectorAll(
            "button,[role=button],a,[class*=btn],[class*=Btn],[class*=button]",
          );
          for (const el of nodes) {
            const txt = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
            if (!txt || txt.length > 24 || seen.has(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 20 || r.height < 10) continue;
            seen.add(txt);
            found.push(txt);
          }
          return found.slice(0, 120);
        },
      });
      const list = (res && res.result) || [];
      if (!list.length) continue;
      out.push({
        url: u,
        title: tb.title || "",
        total: list.length,
        exportButtons: list.filter((t) => /导出|export|eksport|xuất/i.test(t)),
        downloadButtons: list.filter((t) => /下载|download/i.test(t)),
      });
    } catch (e) { /* 页面不允许注入就跳过 */ }
  }
  return out;
}

/** 把一条广告请求实时推给本地桥，落到单独的 ads_export.jsonl。 */
function pushAdsCapture(rec) {
  try {
    fetch(`${BRIDGE}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch: [rec], file: "ads_export.jsonl" }),
    }).catch(() => {});
  } catch (e) { /* 忽略 */ }
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
  // 同一个站，不同页面带的参数集合不一样（商品分析页 18 个、广告页 20 个）。
  // 混在一起发出去，平台会判成非法调用（98001021）。
  // 所以再按「路径」各存一份，取数时用对应路径的那套。
  const pathKey = `${host}|${u.pathname}`;
  params[pathKey] = { ...(params[pathKey] || {}), ...fresh, _at: Date.now() };
  // 再存一份「完整 query」：页面发这条接口时到底带了哪些参数，一个不漏。
  // 有些参数不在学习清单里，但缺了平台照样判非法调用，原样复刻最稳。
  const full = {};
  for (const [k, v] of u.searchParams.entries()) full[k] = v;
  params[`full:${pathKey}`] = { ...full, _at: Date.now() };
  await chrome.storage.local.set({ params });
}

async function buildUrl(host, path, extra = {}, drop = []) {
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  // 三档取值，从准到糊：
  //   ① full:  页面发这条接口时的完整 query（原样复刻，最准）
  //   ② 路径级：按接口路径单独学的那套
  //   ③ host 级：各页面混在一起的（最糊）——此时按 drop 剔掉已知多余的键
  const full = params[`full:${host}|${path}`];
  const byPath = params[`${host}|${path}`];
  let p;
  if (full) {
    p = { ...full };
  } else if (byPath) {
    p = { ...byPath };
  } else {
    p = { ...(params[host] || {}) };
  }
  // 剔除已知多余的键——**任何一档都要剔**。
  // （之前只在兜底时剔，结果 full 那档存的就是扩展自己发出去的 21 键，
  //   剔了也白搭——扩展发的请求同样被记录，等于自己污染自己。）
  for (const k of drop) delete p[k];
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) {
    if (k.startsWith("_")) continue;
    qs.set(k, v);
  }
  for (const [k, v] of Object.entries(extra)) qs.set(k, v);
  return `https://${host}${path}?${qs.toString()}`;
}

function pickHost(params, hint) {
  // 只看 host 级键（不含 "|" 的），路径级的键带 "|" 后缀
  return Object.keys(params)
    .filter((k) => !k.includes("|"))
    .find((h) => h.includes(hint));
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
    // 记下这个自开的标签页，学到参数后关掉——
    // 不然每轮都留一个后台页，几天下来能攒几十个。
    let createdId = 0;
    try {
      const nt = await chrome.tabs.create({ url, active: false });
      createdId = nt && nt.id;
    } catch (e) { log(`打开失败：${e.message}`); }
    for (let i = 0; i < 16; i++) {
      await sleep(2500);
      params = await read();
      host = pickHost(params, hint);
      if (host) {
        log(`已学到 ${host} 的参数`);
        if (createdId) { try { await chrome.tabs.remove(createdId); } catch (e) { /* 忽略 */ } }
        return host;
      }
    }
    if (createdId) { try { await chrome.tabs.remove(createdId); } catch (e) { /* 忽略 */ } }
  }

  const label = hint === "affiliate" ? "达人联盟后台" : "卖家后台";
  throw new Error(`拿不到${label}的参数：请手动打开一次${label}页面后重试`);
}

// ---------------- 请求封装 ----------------

async function apiPost(url, body, extraHeaders) {
  let host = "";
  try { host = new URL(url).hostname; } catch (e) { /* 忽略 */ }
  const { [HEADER_KEY]: hp = {} } = await skGet({ [HEADER_KEY]: {} });
  const learned = (hp[host] || {});
  const headers = { "Content-Type": "application/json" };
  for (const [k, v] of Object.entries(learned)) {
    if (k.startsWith("_")) continue;
    headers[k] = v;
  }
  // 广告接口有的站点要求显式带账户 ID（报错原文：req adv id missing）
  for (const [k, v] of Object.entries(extraHeaders || {})) {
    if (v) headers[k] = String(v);
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
    // 平台经常只回一个 code、不给人话，带上 extra 才能知道是哪一步被拒
    const extra = json.extra ? `｜${JSON.stringify(json.extra).slice(0, 240)}` : "";
    const raw = (json.message || json.msg) ? "" : `｜${text.slice(0, 200)}`;
    throw new Error(`接口返回 code=${json.code} ${json.message || json.msg || ""}${extra}${raw}`);
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

// ---------------- 店铺身份 ----------------
//
// 多店铺的核心：先搞清楚「现在这个浏览器登的是哪家店」，再决定数据存哪。
// shop_id 是平台给的唯一标识，一长串数字；名字给人看，可以在程序里改成自己认得的名。
//
// 识别办法：卖家后台自己的页面就在调 /api/v1/affiliate/account/info_v2，
// 返回里有 shop.shop_id / shop.shop_name / shop.region。
// 不猜、不解析 DOM，直接问接口。
let CURRENT_SHOP = { shop_id: "", shop_name: "", shop_code: "", region: "" };

// 后台脚本每次被唤醒（MV3 会被回收）内存都是空的，但"这家店是谁"没变。
// 以前只把识别结果写进 storage、启动时不读，于是每次唤醒都要重新识别一遍——
// 面板一打开就"识别中…"好几秒，越用越慢。
try {
  chrome.storage.local.get({ currentShop: null }).then(({ currentShop }) => {
    if (currentShop && currentShop.shop_id && !CURRENT_SHOP.shop_id) {
      CURRENT_SHOP = currentShop;
    }
  }).catch(() => {});
} catch (e) { /* 忽略 */ }

// 当前这一轮取数的区间。上传文件时带给桥，桥拿它给快照打标签，
// 这样界面上「用哪次数据」列表能显示每份快照对应哪个区间。
let CURRENT_RANGE = null;

// 店铺身份可能的来源。按「最像店铺资料」的顺序试，第一个给出 shop_id 的算数。
// 只试一个接口是不行的：联盟接口在没开通联盟的店上会 404/10000，
// 换个国家的新店就识别不出来——而用户不该为了加一家店去「教」程序。
const IDENTIFY_PATHS = [
  "/api/v1/affiliate/account/info_v2",
  "/api/v1/affiliate/account/all_sellers/get",
  "/api/v1/shop/get_shop_info",
  "/api/v1/seller/shop/info",
  "/api/v1/shop/info",
];

// 域名后缀 → 站点国家。接口全挂时至少还能把国家填对。
const HOST_REGION = [
  ["seller-my", "MY"], ["seller-ph", "PH"], ["seller-th", "TH"], ["seller-vn", "VN"],
  ["seller-sg", "SG"], ["seller-id", "ID"], ["seller-us", "US"], ["seller-uk", "GB"],
  ["seller-gb", "GB"], ["seller-de", "DE"], ["seller-fr", "FR"], ["seller-it", "IT"],
  ["seller-es", "ES"], ["seller-mx", "MX"], ["seller-br", "BR"], ["seller-jp", "JP"],
  ["seller-kr", "KR"],
];

function regionFromHost(host) {
  const h = String(host || "").toLowerCase();
  for (const [k, v] of HOST_REGION) if (h.includes(k)) return v;
  return "";
}

/**
 * 这家站点有什么脾气。数据来自本地程序的 配置/sites.json，
 * 面板每次打开都会同步一份到扩展存储里。
 *
 * 为什么不在扩展里写死：站点差异（广告规格接口能不能用、下载要重试几次）
 * 以后只会越来越多，写死在 JS 里就意味着「加一个新站点要重新发一版扩展」。
 */
async function siteProfile(host) {
  const { sites } = await skGet({ sites: null });
  const h = String(host || "").toLowerCase();
  const m = h.match(/^seller-([a-z]{2})\./);
  const key = m ? m[1] : "";
  const table = (sites && sites.sites) || {};
  const def = (sites && sites.defaults) || {};
  return { key, region: regionFromHost(host), ...def, ...(table[key] || {}) };
}

// 给面板看：上一次识别失败的原因（面板上直接显示，不用去翻日志）
let SHOP_ERROR = "";

async function identifyShop(host, log, opts = {}) {
  const force = !!opts.force;
  if (CURRENT_SHOP.shop_id && !force) return CURRENT_SHOP;
  try {
    const urls = [];
    for (const p of IDENTIFY_PATHS) {
      try { urls.push(await buildUrl(host, p)); } catch (e) { /* 这个接口拼不出来就跳过 */ }
    }
    const r = await runInPage(host, async (list) => {
      // 在返回的 JSON 里往下翻，找第一个像店铺的对象。
      // 各站点返回结构不一样（data.shop / data.list[] / data.sellers[]），
      // 按字段名认比按固定路径取活得久。
      const dig = (obj, depth) => {
        if (!obj || depth > 6) return null;
        if (Array.isArray(obj)) {
          for (const it of obj) { const f = dig(it, depth + 1); if (f) return f; }
          return null;
        }
        if (typeof obj !== "object") return null;
        const id = obj.shop_id || obj.shopId;
        if (id && /^\d{6,}$/.test(String(id))) {
          return {
            shop_id: String(id),
            shop_name: String(obj.shop_name || obj.shopName || obj.shop_title || ""),
            shop_code: String(obj.shop_code || obj.shopCode || ""),
            region: String(obj.region || obj.shop_region || obj.region_code || ""),
          };
        }
        for (const k of Object.keys(obj)) { const f = dig(obj[k], depth + 1); if (f) return f; }
        return null;
      };
      const tried = [];
      for (const u of list) {
        try {
          const resp = await fetch(u, { credentials: "include" });
          const t = await resp.text();
          if (!t || t.trim()[0] !== "{") { tried.push(`${u.split("?")[0]} 非 JSON`); continue; }
          const f = dig(JSON.parse(t), 0);
          if (f) return { shop: f, source: u.split("?")[0], tried };
          tried.push(`${u.split("?")[0]} 无 shop_id`);
        } catch (e) {
          tried.push(`${u.split("?")[0]} ${String(e && e.message || e).slice(0, 40)}`);
        }
      }
      // 接口全不行：从页面自己的状态里捞一个 shop_id 兜底。
      // 页面把初始状态直接内联进 HTML，正反两种转义都试（有些站点是 \" 转义过的）。
      try {
        const html = document.documentElement ? document.documentElement.innerHTML : "";
        const pats = [
          /"shop_?[Ii]d"\s*:\s*"?(\d{6,})"?/,
          /\\"shop_?[Ii]d\\"\s*:\s*\\?"?(\d{6,})/,
        ];
        for (const re of pats) {
          const m = html.match(re);
          if (m) return { shop: { shop_id: m[1] }, source: "页面状态", tried };
        }
        // 最后看 cookie / localStorage：有些站点把 shop_id 放在登录态里
        try {
          const ck = String(document.cookie || "");
          const mc = ck.match(/shop_?id=(\d{6,})/i);
          if (mc) return { shop: { shop_id: mc[1] }, source: "cookie", tried };
        } catch (e) { /* 忽略 */ }
        try {
          for (let i = 0; i < localStorage.length; i++) {
            const v = String(localStorage.getItem(localStorage.key(i)) || "");
            const m2 = v.match(/"shop_?[Ii]d"\s*:\s*"?(\d{6,})"?/);
            if (m2) return { shop: { shop_id: m2[1] }, source: "localStorage", tried };
          }
        } catch (e) { /* 忽略 */ }
      } catch (e) { /* 忽略 */ }
      return { shop: null, tries: tried };
    }, [urls]);
    if (r.missingTab) throw new Error("没有开着的店铺页面");
    if (r.error) throw new Error(r.error);
    const got = r.result || {};
    const shop = got.shop || {};
    const id = String(shop.shop_id || "").trim();
    if (!id) {
      throw new Error(`几个店铺信息接口都没返回 shop_id（${(got.tries || []).join("；")}）`);
    }

    const region = String(shop.region || "").toUpperCase() || regionFromHost(host);
    CURRENT_SHOP = {
      shop_id: id,
      shop_name: String(shop.shop_name || ""),
      shop_code: String(shop.shop_code || ""),
      region: region,
      _at: Date.now(),
    };
    SHOP_ERROR = "";
    log(`店铺已识别：${CURRENT_SHOP.shop_name || id}（${CURRENT_SHOP.region || "?"}）` +
        (got.source ? `　来源：${got.source}` : ""));
    log(`店铺已识别：${CURRENT_SHOP.shop_name || id}（${CURRENT_SHOP.region || "?"}）`);

    // 告诉本地程序：这家店是谁。它会记住，并把后续数据存到这家店自己的目录。
    try {
      const resp = await fetch(`${BRIDGE}/identify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(CURRENT_SHOP),
      });
      const ack = await resp.json();
      if (ack && ack.ok) {
        log(`   数据目录：${ack.store_dir}`);
        if (ack.country) CURRENT_SHOP.country = ack.country;
      }
    } catch (e) { /* 桥不在也不影响识别本身 */ }

    try { await chrome.storage.local.set({ currentShop: CURRENT_SHOP }); } catch (e) {}
    // 认出是哪家店了，立刻告诉桥一声 —— 界面上的卡片马上从「离线」变「在线」，
    // 不用等下一分钟的闹钟。
    beatBridge();
    // 顺手把这家店的基本档案落盘（站点代码/区域）。这些是"这家店的身份证"，
    // 换电脑拷数据时一起带过去，不用重新摸一遍。
    try {
      const _prof = await siteProfile(host);
      await saveStoreProfile({ host, site_key: _prof.key || "" });
    } catch (e) { /* 档案写不了不影响取数 */ }
    return CURRENT_SHOP;
  } catch (e) {
    SHOP_ERROR = String((e && e.message) || e);
    log(`店铺识别失败：${SHOP_ERROR}（数据会存到公共目录）`);
    return CURRENT_SHOP;
  }
}

async function uploadToBridge(name, arrayBuffer, dir, shopOverride) {
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
    body: JSON.stringify({
      name, b64,
      dir: dir || "",
      // 映射类文件是店铺无关的（dir=mapping），带上也无害；
      // 其余文件按 shop_id 落到这家店自己的目录。
      // shopOverride：闹钟/待办补回时后台没有"当前店铺"，要把归属显式带过来，
      // 否则补回的订单会落进公共目录，核算时又变成"未识别店铺"。
      shop_id: (shopOverride && shopOverride.shop_id) || CURRENT_SHOP.shop_id || "",
      shop_name: (shopOverride && shopOverride.shop_name) || CURRENT_SHOP.shop_name || "",
      // 本次取数的区间：服务端拿它给快照打标签，
      // 界面上「用哪次数据」列表就能显示每份快照是哪个区间的
      range: CURRENT_RANGE || null,
    }),
  });
  const j = await resp.json();
  if (!j.ok) throw new Error(j.error || "上传失败");
  if (j.warning) {
    // 认不出店铺之类的提醒：不能让文件悄悄躺在那儿，用户还以为已经入库了
    appendTaskLog(`[上传] ${j.warning}`);
  }
  return j;
}

// ---------------- 任务：订单 ----------------

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}_${p(d.getMinutes())}`;
}

/** 记下"订单导出还在排队"，交给后台闹钟去收。 */
async function rememberPendingOrder(item) {
  const { pendingOrders = [] } = await skGet({ pendingOrders: [] });
  const fresh = (pendingOrders || []).filter((x) => Date.now() - (x.at || 0) < 2 * 3600 * 1000);
  if (!fresh.some((x) => String(x.taskId) === String(item.taskId))) fresh.push(item);
  await skSet({ pendingOrders: fresh.slice(-10) });
}

/**
 * 收排队中的订单导出。
 *
 * 为什么要有这一步：大店导出要几分钟，卡在取数流程里干等会把整轮拖死；
 * 而不等就丢，用户又得重新点。所以在后台留个闹钟，每分钟看一眼，
 * 好了就自动下载回传——用户只要过几分钟回来点「核算」。
 *
 * shopId 传了就只收这家店的（新一轮开始时先试一次，能补回就不用重导）。
 */
async function harvestPendingOrders(log, shopId) {
  const { pendingOrders = [] } = await skGet({ pendingOrders: [] });
  if (!pendingOrders.length) return 0;
  const keep = [];
  let got = 0;
  for (const it of pendingOrders) {
    if (Date.now() - (it.at || 0) > 2 * 3600 * 1000) continue;   // 太旧，丢弃
    if (shopId && String(it.shop_id || "") !== String(shopId)) { keep.push(it); continue; }
    try {
      const rec = await apiPost(
        await buildUrl(it.host, "/api/fulfillment/order/export_record/get"), {});
      const list = (rec && rec.data && rec.data.export_records) || [];
      const hit = list.find((x) => String(x.export_task_id) === String(it.taskId)
                                && x.file_key && !/^\d+$/.test(String(x.file_key)));
      if (!hit) { keep.push(it); continue; }
      let url = "";
      try {
        const dl = await apiPost(
          await buildUrl(it.host, "/api/fulfillment/order/download"),
          { file_key: hit.file_key, export_task_id: it.taskId });
        url = (dl && dl.data && (dl.data.download_url || dl.data.url)) || findUrl(dl) || "";
      } catch (e) { /* 记录里通常还有直链，下面兜住 */ }
      if (!url) url = findUrl(hit) || "";
      if (!url) { keep.push(it); continue; }
      const resp = await fetch(url, { credentials: "include" });
      if (!resp.ok) { keep.push(it); continue; }
      const buf = await resp.arrayBuffer();
      if (!buf || buf.byteLength < 200) { keep.push(it); continue; }
      const saved = await uploadToBridge(it.name || `全部 笔订单-${stamp()}.csv`, buf, "",
        { shop_id: it.shop_id, shop_name: it.shop_name });
      if (log) log(`   订单已自动补回：${saved.path}`);
      got++;
    } catch (e) {
      keep.push(it);
    }
  }
  await skSet({ pendingOrders: keep });
  return got;
}

async function runOrders({ startMs, endMs }, log) {
  const host = await ensureHost("seller", log);

  // 先看上次有没有在排队的导出已经生成好了：能补回就不用再排一个
  try {
    const recovered = await harvestPendingOrders(log, CURRENT_SHOP.shop_id);
    if (recovered > 0) {
      log("上次排队的订单已补回，本轮不再重复导出");
      return { via: "排队补回" };
    }
  } catch (e) { /* 补不回来就正常走 */ }

  // —— 工具：建任务 / 等记录 / 下载落盘 ——
  const isRealFileKey = (k) => !!k && !/^\d+$/.test(String(k));
  const exportRecords = async () => {
    const rec = await apiPost(await buildUrl(host, "/api/fulfillment/order/export_record/get"), {});
    return (rec && rec.data && rec.data.export_records) || [];
  };

  const createTask = async (sMs, eMs, name) => {
    const created = await apiPost(
      await buildUrl(host, "/api/fulfillment/order/export"),
      {
        search_condition: {
          condition_list: { time_order_created: { value: [String(sMs), String(eMs)] } },
        },
        sort_info: "6",
        file_name: encodeURIComponent(name),
        file_type: 2,
      }
    );
    const tid = created?.data?.export_task_id;
    if (!tid) throw new Error("导出任务创建失败：没有拿到 export_task_id");
    return String(tid);
  };

  // 等"对得上本次任务号"的导出记录。注意不能用建任务返回的 file_key：
  // 那个可能是上一份导出的文件号，拿去下载只会回 21016003。
  const waitRecord = async (tid, rounds, label) => {
    let queued = false;
    for (let i = 0; i < rounds; i++) {
      await sleep(4000);
      const list = await exportRecords().catch(() => []);
      const mine = list.find((x) => String(x.export_task_id) === String(tid));
      if (mine && isRealFileKey(mine.file_key)) return mine;
      if (mine && !queued) {
        queued = true;
        const st = Object.entries(mine)
          .filter(([k]) => /status|state|progress|msg|reason/i.test(k))
          .map(([k, v]) => `${k}=${v}`).join(" ");
        log(`   ${label}：已排队，平台还在生成${st ? "｜" + st : ""}`);
      }
      if (i % 5 === 4) log(`   等待中…（${(i + 1) * 4}s${queued ? "，已排队" : ""}）`);
    }
    return null;
  };

  const saveOne = async (tid, rec, name) => {
    let url = "";
    let dlErr = "";
    const prof = await siteProfile(host);
    const maxTry = Math.max(1, Number(prof.download_retry) || 4);
    const gap = Math.max(2000, Number(prof.download_retry_gap) || 6000);
    for (let attempt = 0; attempt < maxTry && !url; attempt++) {
      if (attempt) {
        const wait = gap * attempt;
        log(`   下载还没就绪，${Math.round(wait / 1000)} 秒后再试…`);
        await sleep(wait);
      }
      try {
        const dl = await apiPost(
          await buildUrl(host, "/api/fulfillment/order/download"),
          { file_key: rec.file_key, export_task_id: tid }
        );
        url = dl?.data?.download_url || findUrl(dl) || "";
      } catch (e) {
        dlErr = String((e && e.message) || e);
      }
    }
    if (!url) url = findUrl(rec) || "";
    if (!url) throw new Error(dlErr || "没有拿到下载地址");
    const fileResp = await fetch(url, { credentials: "include" });
    if (!fileResp.ok) throw new Error(`下载失败 HTTP ${fileResp.status}`);
    const buf = await fileResp.arrayBuffer();
    const saved = await uploadToBridge(name, buf);
    log(`   已保存：${saved.path}`);
    return { path: saved.path, bytes: buf.byteLength };
  };

  // 文件名带上区间：同一区间再点一次时，能认出"上次已经导好了"，
  // 直接复用那份，不用再排一个 40MB 的任务（实测大店一次要 5 分钟）。
  // 按本地日期取，不能用 toISOString：东八区的 10-01 00:00 在 UTC 是 09-30，
  // 文件名会写成 20260930-20261030，跟实际区间差一天。
  const ymd = (ms) => {
    const d = new Date(ms);
    const p2 = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}`;
  };
  const rTag = `${ymd(startMs)}-${ymd(endMs)}`;
  const wantName = `全部 笔订单-${rTag}-${stamp()}.csv`;

  // 用户明确要求：**每次运行都重新生成**，不许拿平台上以前那份算。
  //
  // 以前这里有一步「发现同区间已生成的导出就直接复用」，省掉大店 5 分钟的
  // 生成时间；但复用意味着拿到的是**上次那一刻**的状态——这段时间里退的款、
  // 取消的单都看不到。算净利宁可慢，也要反映当下。
  //
  // 所以这里只建新任务，不再查 export_record 复用。
  log("提交订单导出任务（每次重新生成，不复用旧文件）…");
  const fullTask = await createTask(startMs, endMs, wantName);
  log(`任务已创建：${fullTask}`);
  const rec = await waitRecord(fullTask, 12, "订单导出");
  if (rec) {
    log("文件已生成，开始下载…");
    const one = await saveOne(fullTask, rec, wantName);
    return { saved: one.path, bytes: one.bytes };
  }

  await rememberPendingOrder({
    host, taskId: fullTask, name: wantName, range_tag: rTag,
    shop_id: CURRENT_SHOP.shop_id || "", shop_name: CURRENT_SHOP.shop_name || "",
    at: Date.now(),
  });
  throw new Error("订单导出已排队：这家店数据量大，平台生成要几分钟。"
    + "已记下任务，后台会自动下载；过几分钟再点一次「核算」就行（不用再点运行）");
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
  const list = (tabs || []).filter((x) => x && x.id);
  // 被浏览器休眠（discarded）的标签页注入不了脚本，优先挑活着的
  return list.find((x) => !x.discarded) || list[0] || null;
}

/** 等标签页真正加载完。
 *  刚创建的标签页 URL 已经有了、但 status 还是 "loading" 时去注入脚本，
 *  Chrome 会报「Cannot access contents of the page」——看起来像没权限，
 *  其实只是页面还没准备好。这个坑很容易误判成权限问题。 */
async function waitTabReady(tabId, ms = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (!t) return false;
      if (t.status === "complete" && !t.discarded) return true;
    } catch (e) {
      return false;
    }
    await sleep(400);
  }
  return false;
}

/** 在「商品数据分析」页里切到「按 SKU」维度。

 * 页面自己发那条请求时，扩展才能学到它带的完整参数（含签名）。
 * 页面默认停在「按商品」，不发 sku/list，学不到就白搭。
 * 只对扩展自己开的标签页做，不动用户自己开着的那个。
 */
async function clickSkuTab(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: () => {
        const cands = Array.from(
          document.querySelectorAll("span,div,button,a,li,label"));
        const hit = cands.find((e) => {
          const t = (e.textContent || "").replace(/\s+/g, "").trim();
          if (t !== "按SKU") return false;
          if (e.offsetParent === null) return false;      // 只点看得见的
          const kids = e.querySelectorAll("span,div,a,button");
          // 取最内层的那个，避免点到外层容器
          return ![...kids].some((k) => (k.textContent || "").replace(/\s+/g, "").trim() === "按SKU");
        });
        if (!hit) return false;
        hit.click();
        return true;
      },
    });
    return !!(r && r.result);
  } catch (e) {
    return false;
  }
}

/** 在页面里跑一段函数，拿到 JSON。页面没开时返回 null，由调用方决定回退。
 *  preferPath：优先用「URL 含这个片段」的标签页。
 *  有些接口只有在对应页面里发才不被平台拒绝（Referer/页面上下文），
 *  比如「商品数据分析」的 SKU 列表——在别的页面发会被判成非法调用。 */
/** 跑一次页面请求；遇到 "Failed to fetch" 自动重来一次。

 * 扩展刚重载、后台脚本刚被唤醒、或标签页刚被浏览器冻结时，
 * 第一次请求会以 Failed to fetch 收场——这不是平台拒绝，重试就好。
 */
async function runInPage(host, func, args, preferPath) {
  const exclude = new Set();
  let last = "";
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = await _runInPageOnce(host, func, args, preferPath, exclude);
    if (r.missingTab) return r;
    const inner = (r.result && r.result.error) || "";
    const msg = r.error || inner || "";
    if (!msg) return r;
    last = msg;
    if (r.badTab
        || /Failed to fetch|NetworkError|ERR_|about:blank|chrome-error/i.test(msg)) {
      // 这个标签页用不了了（被浏览器冻结、注入不了、加载失败、已关闭）——换一个再试
      if (r.tabId) exclude.add(r.tabId);
      await sleep(1500);
      continue;
    }
    return r;
  }

  // 所有现成标签都不能用：开一个干净的后台标签再试一次。
  // （用户点插件时可能正停在 chrome://extensions，那种页永远注入不了）
  try {
    const nt = await chrome.tabs.create({ url: `https://${host}/`, active: false });
    await waitTabReady(nt.id);
    await sleep(800);
    const r = await _runInPageOnce(host, func, args, preferPath, exclude);
    if (!r.error && !r.missingTab) return r;
    last = r.error || last;
  } catch (e) { /* 开页失败就用原来的错误 */ }

  return { error: last || "页面请求失败" };
}

async function _runInPageOnce(host, func, args, preferPath, exclude) {
  exclude = exclude || new Set();
  // 能用的标签页：没被排除、URL 正常（排除错误页和空白页）
  const usable = (t) => t && t.id && !exclude.has(t.id)
    && String(t.url || "").startsWith(`https://${host}/`)
    && !/^chrome-error:|^about:/.test(String(t.url || ""));
  let tab = null;
  let openedByUs = false;
  const kw = preferPath ? preferPath.split("?")[0] : "";
  if (preferPath) {
    // 匹配时只看主路径（? 之前），query 各页面顺序不同
    const pick = (list) => (list || []).find(
      (t) => usable(t) && String(t.url || "").includes(kw)) || null;
    try {
      tab = pick(await chrome.tabs.query({ url: [`*://${host}/*`] }));
    } catch (e) { /* 忽略 */ }
    if (!tab) {
      // 没有就开一个：后台标签页，不打断用户
      try {
        await chrome.tabs.create({
          url: `https://${host}/${preferPath}`, active: false,
        });
        openedByUs = true;
      } catch (e) { /* 忽略 */ }
      for (let i = 0; i < 12 && !tab; i++) {
        await sleep(2000);
        try {
          tab = pick(await chrome.tabs.query({ url: [`*://${host}/*`] }));
        } catch (e) { break; }
      }
      // 还没匹配上就退回 active 页 / 任意可用页
      if (!tab) {
        try {
          const all = (await chrome.tabs.query({ url: [`*://${host}/*`] })) || [];
          tab = all.find((t) => usable(t) && t.active) || all.find(usable) || null;
        } catch (e) { /* 忽略 */ }
      }
    }
  }
  if (!tab) {
    try {
      const all = (await chrome.tabs.query({ url: [`*://${host}/*`] })) || [];
      // 优先用用户正在看的那个标签页——它最不容易被浏览器冻结
      tab = all.find((t) => usable(t) && t.active) || all.find(usable) || null;
    } catch (e) { /* 忽略 */ }
  }
  if (!tab) {
    // 参数是新鲜的，ensureHost 不会开页，但页面上下文取数必须有 tab
    try { await chrome.tabs.create({ url: `https://${host}/`, active: false }); } catch (e) { /* 忽略 */ }
    for (let i = 0; i < 10 && !tab; i++) {
      await sleep(2000);
      tab = await findTabFor(host);
    }
  }
  if (!tab) return { missingTab: true };
  // 关键：等页面 status 变成 complete 再注入。
  // 只是「URL 已经对了」还不够——标签刚建好时还在 loading，
  // 这时 executeScript 会报 Cannot access contents（看着像没授权，其实不是）。
  await waitTabReady(tab.id);
  if (preferPath && openedByUs) {
    // 让页面自己切到「按 SKU」并发一次请求，扩展借机学到这套参数，
    // 之后我们的请求就能原样复刻
    const clicked = await clickSkuTab(tab.id);
    if (clicked) await sleep(4000);
  }
  try {
    // world: "MAIN" 是关键，别去掉。
    // 扩展默认跑在隔离环境（ISOLATED），页面的 webmssdk 看不见、也不会给我们的
    // fetch 加签。TikTok 的接口（尤其 affiliate 的 compass 系列）要求 URL 上带
    // X-Bogus / X-Gnarly / msToken 这些动态签名，缺了就只回一个 {"code":10000}，
    // 看不出原因。放到页面主世界执行，等于页面自己发的请求，签名才会自动补上。
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func, args, world: "MAIN",
    });
    return { result: res && res.result, tabId: tab.id };
  } catch (e) {
    const msg = String((e && e.message) || e);
    // 这句英文原文很难懂，翻成能照着做的提示
    if (/Cannot access contents|The tab was closed|No tab with id/i.test(msg)) {
      // 这个标签页注入不了（chrome:// 页面、扩展页、被冻结、已关闭）。
      // 标成 badTab，让上层换下一个，别在同一个上死磕三次。
      return {
        badTab: true,
        error: "这个标签页不能注入脚本（可能是 chrome:// 页面或被冻结），换一个再试",
        tabId: tab.id,
      };
    }
    return { error: msg, tabId: tab.id };
  }
}

/** 分页拉全量商品 SKU（在页面上下文里循环，一次调用拿完）。 */
/** 「商品数据分析」页的地址。

 * SKU 列表接口（/insights/seller/ttp/product/sku/list）只在这个页面里发才不被拒，
 * 在别的店铺页面发会回 98001021。页面真实路径是 /compass/product-analysis。
 */
async function skuPageHint() {
  let region = "MY";
  try {
    const st = (await skGet({ settings: {} })).settings || {};
    const map = { "马来西亚": "MY", "菲律宾": "PH", "泰国": "TH", "印尼": "ID" };
    if (map[st.country]) region = map[st.country];
  } catch (e) { /* 忽略 */ }
  return `compass/product-analysis?lang=zh-CN&shop_region=${region}`;
}

async function pagePullSku(host, start, endExclusive, size, maxPages) {
  const SKU_PATH = "/api/v2/insights/seller/ttp/product/sku/list";
  // 优先**原样复刻页面自己发过的那一条**：
  // URL 的 query 一个不漏、body 结构照抄（平台的判定严到差一点就 98001021，
  // 与其猜，不如用页面自己那条）。页面若发的是「按商品」维度的极简 body，
  // 那正好一次就是全量 SKU（含当期没销量的链接）——正是我们要的。
  const last = await lastRequestFor(host, SKU_PATH);
  let url;
  let tpl = null;
  if (last && last.url) {
    url = String(last.url).split("#")[0];
    try {
      tpl = typeof last.body === "string" ? JSON.parse(last.body) : last.body;
    } catch (e) { tpl = null; }
  } else {
    // 这三个键是广告页那套参数带来的，商品分析页的请求里没有。
    // 带着它们发，平台判非法调用（98001021）——抓包对比出来的。
    url = await buildUrl(
      host, SKU_PATH, {},
      ["aadvid", "shop_region", "user_language"],
    );
  }
  // 打上扩展自己的标记，免得这条又被当成「页面那条」记回去
  url = url + EXT_MARK;

  // reporting_level=2 就是「商品数据分析 → 按 SKU」那个页面。
  // 在这个页面里发请求，平台的判定和页面自己发的一致（否则会 98001021）。
  const r = await runInPage(host, async (u, s, e, sz, mp, tpl) => {
    const all = [];
    // 页面发的是极简 body（没有 list_control）时，平台一次就返回全量，
    // 不需要翻页；硬加 list_control 反而会被拒。
    const tplPaging = !!(tpl && tpl.request && tpl.request.list_control);
    if (tpl && !tplPaging) mp = 1;
    // 探针：同源的一个小资源能不能取到？
    // 取不到 → 是这个页面/网络/标签页的问题（不是平台拒绝）；
    // 取得到 → 是平台对这个接口的判定问题。两种情况以前分不清。
    let probe = "";
    try {
      const pr = await fetch("/favicon.ico?j=" + Date.now(), { cache: "no-store" });
      probe = `同源探针 ${pr.status}`;
    } catch (e2) {
      probe = `同源探针也失败：${(e2 && e2.name) || ""} ${(e2 && e2.message) || e2}`;
    }
    for (let page = 0; page < mp; page++) {
      let body;
      if (tpl && typeof tpl === "object") {
        // 原样复制页面那条的 body，只改必要的地方
        body = JSON.parse(JSON.stringify(tpl));
        const rq = body.request || (body.request = {});
        const td = rq.time_descriptor;
        if (td && typeof td === "object" && Object.keys(td).length) {
          rq.time_descriptor = { start: s, end: e };
        }
        if (rq.list_control && rq.list_control.pagination) {
          rq.list_control.pagination.size = sz;
          rq.list_control.pagination.page = page;
        }
      } else {
        body = {
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
      }
      let j;
      let raw = "";
      try {
        const resp = await fetch(u, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        raw = await resp.text();
        j = JSON.parse(raw);
      } catch (err) {
        return {
          error: `第 ${page + 1} 页请求失败：${(err && err.name) || ""} ${(err && err.message) || err}｜${probe}`,
          page: page + 1,
        };
      }
      if (!j || j.code !== 0) {
        // 把平台原话带回去：只给 code=98001021 猜不出原因
        return {
          error: `code=${j && j.code} ${(j && j.message) || ""}｜原文=${raw.slice(0, 200)}｜${probe}`.trim(),
          page: page + 1,
        };
      }
      const items = (j.data && j.data.items) || [];
      all.push(...items);
      if (items.length < sz) return { items: all, pages: page + 1 };
    }
    return { items: all, pages: mp, truncated: true };
  }, [url, start, endExclusive, size, maxPages, tpl], await skuPageHint());

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

async function pagePullSkuByExport(host, start, endExclusive, maxTries = 40, waitMs = 1500, log = () => {}) {
  const exportUrl = await buildUrl(host, "/api/v2/insights/seller/ttp/product/sku/list/export");

  const r = await runInPage(host, async (expUrl, s, e, tries, gap) => {
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
    // 诊断信息：这条导出路径一直不明不白地失败，把平台的真实回应带回去，
    // 否则只能靠猜（已验证：猜错过三次）。
    const diag = { create: "", poll: "" };

    let j;
    try {
      const resp = await fetch(expUrl, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await resp.text();
      j = JSON.parse(text);
    } catch (err) {
      return { error: `创建导出失败：${(err && err.message) || err}` };
    }
    if (!j || j.code !== 0) {
      diag.create = `code=${j && j.code} msg=${String((j && j.message) || "")}`;
      return { error: `创建导出 code=${j && j.code} ${(j && j.message) || ""}`.trim(), diag };
    }

    const file = j.data && j.data.file;
    diag.create = `code=0 file=${String(file || "(无)").slice(0, 100)}`;
    if (!file) return { error: "创建成功但没返回文件路径", diag };

    const fileUrl = file.startsWith("http") ? file : `${location.origin}${file}`;

    // 文件是异步生成的，轮询到能取到二进制为止。
    // 次数由调用方决定：商品管理接口已经失败时，这条路大概率也走不通，
    // 就别在这儿干等两分钟（实测一次能白等 4 分钟）。
    for (let i = 0; i < tries; i++) {
      await new Promise((r2) => setTimeout(r2, gap));
      try {
        const fr = await fetch(fileUrl, { credentials: "include" });
        const ct = fr.headers.get("content-type") || "";
        if (i === 0) diag.poll = `status=${fr.status} ct=${ct.slice(0, 40)}`;
        if (fr.ok && !ct.includes("json")) {
          const buf = await fr.arrayBuffer();
          if (buf.byteLength > 512) {
            const bytes = new Uint8Array(buf);
            let bin = "";
            const CH = 0x8000;
            for (let k = 0; k < bytes.length; k += CH) {
              bin += String.fromCharCode.apply(null, bytes.subarray(k, k + CH));
            }
            return { b64: btoa(bin), bytes: buf.byteLength, diag };
          }
          if (i === 0) diag.poll += ` bytes=${buf.byteLength}(太小)`;
        } else if (i === 0 && fr.ok) {
          // 返回了 JSON：多半是「任务还没好」的状态，把前 160 字带回去看看
          try { diag.poll += ` body=${(await fr.text()).slice(0, 160)}`; } catch (e2) { /* 忽略 */ }
        }
      } catch (err) { /* 还没生成好，继续等 */ }
    }
    return { error: `等了约 ${Math.round(tries * (gap + 3500) / 1000)} 秒仍取不到导出文件`, diag };
  }, [exportUrl, start, endExclusive, maxTries, waitMs]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  const out0 = r.result || {};
  if (out0.diag && (out0.diag.create || out0.diag.poll)) {
    log(`   导出诊断：${out0.diag.create}${out0.diag.poll ? " ｜ 轮询 " + out0.diag.poll : ""}`);
  }
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
  // 只按 3 天切一次。以前还会再按 1 天切 21 段重试，但实测两边返回的都是
  // 同一个 code=98001021（平台下游错误），跟区间大小无关，纯粹多花一分多钟。
  const MAX_TRIES = [3];

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
    log(`按 ${chunk} 天切段仍全部失败（平台返回下游错误，与区间大小无关）`);
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
    const bytes = new TextEncoder().encode(JSON.stringify({ skus }));
    const saved = await uploadToBridge(`sku_list_商品管理_${today}.json`, bytes.buffer, "mapping");
    log(`   已保存：${saved.path}（${skus.length} 个 SKU）`);
    done.push(`商品管理 ${skus.length} 条`);
    // 同一批数据顺便存一份「链接资料」：报表的款式编码列靠它，
    // 不用再单独抓一次接口，也不用手工下载后台的表。
    try {
      const linkSaved = await saveLinkInfoCsv(skus, today, log);
      if (linkSaved) done.push(`链接资料 ${linkSaved.sku_count} 条`);
    } catch (e) {
      log(`   链接资料导出失败（不影响其余取数）：${(e && e.message) || e}`);
    }
  } catch (e) {
    log(`   商品管理接口失败：${(e && e.message) || e}`);
  }

  // ①-a 直接点页面的「导出数据」，接住平台导出的那张表。
  //
  //   为什么优先这条：
  //     · 接口返回的数据里**没有「卖家SKU」**，而报表的款式编码列靠它算；
  //     · 导出的表才是后台「商品数据分析 → 按 SKU → 导出」的完整结果，
  //       含全部 SKU（不受分页 10 条限制）。
  //
  //   用户要求：**每轮都按本次选区重新导一份**，不能因为「商品管理接口通了」
  //   就跳过。那份接口数据是全量、不带区间的；区间内的归属只有这张表才准。
  let skuTableSaved = false;
  {
    try {
      log(`①-a 按区间 ${startDate} ~ ${shiftDate(endDateExclusive, -1)} 重新导出商品SKU表…`);
      // 页面的 timeRange 结束日期是**开区间**：
      // 传 10-21 只导到 10-20（实测表头就是 10-01 ~ 10-20，少一天）。
      // 所以这里传「区间的次日」，让它正好覆盖到末日。
      const got = await exportViaApi("sku", host, log, {
        range: { start: startDate, end: endDateExclusive },
      });
      if (!got || !got.path) {
        // 不再退回「浏览器下载」那条：会弹「另存为」把自动化卡住。
        // 直接交给下面的 ①-b 深度抓取（虽然只有第一页，但不会卡住）。
        log(`   自动导出没成：${(got && got.error) || "未知原因"}；改用页面响应抓取`);
      }
      if (got && got.path) {
        log(`   已接住导出表：${got.path}（${Math.round((got.bytes || 0) / 1024)} KB）`);
        done.push(`导出表 ${got.name || ""}`);
        skuTableSaved = true;
      } else {
        log(`   导出没成功：${(got && got.error) || "未知原因"}`);
      }
    } catch (e) {
      log(`   导出出错：${(e && e.message) || e}`);
    }
  }

  // ①-b 深度抓取：直接读页面自己成功的那次 sku/list 响应。
  //
  //   为什么必须走这条：平台对这条接口的校验极严，我们自己发请求一律被判
  //   98001021（哪怕 URL 参数、请求体、请求头、页面上下文全部对齐）。
  //   但页面自己发的请求是成功的——既然拿不到「正确的请求长什么样」，
  //   就直接拿「页面已经拿到的答案」：用 chrome.debugger 挂在商品分析页上，
  //   等页面发一次请求，用 Network.getResponseBody 把响应体读出来。
  //
  //   这就是用户手动导出的那份「商品数据分析 → 按 SKU」的数据本身。
  if (!done.length) {
    try {
      log("①-b 深度抓取页面自己的商品SKU响应…");
      const cap = await deepCaptureSku(host, await skuPageHint(), log);
      if (cap.error) {
        log(`   深度抓取失败：${cap.error}`);
        if (cap.sawRequest) {
          log(`   但抓到了请求：${String(cap.lastUrl || "").slice(0, 120)}`);
          if (cap.lastPost) log(`   请求体：${String(cap.lastPost).slice(0, 300)}`);
        }
        if (cap.rawSample) log(`   响应原文（前 3000 字）：${cap.rawSample}`);
      } else {
        const skus = cap.rows || [];
        log(`   拿到 ${cap.candidates || 1} 条响应，取最全的那条：${skus.length} 个 SKU`);
        if (skus.length) {
          const bytes2 = new TextEncoder().encode(JSON.stringify({ skus }));
          const saved = await uploadToBridge(
            `sku_list_商品数据分析_${today}.json`, bytes2.buffer, "mapping");
          log(`   已保存：${saved.path}（${skus.length} 个 SKU）`);
          done.push(`商品数据分析 ${skus.length} 条`);
          try {
            const linkSaved = await saveLinkInfoCsv(skus, today, log);
            if (linkSaved) done.push(`链接资料 ${linkSaved.sku_count} 条`);
          } catch (e) {
            log(`   链接资料导出失败（不影响其余取数）：${(e && e.message) || e}`);
          }
        }
      }
    } catch (e) {
      log(`   深度抓取出错：${(e && e.message) || e}`);
    }
  }

  // ② 区间导出：这是「数据分析 → 商品数据分析 → 按 SKU → 导出数据」同一条路，
  //    也是链接归属的权威来源——已删除的 SKU 只有这份表里才有，
  //    累积主表只是临时兜底，所以这条路必须走通。
  //
  //    诊断确认过：创建导出成功，轮询返回的确实是 xlsx，
  //    只是刚创建时还是 0 字节，平台生成文件要 1~4 分钟。
  //    之前阻塞在这里等，把一轮拖到 279 秒；现在改成后台轮询：
  //    报表先用现有主表算出来，文件一好就自动入库，下次核算生效。
  const bgTries = 30;      // 最多约 2 分钟（90 次实测拖到 8 分钟，白等）
  const bgWait = 2000;
  const startBgExport = () => {
    // 放最后启动：它也要用页面标签页，跟③ 并发会互相干扰（表现为 Failed to fetch）
    pagePullSkuByExport(host, startDate, endDateExclusive, bgTries, bgWait, log)
      .then((saved) => {
        log(`② 商品SKU导出完成（后台）：${saved.path}`);
        log(`   新映射已入库，再点一次「立即运行」即用新归属重新核算`);
      })
      .catch((e) => log(`② 商品SKU导出（后台）：${(e && e.message) || e}`));
  };
  // 故意不 await：不让这 1~4 分钟拖住报表

  // ③ 兜底：list 接口分段
  if (!done.length) {
    const all = await pagePullSkuRanged(host, startDate, endDateExclusive, log);
    if (all.length) {
      const bytes = new TextEncoder().encode(JSON.stringify({ data: { items: all } }));
      const saved = await uploadToBridge(`sku_list_${startDate}_${shiftDate(endDateExclusive, -1)}.json`, bytes.buffer, "mapping");
      log(`③ 已保存：${saved.path}（${all.length} 条）`);
      done.push(`list 分段 ${all.length} 条`);
      // 同一批数据顺便生成「链接资料」（链接 → 款式编码），
      // 免得再单独跑一次链接资料任务
      try {
        const linkSaved = await saveLinkInfoCsv(all, today, log);
        if (linkSaved) done.push(`链接资料 ${linkSaved.sku_count} 条`);
      } catch (e) {
        log(`   链接资料导出失败（不影响其余取数）：${(e && e.message) || e}`);
      }
    }
  }

  if (skuTableSaved) {
    log("② 本轮已按区间导好商品SKU表，后台不再重复排一份");
  } else {
    startBgExport();
  }

  if (!done.length) {
    // ② 已经在后台轮询（上面不 await），文件好了会自动入库；
    //    本轮先用现有主表把报表算出来，归属不变。
    log("本轮商品SKU没取到：② 正在后台等导出文件（好了会写日志），"
        + "本轮沿用现有主表，归属不变");
    return;   // 不算失败：主流程继续，报表照常出
  }
  await skSet({ skuCache: { at: Date.now(), items: null } });
  log(`映射取数完成：${done.join(" + ")}`);
  return { via: done.join(" + ") };
}

// ---------------- 任务：链接资料 ----------------

/**
 * 链接资料：报表上「款式编码」那一列的来源。
 *
 * 平台后台下载的「批量编辑 / 商品信息」表长这样：
 *     product_id | category | product_name | sku_id | variation_value | price | seller_sku
 * 它**没有款式编码列**——那一列是卖家自己加的。
 * 但款式编码就藏在 seller_sku 的第一段里：
 *     V27-White-10Pcs   → V27
 *     MY-J68-22cm+clip  → MY-J68
 *     V1【Set-LOVE】    → V1
 * 所以自动取数时按同样的规则推导一份，不用手工维护。
 *
 * 数据源用的是商品管理的 SKU 全量接口——它带 seller_sku，且覆盖
 * 所有链接（包括当期没出单的），跟后台下载的表是同一批数据。
 */
function deriveStyleCode(sellerSku) {
  const s = String(sellerSku || "").trim();
  if (!s) return "";
  // 两段式（MY-J68）要保留两段，所以先试「字母前缀-字母+数字」整段
  let m = s.match(/^([A-Za-z]{1,3}-[A-Za-z]+\d*)(?=[-\[【（(\s]|$)/);
  if (m) return m[1].trim();
  m = s.match(/^([^\-\[【（(\s]+)/);
  return m ? m[1].trim() : "";
}

/**
 * 把商品管理的 SKU 列表存成「链接资料」CSV（含推导出的款式编码）。
 *
 * 与后台下载的「批量编辑 / 商品信息」表同格式，程序按表头认。
 * 抽成函数是为了让「取商品 SKU」和「只取链接资料」两条路共用同一份逻辑。
 */
async function saveLinkInfoCsv(skus, today, log) {
  const rows = [];
  const seen = new Set();
  for (const it of skus || []) {
    // 两种来源结构都要认：
    //   商品管理接口  → {sku_id, product_id, seller_sku, product_title}
    //   商品数据分析  → {meta: {sku_id, product_id, seller_sku, ...}}
    const src = (it && it.meta) || it || {};
    const sid = String(src.sku_id || src.skuId || "");
    const pid = String(src.product_id || src.productId || "");
    if (!sid || !pid || seen.has(sid)) continue;
    seen.add(sid);
    const sku = String(src.seller_sku || src.sellerSku || "");
    rows.push({
      sku_id: sid,
      product_id: pid,
      seller_sku: sku,
      product_name: String(src.product_title || src.productTitle || src.product_name || ""),
      style_code: deriveStyleCode(sku),
    });
  }
  if (!rows.length) return null;

  const head = "sku_id,product_id,seller_sku,product_name,style_code";
  const esc = (v) => {
    const t = String(v == null ? "" : v);
    return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const csv = head + "\n"
    + rows.map((r) => [r.sku_id, r.product_id, r.seller_sku, r.product_name, r.style_code]
      .map(esc).join(",")).join("\n");
  const bytes = new TextEncoder().encode(csv);
  const saved = await uploadToBridge(`链接资料_${today}.csv`, bytes.buffer, "mapping");
  if (log) log(`   链接资料已保存：${saved.path}（${rows.length} 个 SKU）`);

  // 统计一下，方便核对
  const byLink = new Map();
  for (const r of rows) {
    if (!r.style_code) continue;
    if (!byLink.has(r.product_id)) byLink.set(r.product_id, new Set());
    byLink.get(r.product_id).add(r.style_code);
  }
  const multi = Array.from(byLink.values()).filter((s) => s.size > 1).length;
  return { sku_count: rows.length, link_count: byLink.size, multi_count: multi };
}

async function runLinkInfo(range, log, opts = {}) {
  const host = await ensureHost("seller", log);
  const today = shiftDate(new Date().toISOString().slice(0, 10), 0);

  log("取商品管理全量 SKU（含链接资料与款式编码）…");
  const skus = await pagePullSkuFromProductList(host, log);
  if (!skus.length) throw new Error("商品管理接口返回 0 个 SKU");

  const r = await saveLinkInfoCsv(skus, today, log);
  if (!r) throw new Error("商品管理接口里没读到 sku_id / product_id");
  log(`链接资料完成：${r.sku_count} 个 SKU / ${r.link_count} 条链接`
    + `（${r.multi_count} 条含多个款式编码）`);
  return r;
}

// ---------------- 任务：联盟佣金 ----------------

/**
 * 联盟佣金：直接取「商品维度」的交易明细。
 *
 * 为什么不用导出：
 *   导出任务的 list 接口只返回 task_id / file_name / status，**没有下载地址**，
 *   抓包里也找不到任何 download 接口——这条路是死的，之前一直在等它，纯属白等。
 *
 * 为什么用 detail_list/get：
 *   联盟后台的「商品分析」页面自己就在调这个接口，请求体与响应结构都已在抓包里确认：
 *     请求 params.product_list_params[0].time_descriptor.time_context = "101"   ← 之前缺这个所以没命中
 *     响应 data.product_list_segments[].product_performances[]
 *       .product_base.id                                → 商品ID
 *       .product_metrics.product_estimated_commission  → 预估佣金（本地货币，如 RM225.95）
 *       .product_metrics.product_gmv                    → 商品 GMV
 *       .product_metrics.product_orders_cnt             → 订单数
 *   旧工作簿的「达人佣金」口径就是「联盟报表预估佣金，按商品ID汇总」，与这里一致。
 */
const AFFILIATE_METRICS = [1, 3, 2, 4, 9, 20, 13, 14, 19, 6, 24, 22, 16, 17, 18, 25, 26, 23, 5];

async function pagePullAffiliateProducts(host, startSec, endSec, tz, log) {
  const url = await buildUrl(host, "/api/v1/oec/affiliate/compass/transaction/detail_list/get");
  const PAGE_SIZE = 100;
  const MAX_PAGES = 100;

  const r = await runInPage(host, async (u, s, e, tzOffset, metrics, pageSize, maxPages) => {
    const rows = [];
    for (let pageNo = 1; pageNo <= maxPages; pageNo++) {
      const body = {
        params: {
          detail_list_type: 2,
          product_list_params: [{
            time_descriptor: {
              granularity_type: 1,
              timezone_offset: tzOffset,
              start_time: s,
              end_time: e,
              time_option_type: 1,
              time_context: "101",
            },
            metric_types: metrics,
            page_param: { page_no: pageNo, page_size: pageSize },
            sorter: { sort_type: 1, order_type: 1 },
            filter: {},
          }],
        },
        module_type: 104,
        locale: "zh-CN",
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
        return { error: `第 ${pageNo} 页请求失败：${(err && err.message) || err}`, got: rows.length };
      }
      if (!j || j.code !== 0) {
        return {
          error: `第 ${pageNo} 页 code=${j && j.code} ${(j && j.message) || ""}`.trim(),
          got: rows.length,
        };
      }
      const segs = (j.data && j.data.product_list_segments) || [];
      const perfs = [];
      for (const seg of segs) for (const p of (seg.product_performances || [])) perfs.push(p);
      rows.push(...perfs);
      if (perfs.length < pageSize) return { rows, pages: pageNo, done: true };
    }
    return { rows, pages: maxPages, done: false, truncated: true };
  }, [url, startSec, endSec, tz, AFFILIATE_METRICS, PAGE_SIZE, MAX_PAGES]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  if (r.error) throw new Error(r.error);
  const out = r.result || {};
  if (out.error) throw new Error(out.error);
  if (!out.rows || !out.rows.length) throw new Error("联盟接口返回 0 条商品数据");
  log(`联盟接口：${out.pages} 页，${out.rows.length} 个商品${out.truncated ? "（已达上限，可能没取完）" : ""}`);
  return out.rows;
}

/** 把商品维度明细转成核算引擎能直接吃的 CSV。 */
function affiliateRowsToCsv(rows) {
  const esc = (v) => {
    const s = String(v == null ? "" : v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const amt = (o) => (o && o.amount != null ? o.amount : "");
  const num = (o) => (o && o.value != null ? o.value : "");

  // 表头用英文：核算引擎按归一化列名匹配（productid / estcommission），
  // 带 BOM 让 Excel 打开不乱码
  const lines = ["\uFEFFProduct ID,Est. commission,Product name,Product GMV,Orders,Items sold,Refunded GMV"];
  for (const p of rows) {
    const base = p.product_base || {};
    const m = p.product_metrics || {};
    const pid = base.id || "";
    if (!pid) continue;
    lines.push([
      pid,
      amt(m.product_estimated_commission),
      base.title || "",
      amt(m.product_gmv),
      num(m.product_orders_cnt),
      num(m.product_items_sold_cnt),
      amt(m.product_refunded_gmv),
    ].map(esc).join(","));
  }
  return lines.join("\r\n");
}

async function runAffiliate({ startDate, endDateExclusive, tzOffset }, log) {
  const host = await ensureHost("affiliate", log);

  const startSec = Math.floor(new Date(startDate + "T00:00:00").getTime() / 1000);
  const endSec = Math.floor(new Date(endDateExclusive + "T00:00:00").getTime() / 1000) - 1;
  const tz = tzOffset ?? -new Date().getTimezoneOffset() * 60;

  log("取联盟商品维度明细…");
  let rows = [];
  try {
    rows = await pagePullAffiliateProducts(host, startSec, endSec, tz, log);
  } catch (e) {
    log(`   接口取数失败：${(e && e.message) || e}`);
  }

  if (rows.length) {
    const csv = affiliateRowsToCsv(rows);
    const bytes = new TextEncoder().encode(csv);
    const name = `Transaction_Analysis_Product_List_${startDate}_${endDateExclusive}.csv`;
    const saved = await uploadToBridge(name, bytes.buffer);
    log(`已保存：${saved.path}（${rows.length} 个商品）`);
    return { saved: saved.path, products: rows.length };
  }

  // 接口不行就自己去页面点导出——不靠人操作。
  // 优先用「拦截下载」这条（不弹保存框），不行才退回老的下载方式。
  log("   改用「页面导出」取联盟佣金表…");
  let got = await exportViaApi("affiliate", host, log, {
    range: { start: startDate, end: shiftDate(endDateExclusive, -1) },
  });
  if (!got || !got.path) {
    log(`   拦截式导出没成：${(got && got.error) || "未知原因"}；改试浏览器下载…`);
    got = await exportByButton("affiliate", host, log);
  }
  if (!got || got.error || !got.path) {
    throw new Error((got && got.error) || "联盟导出失败");
  }
  log(`   已接住导出表：${got.path}`);
  return { saved: got.path, products: 0, byExport: true };
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

// ---------------- 通用取数执行器（规格驱动） ----------------

/** 当前选中的店铺 id（从扩展存储读，面板改设置时会写进来）。 */
async function currentStoreId() {
  const s = await skGet({ settings: {} });
  return (s.settings || {}).store || "";
}

/**
 * 按规格执行一次取数。
 *
 * 扩展在这里只干两件事：
 *   1. 在页面上下文里把请求发出去（带 Cookie 与风控参数，只有那里有）
 *   2. 把原始响应原样带回来
 *
 * 「取哪条路径、映射哪些字段、怎么汇总、出什么文件」全在本地程序那边，
 * 规格见 config/api_specs.json。以后加数据源 = 加一段 JSON，
 * 改取数逻辑也不用刷新扩展。
 */
async function runSpecTask(task, range, log) {
  // 后台脚本没有 popup 那个 currentStore（那是面板里的 DOM 读取），
  // 这里从扩展存储读当前选的店铺。
  const store = await currentStoreId();
  log("取取数规格…");
  const specResp = await fetch(`${BRIDGE}/spec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, range, store }),
  });
  const sj = await specResp.json();
  if (!sj.ok) throw new Error(sj.error || "取规格失败");

  const spec = sj.spec || {};
  const steps = spec.steps || [];
  if (!steps.length) throw new Error(`规格里没有步骤（任务 ${task}）`);
  const specHost = spec.host || "seller";
  // spec.host 写的是「哪个后台」（seller / affiliate），**不是域名**。
  // 真正的域名（如 seller-my.tiktok.com）要由 ensureHost 从学到的参数里取。
  // 之前把 "seller" 直接当域名去匹标签页（*://seller/*），永远匹配不到，
  // 于是广告 / 联盟佣金两步一律报「没有开着的 seller 页面」。
  const hint = (specHost === "seller" || specHost === "affiliate") ? specHost : "seller";
  // 先确保那个域的页面开着、参数（fp、X-Tts-Oec-Bsid 等）是新鲜的。
  // 接口要这些风控参数，缺了就返回含糊的 downstream server error。
  const host = await ensureHost(hint, log);
  // spec.host 本身就是域名时（以后可能有），直接用；否则用 ensureHost 返回的真实域名
  const realHost = (specHost === hint) ? (host || hint) : specHost;

  const results = [];
  for (const step of steps) {
    const label = step.name || step.path;
    log(`  · ${label}`);
    const r = await callApi(realHost, step.path, step.method || "POST", step.body);
    results.push({
      path: step.path,
      method: step.method || "POST",
      request_body: step.body || null,
      response: r.data || null,
      error: r.error || "",
    });
    if (r.error) log(`    请求失败：${r.error}`);
    else if (r.data && r.data.code !== undefined) log(`    code=${r.data.code}`);
  }

  log("交回本地程序解析落盘…");
  const fr = await fetch(`${BRIDGE}/fetch-result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, range, store, results }),
  });
  const fj = await fr.json();
  for (const i of fj.issues || []) log(`  ${i}`);
  if (!fj.ok) throw new Error((fj.issues || []).join("；") || fj.error || "处理失败");
  log(`完成：${(fj.files || []).length} 个文件`);
  return { via: "规格驱动", files: fj.files };
}

/** 在页面上下文里发一个请求，把响应原样带回来。 */
async function callApi(host, path, method, body) {
  const url = await buildUrl(host, path);
  const r = await runInPage(host, async (u, m, b) => {
    try {
      const opt = {
        method: m,
        credentials: "include",
        headers: { "Content-Type": "application/json" },
      };
      if (m !== "GET" && m !== "HEAD" && b != null) opt.body = JSON.stringify(b);
      const resp = await fetch(u, opt);
      const text = await resp.text();
      let j = null;
      try { j = JSON.parse(text); } catch (e) { j = { _raw: text.slice(0, 2000) }; }
      return { ok: true, status: resp.status, data: j };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  }, [url, method, body]);

  if (r.missingTab) return { error: `没有开着的 ${host} 页面` };
  if (r.error) return { error: r.error };
  const res = r.result || {};
  if (!res.ok) return { error: res.error || "请求失败" };
  return { status: res.status, data: res.data };
}

// ---------------- 任务：广告商品维度花费（直接取数，不等导出） ----------------

/**
 * 广告花费：直接调数据接口拿商品维度数据，不再等平台生成导出文件。
 *
 * 抓包实测（用户在广告页面点「导出」时，页面自己发的请求）：
 *
 * POST /oec_ads/shopping/v1/oec/stat/post_product_list_v2
 * {"query_list":["product_name","campaign_id","campaign_name","product_id","mixed_real_cost",
 *               "onsite_roi2_shopping_sku","mixed_real_cost_per_onsite_roi2_shopping_sku",
 *               "onsite_roi2_shopping_value","onsite_mixed_real_roi2_shopping","extra_currency"],
 *  "action":"download",
 *  "start_time":"2026-10-01","end_time":"2026-10-15",
 *  "order_field":"mixed_real_cost","order_type":1,
 *  "page":1,"page_size":-1,
 *  "filters":[{"field":"campaign_status","filter_type":0,"in_field_values":["no_delete"]},
 *             {"field":"gmv_roi_mode","filter_type":10,"in_field_values":["2"]}],
 *  "dims":["spu_id","campaign_id"]}
 *
 * 三个关键点：
 *  · action="download" 是「创建导出任务」，只返回 task_id，要等平台生成文件（慢、且要人点）
 *  · page_size=-1 表示一次取全，不用翻页
 *  · dims=["spu_id","campaign_id"] 说明一行 = 一个「商品 × 计划」，
 *    同一商品会有多行（每个计划一行），花费要按商品ID 汇总
 *
 * 换成查询动作应该能直接拿到 data.table。到底是哪个 action 值能查，
 * 接口没写文档，所以这里自动探测：逐个试，看谁返回商品行。
 */
const ADS_QUERY_FIELDS = [
  "product_name", "campaign_id", "campaign_name", "product_id", "mixed_real_cost",
  "onsite_roi2_shopping_sku", "mixed_real_cost_per_onsite_roi2_shopping_sku",
  "onsite_roi2_shopping_value", "onsite_mixed_real_roi2_shopping", "extra_currency",
];

const ADS_FILTERS = [
  { field: "campaign_status", filter_type: 0, in_field_values: ["no_delete"] },
  { field: "gmv_roi_mode", filter_type: 10, in_field_values: ["2"] },
];

async function pagePullAdsProducts(host, startDate, endDate, log) {
  const url = await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/post_product_list_v2");
  // 不带 action 参数实测就能直接返回数据（data.table_v2），
  // 所以只用这一个动作就够
  const ACTIONS = [""];

  const r = await runInPage(host, async (u, s, e, query, filters, actions) => {
    const mkBody = (action, page) => {
      const b = {
        query_list: query,
        start_time: s,
        end_time: e,
        order_field: "mixed_real_cost",
        order_type: 1,
        page: page || 1,
        page_size: -1,
        filters,
        dims: ["spu_id", "campaign_id"],
      };
      if (action) b.action = action;
      return b;
    };
    const pickRows = (j) => {
      const d = j && j.data;
      if (!d) return null;
      // table_v2 是这个接口实际的行数据键（实测：不带 action 参数也直接返回它）
      for (const k of ["table_v2", "table", "items", "list", "rows", "data_list"]) {
        if (Array.isArray(d[k]) && d[k].length) return d[k];
      }
      return null;
    };
    const post = async (body) => {
      const resp = await fetch(u, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return JSON.parse(await resp.text());
    };

    const out = { tried: [], rows: [], action: "", pagination: null, sample: [] };
    for (const a of actions) {
      let j = null;
      let err = "";
      try {
        j = await post(mkBody(a, 1));
      } catch (ex) {
        err = String((ex && ex.message) || ex);
      }
      const info = {
        action: a || "(不带 action)",
        code: j && j.code,
        msg: String((j && (j.message || j.msg)) || err).slice(0, 70),
        dataKeys: j && j.data ? Object.keys(j.data).slice(0, 8) : [],
        extra: j && j.extra ? JSON.stringify(j.extra).slice(0, 120) : "",
      };
      const rows = pickRows(j);
      if (rows && rows.length) {
        // table_v2 是列式：[[表头…], [行…], …]。
        // 先原样留两份，日志里打出来核对，别猜。
        out.sample = rows.slice(0, 2);
        out.pagination = (j.data && j.data.pagination) || null;
        info.rowCount = rows.length;
        info.firstIsArray = Array.isArray(rows[0]);
        info.rowKeys = Array.isArray(rows[0])
          ? rows[0].map((h) => String(h)).slice(0, 26)
          : Object.keys(rows[0]).slice(0, 26);

        // pagination 说有几页就翻几页（page_size=-1 也可能仍分页）
        let all = rows.slice();
        const pc = Number((out.pagination || {}).page_count || 1);
        for (let pg = 2; pg <= Math.min(pc, 50); pg++) {
          try {
            const j2 = await post(mkBody(a, pg));
            const more = pickRows(j2);
            if (!more || !more.length) break;
            all = all.concat(more);
          } catch (ex) { break; }
        }
        info.totalRows = all.length;
        out.tried.push(info);
        out.rows = all;
        out.action = a;
        return out;
      }
      out.tried.push(info);
    }
    return out;
  }, [url, startDate, endDate, ADS_QUERY_FIELDS, ADS_FILTERS, ACTIONS]);

  if (r.missingTab) throw new Error(`没有开着的 ${host} 页面`);
  if (r.error) throw new Error(r.error);
  const out = r.result || {};
  for (const t of out.tried || []) {
    log(`  · action=${t.action} → code=${t.code} ${t.msg}｜data键=${(t.dataKeys || []).join(",") || "无"}`
      + `${t.extra ? "｜" + t.extra : ""}${t.rowCount ? `｜**${t.rowCount} 行**` : ""}`
      + `${t.totalRows && t.totalRows !== t.rowCount ? `｜翻页后共 ${t.totalRows} 行` : ""}`);
    if (t.rowKeys && t.rowKeys.length) log(`    列名：${t.rowKeys.join(" | ")}`);
  }
  for (const smp of out.sample || []) {
    log(`    原始行：${JSON.stringify(smp).slice(0, 400)}`);
  }
  if (!out.rows || !out.rows.length) {
    throw new Error("广告数据接口没返回商品行（各 action 的返回见上方日志）");
  }
  log(`商品维度广告数据：action="${out.action || "(不带)"}"，${out.rows.length} 行`);
  return out;
}

/** 从一行里按候选名取字段：先精确匹配，再按 key 包含关系模糊匹配。 */
function pickField(row, cands) {
  if (!row) return "";
  for (const c of cands) {
    const v = row[c];
    if (v != null && v !== "") return v;
  }
  const keys = Object.keys(row);
  for (const c of cands) {
    const lc = c.toLowerCase();
    for (const k of keys) {
      // 模糊匹配时排除「衍生指标」：cost_per_xxx / xxx_boost_cost / creative_nobid_cost
      // 这些是花费的分项或比率，累加进去会把花费算大好几倍
      if (/_per_|_boost_|nobid|_rate|_ratio|_cnt|_count|_value|_sku$/i.test(k)) continue;
      if (k.toLowerCase().includes(lc) && row[k] != null && row[k] !== "") return row[k];
    }
  }
  return "";
}

/** 金额转数字：兼容 "1,234.56" 这类带逗号的字符串。 */
function numOf(v) {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (v == null || v === "") return null;
  const n = parseFloat(String(v).replace(/[^0-9.\-]/g, ""));
  return Number.isFinite(n) ? n : null;
}

/** 列式转对象式。
 *
 * table_v2 实际是「列式」：第一行是列名，后面每行是一个数组，
 * 而不是每行一个对象。所以取字段前要先转一道。
 */
function rowsToObjects(rows) {
  if (!rows || !rows.length) return [];
  if (!Array.isArray(rows[0])) return rows;        // 已经是对象数组
  const head = rows[0].map((h) => String(h));
  return rows.slice(1).map((r) => {
    const o = {};
    head.forEach((h, i) => { o[h] = Array.isArray(r) ? r[i] : r; });
    return o;
  });
}

/** 把商品维度广告行汇总成核算引擎能吃的 CSV（按商品ID 汇总花费）。 */
function adsRowsToCsv(rows) {
  const esc = (v) => {
    const s = String(v == null ? "" : v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const objs = rowsToObjects(rows);
  const agg = new Map();
  for (const r of objs) {
    const pid = String(pickField(r, ["product_id", "spu_id", "productid"]) || "");
    if (!pid) continue;
    const raw = pickField(r, ["mixed_real_cost", "cost", "billed_cost", "basic_cost", "spend"]);
    const cur = String(pickField(r, ["extra_currency", "currency"]) || "").trim();
    const name = String(pickField(r, ["product_name", "title"]) || "");
    const key = `${pid}\u0001${cur}`;
    const agg0 = agg.get(key) || { pid, cur, name: "", sum: 0, has: false };
    const n = numOf(raw);
    if (n !== null) { agg0.sum += n; agg0.has = true; }
    if (name && !agg0.name) agg0.name = name;
    agg.set(key, agg0);
  }
  // 一个商品ID都没取到 = 结构判断错了。
  // 这时把原始前两行带出来，日志里能直接看出该按哪列取，别让问题悄悄过去。
  if (!agg.size) {
    const raw = (rows || []).slice(0, 2).map((r) => JSON.stringify(r).slice(0, 300)).join(" / ");
    throw new Error(`广告行里取不到商品ID（列式转换后仍为空）。原始行：${raw}`);
  }
  // 表头用英文：核算引擎按归一化列名匹配（productid / spend / currency）
  const lines = ["\uFEFFProduct ID,Spend,Currency,Product name"];
  for (const v of agg.values()) {
    lines.push([v.pid, v.has ? v.sum : "", v.cur, v.name].map(esc).join(","));
  }
  return lines.join("\r\n");
}

async function runAds(range, log, opts = {}) {
  const quick = !!opts.quick;
  const host = await ensureHost("seller", log);

  // 优先直取数据接口：一次拿全，不用等平台生成文件
  if (!opts.noDirect) {
    // 区间缺失时不能硬发：executeScript 的 args 里有 undefined 会报
    // "Value is unserializable"，看不出是哪个参数的问题。
    // 没有区间就只能走「下载已完成的导出任务」那条路。
    const startDate = range && range.startDate;
    const endDate = range && (range.endDateExclusive ? shiftDate(range.endDateExclusive, -1) : range.to);
    if (!startDate || !endDate) {
      log("没有日期区间，跳过接口直取（只能下载已完成的导出任务）");
    } else {
    try {
      log("直取商品维度广告数据…");
      const res = await pagePullAdsProducts(host, startDate, endDate, log);
      const csv = adsRowsToCsv(res.rows);
      const bytes = new TextEncoder().encode(csv);
      const name = `creative data for product campaigns (${range.startDate} 00 ~ ${endDate} 23).csv`;
      const saved = await uploadToBridge(name, bytes.buffer);
      log(`已保存：${saved.path}`);
      return { via: "接口直取", rows: res.rows.length, saved: saved.path };
    } catch (e) {
      log(`接口直取失败：${(e && e.message) || e}`);
      if (quick) return { skipped: true, reason: String((e && e.message) || e) };
    }
    }
  }

  // 接口不通时，程序自己去广告页点「导出数据」（不弹保存框那条路）。
  // 注意这里只看 quick：noDirect 的意思是「别先试直取接口」，不是「别导」。
  // 之前两个条件一起写，菲律宾这种 noDirect 的站点连页面导出都被跳过，
  // 直接掉到最后「查已完成任务」，而没人手动导过就是空等 5 分钟。
  if (!quick) {
    const startDate = range && range.startDate;
    const endDate = range && (range.endDateExclusive ? shiftDate(range.endDateExclusive, -1) : range.to);
    if (startDate && endDate) {
      log("接口不通，改由程序自己去广告页点导出…");
      const got = await exportViaApi("ads", host, log, {
        range: { start: startDate, end: endDate },
      });
      if (got && got.path) {
        log(`已接住广告导出表：${got.path}`);
        return { via: "页面导出", saved: got.path };
      }
      log(`   页面导出没成：${(got && got.error) || "未知原因"}；改查导出任务`);
    }
  }

  log("查询广告导出任务…");
  const maxRounds = quick ? 1 : 20;      // 非快查模式最多等 5 分钟
  let idle = 0;                          // 连续「进行中 0 个」的次数
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
    // 连续三次都「没有任务在跑」，说明根本没人建过导出任务，
    // 再等下去只是干耗 5 分钟，不如早点把结论交回去。
    idle = r.running ? 0 : (idle + 1);
    if (idle >= 3) {
      log("没有在跑的广告导出任务，不再空等");
      return { pending: false, none: true };
    }
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
      // 心跳跟轮询绑在一起：界面靠它判断「这家店的浏览器开着没」。
      // 每分钟一次，桥那边超过 150 秒没收到就算离线。
      if (a.name === "pending-watch") beatBridge().catch(() => {});
      if (a.name === "pending-watch") consumePending().catch(() => {});
      // 排队中的订单导出：每分钟看一眼，生成好就自动下载回传。
      // 大店导出入队要几分钟，不能靠人守着。
      if (a.name === "pending-watch") harvestPendingOrders(null, "").catch(() => {});
    });
    chrome.alarms.onAlarm.addListener((a) => {
      if (a.name === "ads-poll") pollAdsQuiet();
    });
  }
} catch (e) {
  console.warn("广告守望注册失败：", e);
}

// ---------------- 任务：抓广告导出接口 ----------------

/**
 * 专门抓广告页面的「导出」请求。
 *
 * 背景：广告导出的**创建**接口在 ads 域。
 * 之前的抓包记录里 ads 域只有 download_task/query（查询），
 * 没有创建——因为抓包时人不在广告页面上，ads 域的写请求根本没发生过。
 * 这不是程序的问题，是抓不到就没数据。
 *
 * 这一步要人配合一次：在广告页面点一次「导出」。
 * 抓到请求体之后就能照着复现，之后就不用再点了。
 */
async function captureAds(seconds, log) {
  const secs = Math.max(30, Math.min(600, seconds || 180));

  log("请在广告页面点「导出面板里那一行右边的『导出』小按钮」——右上角那个大的只是打开面板");
  log("（监听已经开着，不用先点面板再回来找按钮）");

  // 扫一遍各页面上的「导出」按钮，直接告诉用户点哪个
  try {
    const scan = await scanExportButtons();
    let any = false;
    for (const s of scan) {
      if (!s.total) continue;
      any = true;
      const mark = s.exportButtons.length ? "★ 有导出按钮" : "无导出按钮";
      log(`  · ${(s.title || "(无标题)").slice(0, 26)}｜${mark}｜${s.exportButtons.join(" / ") || "—"}`);
    }
    if (!any) log("  （没扫到可注入的店铺页面，建议把广告页面保持在前台）");
  } catch (e) { /* 忽略 */ }

  const recs = [];
  adsWatch = {
    recs,
    log: (m) => log(m),
    startedAt: Date.now(),
  };

  // 到点自动收工，顺带把观察窗口关掉
  const timer = setTimeout(() => { adsWatch = null; }, (secs + 10) * 1000);

  try {
    const deadline = Date.now() + secs * 1000;
    while (Date.now() < deadline) {
      await sleep(1000);
      // 抓到「创建导出任务」的请求就收工，不用让人干等满时间
      const created = recs.find((x) => {
        const u = x.url || "";
        return !/query|list|\/get\b|health|ping/i.test(u) && /export|create|download|task|generate/i.test(u);
      });
      if (created) {
        log("已抓到创建类请求，可以收工");
        break;
      }
    }
  } finally {
    clearTimeout(timer);
    adsWatch = null;
  }

  log(`共捕获 ${recs.length} 条广告请求`);
  return {
    seconds: secs,
    requests: recs.length,
    urls: recs.map((x) => `${x.method} ${(x.url || "").split("?")[0]}`),
  };
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
  // 定时已经交给本地桥的「计划任务」了（那边能配多条、能按周期算区间、
  // 浏览器关着也能排队）。扩展自己这套周/月定时是旧设计，
  // 两边同时开着的后果是**同一件事跑两遍**：两份报表、两次核算。
  // 只要桥在线就认桥，扩展这边不再触发。
  try {
    const r = await fetch(`${BRIDGE}/health`, { cache: "no-store" });
    if (r.ok) {
      const j = await r.json();
      if (j && j.ok) return null;
    }
  } catch (e) { /* 桥没开，才轮到扩展自己定时 */ }

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


// ---------------- 手动导数据（自动取数挂掉时的兜底） ----------------
//
// 场景：平台接口改了、登录态掉了、弹风控——自动那条路断了，
// 但人在后台页面上照样能把表导出来。这个模块负责：
//   1. 打开对应的店铺页面（用你现有的登录态）
//   2. 「陪同导出」模式下盯着浏览器下载，文件一下载就抓回来入库
//
// 抓下载的思路：监听 chrome.downloads.onChanged，下载完成时拿到
// download.url，再从后台 fetch 回来（后台里的 fetch 带 cookie，
// 平台的 CDN 文件就是这么下的）。

const MANUAL_PAGES = {
  orders: { url: "https://seller-my.tiktok.com/", label: "卖家后台（订单管理）" },
  sku: { url: "https://seller-my.tiktok.com/", label: "卖家后台（商品管理）" },
  affiliate: { url: "https://affiliate.tiktok.com/affiliate/platform/homepage", label: "达人联盟后台" },
  ads: { url: "https://seller-my.tiktok.com/", label: "卖家后台（广告）" },
};

const MANUAL_CAPTURE = { active: false, until: 0, seen: new Set() };

/** 打开店铺后台页面；capture=true 时顺便开始盯着下载。 */
async function openBackoffice(kind, capture, log) {
  const page = MANUAL_PAGES[kind] || MANUAL_PAGES.orders;
  if (capture) {
    MANUAL_CAPTURE.active = true;
    MANUAL_CAPTURE.until = Date.now() + 15 * 60 * 1000;
    MANUAL_CAPTURE.seen = new Set();
    log("开始盯着下载（15 分钟内有效）");
  }
  await chrome.tabs.create({ url: page.url, active: true });
  return { url: page.url, label: page.label, capture: !!capture };
}

// 正在等「页面导出」结果的地方（点导出按钮后，文件下载完就通知它）
let EXPORT_WAIT = null;

/** 下载完成 → 抓回来入库。 */
async function onDownloadComplete(delta) {
  if (!MANUAL_CAPTURE.active || Date.now() > MANUAL_CAPTURE.until) return;
  if (!delta || !delta.state || delta.state.current !== "complete") return;
  if (MANUAL_CAPTURE.seen.has(delta.id)) return;
  MANUAL_CAPTURE.seen.add(delta.id);

  const log = (m) => appendTaskLog(`[manual] ${m}`);
  let item = null;
  try {
    const list = await chrome.downloads.search({ id: delta.id });
    item = (list && list[0]) || null;
  } catch (e) { /* 查不到就放弃 */ }
  if (!item || !item.url) return;

  const fname = String(item.filename || "").split(/[\\/]/).pop() || "导出文件";
  if (!/\.(csv|xlsx|xls|txt|json)$/i.test(fname)) {
    log(`跳过「${fname}」：不是表格文件`);
    return;
  }
  try {
    const resp = await fetch(item.url, { credentials: "include" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await resp.arrayBuffer();
    if (!buf || buf.byteLength < 64) throw new Error("文件太小，可能还没生成好");
    // 等导出结果的那一刻说明这是「商品SKU表」，直接进 mapping 目录，
    // 核算前会自动合并进映射主表
    const saved = await uploadToBridge(fname, buf, EXPORT_WAIT ? "mapping" : "");
    log(`已接住「${fname}」→ ${saved.path}`);
    if (EXPORT_WAIT) {
      const w = EXPORT_WAIT; EXPORT_WAIT = null;
      w({ name: fname, path: saved.path, bytes: buf.byteLength });
    }
  } catch (e) {
    log(`接不住「${fname}」（${e.message}）：请改用「只开页面」模式，`
      + `导出后到桌面端「手动导入」上传`);
  }
}

// 走「接口级导出」时，浏览器仍可能弹「另存为」——直接取消掉，
// 免得用户被一个对话框拦住（文件我们已经从接口取回来了）。
let SUPPRESS_DL = false;

// 导出流程中浏览器如果真发起了下载：
//   ① 把地址记下来（这就是我们要的文件地址）
//   ② 立刻取消（避免「另存为」挡住流程）
let DL_SEEN = [];

if (chrome.downloads && chrome.downloads.onCreated) {
  chrome.downloads.onCreated.addListener((item) => {
    if (!SUPPRESS_DL || !item || !item.id) return;
    try {
      DL_SEEN.push({
        url: item.url || item.finalUrl || "",
        name: item.filename || "",
        at: Date.now(),
      });
    } catch (e) { /* 忽略 */ }
    try { chrome.downloads.cancel(item.id); } catch (e) { /* 忽略 */ }
    try { chrome.downloads.erase({ id: item.id }); } catch (e) { /* 忽略 */ }
  });
}

if (chrome.downloads && chrome.downloads.onChanged) {
  chrome.downloads.onChanged.addListener((delta) => {
    onDownloadComplete(delta).catch(() => {});
  });
}


// ---------------- SKU 深度抓取（读页面自己成功的响应） ----------------
//
// 为什么走这条路：
//   平台对这条接口的校验极严——我们自己发请求会被判 98001021，
//   哪怕 URL 参数、请求体结构、请求头、页面上下文全都对得上。
//   而**页面自己发的请求是成功的**，只是我们的抓包方式拿不到它的请求体
//   （webRequest 的 requestBody 对页面那些请求是 undefined）。
//
//   所以换个思路：不去猜「正确的请求长什么样」，而是**直接拿页面已经拿到的答案**。
//   用 chrome.debugger 挂在商品分析页上，等页面自己发一次 sku/list，
//   然后用 Network.getResponseBody 把响应体读出来——那就是全量 SKU 数据。
//   页面维度切到「按 SKU」时一次就会返回全部（含当期没销量的链接）。
//
/** 从页面 sku/list 响应里挖出 SKU 列表。

 * 平台这个接口的返回结构我们没拿到过样本（页面请求的响应体读不到，
 * 自己的请求又被拒），所以这里把见过的几种形态都兼容一遍：
 *   ① {code:0, data:{list:[{meta:{sku_id,...}}]}}   ← 商品数据分析（字段在 meta 下）
 *   ② {code:0, data:{list:[{sku_id,...}]}}
 *   ③ {code:0, data:{items:[...]}} / rows / records / sku_list
 *   ④ {code:0, data:[...]}
 * 逐层往下找第一个「像 SKU 列表」的数组，认字段名时大小写/下划线都容错。
 */
function extractSkuList(respObj) {
  if (!respObj || typeof respObj !== "object") return [];

  const SKEYS = ["sku_id", "skuId", "skuID", "sku"];
  const PKEYS = ["product_id", "productId", "productID"];
  const NAME_KEYS = ["product_title", "productTitle", "product_name", "productName", "title"];
  const SKU_NAME_KEYS = ["seller_sku", "sellerSku", "sku_name", "skuName"];

  const pick = (obj, keys) => {
    for (const k of keys) {
      const v = obj[k];
      if (v !== undefined && v !== null && v !== "") return String(v);
    }
    return "";
  };

  const looksLikeSku = (o) => o && typeof o === "object" && !Array.isArray(o)
    && (SKEYS.some((k) => o[k] !== undefined) || PKEYS.some((k) => o[k] !== undefined));

  const toRows = (arr) => {
    const out = [];
    for (const it of arr || []) {
      if (!it || typeof it !== "object") continue;
      const src = (it.meta && typeof it.meta === "object") ? it.meta : it;
      const sid = pick(src, SKEYS);
      const pid = pick(src, PKEYS);
      // 归属靠的是 sku_id → product_id，缺 sku_id 的行（比如「按商品」维度的结果）
      // 对映射没用，直接丢掉——否则会把最优响应判错
      if (!sid || !pid) continue;
      const skuName = pick(src, SKU_NAME_KEYS);
      out.push({
        sku_id: sid,
        product_id: pid,
        seller_sku: skuName,
        product_name: pick(src, NAME_KEYS),
        style_code: deriveStyleCode(skuName),
      });
    }
    return out;
  };

  // 广度优先找第一个可用的数组
  const seen = new Set();
  const queue = [respObj];
  while (queue.length) {
    const cur = queue.shift();
    if (!cur || typeof cur !== "object" || seen.has(cur)) continue;
    seen.add(cur);
    if (Array.isArray(cur)) {
      if (cur.length && looksLikeSku(cur[0])) return toRows(cur);
      // 数组里每个元素可能再包一层
      for (const it of cur) {
        if (it && typeof it === "object" && !Array.isArray(it)) queue.push(it);
      }
      continue;
    }
    // 优先看常见的列表字段名
    const prefer = ["list", "items", "rows", "records", "sku_list", "skuList",
                    "data_list", "dataList", "result"];
    for (const k of prefer) {
      if (Array.isArray(cur[k])) {
        const r = toRows(cur[k]);
        if (r.length) return r;
        queue.push(cur[k]);
      }
    }
    for (const k of Object.keys(cur)) {
      const v = cur[k];
      if (v && typeof v === "object") queue.push(v);
    }
  }
  return [];
}

/** 点页面的「导出数据」，把平台导出的那张表接回来。

 * 为什么要走这条路（而不是自己发接口）：
 *   1. 平台对 sku/list 接口校验极严，我们自己发一律被判 98001021；
 *   2. 更关键的是——那个接口返回的数据里**没有「卖家SKU」**，
 *      而报表的款式编码列（V27-White → V27）就是靠它算的。
 *      只有「导出数据」这张表才带卖家SKU。
 *
 * 做法：后台开一个商品分析页 → 切「按 SKU」→ 点「导出数据」
 *      → 有确认弹窗就点掉 → 等浏览器下载完成 → 扩展接住文件回传。
 */
/** 四张表的「导出」配置：页面路径 + 要点的按钮文案。
 *
 * 依据是抓包实测（每个导出在哪个页面、点哪个按钮）：
 *   · 订单      seller-my/order                      「导出」
 *   · 广告      seller-my/ads-creation/dashboard      「导出数据」
 *   · 联盟佣金  affiliate/insights/transaction-analysis 「导出」
 *   · 商品SKU   seller-my/compass/product-analysis     「按 SKU」→「导出数据」
 *
 * 按钮文案可能随语言/版本变，所以找不到按钮时会把页面上所有含「导出」的
 * 按钮文案打进日志，下次照着改就行，不用猜。
 */
const EXPORT_FLOWS = {
  orders: {
    label: "订单表",
    dir: "",                 // 按表头自动归类 → 店铺/inbox/订单表

    // 订单后台的导出分两步：先点「导出」，弹窗里再点一次「导出」
    page: "order",
    steps: [["导出"], ["导出", "确定", "确认"]],
  },
  ads: {
    label: "广告表",
    dir: "ads",              // → 店铺/inbox/广告表

    page: "ads-creation/dashboard",
    // 新版后台（GMV Max）右上角就是一个「导出」按钮，点了弹窗里再确认一次。
    // 老版后台叫「导出数据」。两组词都留着，谁在页面上就用谁。
    steps: [["导出数据", "导出", "Export"], ["导出", "确定", "确认", "下载", "Export"]],
  },
  affiliate: {
    label: "联盟佣金表",
    dir: "affiliate",        // → 店铺/inbox/联盟佣金表

    host: "affiliate.tiktok.com",
    page: "insights/transaction-analysis",
    steps: [["导出数据", "导出"], ["导出", "确定", "确认", "下载"]],
  },
  sku: {
    label: "商品SKU表",
    dir: "mapping",          // → 数据/mapping（核算前自动并进主表）

    page: "compass/product-analysis",
    steps: [["按SKU", "按 SKU"], ["导出数据", "导出"], ["导出", "确定", "确认"]],
  },
};

/** 在页面里按文案点按钮（取最内层、可见的那个）。 */
async function clickByTextIn(tabId, want) {
  try {
    // allFrames 很关键：新版广告后台是嵌在 iframe 里的（外层只有卖家后台侧边栏），
    // 只在主文档里找，永远找不到「导出」——菲律宾就是这么卡住的。
    const rs = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true }, world: "MAIN",
      func: (texts) => {
        const norm = (x) => (x || "").replace(/\s+/g, "").trim();
        const cands = Array.from(
          document.querySelectorAll("button,span,div,a,li,label,td,[role=button]"));
        const visible = (e) => e.offsetParent !== null;

        // 先精确匹配（最安全）
        const exact = cands.find((e) => {
          const t = norm(e.textContent);
          if (!texts.includes(t)) return false;
          if (!visible(e)) return false;
          const kids = e.querySelectorAll("button,span,a,li,label,td");
          return ![...kids].some((k) => texts.includes(norm(k.textContent)));
        });
        if (exact) { exact.click(); return "exact"; }

        // 再退一步：包含匹配（弹窗按钮常叫「确认导出」「导出数据」这类）
        const loose = cands.find((e) => {
          const t = norm(e.textContent);
          if (!t || t.length > 12) return false;
          if (!texts.some((w) => t.includes(w))) return false;
          if (!visible(e)) return false;
          const kids = e.querySelectorAll("button,span,a,li,label");
          return ![...kids].some((k) => {
            const kt = norm(k.textContent);
            return kt && kt.length <= 12 && texts.some((w) => kt.includes(w));
          });
        });
        if (loose) { loose.click(); return "loose"; }
        return "";
      },
      args: [want],
    });
    // 哪个 frame 点中了就返回它（外层框架可能没有按钮）
    for (const x of rs || []) {
      if (x && x.result) return x.result;
    }
    return "";
  } catch (e) {
    return "";
  }
}

/** 页面上现在有没有这些文案的可点按钮（只看可见元素）。 */
async function hasButtonText(tabId, texts) {
  try {
    const rs = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true }, world: "MAIN",
      func: (want) => {
        const norm = (x) => (x || "").replace(/\s+/g, "").trim();
        for (const el of document.querySelectorAll("button,[role=button],a,span,div")) {
          const t = norm(el.textContent);
          if (!t || t.length > 12) continue;
          if (!want.some((w) => t === norm(w) || t.includes(norm(w)))) continue;
          if (el.offsetParent === null) continue;
          return true;
        }
        return false;
      },
      args: [texts],
    });
    return (rs || []).some((x) => x && x.result);
  } catch (e) {
    return false;
  }
}

/**
 * 等页面上出现目标按钮，等到再点。
 *
 * 为什么要等：广告后台（GMV Max）是重量级单页应用，浏览器报 status=complete
 * 只代表 HTML 到了，面板还要好几秒才渲染出来。之前固定 sleep 3~5 秒就去点，
 * 页面上还只有侧边栏，于是被判「没有导出按钮」。
 */
async function waitForButton(tabId, texts, timeoutMs = 40000, log) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await hasButtonText(tabId, texts)) {
      if (log && Date.now() - t0 > 3000) {
        log(`   等 ${Math.round((Date.now() - t0) / 1000)} 秒后按钮出现`);
      }
      return true;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  return false;
}

/** 扫页面上所有含「导出」的按钮文案——按钮找不到时用来诊断。 */
async function scanExportButtons(tabId) {
  try {
    const rs = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const out = [];
        const seen = new Set();
        for (const el of document.querySelectorAll("button,[role=button],a,span,div")) {
          const t = (el.innerText || "").trim().replace(/\s+/g, " ");
          if (!t || t.length > 16 || seen.has(t)) continue;
          if (!/导出|Export|Eksport|Xuất|ส่งออก/i.test(t)) continue;
          const b = el.getBoundingClientRect();
          if (b.width < 10 || b.height < 8) continue;
          seen.add(t);
          out.push(t);
        }
        return out.slice(0, 12);
      },
    });
    const merged = [];
    for (const x of rs || []) {
      for (const t of (x && x.result) || []) {
        if (!merged.includes(t)) merged.push(t);
      }
    }
    return merged;
  } catch (e) {
    return [];
  }
}

/**
 * 诊断用：每个 frame 的地址 + 按钮数量。
 *
 * 广告面板到底在主文档还是 iframe 里、iframe 有没有注入成功，
 * 光看「找不到按钮」是分不清的。有这张表就能一次定位。
 */
async function frameInfo(tabId) {
  try {
    const rs = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => ({
        url: String(location.href).slice(0, 120),
        buttons: document.querySelectorAll("button,[role=button],a").length,
        vis: `${document.visibilityState}/${document.hasFocus() ? "focus" : "blur"}`,
        text: String(document.body ? document.body.innerText : "").replace(/\s+/g, " ").slice(0, 60),
      }),
    });
    return (rs || []).map((x) => x && x.result).filter(Boolean);
  } catch (e) {
    return [{ url: `注入失败：${(e && e.message) || e}`, buttons: 0, text: "" }];
  }
}

/**
 * 诊断用：把页面上看得见的按钮文案全列出来（不筛「导出」）。
 *
 * 为什么需要：换个国家/换个广告版本，按钮可能叫「下载」「报表」「Export」
 * 甚至藏在下拉里。只报「没找到」等于没报，有这个清单就能一次定位。
 */
async function dumpButtons(tabId) {
  try {
    const rs = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const out = [];
        const seen = new Set();
        const nodes = document.querySelectorAll("button,[role=button],a,[class*=btn],[class*=Btn]");
        for (const el of nodes) {
          const t = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
          if (!t || t.length > 14 || seen.has(t)) continue;
          const b = el.getBoundingClientRect();
          if (b.width < 8 || b.height < 6) continue;
          seen.add(t);
          out.push(t);
          if (out.length >= 40) break;
        }
        return out;
      },
    });
    const merged = [];
    for (const x of rs || []) {
      for (const t of (x && x.result) || []) {
        if (!merged.includes(t)) merged.push(t);
      }
    }
    return merged;
  } catch (e) {
    return [];
  }
}

/** 通用导出：开页 → 按配置逐步点按钮 → 等下载 → 接住文件。
 *
 * 返回 { ok, name, path, bytes } 或 { error }。
 */
async function exportByButton(kind, host, log) {
  // 统一走「拦截式导出」：页面想下载时我们拦下来，浏览器不发起下载，
  // 所以**永远不会弹「另存为」**。
  //
  // 原来的实现是真的让浏览器下载、再用 chrome.downloads 去接，
  // 一旦浏览器设置了「下载前询问保存位置」，就会弹框卡住自动化
  // （广告、联盟、SKU 都会遇到）。这里直接改成拦截式，彻底断掉这条路。
  return exportViaApi(kind, host, log);
}

/** 商品SKU表：走通用导出（保留原函数名，调用处不用改）。 */
async function exportSkuByButton(host, preferPath, log) {
  return exportByButton("sku", host, log);
}

/** 导出并直接落库：不经过浏览器下载（避开「另存为」对话框）。
 *
 * 为什么要这样：
 *   点「导出数据」后，浏览器会弹「另存为」让用户选路径——
 *   自动化就断在这一步。但平台其实已经生成了文件，
 *   文件地址就在**导出接口的响应**里。
 *
 * 做法：挂调试器 → 点按钮 → 读导出接口的响应 → 拿到文件地址 →
 *      后台带 cookie 把文件取回来 → 直接回传落盘。
 */
/** 装一个「下载拦截器」：页面想下载文件时，把地址/内容拿下来，但不真的下载。
 *
 * 为什么必须要它：
 *   点「导出数据」后页面会用 <a download> 触发浏览器下载，
 *   而浏览器设置了「下载前询问保存位置」，就会弹「另存为」——
 *   自动化就卡在这个对话框上（用户必须手点保存）。
 *
 *   我们在页面主世界里把 a.click() 拦下来，既拿到文件地址，
 *   又不让浏览器发起下载，所以**根本不会弹框**。
 *   如果是 blob: 地址，就把 Blob 本身留在页面里，稍后由我们来取内容。
 */
async function installDownloadGuard(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: () => {
        if (window.__jlGuard) return;
        window.__jlGuard = true;
        window.__jlDl = [];
        window.__jlBlobs = {};

        const record = (href, name, blob) => {
          try {
            if (blob) {
              const key = "b" + Object.keys(window.__jlBlobs).length;
              window.__jlBlobs[key] = blob;
              window.__jlDl.push({ key, name: name || "", blob: true });
            } else if (href) {
              window.__jlDl.push({ href: String(href), name: name || "", blob: false });
            }
          } catch (e) { /* 忽略 */ }
        };

        // ① 拦 <a> 点击：凡是「像文件」的链接都拦下来。
        //    不能只看 download 属性——平台的下载链接经常是
        //    href 指向一个返回附件头（Content-Disposition）的接口，
        //    没有 download 属性，点了照样弹「另存为」。
        const origClick = HTMLAnchorElement.prototype.click;
        HTMLAnchorElement.prototype.click = function () {
          try {
            const href = this.href || "";
            const name = this.getAttribute("download") || "";
            const isFile = this.hasAttribute("download")
              || href.startsWith("blob:")
              || /\.(xlsx|xls|csv|zip)(\?|$)/i.test(href)
              || /\/(export|download|export_task|download_task)(\/|\?|$)/i.test(href);
            if (isFile) { record(href, name); return; }   // 不真的点，浏览器就不会下载
          } catch (e) { /* 忽略 */ }
          return origClick.apply(this, arguments);
        };

        // ①-b 拦 window.open（有些导出是开新窗口触发下载）
        const origOpen = window.open;
        window.open = function (u) {
          try {
            const href = String(u || "");
            if (/\.(xlsx|xls|csv|zip)(\?|$)/i.test(href)
              || /\/(export|download|export_task|download_task)(\/|\?|$)/i.test(href)) {
              record(href, "");
              return null;
            }
          } catch (e) { /* 忽略 */ }
          return origOpen.apply(this, arguments);
        };

        // ①-c 测试/诊断用：暴露一下当前记到了什么
        window.__jlDlPeek = () => (window.__jlDl || []).map((x) => x.href || ("blob:" + x.key));

        // ② 拦 URL.createObjectURL：把 Blob 留下来
        const origCreate = URL.createObjectURL;
        URL.createObjectURL = function (obj) {
          const u = origCreate.apply(this, arguments);
          try {
            if (obj && typeof obj.size === "number") {
              window.__jlBlobs[u] = obj;
            }
          } catch (e) { /* 忽略 */ }
          return u;
        };
      },
    });
    return true;
  } catch (e) {
    return false;
  }
}

/** 取回拦截器记下的文件（blob 或 http 地址），返回 {name, buf} 或 null。 */
async function harvestDownload(tabId) {
  let list = [];
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: () => (window.__jlDl || []).slice(-6),
    });
    list = (r && r.result) || [];
  } catch (e) { return null; }
  if (!list.length) return null;

  // 优先 blob（内容已经在页面里，最稳）
  for (const it of list.slice().reverse()) {
    if (!it.blob) continue;
    try {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId }, world: "MAIN",
        func: async (key) => {
          const b = (window.__jlBlobs || {})[key];
          if (!b) return null;
          const buf = await b.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let bin = "";
          const CH = 0x8000;
          for (let i = 0; i < bytes.length; i += CH) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
          }
          return { b64: btoa(bin), size: bytes.length };
        },
        args: [it.key],
      });
      const got = r && r.result;
      if (got && got.size > 1024) {
        const bin = atob(got.b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return { name: it.name || "", buf: bytes.buffer };
      }
    } catch (e) { /* 换下一个 */ }
  }

  // 再试 http 地址
  for (const it of list.slice().reverse()) {
    if (it.blob || !it.href || !/^https?:/i.test(it.href)) continue;
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        const resp = await fetch(it.href, { credentials: "include" });
        if (resp.ok) {
          const ct = resp.headers.get("content-type") || "";
          if (!ct.includes("json")) {
            const buf = await resp.arrayBuffer();
            if (buf && buf.byteLength > 1024) {
              return { name: it.name || "", buf };
            }
          }
        }
      } catch (e) { /* 还在生成 */ }
      await sleep(4000);
    }
  }
  return null;
}

async function exportViaApi(kind, host, log, opts = {}) {
  const cfg = EXPORT_FLOWS[kind];
  if (!cfg) return { error: `没有配置「${kind}」的导出流程` };
  const realHost = cfg.host || host;

  // 页面默认按「今天」导出，必须把区间带进地址，否则导出来的是当天数据。
  // 商品分析页认 timeRange=开始|结束（闭区间）。
  let pagePath = cfg.page;
  // 广告后台要带站点参数：不带的时候菲律宾会被路由踢回首页
  // （地址栏看着还是 ads-creation/dashboard，页面其实是卖家后台首页）。
  if (kind === "ads") {
    let region = "MY";
    try {
      const prof = await siteProfile(realHost);
      region = String(prof.region || "MY").toUpperCase();
    } catch (e) { /* 忽略 */ }
    // 这三个参数缺一不可（菲律宾实测）：
    //   shop_region=站点  type=product（商品广告）  mpa=1（新版广告后台入口）
    // 少了 type/mpa 会停在卖家后台首页，地址栏却还是 ads-creation/dashboard。
    pagePath += `?shop_region=${region}&type=product&mpa=1`;
    if (log) log(`   广告页带站点参数：${region}`);
  }
  if (kind === "sku" && opts.range && opts.range.start) {
    let region = "MY";
    try {
      const st = (await skGet({ settings: {} })).settings || {};
      const map = { "马来西亚": "MY", "菲律宾": "PH", "泰国": "TH", "印尼": "ID" };
      if (map[st.country]) region = map[st.country];
    } catch (e) { /* 忽略 */ }
    pagePath += `?lng=zh-CN&shop_region=${region}`
      + `&timeRange=${opts.range.start}%7C${opts.range.end}`;
    if (log) log(`   导出区间：${opts.range.start} ~ ${opts.range.end}`);
  }

  let tabId = 0;
  let attached = false;
  let restoreWinId = 0;
  let openedByUs = false;   // 复用用户的标签时不能关掉它
  const fileUrls = [];      // 候补文件地址
  const reqIds = new Map();
  const FILE_RE = /(https?:)?\/\/[^"'\\s]+\.(xlsx|xls|csv)(\?[^"'\\s]*)?|\/api\/[^"'\\s]*\/(export|download)[^"'\\s]*/gi;

  const onEvent = async (source, method, params) => {
    if (source.tabId !== tabId) return;
    try {
      if (method === "Network.requestWillBeSent") {
        const u = String((params.request || {}).url || "");
        if (/export|download_task|export_task/i.test(u)) {
          reqIds.set(params.requestId, { url: u });
        }
        return;
      }
      if (method === "Network.loadingFinished") {
        if (!reqIds.has(params.requestId)) return;
        let body = "";
        try {
          const b = await chrome.debugger.sendCommand(
            { tabId }, "Network.getResponseBody", { requestId: params.requestId });
          body = (b && b.body) || "";
          if (b && b.base64Encoded) body = atob(body);
        } catch (e) { return; }
        if (!body) return;
        // 从响应里挖出 .xlsx/.csv 地址
        const m = body.match(FILE_RE);
        if (m) {
          for (const one of m) {
            const u2 = one.startsWith("http") ? one : `https://${realHost}${one}`;
            if (!fileUrls.includes(u2)) fileUrls.push(u2);
          }
          if (log) log(`   响应里找到 ${m.length} 个候选文件地址`);
        }
        // 顺便记一下业务码，失败时能看出原因
        try {
          const j = JSON.parse(body);
          if (j && j.code !== undefined && Number(j.code) !== 0 && log) {
            log(`   导出接口回 code=${j.code} ${j.message || ""}`);
          }
        } catch (e) { /* 不是 JSON 就算了 */ }
      }
    } catch (e) { /* 忽略 */ }
  };

  chrome.debugger.onEvent.addListener(onEvent);
  SUPPRESS_DL = true;      // 这个流程里不需要浏览器下载
  DL_SEEN = [];            // 清空上一轮记下的地址
  // 广告后台（GMV Max）在**后台标签**里不初始化：页面停在卖家后台首页，
  // 一个导出按钮都没有；同一个地址在前台标签打开就正常。所以广告这步必须开前台，
  // 导完再把用户原来那个标签切回去。
  const wantFront = kind === "ads";
  let prevTabId = 0;
  if (wantFront) {
    try {
      const [cur] = await chrome.tabs.query({ active: true, currentWindow: true });
      prevTabId = (cur && cur.id) || 0;
    } catch (e) { /* 忽略 */ }
  }
  try {
    // 广告页优先「复用你已经打开的那个」。
    // 原因：后台新建的标签会被浏览器判成 hidden，重 SPA 直接不渲染
    // （日志实测 vis=hidden/blur，页面上只剩侧边栏）；而你自己点开的那个是可见的。
    if (wantFront) {
      try {
        const cands = await chrome.tabs.query({ url: [`*://${realHost}/ads-creation/*`] });
        const pick = (cands || []).find((t) => t.active) || (cands || [])[0];
        if (pick && pick.id) {
          tabId = pick.id;
          openedByUs = false;
          try { await chrome.tabs.update(tabId, { active: true }); } catch (e) { /* 忽略 */ }
          await focusWindow(pick.windowId);
          restoreWinId = pick.windowId;
          if (log) log("   复用你已打开的广告页");
        }
      } catch (e) { /* 忽略 */ }
    }
    if (!tabId) {
      const nt = await chrome.tabs.create({
        url: `https://${realHost}/${pagePath}`, active: wantFront,
      });
      tabId = nt.id;
      openedByUs = true;
      // 光 active:true 不够：如果标签落在最小化/无焦点的窗口里，
      // Chrome 依然把页面当不可见，重 SPA 就不渲染。窗口要恢复 + 置前。
      if (wantFront && nt && nt.windowId) {
        await focusWindow(nt.windowId);
        restoreWinId = nt.windowId;
      }
    }
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
      await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
      attached = true;
    } catch (e) {
      if (log) log(`   调试器挂不上：${(e && e.message) || e}`);
    }
    if (log) log(`   开「${cfg.label}」页面准备导出…`);

    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      try {
        const t = await chrome.tabs.get(tabId);
        if (!t || t.status === "complete") break;
      } catch (e) { break; }
    }
    await sleep(3000);
    // 装拦截器：页面要下载时我们拦住，浏览器就不会弹「另存为」
    await installDownloadGuard(tabId);

    // 广告后台的入口各站点不统一：菲律宾的 /ads-creation/dashboard 会直接跳回首页，
    // 页面上一个导出按钮都没有。所以不猜路径——从卖家后台侧边栏点「店铺广告」进去，
    // 落到哪个页面就在哪个页面找导出。这一招对没开通广告的店也有效：
    // 点进去还是首页，那就说明这家店确实没有广告后台，如实报出来。
    if (kind === "ads") {
      // 只在「当前不是广告页」时才点侧边栏。
      // 复用了用户已经打开的广告页还去点一次「店铺广告」，会把面板重新导回空壳——
      // 之前一直"打开就变空"就是这个自己踩的坑。
      let onAds = false;
      try {
        const t0 = await chrome.tabs.get(tabId);
        onAds = /ads-creation|ads-manager|\/ads\b/i.test(String((t0 && t0.url) || ""));
      } catch (e) { /* 忽略 */ }
      if (onAds) {
        if (log) log("   已在广告页，不再重复点侧边栏");
      } else {
        const nav = await clickByTextIn(tabId, ["店铺广告", "广告", "Ads", "Marketing"]);
        if (log) log(`   从侧边栏进广告后台：${nav || "没点到「店铺广告」"}`);
      }
      await sleep(5000);
      try {
        const t = await chrome.tabs.get(tabId);
        if (log) log(`   当前页：${String((t && t.url) || "").split("?")[0]}`);
      } catch (e) { /* 忽略 */ }
      await installDownloadGuard(tabId);
    }

    for (let si = 0; si < cfg.steps.length; si++) {
      const texts = cfg.steps[si];
      // 第一步先等按钮渲染出来（广告面板要好几秒），后面几步是弹窗，直接点
      if (si === 0) await waitForButton(tabId, texts, 40000, log);
      let clicked = false;
      for (let attempt = 0; attempt < 3; attempt++) {
        clicked = await clickByTextIn(tabId, texts);
        if (clicked) break;
        await sleep(1500);
      }
      if (log) log(`   第 ${si + 1} 步点「${texts[0]}」：${clicked}`);
      if (!clicked && si === 0) {
        const names = await scanExportButtons(tabId);
        // 页面上连「导出」两个字都没有时，光说"没找到"没法查。
        // 把当前地址 + 页面上所有可见按钮文案打出来，一眼就知道是
        // 走错页 / 没登录 / 还是这个站点的按钮叫别的名字。
        let where = "";
        try {
          const t = await chrome.tabs.get(tabId);
          where = String((t && t.url) || "");
        } catch (e) { /* 忽略 */ }
        const all = await dumpButtons(tabId);
        const frames = await frameInfo(tabId);
        if (log) {
          for (const f of frames) {
            log(`   [frame] ${f.url}｜按钮 ${f.buttons}｜${f.vis || ""}｜${f.text}`);
          }
        }
        return {
          error: (names.length
            ? `没找到「${texts[0]}」，页面上含「导出」的按钮有：${names.join(" / ")}`
            : "页面上没找到「导出」类按钮")
            + `｜当前页：${where.split("?")[0]}`
            + `｜可见按钮：${all.length ? all.slice(0, 20).join(" / ") : "（一个都没扫到）"}`,
        };
      }
      await sleep(2500);
    }

    // 诊断：拦截器到底记到了什么（记不到时这一行能定位问题）
    try {
      const [pr] = await chrome.scripting.executeScript({
        target: { tabId }, world: "MAIN",
        func: () => (window.__jlDlPeek ? window.__jlDlPeek() : []),
      });
      const seen = (pr && pr.result) || [];
      if (log) log(`   拦截器记录 ${seen.length} 条${seen.length ? "：" + seen.slice(0, 2).join(" | ").slice(0, 160) : ""}`);
    } catch (e) { /* 忽略 */ }

    // 先看拦截器有没有截到文件（这条路不弹框、最快）
    for (let i = 0; i < 6; i++) {
      const h = await harvestDownload(tabId);
      if (h && h.buf && h.buf.byteLength > 1024) {
        const nm = (kind === "sku")
          ? `sku_export_商品数据分析_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.xlsx`
          : (h.name || `${kind}_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.xlsx`);
        const saved = await uploadToBridge(nm, h.buf, cfg.dir || "");
        if (log) log(`   已拦截到导出文件：${saved.path}（${Math.round(h.buf.byteLength / 1024)} KB）`);
        return { ok: true, name: nm, path: saved.path, bytes: h.buf.byteLength };
      }
      await sleep(3000);
    }

    // 再试「浏览器真发起了下载」时记下的地址：
    // 我们把它取消掉了，不会弹「另存为」，但地址已经拿到，自己取回来即可。
    if (DL_SEEN.length) {
      if (log) log(`   捕获到 ${DL_SEEN.length} 个下载地址，自己取回来…`);
      for (const d of DL_SEEN.slice().reverse()) {
        if (!/^https?:/i.test(d.url || "")) continue;
        for (let attempt = 0; attempt < 10; attempt++) {
          try {
            const resp = await fetch(d.url, { credentials: "include" });
            if (resp.ok) {
              const ct = resp.headers.get("content-type") || "";
              if (!ct.includes("json")) {
                const buf = await resp.arrayBuffer();
                if (buf && buf.byteLength > 1024) {
                  const nm = (d.name || "").split(/[\\/]/).pop()
                    || `${kind}_${Date.now()}.xlsx`;
                  const saved = await uploadToBridge(nm, buf, cfg.dir || "");
                  if (log) log(`   已取回导出文件：${saved.path}（${Math.round(buf.byteLength / 1024)} KB）`);
                  return { ok: true, name: nm, path: saved.path, bytes: buf.byteLength };
                }
              }
            }
          } catch (e) { /* 还没生成好 */ }
          await sleep(4000);
        }
      }
      if (log) log("   下载地址拿到了但取不到内容");
    }

    // 拦截器没截到 → 退回「从接口响应里找文件地址」
    if (log) log("   拦截器没截到文件，改从接口响应里找地址…");
    for (let i = 0; i < 30 && !fileUrls.length; i++) await sleep(2000);
    if (!fileUrls.length) {
      return { error: "没在导出接口的响应里找到文件地址" };
    }
    if (log) log(`   拿到文件地址，开始下载…`);

    // 逐个试：取第一个能下到有效内容的
    for (const u of fileUrls.slice(0, 6)) {
      for (let attempt = 0; attempt < 12; attempt++) {
        try {
          const r = await fetch(u, { credentials: "include" });
          if (r.ok) {
            const ct = r.headers.get("content-type") || "";
            if (!ct.includes("json")) {
              const buf = await r.arrayBuffer();
              if (buf && buf.byteLength > 1024) {
                // 文件名要带 sku_export 前缀：本地合并逻辑靠它识别
                // （以前叫 sku_20261030.xlsx，名字不含关键词，一直没被合并进去）
                const base = kind === "sku"
                  ? `sku_export_商品数据分析_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.xlsx`
                  : decodeURIComponent(
                      (u.split("?")[0].split("/").pop() || `${kind}.xlsx`));
                const saved = await uploadToBridge(name, buf, cfg.dir || "");
                return { ok: true, name, path: saved.path, bytes: buf.byteLength };
              }
            }
          }
        } catch (e) { /* 还没生成好，等一会儿再试 */ }
        await sleep(4000);
      }
    }
    return { error: "文件地址拿到了但下不到内容（平台可能还在生成）" };
  } finally {
    SUPPRESS_DL = false;
    try { chrome.debugger.onEvent.removeListener(onEvent); } catch (e) { /* 忽略 */ }
    if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }
    if (tabId && openedByUs) { try { await chrome.tabs.remove(tabId); } catch (e) { /* 忽略 */ } }
    // 把用户原来在看的标签切回来，别把人家扔在一个空页面上
    if (prevTabId) {
      try { await chrome.tabs.update(prevTabId, { active: true }); } catch (e) { /* 忽略 */ }
    }
    void restoreWinId;   // 窗口不主动切回（切来切去更扰人），只记录
  }
}

/** 点「下一页」：文案/aria-label/箭头类控件都试一遍。 */
async function clickNextPage(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: () => {
        const norm = (x) => (x || "").replace(/\s+/g, "").trim();
        const vis = (e) => e.offsetParent !== null;
        const cands = Array.from(document.querySelectorAll(
          "button,[role=button],a,li,span,div,svg"));

        // ① 明确文案 / aria-label
        for (const e of cands) {
          if (!vis(e)) continue;
          const t = norm(e.textContent);
          const al = norm(e.getAttribute && e.getAttribute("aria-label"));
          if (t === "下一页" || al === "下一页" || t === "Next" || al === "Next") {
            e.click();
            return "text";
          }
        }
        // ② 分页控件里的「>」「›」箭头
        for (const e of cands) {
          if (!vis(e)) continue;
          const t = norm(e.textContent);
          if (t !== ">" && t !== "›" && t !== "»") continue;
          const cls = String(e.className || "") + " " +
            String((e.parentElement && e.parentElement.className) || "");
          if (!/page|pagination|pager|next/i.test(cls)) continue;
          e.click();
          return "arrow";
        }
        // ③ 整页找 class 带 next 的可点元素
        for (const e of cands) {
          if (!vis(e)) continue;
          const cls = String(e.className || "");
          if (!/next/i.test(cls)) continue;
          if (/disabled/i.test(cls)) continue;
          e.click();
          return "class";
        }
        return "";
      },
    });
    return !!(r && r.result);
  } catch (e) {
    return false;
  }
}

async function deepCaptureSku(host, preferPath, log) {
  // 个别定制浏览器可能整个裁掉调试接口，这里先挡一下，
  // 免得抛 TypeError 把整轮取数带崩。
  if (!chrome.debugger || !chrome.debugger.attach) {
    throw new Error("这个浏览器没有开放调试接口（chrome.debugger 不可用）");
  }
  const SKU_PATH = "/api/v2/insights/seller/ttp/product/sku/list";
  const kw = preferPath ? preferPath.split("?")[0] : "";

  let tabId = 0;
  let ownTab = false;
  const reqIds = new Map();     // requestId → {url, postData, headers}
  const hits = [];              // 所有拿到的响应（可能有多条，最后挑最好的）
  let wake = null;
  const bump = () => { if (wake) { const w = wake; wake = null; w(); } };

  const onEvent = async (source, method, params) => {
    if (source.tabId !== tabId) return;
    try {
      if (method === "Network.requestWillBeSent") {
        const req = params.request || {};
        if (!String(req.url || "").includes(SKU_PATH)) return;
        reqIds.set(params.requestId, {
          url: req.url,
          postData: req.postData || "",
          headers: req.headers || {},
        });
        return;
      }
      // 这条事件里有浏览器实际发出的全部请求头（含自定义头、Cookie 之外的签名头）
      if (method === "Network.requestWillBeSentExtraInfo") {
        const info = reqIds.get(params.requestId);
        if (!info) return;
        const h = params.headers || {};
        info.extraHeaders = h;
        return;
      }
      if (method === "Network.responseReceived") {
        const info = reqIds.get(params.requestId);
        if (!info) return;
        const resp = params.response || {};
        info.status = resp.status;
        info.mimeType = resp.mimeType || "";
        if (resp.status && resp.status >= 400) return;
        // 响应体这时通常还取不到，先记下，等 loadingFinished 再取
        return;
      }
      if (method === "Network.loadingFinished") {
        const info = reqIds.get(params.requestId);
        if (!info || info.bodyText) return;
        try {
          const body = await chrome.debugger.sendCommand(
            { tabId }, "Network.getResponseBody", { requestId: params.requestId });
          info.bodyText = (body && body.body) || "";
          info.base64 = !!(body && body.base64Encoded);
        } catch (e) { /* 拿不到就算了 */ }
        if (info.bodyText) { hits.push(info); bump(); }
      }
    } catch (e) { /* 单个事件出错不能影响后面的 */ }
  };

  const waitFor = (ms) => new Promise((resolve) => {
    const t = setTimeout(() => { wake = null; resolve(false); }, ms);
    wake = () => { clearTimeout(t); resolve(true); };
  });

  const attachTo = async (tid) => {
    await chrome.debugger.attach({ tabId: tid }, "1.3");
    await chrome.debugger.sendCommand({ tabId: tid }, "Network.enable", {});
  };

  chrome.debugger.onEvent.addListener(onEvent);
  let attached = false;

  // 先开新页（后台）再挂调试器：反过来的话，新页加载时发的请求就漏掉了。
  // 用新页而不是刷新用户的页面——刷新会把他正在做的筛选、翻页全冲掉。
  try {
    const url = `https://${host}/${kw || "compass/product-analysis"}`;
    const nt = await chrome.tabs.create({ url, active: false });
    ownTab = true;
    tabId = nt.id;
    await attachTo(tabId);
    attached = true;
    if (log) log("   已后台新开商品分析页并挂上调试器…");
  } catch (e) {
    if (log) log(`   后台新开+挂载失败：${(e && e.message) || e}`);
  }

  // 退路：用现有标签页
  if (!attached) {
    try {
      const tabs = await chrome.tabs.query({ url: [`*://${host}/*`] });
      const usable = (t) => t && t.id && !/^chrome-error:|^about:/.test(String(t.url || ""));
      let t0 = (tabs || []).find((t) => usable(t) && kw && String(t.url || "").includes(kw))
        || (tabs || []).find((t) => usable(t) && t.active)
        || (tabs || []).find(usable);
      if (!t0) throw new Error(`没有找到 ${host} 的标签页`);
      tabId = t0.id;
      await attachTo(tabId);
      attached = true;
      if (log) log("   已挂到现有标签页的调试器上");
    } catch (e) {
      chrome.debugger.onEvent.removeListener(onEvent);
      // 这条信息要足够具体：定制版浏览器可能禁用了调试接口
      throw new Error(
        `挂不上调试器：${(e && e.message) || e}。`
        + "如果一直失败，说明这个浏览器禁用了开发者调试接口（部分定制版会这样），"
        + "换 Chrome / Edge 跑扩展即可",
      );
    }
  }

  // 让页面自己发一次 sku/list：
  // 新开的页默认停在「按商品」维度，点「按 SKU」它才会发这条请求。
  const clickSkuTab = async () => {
    try {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId }, world: "MAIN",
        func: () => {
          const norm = (x) => (x || "").replace(/\s+/g, "").trim();
          const cands = Array.from(
            document.querySelectorAll("span,div,button,a,li,label,td"));
          const hit = cands.find((e) => {
            const t = norm(e.textContent);
            if (t !== "按SKU") return false;
            if (e.offsetParent === null) return false;
            const kids = e.querySelectorAll("span,div,a,button,li,label,td");
            return ![...kids].some((k) => norm(k.textContent) === "按SKU");
          });
          if (!hit) return false;
          hit.click();
          return true;
        },
      });
      return !!(r && r.result);
    } catch (e) {
      return false;
    }
  };

  // 等新页加载完
  for (let i = 0; i < 25; i++) {
    await sleep(1000);
    try {
      const t = await chrome.tabs.get(tabId);
      if (!t || t.status === "complete") break;
    } catch (e) { break; }
  }

  const clicked = await clickSkuTab();
  if (log) log(`   已点「按 SKU」维度：${clicked}`);

  // 等第一批响应（点完按钮后给 20 秒）
  await waitFor(20000);

  // 还没拿到 → 刷新一次页面：页面已停在「按 SKU」时点维度不会触发新请求
  if (!hits.length && attached) {
    try {
      await chrome.tabs.reload(tabId);
      if (log) log("   还没等到请求，刷新页面再试…");
      await sleep(3000);
      for (let i = 0; i < 15; i++) {
        await sleep(1000);
        try {
          const t = await chrome.tabs.get(tabId);
          if (!t || t.status === "complete") break;
        } catch (e) { break; }
      }
      await clickSkuTab();
      await waitFor(20000);
    } catch (e) {
      if (log) log(`   刷新失败：${(e && e.message) || e}`);
    }
  }

  // 翻页：这一页只有 10 条，后台实际可能上百个 SKU。
  // 点「下一页」让页面自己继续请求，我们照样把响应收下来。
  if (hits.length) {
    let guard = 0;
    while (guard < 40) {
      guard += 1;
      const before = hits.length;
      const ok = await clickNextPage(tabId);
      if (!ok) break;
      // 等新响应（最多 10 秒）
      let waited = 0;
      while (hits.length === before && waited < 10) {
        await sleep(1000);
        waited += 1;
      }
      if (hits.length === before) break;      // 没有新数据了，停
      if (log) log(`   翻页 +1（已收 ${hits.length} 条响应）`);
    }
    if (log) log(`   翻页结束，共 ${hits.length} 条响应`);
  }

  try { chrome.debugger.onEvent.removeListener(onEvent); } catch (e) { /* 忽略 */ }
  if (attached) {
    try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ }
  }
  if (ownTab) {
    try { await chrome.tabs.remove(tabId); } catch (e) { /* 忽略 */ }
  }

  if (!hits.length) {
    // 用抓到的请求信息给出可诊断的结论
    const anyReq = [...reqIds.values()][0];
    return {
      error: "没拿到 sku/list 响应",
      sawRequest: !!anyReq,
      lastUrl: anyReq && anyReq.url,
      lastPost: anyReq && anyReq.postData,
    };
  }

  // 页面可能发过多次（按商品、按SKU、翻页）——挑「能解析出最多 SKU」的那条
  let best = null;
  let bestRows = [];
  for (const h of hits) {
    let obj = null;
    try {
      if (h.base64) {
        const bin = atob(h.bodyText);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        obj = JSON.parse(new TextDecoder().decode(bytes));
      } else {
        obj = JSON.parse(h.bodyText);
      }
    } catch (e) { continue; }
    if (obj && obj.code !== undefined && Number(obj.code) !== 0) continue;
    const rows = extractSkuList(obj);
    if (rows.length > bestRows.length) { best = h; bestRows = rows; }
  }
  // 多页合并：把所有响应的行按 sku_id 去重后拼起来（翻页拿到的才算全量）
  if (hits.length > 1) {
    const seen = new Set();
    const merged = [];
    for (const h of hits) {
      let obj = null;
      try {
        obj = h.base64
          ? JSON.parse(new TextDecoder().decode(
              Uint8Array.from(atob(h.bodyText), (c) => c.charCodeAt(0))))
          : JSON.parse(h.bodyText);
      } catch (e) { continue; }
      if (obj && obj.code !== undefined && Number(obj.code) !== 0) continue;
      for (const row of extractSkuList(obj)) {
        if (!row.sku_id || seen.has(row.sku_id)) continue;
        seen.add(row.sku_id);
        merged.push(row);
      }
    }
    if (merged.length > bestRows.length) bestRows = merged;
  }

  if (!best) {
    // 保留一份原始响应，便于下次照着改解析
    const raw = hits[0];
    return {
      error: "拿到了响应但解析不出 SKU（结构未知）",
      rawSample: raw && raw.bodyText ? raw.bodyText.slice(0, 3000) : "",
    };
  }
  return {
    rows: bestRows,
    status: best.status,
    url: best.url,
    postData: best.postData,
    candidates: hits.length,
  };
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

  // 抓「导出流程」时，只有请求体不够——任务 ID、文件地址都在**响应**里。
  // 所以这里把响应用 Network.getResponseBody 读出来，一起落盘。
  const flow = [];                 // 完整流程（请求 + 响应），给开发侧分析用
  const flowOf = new Map();        // requestId → flow 条目
  const KEEP = /export|download|task|insight|stat|order\/list|sku\/list|transaction/i;

  const onEvent = async (source, method, params) => {
    if (!ids.has(source.tabId)) return;
    try {
      if (method === "Network.requestWillBeSent") {
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
        if (KEEP.test(url)) {
          const item = {
            at: new Date().toISOString(),
            url,
            method: req.method,
            postData: (req.postData || "").slice(0, 4000),
            headers: req.headers || {},
          };
          flowOf.set(params.requestId, item);
          flow.push(item);
        }
        return;
      }
      if (method === "Network.loadingFinished") {
        const item = flowOf.get(params.requestId);
        if (!item || item.respBody) return;
        try {
          const b = await chrome.debugger.sendCommand(
            { tabId: source.tabId }, "Network.getResponseBody",
            { requestId: params.requestId });
          item.respBody = ((b && b.body) || "").slice(0, 20000);
          item.base64 = !!(b && b.base64Encoded);
        } catch (e) { /* 有些响应取不到，忽略 */ }
      }
    } catch (e) { /* 单个事件出错不影响其它 */ }
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
    // 把「请求+响应」完整流程落一份，导出自动化就是照它写的
    try {
      if (flow.length) {
        const text = JSON.stringify({ at: new Date().toISOString(), flow }, null, 2);
        const bytes = new TextEncoder().encode(text);
        const saved = await uploadToBridge(
          `导出流程_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.json`,
          bytes.buffer, "capture");
        log(`导出流程已保存：${saved.path}（${flow.length} 条请求，含响应）`);
      }
    } catch (e) {
      log(`导出流程保存失败：${(e && e.message) || e}`);
    }
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
  // 店铺决定国家：传了店铺，桥那边会以 config/stores.json 为准解析国家与费率
  const store = opts.store || settings.store || "";
  const from = range.startDate;
  const to = range.to || (range.endDateExclusive ? shiftDate(range.endDateExclusive, -1) : "");

  log(`开始核算：${country} ${from} ~ ${to}`);
  let resp;
  try {
    resp = await fetch(`${BRIDGE}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ country, from, to, store,
        // 带上 shop_id：数据按店铺分目录存放，核算时才知道该读哪一家。
        // 只传 store（配置里的别名）有时候对不上真实目录。
        shop_id: CURRENT_SHOP.shop_id || "",
        // auto：计划任务触发的，桥会按那条计划的设置决定下载目录
        auto: !!opts.auto,
        // 计划任务里勾了「带公式 / 不带公式」就按它来；没给就不传，
        // 桥会退回设置页里的开关
        with_formula: (typeof opts.with_formula === "boolean" ? opts.with_formula : undefined) }),
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
// ---------------- 进度上报 ----------------
// 跑一步报一步，界面上的店铺卡片靠它显示「卡在哪」。
// 以前大店取数要 8 分钟，界面只有一句「取数中」，分不清是在动还是死了。

let RUN_ID = "";

function newRunId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// 取数任务名 → 界面上的步骤名（两个地方叫法不一样，这里统一）
const STEP_OF_TASK = {
  sku: "sku", link: "sku", orders: "orders",
  ads: "ads", manualAds: "ads", affiliate: "affiliate",
};

async function reportProgress(step, state, extra = {}) {
  const sid = (CURRENT_SHOP && CURRENT_SHOP.shop_id) || "";
  if (!sid) return;              // 还没认出是哪家店，报了也对不上号
  try {
    await fetch(`${BRIDGE}/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        shop_id: sid, task: "all", run_id: RUN_ID, step, state, ...extra,
      }),
    });
  } catch (e) { /* 桥没开就不报，不能因为上报失败把取数搞挂 */ }
}

function shortDetail(r) {
  if (!r || typeof r !== "object") return "";
  const p = r.saved || r.path || "";
  if (p) return String(p).split(/[\\/]/).pop().slice(0, 40);
  if (r.rows) return `${r.rows} 行`;
  if (r.count) return `${r.count} 条`;
  return "";
}

// 心跳：告诉桥「我是哪家店、还开着、在不在跑」。
// 没有它，界面只能说「上次上传是 10/31」，点运行后一直排队，人以为卡死了。
async function beatBridge(running) {
  const sid = (CURRENT_SHOP && CURRENT_SHOP.shop_id) || "";
  if (!sid) return;
  let ver = "";
  try { ver = chrome.runtime.getManifest().version; } catch (e) { /* 忽略 */ }
  try {
    await fetch(`${BRIDGE}/hello`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        shop_id: sid,
        shop_name: CURRENT_SHOP.shop_name || "",
        region: CURRENT_SHOP.region || "",
        ver,
        running: running || null,
      }),
    });
  } catch (e) { /* 桥没开，忽略 */ }
}

async function executeTask(task, range, log, opts = {}) {
  RUN_ID = newRunId();
  try {
    startKeepAlive();
    CURRENT_RANGE = range || null;
    await skSet({ running: { task, at: Date.now() } });
    log(`开始（${range?.startDate} ~ ${range?.to || range?.endDateExclusive}）`);
    beatBridge({ task, at: Date.now() });
    const withCalc = opts.withCalc !== false && task !== "ads";
    // all 的 4 步由 runTask 内部报；单跑某一个任务就在这儿包一层
    const isAll = task === "all";
    const stepName = STEP_OF_TASK[task] || "";
    const t0 = Date.now();
    if (!isAll && stepName) await reportProgress(stepName, "running");
    const result = withCalc
      ? await runTaskThenCalc(task, range || {}, log, opts)
      : await runTask(task, range || {}, log, opts);
    if (!isAll && stepName) {
      await reportProgress(stepName, "done", {
        elapsed: (Date.now() - t0) / 1000,
        detail: shortDetail(result && (result.fetch || result)),
      });
    }
    await skSet({ lastResult: { task, ok: true, at: Date.now(), result } });
    log("完成");
    await reportProgress("__finish__", "done");
    return { ok: true, result };
  } catch (e) {
    const err = String((e && (e.message || e)) || e);
    log(`失败：${err}`);
    await skSet({ lastResult: { task, ok: false, at: Date.now(), error: err } });
    await reportProgress("__finish__", "fail", { error: err });
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

const PENDING_KEY = "pendingQueue";

async function consumePending() {
  // 两条来源：面板写的本地存储，以及面板通过桥中转的任务。
  // 前者依赖扩展自己的 storage，后者依赖普通 HTTP，互为备份。
  //
  // 队列是数组而不是单槽位，因为自动取数和手动取数会同时存在：
  // 比如自动任务正在跑（要几分钟），你又点了手动取另一个区间。
  // 单槽位下后一个会把前一个冲掉，表现是「自动任务今天没跑」，
  // 而且不报错，极难发现。改成 FIFO 后两个都会依次执行。
  const { running } = await skGet({ running: null });
  if (running) {
    // 上次跑到一半断电/关浏览器，running 标记会一直留在 storage 里。
    // 以前只要它有值就 return，结果**重启之后扩展再也不领任务**，
    // 界面上看着「在线」，就是不动——这种状态最难查。
    // 超过 40 分钟一律当残留清掉（单店最长也就十几分钟）。
    const age = Date.now() - Number(running.at || 0);
    if (age < 40 * 60 * 1000) return null;   // 真在跑，等它跑完再领下一个
    await skSet({ running: null });
    appendTaskLog("上次任务没跑完（断电或中途关了浏览器），已清掉运行标记，继续领任务");
  }

  let p = null;
  try {
    // 带上自己的店铺身份：桥按店投递任务。
    // 不带的话就是「谁先轮询谁拿走」——7 个浏览器同时开着会互相抢，跑出来串店。
    const sid = (CURRENT_SHOP && CURRENT_SHOP.shop_id) || "";
    const resp = await fetch(`${BRIDGE}/pending?shop=${encodeURIComponent(sid)}`);
    const j = await resp.json();
    if (j && j.job && j.job.task) {
      p = { ...j.job, at: j.job.at || Date.now() };
      appendTaskLog(`[${p.task}] 从本地桥领取任务`);
    }
  } catch (e) { /* 桥没开，忽略 */ }

  if (!p) {
    const { pendingQueue } = await skGet({ pendingQueue: [] });
    const fresh = (pendingQueue || []).filter(
      (x) => x && x.task && Date.now() - (x.at || 0) <= 30 * 60 * 1000);
    p = fresh[0] || null;
    await skSet({ pendingQueue: fresh.slice(1) });
  }

  if (!p || !p.task) return null;

  const log = (m) => appendTaskLog(`[${p.task}] ${m}`);
  log("从待办队列领取任务");
  return executeTask(p.task, p.range || {}, log, {
    force: !!p.force, withCalc: p.withCalc, store: p.store, country: p.country,
    // auto：这是不是「计划任务」派下来的任务。
    // 桥那边靠它决定报表下载到哪个目录。
    auto: !!p.auto,
    // 计划任务可以单独指定带不带公式；没指定就交给桥按设置页的开关走。
    with_formula: p.with_formula,
  });
}

// ---------------- 任务调度 ----------------
// 这个调度器在一次重构里被误删，只留下调用（runTaskThenCalc / executeTask），
// 后果是所有取数任务在第一步就抛 ReferenceError: runTask is not defined。
// 现在补回来：按任务名分派、一键全跑、失败时自愈重试一次。

/**
 * 把某个窗口放到前台，但**不许改它的大小**。
 *
 * 之前直接写 {state:"normal", focused:true}，结果把用户最大化的浏览器
 * 还原成上次的小窗了——用户看到的就是"点一下广告，浏览器缩小了"。
 * 正确做法：只有最小化时才恢复，最大化的保持最大化，其余只置前。
 */
async function focusWindow(winId) {
  if (!winId) return;
  try {
    const w = await chrome.windows.get(winId);
    if (w && w.state === "minimized") {
      await chrome.windows.update(winId, { state: "normal" });
    }
  } catch (e) { /* 忽略 */ }
  try { await chrome.windows.update(winId, { focused: true }); } catch (e) { /* 忽略 */ }
}

/**
 * 手动模式：装好拦截器，然后由用户自己在页面上点「导出」，我们只负责接住文件。
 *
 * 为什么留这条路：广告后台的页面状态太娇气，由程序去点按钮，
 * 有时会点到下拉、有时弹层里还有一层确认；而人手点一下是 100% 能点对的。
 * 程序该做的是「别添乱 + 把文件收好」，不是非要自己点。
 */
async function manualAdsCapture(log) {
  let tab = null;
  try {
    const wins = await chrome.windows.getAll({ populate: true });
    const order = wins.slice().sort((a, b) => (b.focused ? 1 : 0) - (a.focused ? 1 : 0));
    for (const w of order) {
      const t = (w.tabs || []).find((x) => /ads-creation\/dashboard/.test(String(x.url || "")));
      if (t) { tab = { id: t.id, windowId: w.id }; break; }
    }
  } catch (e) { /* 忽略 */ }
  if (!tab) throw new Error("没有打开着的广告页（先打开菲律宾广告面板再点这个）");

  const tabId = tab.id;
  try { await chrome.tabs.update(tabId, { active: true }); } catch (e) { /* 忽略 */ }
  await focusWindow(tab.windowId);
  await sleep(800);

  await installDownloadGuard(tabId);
  DL_SEEN = [];

  // 同时盯网络：把「点导出」那一刻发出的每个请求记下来（方法+路径+请求体）。
  // 目的不是这次拿文件，而是认清「创建导出任务」到底是哪个接口——
  // download_task/query 只负责查，建任务的是另一个，抓到它就能纯接口完成。
  let traced = 0;
  let learnedScene = "";
  let sawTaskId = false;
  const onTrace = (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method !== "Network.requestWillBeSent") return;
    const req = params.request || {};
    const u = String(req.url || "");
    if (!/oec_ads|ads-creation|export|download/i.test(u)) return;
    if (/\.(js|css|png|jpg|svg|woff2?)(\?|$)/i.test(u)) return;
    traced++;
    const pth = u.split("?")[0].replace(/^https?:\/\/[^/]+/, "");
    const postData = String(req.postData || "");
    // 从请求体里认「场景名」和「任务号」：
    //   场景名 = 这家店导出用的是哪一种（计划/商品/…）
    //   任务号 = 说明这次确实建出了任务，场景名可信
    const sc = postData.match(/"download_scene"\s*:\s*"([^"]+)"/);
    if (sc) learnedScene = sc[1];
    if (/"download_id"\s*:/.test(postData)) sawTaskId = true;
    const body = postData.slice(0, 200);
    if (log) log(`[trace] ${req.method || "?"} ${pth}${body ? " ｜ " + body : ""}`);
  };
  let trAttached = false;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    trAttached = true;
    await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
    chrome.debugger.onEvent.addListener(onTrace);
  } catch (e) {
    if (log) log(`   网络追踪没挂上（不影响收文件）：${(e && e.message) || e}`);
  }

  const cleanup = () => {
    try { chrome.debugger.onEvent.removeListener(onTrace); } catch (e) { /* 忽略 */ }
    if (trAttached) { chrome.debugger.detach({ tabId }).catch(() => {}); }
  };
  if (log) log("   已就绪：请在页面上点「导出」，文件会自动接住（最多等 3 分钟）");

  // 学到了就落盘：这家站点以后自动用这个场景名，不用再点、不用再猜。
  const saveLearned = async () => {
    if (!learnedScene || !(sawTaskId || captured)) return;
    try {
      const key = String((await siteProfile(host)).key || "");
      if (!key) return;
      const { adScene } = await skGet({ adScene: null });
      const next = Object.assign({}, adScene || {});
      next[key] = learnedScene;
      await chrome.storage.local.set({ adScene: next });
      if (log) log(`   已学会这家店的导出方式：${learnedScene}（以后自动用，不用再点）`);
    } catch (e) { /* 记住失败不影响本次 */ }
  };

  let captured = false;
  for (let i = 0; i < 90; i++) {
    await sleep(2000);
    const got = await harvestDownload(tabId);
    if (got && got.buf && got.buf.byteLength > 1024) {
      const nm = got.name || `creative data for product campaigns (${new Date()
        .toISOString().slice(0, 10)}).csv`;
      const saved = await uploadToBridge(nm, got.buf, "ads");
      if (log) log(`   已接住广告导出表：${saved.path}`);
      captured = true;
      await saveLearned();
      cleanup();
      return { via: "手动导出", saved: saved.path };
    }
    if (i % 10 === 9 && log) log(`   还在等下载…（${(i + 1) * 2} 秒）`);
  }
  if (log && traced) log(`   本轮共记录 ${traced} 个广告相关请求`);
  await saveLearned();
  cleanup();
  throw new Error("3 分钟内没等到下载（确认点的是「导出」并完成了确认框）");
}

/**
 * 直接在你已经打开的广告页上点「导出」——不刷新、不换页、不挂调试器。
 *
 * 为什么会走到这一步：广告后台的页面状态太娇气——刷新会掉回「活动」页、
 * 侧边栏点击会跳走、后台标签又不渲染。唯一稳定的是「你现在正看着的那一屏」，
 * 上面就有导出按钮。那就别动页面，只点按钮、把文件接住。
 */
/**
 * 新版广告后台（GMV Max）的「导出」其实是两个接口，不用点页面：
 *   1) download_task/query + {download_scene} → 建任务，回 download_id
 *   2) download_task/query + {download_id}    → 轮询，好了回下载地址
 * 实测抓包（菲律宾 18:48 手动导出）就是这两步。
 */
/**
 * 建广告导出任务——纯接口，不看页面。
 *
 * 关键：平台用 `post_product_list_v2` + `action:"download"` 来建任务，
 * 建完再用 download_task/query 轮询拿地址。之前一直只调 query（那是"查"），
 * 所以永远拿不到任务号——这才是"每加一家新店就卡住"的真正原因。
 *
 * 请求体的字段是从真实页面抓的（2026-10-31 LR Stationery）：
 *   query_list 必须带 mixed_real_cost / extra_currency，page_size = -1，
 *   dims 要 spu_id + campaign_id，action = download。
 */
/**
 * 宽松版 POST：业务码非 0 也不抛错，把 JSON 原样返回。
 * 用途是"取信息"（比如广告账户 ID）——平台常常 code!=0 但 data 里仍带着需要的字段。
 */
async function apiPostSoft(url, body) {
  const resp = await apiPost(url, body).catch((e) => ({ __err: e.message }));
  if (resp && resp.__err) {
    // apiPost 对非 0 会抛，这里退化成"自己发一次"
    try {
      let host = "";
      try { host = new URL(url).hostname; } catch (e) { /* 忽略 */ }
      const { [HEADER_KEY]: hp = {} } = await skGet({ [HEADER_KEY]: {} });
      const learned = (hp[host] || {});
      const headers = { "Content-Type": "application/json" };
      for (const [k, v] of Object.entries(learned)) {
        if (!k.startsWith("_")) headers[k] = v;
      }
      const r = await fetch(url, {
        method: "POST", credentials: "include", headers,
        body: JSON.stringify(body || {}),
      });
      return await r.json();
    } catch (e) {
      return null;
    }
  }
  return resp;
}

/**
 * 把平台的原始报错翻成人话。
 *
 * 平台的 system_msg 其实是说清楚了的（req adv id missing），但那是一句英文
 * 混在 JSON 里，出问题时没人愿意去翻。翻译一层，日志里直接写"缺什么"。
 */
function explainAdsError(raw) {
  const t = String(raw || "");
  const m = t.match(/"system_msg"\s*:\s*"([^"]*)"/);
  const sys = (m ? m[1] : t).toLowerCase();
  if (sys.includes("adv id missing")) return "缺少广告账户 ID（这家店绑了多个广告账户）";
  if (sys.includes("no download_id provided")) return "接口被当成「按任务号查询」，没给任务号";
  if (sys.includes("param_invalid") || sys.includes("参数无效")) return "参数不合法（字段名或取值变了）";
  if (sys.includes("permission") || sys.includes("no permission")) return "没有广告数据权限（该店可能没开通广告）";
  if (/21016003/.test(t)) return "导出文件还在生成";
  if (/98001021/.test(t)) return "请求被判非法（风控参数缺失）";
  return sys ? sys.slice(0, 60) : "";
}

/**
 * 店铺档案：这家店的"运行时事实"。
 *
 * 为什么要有：场景名、广告账户 ID 这些东西平台不写文档，只能从页面里学。
 * 学到的不能只存在本机扩展里——换台电脑就白学了。所以写进
 * `数据/stores/<店>/profile.json`，档案跟着数据走，拷过去就生效。
 */
async function getStoreProfile(shopId) {
  if (!shopId) return {};
  try {
    const r = await fetch(`${BRIDGE}/store_profile?shop_id=${encodeURIComponent(shopId)}`);
    const j = await r.json();
    return (j && j.profile) || {};
  } catch (e) {
    return {};
  }
}

async function saveStoreProfile(extra) {
  const sid = CURRENT_SHOP.shop_id;
  if (!sid) return;
  try {
    const prof = Object.assign({
      shop_id: sid,
      shop_name: CURRENT_SHOP.shop_name || "",
      region: CURRENT_SHOP.region || "",
      country: CURRENT_SHOP.country || "",
    }, extra || {});
    await fetch(`${BRIDGE}/store_profile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prof),
    });
  } catch (e) { /* 桥不在就不写，不影响取数 */ }
}

/**
 * 找这家店的广告账户 ID。
 *
 * 顺序：① 已学到的请求头里有没有（页面请求会带 x-tt-oec-ads-account-id）
 *      ② 问 get_onboard_info（宽松解析，业务码非 0 也读 data）
 * 两个都拿不到就返回空——不阻断，让平台自己决定（有的店不需要）。
 */
async function resolveAdvId(host, log) {
  const digAdv = (o, d) => {
    if (!o || d > 4) return "";
    if (typeof o === "object") {
      for (const [k, v] of Object.entries(o)) {
        if (/adv.*id$/i.test(k) && /^\d{6,}$/.test(String(v))) return String(v);
        const x = digAdv(v, d + 1);
        if (x) return x;
      }
    }
    return "";
  };

  // ① 学到的 URL 参数里的 aadvid —— 实测账户 ID 是放在 URL 上的，不在请求头
  try {
    const { params = {} } = await chrome.storage.local.get({ params: {} });
    const p = params[host] || {};
    const cand = [p.aadvid, p.adv_id, p.advid, p.advertiser_id]
      .find((v) => v && /^\d{6,}$/.test(String(v)));
    if (cand) {
      if (log) log(`   广告账户（来自页面 URL 参数 aadvid）：${cand}`);
      return String(cand);
    }
  } catch (e) { /* 忽略 */ }

  // ② 店铺档案里的（换电脑后不用重新学）
  try {
    const prof = await getStoreProfile(CURRENT_SHOP.shop_id);
    if (prof && prof.adv_id && /^\d{6,}$/.test(String(prof.adv_id))) {
      if (log) log(`   广告账户（来自店铺档案）：${prof.adv_id}`);
      return String(prof.adv_id);
    }
  } catch (e) { /* 忽略 */ }

  // ③ 学到的请求头（有的站点放在头里）
  try {
    const { [HEADER_KEY]: hp = {} } = await skGet({ [HEADER_KEY]: {} });
    for (const [k, v] of Object.entries(hp[host] || {})) {
      if (/ads-account-id|adv.?id/i.test(k) && /^\d{6,}$/.test(String(v))) {
        if (log) log(`   广告账户（来自学到的请求头）：${v}`);
        return String(v);
      }
    }
  } catch (e) { /* 忽略 */ }

  // ② 接口
  try {
    const ob = await apiPostSoft(
      await buildUrl(host, "/oec_ads/shopping/v1/oec/get_onboard_info"), {});
    const id = digAdv(ob, 0);
    if (id && log) log(`   广告账户：${id}`);
    return id;
  } catch (e) {
    return "";
  }
}

/**
 * 打开广告页 12 秒，只为把请求头学下来（广告账户 ID 就在里头）。
 * 纯自学：没有人工、没有远程，任何一台机器上都能跑。
 */
async function learnAdsHeaders(host, log) {
  let tabId = 0;
  let winId = 0;
  try {
    const w = await chrome.windows.getLastFocused();
    winId = (w && w.id) || 0;
  } catch (e) { /* 忽略 */ }
  try {
    const region = String((await siteProfile(host)).region || "MY").toUpperCase();
    const nt = await chrome.tabs.create({
      url: `https://${host}/ads-creation/dashboard?shop_region=${region}&type=product&mpa=1`,
      active: true, ...(winId ? { windowId: winId } : {}),
    });
    tabId = nt.id;
  } catch (e) {
    return false;
  }
  const onReq = (source, method, params) => {
    if (source.tabId !== tabId || method !== "Network.requestWillBeSent") return;
    const req = params.request || {};
    const u = String(req.url || "");
    if (!/oec_ads/i.test(u)) return;
    // URL 参数也要学：广告账户 ID（aadvid）就在 URL 上，不在请求头里。
    // 新电脑只点「广告」、没先开过广告页时，这份参数是空的——所以点导出/开页面
    // 这一刻必须顺手把参数也学下来，否则永远报「缺广告账户」。
    try { learnParams(u); } catch (e) { /* 忽略 */ }
    if (req.headers) {
      try { learnHeaders(new URL(u).hostname, req.headers); } catch (e) { /* 忽略 */ }
    }
  };
  let attached = false;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    attached = true;
    await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
    chrome.debugger.onEvent.addListener(onReq);
    await sleep(12000);
    if (log) log("   已从页面学到一批请求头");
    return true;
  } catch (e) {
    if (log) log(`   学请求头失败：${(e && e.message) || e}`);
    return false;
  } finally {
    try { chrome.debugger.onEvent.removeListener(onReq); } catch (e) { /* 忽略 */ }
    if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }
    if (tabId) { try { await chrome.tabs.remove(tabId); } catch (e) { /* 忽略 */ } }
  }
}

async function createAdsExportByApi(host, log, range) {
  const api = await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/post_product_list_v2");

  // 有的店铺（不是国家决定）要求显式带广告账户 ID，否则回
  // code=3「req adv id missing」。页面自己是从 get_onboard_info 拿的 primary_adv_id，
  // 这里同样取一次；取不到就照常发，让平台自己决定。
  const advId = await resolveAdvId(host, log);
  if (advId) { try { await saveStoreProfile({ adv_id: advId }); } catch (e) { /* 忽略 */ } }
  const st = (range && range.startDate) || "";
  const en = (range && (range.endDateExclusive
    ? shiftDate(range.endDateExclusive, -1) : range.to)) || "";
  const body = {
    query_list: ["product_name", "campaign_id", "campaign_name", "product_id",
                 "mixed_real_cost", "onsite_roi2_estimate_total_commission_pre_value",
                 "onsite_roi2_shopping_sku",
                 "mixed_real_cost_per_onsite_roi2_shopping_sku",
                 "onsite_roi2_shopping_value", "onsite_mixed_real_roi2_shopping",
                 "extra_currency"],
    action: "download",
    start_time: st,
    end_time: en,
    order_field: "mixed_real_cost",
    order_type: 1,
    page: 1,
    page_size: -1,
    filters: [
      { field: "campaign_status", filter_type: 0, in_field_values: ["no_delete"] },
      { field: "gmv_roi_mode", in_field_values: ["2"], filter_type: 10 },
    ],
    dims: ["spu_id", "campaign_id"],
  };
  if (advId) {
    // 两种常见写法都带上，多带不会错，少带一定会报 req adv id missing
    body.adv_id = advId;
    body.adv_id_list = [advId];
  }
  const extra = advId
    ? { "x-tt-oec-ads-account-id": advId, "x-adv-id": advId }
    : {};
  const apiWithAdv = advId
    ? api + (api.includes("?") ? "&" : "?") + "aadvid=" + encodeURIComponent(advId)
    : api;
  let r = await apiPostSoft(apiWithAdv, { ...body });
  // 平台说"缺广告账户"而我们手上没有：去页面把请求头学一遍，再重试一次。
  // 这一步是程序自己做的，不需要人点，也不需要远程。
  if ((!r || r.code !== 0) && !advId && /adv id missing/i.test(JSON.stringify(r || {}))) {
    if (log) log("   平台说缺广告账户，先去页面把请求头学一遍…");
    await learnAdsHeaders(host, log);
    const again = await resolveAdvId(host, log);
    if (again) {
      body.adv_id = again;
      body.adv_id_list = [again];
      const api2 = api + (api.includes("?") ? "&" : "?") + "aadvid=" + encodeURIComponent(again);
      r = await apiPostSoft(api2, body, {
        "x-tt-oec-ads-account-id": again, "x-adv-id": again,
      });
      if (log) log(`   带上学到的账户 ${again} 重试：code=${r && r.code}`);
    }
  }
  if (log) {
    const keys = r && r.data ? Object.keys(r.data).slice(0, 6).join(",") : "";
    const why = explainAdsError(JSON.stringify(r || {}));
    log(`   建导出返回：code=${r && r.code}${why ? "｜" + why : ""}｜data 键=${keys || "空"}`);
  }
  // 有的返回里直接给 download_id，有的要再查一次
  const digId = (o, d) => {
    if (!o || d > 4) return "";
    if (Array.isArray(o)) { for (const it of o) { const x = digId(it, d + 1); if (x) return x; } return ""; }
    if (typeof o !== "object") return "";
    for (const [k, v] of Object.entries(o)) {
      if (/id$/i.test(k) && /^\d{6,}$/.test(String(v))) return String(v);
      const x = digId(v, d + 1);
      if (x) return x;
    }
    return "";
  };
  const direct = digId(r, 0);
  if (direct) return direct;

  // 没直接给：按场景查一次（两个场景都试）
  const qapi = await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/download_task/query");
  for (const sc of ["product_list_navigation", "campaign_list_pgm_navigation"]) {
    const q = await apiPost(qapi, { download_scene: sc }).catch(() => null);
    const id = digId(q, 0);
    if (id) {
      if (log) log(`   场景 ${sc} 查到任务：${id}`);
      return id;
    }
  }
  return "";
}

async function exportAdsByApi(host, log, range) {
  const api = await buildUrl(host, "/oec_ads/shopping/v1/oec/stat/download_task/query");

  // 返回里 id 的字段名各版本不一样（download_id / task_id / 嵌在 offline_tasks 里），
  // 不猜名字：递归找「字段名以 id 结尾、值是长数字」的那个。
  const digId = (o, d) => {
    if (!o || d > 4) return "";
    if (Array.isArray(o)) {
      for (const it of o) { const x = digId(it, d + 1); if (x) return x; }
      return "";
    }
    if (typeof o !== "object") return "";
    for (const [k, v] of Object.entries(o)) {
      if (/id$/i.test(k) && /^\d{6,}$/.test(String(v))) return String(v);
      const x = digId(v, d + 1);
      if (x) return x;
    }
    return "";
  };

  // 场景名各站点/各版本可能不同：一个个试，谁回 download_id 就用谁，
  // 并记住这个站点成功的那个，下次直接用——新店不用改代码。
  let fav = "";
  let key = "";
  try {
    key = String((await siteProfile(host)).key || "");
    const { adScene } = await skGet({ adScene: null });
    if (adScene && adScene[key]) fav = adScene[key];
    if (!fav) {
      const prof = await getStoreProfile(CURRENT_SHOP.shop_id);
      if (prof && prof.ad_scene) fav = String(prof.ad_scene);
    }
  } catch (e) { /* 忽略 */ }

  // 场景名 = 页面上哪个标签页。实测：
  //   计划 → campaign_list_pgm_navigation
  //   商品 → product_list_navigation   ← 之前漏了这个，商品页的店全部落空
  // 两个都试，谁给任务号用谁。
  const scenes = [...new Set([fav,
                              "campaign_list_pgm_navigation",
                              "product_list_navigation",
                              "campaign_list_product",
                              "campaign_list_export",
                              "offline_export"].filter(Boolean))];
  let did = "";
  // ① 先走"建任务"接口（action=download）——这才是正路，纯接口不问页面
  try {
    did = await createAdsExportByApi(host, log, range);
    if (did && log) log(`   导出任务已建：${did}`);
  } catch (e) {
    if (log) log(`   建任务失败：${(e && e.message) || e}，改按场景查`);
  }
  // ② 建不出来再按场景查已有任务（兼容旧任务/旧后台）
  for (const sc of (did ? [] : scenes)) {
    const r = await apiPost(api, { download_scene: sc }).catch(() => null);
    const id = digId(r);
    if (!id) {
      if (log) log(`   场景「${sc}」没给任务号（code=${r && r.code}）`);
      continue;
    }
    did = id;
    if (log) log(`   导出任务已建（场景 ${sc}）：${id}`);
    if (key && sc !== fav) {
      try {
        const { adScene } = await skGet({ adScene: null });
        const next = Object.assign({}, adScene || {});
        next[key] = sc;
        await chrome.storage.local.set({ adScene: next });
      } catch (e) { /* 记住失败不影响本次 */ }
    }
    break;
  }
  if (!did) throw new Error("所有场景都没能建出导出任务");

  for (let i = 0; i < 30; i++) {
    await sleep(3000);
    let r = null;
    try {
      const body = i % 2 === 0
        ? { download_id: did }
        : { download_id: did, download_scene: "campaign_list_pgm_navigation" };
      r = await apiPost(api, body);
    } catch (e) {
      if (log) log(`   轮询失败：${(e && e.message) || e}`);
      continue;
    }
    const pickUrl = (o) => {
      if (!o) return "";
      if (typeof o.url === "string" && /^https?:/i.test(o.url)) return o.url;
      const d = o.data || {};
      if (d.download_url) return d.download_url;
      if (typeof d.url === "string" && /^https?:/i.test(d.url)) return d.url;
      for (const t of (o.offline_tasks || d.offline_tasks || [])) {
        if (t && t.url && String(t.task_status || "").toUpperCase() === "SUCCESS") return t.url;
      }
      return findUrl(o) || "";
    };
    const url = pickUrl(r);
    if (url && log) log(`   任务状态：${r.task_status || (r.data && r.data.task_status) || "?"}`);
    if (url) {
      if (log) log("   文件已就绪，开始取…");
      const resp = await fetch(url, { credentials: "include" });
      if (resp.ok) {
        const buf = await resp.arrayBuffer();
        if (buf && buf.byteLength > 1024) {
          const st = (range && range.startDate) || "";
          const en = (range && (range.endDateExclusive
            ? shiftDate(range.endDateExclusive, -1) : range.to)) || "";
          const nm = (st && en)
            ? `Product campaign data ${st} - ${en}.xlsx`
            : `Product campaign data ${new Date().toISOString().slice(0, 10)}.xlsx`;
          const saved = await uploadToBridge(nm, buf, "ads");
          if (log) log(`   已取回广告导出表：${saved.path}（${Math.round(buf.byteLength / 1024)} KB）`);
          return { via: "导出接口", saved: saved.path };
        }
      }
    }
    if (i % 3 === 2 && log) {
      log(`   等导出文件…（${(i + 1) * 3} 秒）${JSON.stringify(r).slice(0, 160)}`);
    }
  }
  throw new Error("导出任务 90 秒内没生成文件");
}

/**
 * 自己开一个「前台窗口里的广告页」，点导出、抓文件。
 *
 * 之前一直失败的真正原因：用 chrome.tabs.create 开的标签落在**上一次聚焦的窗口**，
 * 那个窗口可能被别的窗口盖住/最小化，页面被判 hidden，重 SPA 就不渲染，
 * 页面上根本没有「导出」按钮。
 * 这里改成：先拿当前聚焦窗口 → 在**那个窗口**里开标签 → 真的激活 → 再等按钮。
 *
 * 这是兜底链的最后一层，不依赖国家、不依赖场景名，也不依赖你手动点。
 */
async function exportAdsByClickFresh(host, log, range) {
  let winId = 0;
  try {
    const w = await chrome.windows.getLastFocused();
    winId = (w && w.id) || 0;
    if (w && w.state === "minimized") {
      await chrome.windows.update(winId, { state: "normal" });
    }
  } catch (e) { /* 忽略 */ }

  const region = String((await siteProfile(host)).region || "MY").toUpperCase();
  const path = `ads-creation/dashboard?shop_region=${region}&type=product&mpa=1`;
  let tabId = 0;
  try {
    const nt = await chrome.tabs.create({
      url: `https://${host}/${path}`, active: true,
      ...(winId ? { windowId: winId } : {}),
    });
    tabId = nt.id;
    if (winId) { try { await chrome.windows.update(winId, { focused: true }); } catch (e) {} }
  } catch (e) {
    throw new Error(`开广告页失败：${(e && e.message) || e}`);
  }

  const urls = [];
  let learnedScene = "";
  const onNet = (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method === "Network.requestWillBeSent") {
      // 自己点导出时，顺手把平台用的场景名记下来——这一步是"自学"的关键：
      // 下次同站点直接用它，不用再走点页面这条慢路。
      const _req = params.request || {};
      try {
        // 同时学请求头：广告账户 ID（x-tt-oec-ads-account-id）这类
        // 判权参数就在头里，学到之后我们发的请求自然带上，不用再猜。
        if (/oec_ads/i.test(String(_req.url || "")) && _req.headers) {
          learnHeaders(new URL(_req.url).hostname, _req.headers);
        }
      } catch (e) { /* 忽略 */ }
      const pd = String((_req.postData) || "");
      const m = pd.match(/"download_scene"\s*:\s*"([^"]+)"/);
      if (m) learnedScene = m[1];
      return;
    }
    if (method !== "Network.responseReceived") return;
    const u = String((params.response || {}).url || "");
    if (!/\.(xlsx|xls|csv)(\?|$)|\/object\/wsos|download/i.test(u)) return;
    if (/\.(js|css|png|jpg|svg|woff2?)(\?|$)/i.test(u)) return;
    if (!urls.includes(u)) {
      urls.push(u);
      if (log) log(`   捕获到文件地址：${u.split("?")[0].slice(0, 100)}`);
    }
  };
  let attached = false;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    attached = true;
    await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
    chrome.debugger.onEvent.addListener(onNet);

    // 等页面真的开始请求（SPA 启动要几秒），再等「导出」按钮出现
    const ok = await waitForButton(tabId, ["导出", "导出数据", "Export"], 45000, log);
    if (!ok) {
      const vis = await chrome.scripting.executeScript({
        target: { tabId }, func: () => document.visibilityState,
      }).then((r) => (r && r[0] && r[0].result) || "?").catch(() => "?");
      const frames = await frameInfo(tabId);
      if (log) log(`   等 45 秒没出现「导出」按钮（页面 ${vis}；帧 ${frames.length} 个）`);
    }
    await installDownloadGuard(tabId);
    const clicked = await clickByTextIn(tabId, ["导出", "导出数据", "Export"]);
    if (log) log(`   点「导出」：${clicked || "没点到"}`);
    if (!clicked) throw new Error("页面上没有可点的「导出」按钮");
    await sleep(1800);
    await clickByTextIn(tabId, ["导出", "确定", "确认", "下载", "Export"]);

    for (let i = 0; i < 40; i++) {
      await sleep(2000);
      const got = await harvestDownload(tabId);
      if (got && got.buf && got.buf.byteLength > 1024) {
        const nm = got.name || `Product campaign data ${new Date().toISOString().slice(0, 10)}.xlsx`;
        const saved = await uploadToBridge(nm, got.buf, "ads");
        if (log) log(`   已接住广告导出表：${saved.path}`);
        return { via: "页面导出", saved: saved.path };
      }
      for (const u of urls.slice().reverse()) {
        try {
          const r = await fetch(u, { credentials: "include" });
          if (!r.ok) continue;
          const buf = await r.arrayBuffer();
          if (!buf || buf.byteLength < 1024) continue;
          const nm = decodeURIComponent((u.split("?")[0].split("/").pop() || ""))
            || `Product campaign data ${new Date().toISOString().slice(0, 10)}.xlsx`;
          const saved = await uploadToBridge(nm, buf, "ads");
          if (log) log(`   已取回广告导出表：${saved.path}`);
          return { via: "页面导出", saved: saved.path };
        } catch (e) { /* 还没生成 */ }
      }
    }
    throw new Error("点了导出，但 80 秒内没拿到文件");
  } finally {
    if (learnedScene) {
      // 存到「按站点」的表里，全体浏览器共用同一套学习结果
      try {
        const key = String((await siteProfile(host)).key || "");
        if (key) {
          const { adScene } = await skGet({ adScene: null });
          const next = Object.assign({}, adScene || {});
          next[key] = learnedScene;
          await chrome.storage.local.set({ adScene: next });
          if (log) log(`   已学会这家站点的导出场景：${learnedScene}（以后自动用）`);
        }
      } catch (e) { /* 忽略 */ }
    }
    try { chrome.debugger.onEvent.removeListener(onNet); } catch (e) { /* 忽略 */ }
    if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }
    if (tabId) { try { await chrome.tabs.remove(tabId); } catch (e) { /* 忽略 */ } }
  }
}

async function exportAdsByClick(host, log) {
  // 自动开过很多次广告页，会留下好几个背景标签。
  // 必须挑「你现在正看着的那个」——背景标签不渲染，点不到任何按钮。
  let cands = [];
  try {
    const wins = await chrome.windows.getAll({ populate: true });
    for (const w of wins) {
      for (const t of (w.tabs || [])) {
        if (/ads-creation\/dashboard/.test(String(t.url || ""))) {
          cands.push({ id: t.id, windowId: w.id, active: !!t.active, focused: !!w.focused,
                       last: t.lastAccessed || 0 });
        }
      }
    }
  } catch (e) { /* 忽略 */ }
  if (!cands.length) throw new Error("没有打开着的广告页");
  cands.sort((a, b) => (b.focused - a.focused) || (b.active - a.active) || (b.last - a.last));

  let tabId = 0;
  for (const c of cands.slice(0, 4)) {
    try {
      await chrome.tabs.update(c.id, { active: true });
      await focusWindow(c.windowId);
      await sleep(900);
      const r = await chrome.scripting.executeScript({
        target: { tabId: c.id }, func: () => document.visibilityState,
      });
      const vis = String((r && r[0] && r[0].result) || "");
      if (log) log(`   广告页标签 #${c.id}：${vis || "?"}`);
      if (vis === "visible") { tabId = c.id; break; }
    } catch (e) { /* 换下一个 */ }
  }
  if (!tabId) throw new Error("找到的广告页都不可见（请先切到广告页再点）");

  // 只要「导出」出现就点。找不到就等一会儿（页面可能刚渲染完）。
  await waitForButton(tabId, ["导出", "导出数据", "Export"], 15000, log);
  await installDownloadGuard(tabId);
  DL_SEEN = [];

  // 挂调试器盯网络：点导出后平台会给出文件地址（或先建任务再给地址），
  // 拿到地址我们自己用 fetch 去取，浏览器就不会弹「另存为」。
  const urls = [];
  let learnedScene = "";
  const onNet = (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method === "Network.requestWillBeSent") {
      // 自己点导出时，顺手把平台用的场景名记下来——这一步是"自学"的关键：
      // 下次同站点直接用它，不用再走点页面这条慢路。
      const _req = params.request || {};
      try {
        // 同时学请求头：广告账户 ID（x-tt-oec-ads-account-id）这类
        // 判权参数就在头里，学到之后我们发的请求自然带上，不用再猜。
        if (/oec_ads/i.test(String(_req.url || "")) && _req.headers) {
          learnHeaders(new URL(_req.url).hostname, _req.headers);
        }
      } catch (e) { /* 忽略 */ }
      const pd = String((_req.postData) || "");
      const m = pd.match(/"download_scene"\s*:\s*"([^"]+)"/);
      if (m) learnedScene = m[1];
      return;
    }
    if (method !== "Network.responseReceived") return;
    const u = String((params.response || {}).url || "");
    if (!/\.(xlsx|xls|csv)(\?|$)|download|export_task|file_url/i.test(u)) return;
    if (/\.(js|css|png|jpg|svg|woff2?)(\?|$)/i.test(u)) return;
    if (!urls.includes(u)) {
      urls.push(u);
      if (log) log(`   捕获到文件地址：${u.split("?")[0].slice(0, 110)}`);
    }
  };
  let attached = false;
  chrome.debugger.onEvent.addListener(onNet);
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    attached = true;
    await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
  } catch (e) {
    if (log) log(`   调试器没挂上（不影响拦截）：${(e && e.message) || e}`);
  }

  const first = await clickByTextIn(tabId, ["导出", "导出数据", "Export"]);
  if (log) log(`   点「导出」：${first || "没点到"}`);
  await sleep(1500);
  // 可能弹层里还有一次确认（有的站点是「导出」下拉 →「下载」）
  const second = await clickByTextIn(tabId, ["导出", "确定", "确认", "下载", "Export"]);
  if (log) log(`   弹层确认：${second || "没有弹层"}`);

  // 先看网络里有没有文件地址：有就自己下，不依赖浏览器下载（也就不会弹保存框）
  for (let i = 0; i < 30 && !urls.length; i++) await sleep(1000);
  for (const u of urls.slice().reverse()) {
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        const r = await fetch(u, { credentials: "include" });
        if (r.ok) {
          const ct = r.headers.get("content-type") || "";
          if (!ct.includes("json")) {
            const buf = await r.arrayBuffer();
            if (buf && buf.byteLength > 1024) {
              const nm = decodeURIComponent((u.split("?")[0].split("/").pop() || ""))
                || `product campaign data ${new Date().toISOString().slice(0, 10)}.xlsx`;
              const saved = await uploadToBridge(nm, buf, "ads");
              if (log) log(`   已取回广告导出表：${saved.path}（${Math.round(buf.byteLength / 1024)} KB）`);
              if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }
              return { via: "网络取文件", saved: saved.path };
            }
          }
        }
      } catch (e) { /* 还没生成，等会儿再试 */ }
      await sleep(3000);
    }
  }
  if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }

  for (let i = 0; i < 40; i++) {
    await sleep(2000);
    const got = await harvestDownload(tabId);
    if (got && got.buf && got.buf.byteLength > 1024) {
      const nm = got.name || `creative data for product campaigns (${new Date()
        .toISOString().slice(0, 10)}).csv`;
      const saved = await uploadToBridge(nm, got.buf, "ads");
      if (log) log(`   已接住广告导出表：${saved.path}`);
      return { via: "点导出", saved: saved.path };
    }
  }
  throw new Error("点了导出，但 80 秒内没接到文件（可能弹层里还要选日期或确认）");
}

/**
 * 从「已经打开的广告页」直接抄它自己拿到的数据。
 *
 * 为什么这么做：同一个接口，马来能出数、菲律宾回「参数无效」——
 * 页面自己能拿到，说明差的是请求头/上下文（广告账户 id 之类），
 * 我们复刻不完整。与其一遍遍猜参数，不如：
 *   挂上调试器 → 刷新广告页 → 页面自己去请求（它一定有权限）
 *   → 把响应体接住 → 解析成广告表。
 * 不依赖页面渲染出导出按钮，也不依赖我们发的请求被平台认可。
 */
async function captureAdsFromPage(host, log) {
  let tab = null;
  try {
    const wins = await chrome.windows.getAll({ populate: true });
    const order = wins.slice().sort((a, b) => (b.focused ? 1 : 0) - (a.focused ? 1 : 0));
    for (const w of order) {
      const t = (w.tabs || []).find((x) => /ads-creation\/dashboard/.test(String(x.url || "")));
      if (t) { tab = { id: t.id, windowId: w.id }; break; }
    }
  } catch (e) { /* 忽略 */ }
  if (!tab) throw new Error("没有打开着的广告页（请先手动打开菲律宾广告面板）");

  const tabId = tab.id;
  // 广告页必须真的"可见"，否则 SPA 只发配置类请求、不拉数据（实测 80 个响应全是时区/清单）。
  // 这里激活标签 + 恢复窗口，然后回读 visibilityState 确认到底有没有生效。
  for (let i = 0; i < 3; i++) {
    try { await chrome.tabs.update(tabId, { active: true }); } catch (e) { /* 忽略 */ }
    await focusWindow(tab.windowId);
    await sleep(800);
    let vis = "";
    try {
      const r = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => document.visibilityState,
      });
      vis = String((r && r[0] && r[0].result) || "");
    } catch (e) { /* 忽略 */ }
    if (vis === "visible") break;
    if (log) log(`   广告页当前不可见（${vis || "?"}），再切一次…`);
  }

  const bodies = [];
  const onEvent = async (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method !== "Network.responseReceived") return;
    const u = String((params.response || {}).url || "");
    // 实测：新版后台（GMV Max）拿数据用的是 post_campaign_list_v2，
    // 老版是 post_product_list_v2。两个都接。
    if (!/post_(product|campaign)_list_v2/i.test(u)) return;
    try {
      const b = await chrome.debugger.sendCommand(
        { tabId }, "Network.getResponseBody", { requestId: params.requestId });
      const txt = (b && b.body) || "";
      if (txt.includes("table_v2")) bodies.push(txt);
    } catch (e) { /* 响应体拿不到就跳过 */ }
  };

  let attached = false;
  const seen = [];   // 诊断：广告相关的每个响应，路径 + 顶层键，用来认清哪个才是数据接口
  chrome.debugger.onEvent.addListener(async (source, method, params) => {
    if (source.tabId !== tabId || method !== "Network.responseReceived") return;
    const u = String((params.response || {}).url || "");
    if (!/oec_ads|ads-creation|\/api\//i.test(u)) return;
    if (!/json/i.test(String((params.response || {}).mimeType || ""))) return;
    try {
      const b = await chrome.debugger.sendCommand(
        { tabId }, "Network.getResponseBody", { requestId: params.requestId });
      const txt = (b && b.body) || "";
      if (!txt.trim().startsWith("{")) return;
      const j = JSON.parse(txt);
      const keys = Object.keys(j || {}).slice(0, 5).join(",");
      const dataKeys = Object.keys((j && j.data) || {}).slice(0, 6).join(",");
      const p = u.split("?")[0].replace(/^https:\/\/[^/]+/, "");
      seen.push({ p, keys, dataKeys, len: txt.length });
    } catch (e) { /* 取不到就算了 */ }
  });
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    attached = true;
    await chrome.debugger.sendCommand({ tabId }, "Network.enable", {});
    // 不刷新页面：一刷新广告后台就回默认「活动」页，计划列表不再加载，
    // 我们既拿不到数据、也把你正在看的页面搞乱。
    // 改成：挂上监视器后，等页面自己发请求（用户点一下日期旁的刷新即可）。
    if (log) log("   已开始监视，请在广告页点一下日期右边的刷新按钮（⟳）");
    for (let i = 0; i < 75 && !bodies.length; i++) await sleep(1000);
    if (log) {
      log(`   这次共看到 ${seen.length} 个广告相关响应，其中 ${bodies.length} 个带数据表`);
      // 把接口清单打进日志：哪个接口带 product_id / 行数据，一眼就能认出来
      const uniq = [];
      const got = new Set();
      for (const s of seen) {
        const k = s.p + "|" + s.dataKeys;
        if (got.has(k)) continue;
        got.add(k);
        uniq.push(s);
      }
      for (const s of uniq.slice(0, 12)) {
        log(`   · ${s.p}｜${Math.round(s.len / 1024)}KB｜data:${s.dataKeys || "空"}`);
      }
    }
  } catch (e) {
    if (log) log(`   挂调试器失败：${(e && e.message) || e}`);
  } finally {
    try { chrome.debugger.onEvent.removeListener(onEvent); } catch (e) { /* 忽略 */ }
    if (attached) { try { await chrome.debugger.detach({ tabId }); } catch (e) { /* 忽略 */ } }
  }

  if (!bodies.length) throw new Error("刷新后没抓到广告数据接口的响应");

  // 把多页/多次请求的行合起来（table_v2 是「每格 {data,name}」的列式结构，
  // rowsToObjects 会按 name 还原成普通对象）
  const rows = [];
  for (const txt of bodies) {
    try {
      const j = JSON.parse(txt);
      const t2 = j && j.data && j.data.table_v2;
      if (Array.isArray(t2)) rows.push(...t2);
    } catch (e) { /* 跳过坏包 */ }
  }
  if (!rows.length) throw new Error("抓到的响应里没有 table_v2 行数据");
  if (log) {
    // 先把第一行的字段名打出来：新版是「计划」维度，得确认里面有没有商品号，
    // 没有的话就没法按商品摊广告费，得换别的口径算。
    const objs = rowsToObjects(rows.slice(0, 3));
    const keys = objs && objs[0] ? Object.keys(objs[0]).slice(0, 20).join(",") : "";
    log(`   页面自己取到 ${rows.length} 行广告数据｜字段：${keys || "（识别不出）"}`);
  }

  const csv = adsRowsToCsv(rows);
  const bytes = new TextEncoder().encode(csv);
  const name = `creative data for product campaigns (页面读取 ${new Date()
    .toISOString().slice(0, 10)}).csv`;
  const saved = await uploadToBridge(name, bytes.buffer, "ads");
  if (log) log(`   已保存：${saved.path}`);
  return { via: "页面数据", rows: rows.length, saved: saved.path };
}

/**
 * 广告规格接口的「参数探测」。
 *
 * 同一个接口 `/oec_ads/.../post_product_list_v2`，马来返回 code=0 有数据，
 * 菲律宾返回 code=3（连 message 都没有）。两边的差别只可能在请求体，
 * 尤其是 filters 里的 gmv_roi_mode。这里把几种变体各发一次，把平台真实
 * 反应打进日志——一次点击就能定位，不用来回猜、来回改。
 */
async function probeAdsSpec(host, range, log) {
  const store = await currentStoreId();
  const resp = await fetch(`${BRIDGE}/spec`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: "ads", range, store }),
  });
  const sj = await resp.json();
  const step = (((sj.spec || {}).steps) || [])[0];
  if (!step) {
    log("   探测：规格里没有步骤，跳过");
    return;
  }
  const base = JSON.parse(JSON.stringify(step.body || {}));
  const dropFilter = (b, field) => {
    b.filters = (b.filters || []).filter((f) => f.field !== field);
    return b;
  };
  const variants = [
    ["原样", () => JSON.parse(JSON.stringify(base))],
    ["去掉 gmv_roi_mode", () => dropFilter(JSON.parse(JSON.stringify(base)), "gmv_roi_mode")],
    ["gmv_roi_mode=1", () => {
      const b = JSON.parse(JSON.stringify(base));
      for (const f of b.filters || []) if (f.field === "gmv_roi_mode") f.in_field_values = ["1"];
      return b;
    }],
    ["去掉全部 filters", () => { const b = JSON.parse(JSON.stringify(base)); delete b.filters; return b; }],
  ];
  for (const [label, make] of variants) {
    try {
      const r = await callApi(host, step.path, step.method || "POST", make());
      const rows = r && r.data && r.data.table_v2;
      const dataKeys = Object.keys((r && r.data) || {}).slice(0, 6).join(",");
      // 有的站点把业务码放在 data 里（data={code,msg}），外层只有 code 正常，
      // 只看外层会得到 undefined，等于什么都没看到。
      const inner = (r && r.data) || {};
      const code = (r && r.code) !== undefined ? r.code : inner.code;
      const msg = inner.msg || inner.message || (r && r.message) || "";
      log(`   探测「${label}」：code=${code}${msg ? " msg=" + String(msg).slice(0, 80) : ""}`
        + `｜行数=${Array.isArray(rows) ? rows.length : "无 table_v2"}`
        + `｜data 键=${dataKeys || "空"}`);
    } catch (e) {
      log(`   探测「${label}」：请求失败 ${(e && e.message) || e}`);
    }
  }
}

const TASK_IMPL = {
  // 手动导出模式：程序只装拦截器 + 收文件，点按钮由人来（人点最准）
  manualAds: async (range, log) => manualAdsCapture(log),
  orders: runOrders,
  sku: runSkuStats,
  // 链接资料：报表「款式编码」列的来源。跟 sku 取的是同一个接口，
  // 但单独跑一份 CSV，界面上「链接资料」页可以单独导入/更新。
  link: runLinkInfo,
  // 这两个改走「规格驱动」：扩展不再写死接口，
  // 规格见 config/api_specs.json（改取数逻辑不用刷新扩展）
  //
  // 但规格接口会受平台版本/风控影响（返回 code=3 / code=10000 之类）。
  // 那种情况不要直接判死：广告还能靠「导出任务」下载用户点过导出的文件，
  // 联盟还能靠页面上的明细接口，所以失败后回退到老的完整实现。
  affiliate: async (range, log) => {
    try {
      return await runSpecTask("affiliate", range, log);
    } catch (e) {
      log(`   规格接口不可用（${(e && e.message) || e}），改走联盟页面方式…`);
      return runAffiliate(range, log);
    }
  },
  ads: async (range, log) => {
    // 站点档案里标了 ads_spec=false 的（实测菲律宾），别再浪费一轮请求，
    // 直接去广告页点导出。没标的照常先试规格接口。
    let prof = {};
    try {
      prof = await siteProfile(await ensureHost("seller", log));
    } catch (e) { /* 拿不到就当默认 */ }
    if (prof.ads_spec === false) {
      // 这家站的接口我们复刻不全（平台回「参数无效」），但页面自己能拿到数据。
      // 先抄页面的：挂调试器刷新广告页、接住响应——不依赖渲染、不依赖导出按钮。
      const host = await ensureHost("seller", log);
      log(`   站点 ${String(prof.key || "?").toUpperCase()} 改走「导出任务」接口…`);
      try {
        return await exportAdsByApi(host, log, range);
      } catch (e) {
        log(`   导出接口没成：${(e && e.message) || e}；改在页面上点导出`);
      }
      try {
        return await exportAdsByClickFresh(host, log, range);
      } catch (e) {
        log(`   页面导出也没成：${(e && e.message) || e}；改查已完成的导出任务`);
      }
      log("   → 这家店的导出方式还没学会：点一次「广告（我自己点导出）」，"
          + "我会记住它的接口，以后这家店就全自动了");
      return runAds(range, log, { noDirect: true });
    }
    try {
      return await runSpecTask("ads", range, log);
    } catch (e) {
      log(`   规格接口不可用（${(e && e.message) || e}），改走「导出任务」接口…`);
      // 实测：马来西亚的新店（Chroma Home）也回 code=3，跟菲律宾一样——
      // 说明差别不在国家，而在**这家店是不是 GMV Max 账户**。
      // 所以「导出任务」这条路对两个国家都先试，最后才退到点页面。
      try {
        return await exportAdsByApi(await ensureHost("seller", log), log, range);
      } catch (e2) {
        log(`   导出接口也没成：${(e2 && e2.message) || e2}；改在页面上点导出`);
      }
      return runAds(range, log, { noDirect: true });
    }
  },
};
const TASK_HINT = { orders: "seller", sku: "seller", link: "seller", affiliate: "affiliate", ads: "seller" };

async function runTask(task, range, log, opts = {}) {
  if (task === "all") {
    const out = {};
    // 广告排在最后：它只查已完成任务，不会阻塞前面的取数；
    // 剩下几个是算净利必需的，任何一个失败都不影响其余继续跑。
    //
    // link（链接资料）不在这个列表里：它跟 sku 抓的是同一个接口，
    // 重复抓一遍纯属浪费。sku 跑完会自动带出链接资料。
    for (const t of ["orders", "sku", "affiliate", "ads"]) {
      const t0 = Date.now();
      await reportProgress(t, "running");
      try {
        log(`—— 开始：${t} ——`);
        // 广告以前用 quick 模式（只查已完成的导出任务，不主动导）省时间，
        // 但直取接口在部分站点（实测菲律宾 code=3）根本不通，quick 就等于放弃广告，
        // 算出来的净利里没有广告费、看着特别高。现在让它同样走「页面点导出」兜底。
        out[t] = await runTask(t, range, log, { force: !!opts.force });
        await reportProgress(t, "done", {
          elapsed: (Date.now() - t0) / 1000,
          detail: shortDetail(out[t]),
        });
      } catch (e) {
        log(`—— ${t} 失败：${e.message}`);
        out[t] = { error: String(e.message || e) };
        await reportProgress(t, "fail", {
          elapsed: (Date.now() - t0) / 1000,
          error: String(e.message || e),
        });
      }
    }
    return out;
  }

  const impl = TASK_IMPL[task];
  if (!impl) throw new Error(`不支持的取数任务：${task}`);

  // 手动导出模式不碰页面：不识别、不刷参数、不开页，直接装拦截器等着收文件。
  // （ensureHost 在参数过期时会刷新页面，那会把用户正在看的广告页搞没）
  if (task === "manualAds") {
    const impl0 = TASK_IMPL[task];
    return await impl0(range, log, opts);
  }

  // 先确认这是哪家店——数据要按店铺分开存，混在一起就全乱了。
  // 每轮第一家任务都重新认一遍：同一个浏览器里换登店铺时，
  // 缓存里的 shop_id 会让新一轮的数据写进上一家店的目录。
  try {
    const host = await ensureHost(TASK_HINT[task] || "seller", log);
    await identifyShop(host, log, { force: task === "orders" || !CURRENT_SHOP.shop_id });
  } catch (e) {
    log(`店铺识别跳过：${(e && e.message) || e}`);
  }

  try {
    return await impl(range, log, opts);
  } catch (e) {
    const msg = String(e.message || e);
    // 登录态或风控参数失效是最常见的失败原因：刷新一次参数再试，别直接甩给用户。
    // 但「文件还在生成」(21016003) 和「该时段没有订单」跟参数无关，
    // 重排任务只会又多等一轮，必须排除掉。
    const retriable = /40[123]|风控|登录|code=|参数|学不到/.test(msg)
      && !/21016003|没有生成订单导出文件/.test(msg);
    if (retriable) {
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

  // 订单还在平台排队时别急着算：算出来只会是"没有订单表"，白跑一趟。
  // 大店导出要几分钟，这里等它（后台跑，面板关了也不影响），
  // 拿到就直接接着算，用户不用再点第二次。
  const ordErr = (r && r.orders && r.orders.error) ? String(r.orders.error) : "";
  if (/排队/.test(ordErr)) {
    // 大店订单导出要多久？实测 Chroma Home（5.7 万单 / 39MB）约 8 分钟。
    // 以前只等 4 分钟，到点就「没有订单表」失败——文件 3 分钟后才到，
    // 用户还得自己再点一次核算。等够 12 分钟，把这条鸿沟盖住。
    const rounds = 36;           // 36 × 20s = 12 分钟
    log("订单还在平台排队，等它生成好再算（最多 12 分钟，可以先干别的）…");
    for (let i = 0; i < rounds; i++) {
      await sleep(20000);
      const got = await harvestPendingOrders(log, CURRENT_SHOP.shop_id).catch(() => 0);
      if (got > 0) { log("订单已到，继续核算"); break; }
      if (i % 3 === 2) log(`   还在等订单…（${Math.round((i + 1) * 20 / 60)} 分钟）`);
    }
  }
  try {
    await reportProgress("calc", "running");
    const c0 = Date.now();
    const c = await calcNow(range, log, opts);
    await reportProgress("calc", "done", { elapsed: (Date.now() - c0) / 1000 });
    await reportProgress("report", "done", {
      elapsed: (Date.now() - c0) / 1000,
      detail: (c && c.file) ? String(c.file).split(/[\\/]/).pop().slice(0, 40) : "",
    });
    return { fetch: r, calc: c };
  } catch (e) {
    await reportProgress("calc", "fail", { error: String(e.message || e) });
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
        shop: CURRENT_SHOP,
        shopError: SHOP_ERROR,
        running: s.running,
        lastResult: s.lastResult,
        lastCalc: s.lastCalc,
      });
    });
    return true;
  }

  // 面板一打开就问一次「现在这家店是谁」。
  // 新店铺不需要任何手工登记：接口认出来就自动建档、自动带出国家。
  if (msg.type === "identifyNow") {
    (async () => {
      const log = (m) => appendTaskLog(`[identify] ${m}`);
      try {
        // 已经知道这家店是谁：立刻回给面板（瞬时显示），
        // 之后在后台安静地复核一次。以前每次都走完整识别，面板要等好几秒。
        if (CURRENT_SHOP.shop_id) {
          sendResponse({ ok: true, shop: CURRENT_SHOP, country: CURRENT_SHOP.country || "" });
          const fresh = Date.now() - (CURRENT_SHOP._at || 0) < 10 * 60 * 1000;
          if (fresh) return;                       // 10 分钟内不重复复核
          (async () => {
            try {
              const h = await ensureHost("seller", log);
              await identifyShop(h, log, { force: true });
            } catch (e) { /* 复核失败就继续用已知的 */ }
          })();
          return;
        }

        // 没有身份：走和「运行时」完全相同的那条路（先刷参数再识别）
        let host = "";
        try {
          host = await ensureHost("seller", log);
        } catch (e) {
          host = pickHost((await chrome.storage.local.get({ params: {} })).params, "seller") || "";
        }
        if (!host) {
          const t = await findTab("seller");
          host = (t && t.host) || "";
        }
        if (!host) {
          sendResponse({ ok: false, error: "当前没有打开卖家后台页面（seller-xx.tiktok.com）" });
          return;
        }
        const shop = await identifyShop(host, log, { force: true });
        sendResponse({
          ok: !!shop.shop_id,
          shop,
          country: shop.country || "",
          error: shop.shop_id ? "" : (SHOP_ERROR || "没认出店铺"),
        });
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
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

  if (msg.type === "captureAds") {
    const log = (m) => appendTaskLog(`[ads-cap] ${m}`);
    startKeepAlive();
    captureAds(msg.seconds || 180, log)
      .then((r) => {
        stopKeepAlive();
        sendResponse({ ok: true, result: r });
      })
      .catch((e) => {
        stopKeepAlive();
        appendTaskLog(`[ads-cap] 失败：${e.message}`);
        sendResponse({ ok: false, error: String(e.message || e) });
      });
    return true;
  }

  if (msg.type === "manualExport") {
    // 手动导数据：打开店铺页面（可选盯着浏览器下载自动接住）
    const kind = String(msg.kind || "orders");
    const capture = !!msg.capture;
    const log = (m) => appendTaskLog(`[manual:${kind}] ${m}`);
    openBackoffice(kind, capture, log)
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
          store: msg.store, country: msg.country,
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
        const j = await calcNow(msg.range || {}, log, { country: msg.country, store: msg.store });
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
