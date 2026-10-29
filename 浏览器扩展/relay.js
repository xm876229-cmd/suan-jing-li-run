/* 隔离世界：把主世界抓到的消息转给后台。 */
const TAG = "__jingli_capture__";

window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || d.tag !== TAG || !d.payload) return;
  try {
    chrome.runtime.sendMessage({ type: "capture", payload: d.payload }, () => void chrome.runtime.lastError);
  } catch (e) { /* 扩展被重载时忽略 */ }
});
