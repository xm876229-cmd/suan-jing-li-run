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

// ---------------- 公共参数学习 ----------------
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
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  const host = pickHost(params, "seller-") || pickHost(params, "seller");
  if (!host) throw new Error("还没学到卖家后台的公共参数：先在卖家后台点一次导出，让扩展抓到请求");

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
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  const host = pickHost(params, "seller-") || pickHost(params, "seller");
  if (!host) throw new Error("还没学到卖家后台的公共参数：先打开一次店铺后台");

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

  // 这个接口直接返回文件路径，但文件可能还在生成，轮询直到能取到二进制
  const url = `https://${host}${file}`;
  for (let i = 0; i < 30; i++) {
    const head = await fetch(url, { credentials: "include" });
    const ct = head.headers.get("content-type") || "";
    if (head.ok && !ct.includes("json")) {
      const buf = await head.arrayBuffer();
      if (buf.byteLength > 512) {
        const saved = await uploadToBridge(`product_sku_list-${startDate}_${endDateExclusive}.xlsx`, buf);
        log(`已保存：${saved.path}`);
        return { saved: saved.path, bytes: buf.byteLength };
      }
    }
    log(`文件还在生成…（${(i + 1) * 5}s）`);
    await sleep(5000);
  }
  throw new Error("等了两分半仍取不到文件");
}

// ---------------- 任务：联盟佣金 ----------------

async function runAffiliate({ startDate, endDateExclusive, tzOffset }, log) {
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  const host = pickHost(params, "affiliate");
  if (!host) throw new Error("还没学到联盟后台的公共参数：先打开一次达人联盟页面");

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

async function runAds({}, log) {
  const { params = {} } = await chrome.storage.local.get({ params: {} });
  const host = pickHost(params, "seller-") || pickHost(params, "seller");
  if (!host) throw new Error("还没学到卖家后台的公共参数");

  log("查询广告导出任务…");
  for (let i = 0; i < 60; i++) {
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
    log(`任务状态：${running ? running.task_status : "无进行中的任务"}（${(i + 1) * 10}s）`);
    await sleep(10000);
  }
  throw new Error("十分钟内没有等到已完成的广告导出任务（广告导出需要先手工点一次）");
}

// ---------------- 任务调度 ----------------

async function runTask(task, range, log) {
  if (task === "orders") return runOrders(range, log);
  if (task === "sku") return runSkuStats(range, log);
  if (task === "affiliate") return runAffiliate(range, log);
  if (task === "ads") return runAds(range, log);
  if (task === "all") {
    const out = {};
    for (const t of ["orders", "sku", "affiliate"]) {
      try {
        log(`—— 开始：${t} ——`);
        out[t] = await runTask(t, range, log);
      } catch (e) {
        log(`—— ${t} 失败：${e.message}`);
        out[t] = { error: String(e.message || e) };
      }
    }
    return out;
  }
  throw new Error(`暂不支持的取数任务：${task}`);
}

function bridgeLog(msg) {
  fetch(`${BRIDGE}/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msg: String(msg) }),
  }).catch(() => {});
}

// ---------------- 消息路由 ----------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;

  if (msg.type === "capture") {
    buffer.push(msg.payload);
    stats.captured += 1;
    if (buffer.length > MAX_BUFFER) buffer.splice(0, buffer.length - MAX_BUFFER);
    chrome.storage.local.set({ stats });
    if (msg.payload && msg.payload.url) learnParams(msg.payload.url);
    flush();
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
