const $ = (id) => document.getElementById(id);

function render(state) {
  $("enabled").textContent = state.enabled ? "记录中" : "已暂停";
  $("enabled").className = "v " + (state.enabled ? "ok" : "bad");
  $("captured").textContent = state.stats?.captured ?? 0;
  $("pushed").textContent = state.stats?.pushed ?? 0;
  $("toggle").textContent = state.enabled ? "暂停记录" : "开始记录";
  $("toggle").className = state.enabled ? "off" : "";
}

function checkBridge() {
  const el = $("bridge");
  fetch("http://127.0.0.1:8765/health")
    .then((r) => r.json())
    .then((d) => { el.textContent = d.ok ? "已连接" : "无响应"; el.className = "v " + (d.ok ? "ok" : "bad"); })
    .catch(() => { el.textContent = "未启动"; el.className = "v bad"; });
}

chrome.runtime.sendMessage({ type: "state" }, (s) => { if (s) render(s); });
checkBridge();

$("toggle").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "state" }, (s) => {
    const next = !(s && s.enabled);
    chrome.runtime.sendMessage({ type: "toggle", enabled: next }, () => {
      chrome.runtime.sendMessage({ type: "state" }, (s2) => { if (s2) render(s2); });
    });
  });
});
