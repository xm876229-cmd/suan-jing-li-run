const $ = (id) => document.getElementById(id);
const logEl = $("log");

function log(msg) {
  const line = document.createElement("div");
  line.textContent = msg;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

function render(state) {
  $("enabled").textContent = state.enabled ? "记录中" : "已暂停";
  $("enabled").className = "v " + (state.enabled ? "ok" : "bad");
  $("captured").textContent = state.stats?.captured ?? 0;
  $("pushed").textContent = state.stats?.pushed ?? 0;
  $("toggle").textContent = state.enabled ? "暂停记录" : "开始记录";
  const hosts = state.learnedHosts || [];
  $("hosts").textContent = hosts.length ? hosts.length + " 个域名" : "还没学到";
  $("hosts").title = hosts.join("\n");
}

function checkBridge() {
  const el = $("bridge");
  fetch("http://127.0.0.1:8765/health")
    .then((r) => r.json())
    .then((d) => { el.textContent = d.ok ? "已连接" : "无响应"; el.className = "v " + (d.ok ? "ok" : "bad"); })
    .catch(() => { el.textContent = "未启动"; el.className = "v bad"; });
}

function initDates() {
  const today = new Date();
  const first = new Date(today.getFullYear(), today.getMonth(), 1);
  const fmt = (d) => d.toISOString().slice(0, 10);
  $("dfrom").value = fmt(first);
  $("dto").value = fmt(today);
}

chrome.runtime.sendMessage({ type: "state" }, (s) => { if (s) render(s); });
checkBridge();
initDates();

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "taskLog") log(msg.msg);
});

$("toggle").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "state" }, (s) => {
    const next = !(s && s.enabled);
    chrome.runtime.sendMessage({ type: "toggle", enabled: next }, () => {
      chrome.runtime.sendMessage({ type: "state" }, (s2) => { if (s2) render(s2); });
    });
  });
});

$("run").addEventListener("click", () => {
  const from = $("dfrom").value;
  const to = $("dto").value;
  if (!from || !to) { log("请先选日期"); return; }

  // 订单接口要的是毫秒时间戳，且结束时间要取当天 23:59:59.999
  const startMs = new Date(from + "T00:00:00").getTime();
  const endMs = new Date(to + "T23:59:59.999").getTime();

  $("run").disabled = true;
  log(`开始取订单：${from} ~ ${to}`);
  chrome.runtime.sendMessage(
    { type: "runTask", task: "orders", range: { startMs, endMs } },
    (resp) => {
      $("run").disabled = false;
      if (!resp) { log("没有响应，检查扩展是否被重新加载"); return; }
      if (resp.ok) log(`完成：${resp.result?.saved || ""}`);
      else log(`失败：${resp.error}`);
    }
  );
});
