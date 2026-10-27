/* 后台：缓存抓到的接口记录，批量推给本地桥（127.0.0.1:8765）。

本地桥没开时不报错，记录留在内存里，等桥起来后随下一次刷新再推。
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "capture") {
    buffer.push(msg.payload);
    stats.captured += 1;
    if (buffer.length > MAX_BUFFER) buffer.splice(0, buffer.length - MAX_BUFFER);
    chrome.storage.local.set({ stats });
    flush();
    sendResponse({ ok: true, buffered: buffer.length });
    return true;
  }
  if (msg && msg.type === "state") {
    chrome.storage.local.get({ stats }, (s) => {
      sendResponse({ enabled, buffered: buffer.length, stats: s.stats });
    });
    return true;
  }
  if (msg && msg.type === "toggle") {
    enabled = !!msg.enabled;
    chrome.storage.local.set({ enabled });
    if (enabled) flush();
    sendResponse({ ok: true, enabled });
    return true;
  }
  return false;
});

setInterval(flush, 3000);
