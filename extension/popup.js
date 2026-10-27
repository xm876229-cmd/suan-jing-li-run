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

function fmt(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function initDates() {
  const today = new Date();
  const first = new Date(today.getFullYear(), today.getMonth(), 1);
  $("dfrom").value = fmt(first);
  $("dto").value = fmt(today);
}

/** 把用户选的两个日期换算成各接口需要的形态 */
function buildRange() {
  const from = $("dfrom").value;
  const to = $("dto").value;
  if (!from || !to) return null;

  const start = new Date(from + "T00:00:00");
  const endDay = new Date(to + "T00:00:00");
  const endNext = new Date(endDay.getTime() + 24 * 3600 * 1000); // 各接口的 end 多为开区间

  return {
    startMs: start.getTime(),
    endMs: new Date(to + "T23:59:59.999").getTime(),
    startDate: from,
    endDateExclusive: fmt(endNext),
    tzOffset: -new Date().getTimezoneOffset() * 60,
  };
}

function runTasks(task, btn) {
  const range = buildRange();
  if (!range) { log("请先选日期"); return; }

  const label = task === "all" ? "全部" : task;
  if (btn) btn.disabled = true;
  log(`=== 开始取数：${label}（${range.startDate} ~ ${$("dto").value}）===`);

  chrome.runtime.sendMessage({ type: "runTask", task, range }, (resp) => {
    if (btn) btn.disabled = false;
    if (!resp) { log("没有响应，检查扩展是否被重新加载"); return; }
    if (resp.ok) log(`=== ${label} 完成 ===`);
    else log(`=== ${label} 失败：${resp.error} ===`);
  });
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

$("runAll").addEventListener("click", (e) => runTasks("all", e.target));
document.querySelectorAll(".grid button").forEach((b) => {
  b.addEventListener("click", () => runTasks(b.dataset.task, b));
});
