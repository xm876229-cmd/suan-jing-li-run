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

// ---------------- 任务调度 ----------------

async function runTask(task, range, log) {
  if (task === "orders") return runOrders(range, log);
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
      chrome.runtime.sendMessage({ type: "taskLog", msg: m }).catch?.(() => {});
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
