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
  applyPreset("month");
}

/** 不用原生日期控件：它的语言跟随浏览器（马来店里会显示马来语），改用文本 + 预设。 */
function applyPreset(name) {
  const today = new Date();
  let from = new Date(today), to = new Date(today);
  if (name === "month") {
    from = new Date(today.getFullYear(), today.getMonth(), 1);
  } else if (name === "lastMonth") {
    from = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    to = new Date(today.getFullYear(), today.getMonth(), 0);
  } else if (name === "last7") {
    from = new Date(today.getTime() - 6 * 86400000);
  } else if (name === "yesterday") {
    from = new Date(today.getTime() - 86400000);
    to = new Date(from);
  }
  $("dfrom").value = fmt(from);
  $("dto").value = fmt(to);
}

function parseDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((s || "").trim());
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return isNaN(d.getTime()) ? null : d;
}

/** 把用户选的两个日期换算成各接口需要的形态 */
function buildRange() {
  const from = parseDate($("dfrom").value);
  const to = parseDate($("dto").value);
  if (!from || !to) { log("日期格式不对，请用 2026-10-01 这种写法"); return null; }
  if (from > to) { log("起始日期不能晚于结束日期"); return null; }

  const endNext = new Date(to.getTime() + 24 * 3600 * 1000); // 多数接口的 end 是开区间

  return {
    startMs: from.getTime(),
    endMs: new Date(to.getFullYear(), to.getMonth(), to.getDate(), 23, 59, 59, 999).getTime(),
    startDate: fmt(from),
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
document.querySelectorAll(".presets button").forEach((b) => {
  b.addEventListener("click", () => applyPreset(b.dataset.preset));
});

// ---------------- 定时设置 ----------------

function renderSchedule(s) {
  if (!s) return;
  $("wOn").checked = !!s.weekly?.enabled;
  $("wDay").value = String(s.weekly?.weekday ?? 1);
  $("wHour").value = s.weekly?.hour ?? 9;
  $("mOn").checked = !!s.monthly?.enabled;
  $("mDay").value = s.monthly?.day ?? 1;
  $("mHour").value = s.monthly?.hour ?? 9;
  const lr = s.lastRun || {};
  $("lastRun").textContent = `上次自动运行：每周 ${lr.weekly || "从未"}｜每月 ${lr.monthly || "从未"}`;
}

function saveSchedule() {
  const schedule = {
    weekly: { enabled: $("wOn").checked, weekday: +$("wDay").value, hour: +$("wHour").value },
    monthly: { enabled: $("mOn").checked, day: +$("mDay").value, hour: +$("mHour").value },
  };
  chrome.runtime.sendMessage({ type: "setSchedule", schedule }, (r) => {
    if (r && r.ok) {
      renderSchedule(r.schedule);
      log(`定时已保存：每周 ${schedule.weekly.enabled ? "开" : "关"}｜每月 ${schedule.monthly.enabled ? "开" : "关"}`);
    }
  });
}

chrome.runtime.sendMessage({ type: "getSchedule" }, (r) => { if (r && r.ok) renderSchedule(r.schedule); });
["wOn", "wDay", "wHour", "mOn", "mDay", "mHour"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("change", saveSchedule);
});

// ---------------- 自绘日历（中文，不受浏览器语言影响） ----------------

const WD = ["日", "一", "二", "三", "四", "五", "六"];
let calTarget = null;     // 当前在给哪个输入框选日期
let calMonth = null;      // 当前显示的月份

function openCal(inputId) {
  calTarget = inputId;
  calMonth = parseDate($(inputId).value) || new Date();
  calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth(), 1);
  renderCal();
  $("cal").classList.remove("hidden");
}

function closeCal() {
  $("cal").classList.add("hidden");
  calTarget = null;
}

function renderCal() {
  const y = calMonth.getFullYear();
  const m = calMonth.getMonth();
  const first = new Date(y, m, 1);
  const days = new Date(y, m + 1, 0).getDate();
  const lead = first.getDay();
  const sel = calTarget ? parseDate($(calTarget).value) : null;
  const today = new Date();

  const parts = [];
  parts.push(`<div class="cal-head">
      <button data-nav="-1">‹</button>
      <span>${y} 年 ${m + 1} 月</span>
      <button data-nav="1">›</button>
    </div>`);
  parts.push('<div class="cal-grid">');
  WD.forEach((w) => parts.push(`<div class="wd">${w}</div>`));
  for (let i = 0; i < lead; i++) parts.push('<div class="day empty"></div>');
  for (let d = 1; d <= days; d++) {
    const cur = new Date(y, m, d);
    const isSel = sel && cur.getFullYear() === sel.getFullYear()
      && cur.getMonth() === sel.getMonth() && cur.getDate() === sel.getDate();
    const isToday = cur.getFullYear() === today.getFullYear()
      && cur.getMonth() === today.getMonth() && cur.getDate() === today.getDate();
    const cls = `day${isSel ? " sel" : ""}${isToday ? " today" : ""}`;
    parts.push(`<div class="${cls}" data-day="${d}">${d}</div>`);
  }
  parts.push("</div>");

  const cal = $("cal");
  cal.innerHTML = parts.join("");
  cal.querySelectorAll("[data-nav]").forEach((b) => {
    b.addEventListener("click", () => {
      calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + (+b.dataset.nav), 1);
      renderCal();
    });
  });
  cal.querySelectorAll("[data-day]").forEach((b) => {
    b.addEventListener("click", () => {
      const d = new Date(calMonth.getFullYear(), calMonth.getMonth(), +b.dataset.day);
      if (calTarget) $(calTarget).value = fmt(d);
      closeCal();
    });
  });
}

["dfrom", "dto"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("focus", () => openCal(id));
  el.addEventListener("click", () => openCal(id));
});
