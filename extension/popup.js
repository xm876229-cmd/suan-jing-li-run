const $ = (id) => document.getElementById(id);
const logEl = $("log");

// 扩展被刷新/重载后，旧面板的 chrome.runtime 会失效，此时所有调用都会静默失败。
// 一打开就检查，比等用户点了没反应再猜要好。
(function checkContextOnce() {
  try {
    if (!(chrome && chrome.runtime && chrome.runtime.id)) {
      setTimeout(() => onContextLost(), 0);
    }
  } catch (e) {
    setTimeout(() => onContextLost(), 0);
  }
})();

function contextAlive() {
  try {
    return !!(chrome && chrome.runtime && chrome.runtime.id);
  } catch (e) {
    return false;
  }
}

/** 安全发送：上下文失效时抛的是同步异常，必须 try/catch，
 *  否则回调不执行、按钮卡在禁用态，看起来就是"点了没反应"。 */
function safeSend(msg, cb) {
  if (!contextAlive()) {
    onContextLost();
    return false;
  }
  try {
    chrome.runtime.sendMessage(msg, (resp) => {
      if (!contextAlive()) { onContextLost(); return; }
      if (cb) cb(resp);
    });
    return true;
  } catch (e) {
    onContextLost();
    return false;
  }
}

let contextLostShown = false;
function onContextLost() {
  if (contextLostShown) return;
  contextLostShown = true;
  log("⚠ 面板已过期（扩展刚被刷新/重载）。请关掉这个面板，重新点图标打开。");
  document.querySelectorAll("button").forEach((b) => { b.disabled = false; });
}

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

  renderResult(state);
}

/** 结果区：正在跑什么、上次算出来多少。这两个都从扩展存储读，
 *  所以面板关掉再打开也能看到上次的结果，不依赖消息响应。 */
function renderResult(state) {
  const box = $("result");
  if (!box) return;

  const run = state.running;
  if (run) {
    const secs = Math.round((Date.now() - (run.at || Date.now())) / 1000);
    box.className = "result";
    box.innerHTML = `<span class="lbl">正在运行</span> <b>${taskName(run.task)}</b> <span class="lbl">已 ${secs} 秒</span>`;
    return;
  }

  const res = state.lastResult;
  const calc = state.lastCalc;
  if (res && res.ok === false) {
    box.className = "result bad";
    box.innerHTML = `<span class="lbl">${taskName(res.task)}失败</span><br>${escapeHtml(res.error || "")}`;
    return;
  }
  if (calc) {
    const margin = (calc.net_margin * 100).toFixed(2);
    box.className = "result";
    box.innerHTML =
      `<span class="lbl">净利（${escapeHtml(calc.country)} ${escapeHtml(calc.period)}）</span><br>` +
      `<b>${fmtNum(calc.net_local)}</b> <span class="lbl">${escapeHtml(calc.country)}</span>` +
      `　<span class="lbl">人民币</span> <b>${fmtNum(calc.net_cny)}</b><br>` +
      `<span class="lbl">GMV</span> ${fmtNum(calc.gmv)}　` +
      `<span class="lbl">净利率</span> ${margin}%　` +
      `<span class="lbl">商品</span> ${calc.products}　` +
      `<span class="lbl">异常</span> ${calc.issues}` +
      (calc.stale && calc.stale.length
        ? `<br><span style="color:#C0392B">⚠ 有数据文件是旧的，结果可能偏离：</span><br>` +
          calc.stale.map((x) => `<span style="color:#C0392B">${escapeHtml(x)}</span>`).join("<br>")
        : "");
    box.title = calc.file || "";
    return;
  }

  box.className = "result hidden";
}

function taskName(t) {
  return ({ all: "取数+核算", orders: "订单取数", sku: "商品SKU", affiliate: "联盟佣金", ads: "广告", calc: "核算" })[t] || t;
}

function fmtNum(v) {
  return (v == null ? 0 : Number(v)).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
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

const RANGE_KEY = "lastRange";

/** 记忆上次选的区间：面板一关就重置很烦，尤其广告导出要反复核对日期 */
function saveRange() {
  const from = $("dfrom").value.trim();
  const to = $("dto").value.trim();
  if (from && to) chrome.storage.local.set({ [RANGE_KEY]: { from, to } });
}

async function initDates() {
  const { [RANGE_KEY]: saved } = await chrome.storage.local.get({ [RANGE_KEY]: null });
  if (saved && saved.from && saved.to) {
    $("dfrom").value = saved.from;
    $("dto").value = saved.to;
  } else {
    applyPreset("month");
  }
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
  saveRange();
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

  // 后台收到就立刻回执，任务在它自己的上下文里跑完为止。
  // 以前是等任务结束才回，任务一跑几分钟，面板一关消息通道就断了，
  // 用户看到的是「没有响应」——那不是任务失败，是回执方式错了。
  saveCountry();

  // 双通道：先把任务写进扩展存储（后台每分钟会来领），再发消息抢先执行。
  // 消息这条偶尔会断——断的时候存储那条兜住，任务照样跑。
  const force = task === "sku";
  try {
    chrome.storage.local.set(
      { pendingTask: { task, range, force, at: Date.now() } },
      () => {
        safeSend({ type: "ping" }, (pr) => {
          if (!pr || !pr.ok) {
            if (btn) btn.disabled = false;
            log("⚠ 后台没响应消息，但任务已入队，1 分钟内会自动开始");
            return;
          }
          log(`后台在线 v${pr.version}，提交任务…`);
          safeSend({ type: "runTask", task, range, force }, (resp) => {
            if (btn) btn.disabled = false;
            let lasterr = null;
            try { lasterr = chrome.runtime.lastError; } catch (e) { /* 上下文已失效 */ }
            if (lasterr) {
              log(`消息通道断了，任务已入队，1 分钟内自动开始（${lasterr.message}）`);
              return;
            }
            if (resp && resp.started) log(`已启动：${label}，进度看下方日志`);
            else log("已入队，1 分钟内自动开始");
          });
        });
      },
    );
  } catch (e) {
    onContextLost();
  }

  // 8 秒后回头看一眼：真跑起来了就会有 running 标记，没有就说明还在排队
  setTimeout(() => {
    safeSend({ type: "state" }, (s) => {
      if (!s || !s.running) log("（还没开始执行，最多等 1 分钟由后台自动领取）");
    });
  }, 8000);
}

function saveCountry() {
  const country = ($("country").value || "").trim() || "马来西亚";
  chrome.storage.local.get({ settings: {} }, (s) => {
    chrome.storage.local.set({ settings: { ...(s.settings || {}), country } });
  });
}

chrome.storage.local.get({ settings: {} }, (s) => {
  $("country").value = (s.settings && s.settings.country) || "马来西亚";
});
$("country") && $("country").addEventListener("change", saveCountry);

$("calcOnly") && $("calcOnly").addEventListener("click", (e) => {
  const range = buildRange();
  if (!range) { log("请先选日期"); return; }
  saveCountry();
  const country = ($("country").value || "").trim() || "马来西亚";
  e.target.disabled = true;
  log(`=== 只算净利：${country} ${range.startDate} ~ ${$("dto").value} ===`);
  safeSend({ type: "calcNow", range, country }, (resp) => {
    e.target.disabled = false;
    if (chrome.runtime.lastError) { log(`后台未响应：${chrome.runtime.lastError.message}`); return; }
    if (resp && resp.started) log("已开始核算，用现有导出文件算，不重新取数");
  });
});

// 打开面板先探一次后台：通了说明「面板 → 后台」这条路是活的，
// 不通就是面板过期或后台没起来，不用等用户点按钮才发现。
safeSend({ type: "ping" }, (r) => {
  const v = r && r.ok ? r.version : "";
  if ($("ver")) $("ver").textContent = v ? `v${v}` : "";
  if (!r || !r.ok) { log("⚠ 后台没有响应，扩展可能没加载好"); return; }
  log(`后台在线 v${v}`);
});

safeSend({ type: "state" }, (s) => { if (s) render(s); });
checkBridge();

// 面板不会自己刷新，这里每秒拉一次状态，否则看着像"数字不动"
setInterval(() => {
  safeSend({ type: "state" }, (s) => { if (s) render(s); });
}, 1200);
initDates();

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "taskLog") log(msg.msg);
});

// 面板关掉会丢日志，这里从扩展存储里把上次的记录捞回来
safeSend({ type: "getLog" }, (r) => {
  if (!r || !r.ok || !r.logs || !r.logs.length) return;
  const head = document.createElement("div");
  head.textContent = "—— 上次的记录 ——";
  head.style.color = "#8A99A8";
  logEl.appendChild(head);
  r.logs.slice(-60).forEach((line) => {
    const d = document.createElement("div");
    d.textContent = line;
    logEl.appendChild(d);
  });
  logEl.scrollTop = logEl.scrollHeight;
});

$("toggle").addEventListener("click", () => {
  safeSend({ type: "state" }, (s) => {
    const next = !(s && s.enabled);
    safeSend({ type: "toggle", enabled: next }, () => {
      safeSend({ type: "state" }, (s2) => { if (s2) render(s2); });
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
  safeSend({ type: "setSchedule", schedule }, (r) => {
    if (r && r.ok) {
      renderSchedule(r.schedule);
      log(`定时已保存：每周 ${schedule.weekly.enabled ? "开" : "关"}｜每月 ${schedule.monthly.enabled ? "开" : "关"}`);
    }
  });
}

safeSend({ type: "getSchedule" }, (r) => { if (r && r.ok) renderSchedule(r.schedule); });
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
      saveRange();
    });
  });
}

["dfrom", "dto"].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener("focus", () => openCal(id));
  el.addEventListener("click", () => openCal(id));
  el.addEventListener("change", saveRange);
  el.addEventListener("blur", saveRange);
});

// ---------------- 深度抓包 ----------------

const deepBtn = $("deep");
if (deepBtn) {
  deepBtn.addEventListener("click", () => {
    deepBtn.disabled = true;
    log("正在挂载调试器（顶部会出现“正在调试”提示条，属正常）…");
    safeSend({ type: "deepCapture", seconds: 300 }, (r) => {
      deepBtn.disabled = false;
      if (!r) { log("没有响应"); return; }
      if (r.ok) log(`深度抓包已开始（挂载 ${r.result?.tabs ?? "?"} 个标签页），现在去点一次广告导出，5 分钟后自动结束`);
      else log(`失败：${r.error}`);
    });
  });
}
