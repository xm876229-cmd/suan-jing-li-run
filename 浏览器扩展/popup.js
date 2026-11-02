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
  // 「已识别店铺」这一行由 identifyCurrent 负责写（那是店铺身份，不是域名数量）。
  // 这里只在还没识别出来时给个占位，别把店名覆盖成「N 个域名」。
  const hosts = state.learnedHosts || [];
  if ($("hosts") && !$("hosts").dataset.shop) {
    $("hosts").textContent = hosts.length ? "未识别" : "还没识别";
  }
  if ($("hosts")) {
    $("hosts").title = hosts.length ? "已学到的域名：\n" + hosts.join("\n") : "还没学到域名";
  }

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
        : "") +
      (calc.missing && calc.missing.length
        ? `<br><span style="color:#C0392B">⚠ 本轮没有${escapeHtml(calc.missing.join("、"))}数据，` +
          `这部分按 0 计，净利偏高</span>`
        : "") +
      (calc.reused && calc.reused.length
        ? `<br><span class="lbl">复用今日已取</span> ${escapeHtml(calc.reused.join("、"))}`
          + `<span class="lbl">（同区间同一天，没重新导）</span>`
        : "");
    box.title = calc.file || "";
    return;
  }

  box.className = "result hidden";
}

function taskName(t) {
  return ({ all: "取数+核算", orders: "订单取数", sku: "商品SKU", affiliate: "联盟佣金",
            ads: "广告", manualAds: "广告（手动导出）", calc: "核算" })[t] || t;
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
    .then((d) => {
      el.textContent = d.ok ? "已连接" : "无响应";
      el.className = "v " + (d.ok ? "ok" : "bad");
      checkExtVersion(d && d.extVersion);
    })
    .catch(() => { el.textContent = "未启动"; el.className = "v bad"; });
}

/**
 * 扩展"内存里跑的版本" vs "磁盘上的版本"。
 *
 * 为什么需要：改完代码只刷新页面、没点扩展的「重新加载」时，
 * 扩展页上显示的版本号已经是新的（读的是磁盘 manifest），
 * 但跑的还是旧代码——日志里就会冒出 old 版本号，看着像"多个版本混跑"。
 * 这个横幅直接把差异摆出来，点一下自己重载。
 */
function checkExtVersion(diskVer) {
  const box = $("vermiss");
  if (!box) return;
  const runVer = (chrome.runtime.getManifest() || {}).version || "";
  if (!diskVer || !runVer || diskVer === runVer) {
    box.classList.add("hidden");
    return;
  }
  $("vm-run").textContent = runVer;
  $("vm-disk").textContent = diskVer;
  box.classList.remove("hidden");
  const btn = $("vm-reload");
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.onclick = () => {
      chrome.runtime.reload();
      window.close();
    };
  }
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
  saveStore();

  // 以前这里一次点击会做三件事：投桥队列、写扩展存储队列、再直接发消息启动。
  // 直接启动先把任务跑完了，可队列里还留着两份 —— 后台每分钟来领一次，
  // 于是同一份任务又跑两遍（实测一轮 41 秒，会被连跑三轮）。
  // 现在只走「先直接启动，失败才入队兜底」这一条路。
  // 「强制重新取数」：默认关。关着的时候，今天已经导过的同区间数据会直接复用
  // （改完佣金费率再算一遍就不用再等导出）；要拿最新数据就勾上。
  const force = !!( $("forceFetch") && $("forceFetch").checked );
  const store = currentStore();
  const countryNow = ($("country").value || "").trim();

  const enqueue = (why) => {
    // 兜底通道一：投到本地桥，后台每分钟来取一次（走普通 HTTP，不依赖消息机制）
    try {
      fetch("http://127.0.0.1:8765/pending", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task, range, force, store, country: countryNow,
                               withCalc: true, at: Date.now() }),
      }).catch(() => {});
    } catch (e) { /* 桥没开就算了 */ }

    // 兜底通道二：写进扩展存储，后台每分钟来领（桥没开时也能生效）
    try {
      chrome.storage.local.get({ pendingQueue: [] }, (s0) => {
        const prev = Array.isArray(s0 && s0.pendingQueue) ? s0.pendingQueue : [];
        const job = { task, range, force, store, country: countryNow, at: Date.now() };
        chrome.storage.local.set({ pendingQueue: prev.concat([job]) });
      });
    } catch (e) { onContextLost(); }

    if (btn) btn.disabled = false;
    log(`⚠ ${why}，任务已入队，1 分钟内会自动开始`);
  };

  try {
    safeSend({ type: "ping" }, (pr) => {
      if (!pr || !pr.ok) { enqueue("后台没响应消息"); return; }
      log(`后台在线 v${pr.version}，提交任务…`);
      safeSend({ type: "runTask", task, range, force, store, country: countryNow }, (resp) => {
        if (btn) btn.disabled = false;
        let lasterr = null;
        try { lasterr = chrome.runtime.lastError; } catch (e) { /* 上下文已失效 */ }
        if (lasterr) { enqueue("消息通道断了"); return; }
        if (resp && resp.started) log(`已启动：${label}，进度看下方日志`);
        else enqueue("后台没有接手");
      });
    });
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

// ---------------- 店铺 / 国家 ----------------
// 不同店铺在不同国家，所以界面上选「店铺」，国家自动跟着走。
// 店铺清单来自本地桥（扩展读不到磁盘文件），桥没开时退化成手填国家。

function currentStore() {
  return $("store") ? $("store").value : "";
}

function saveStore() {
  const store = currentStore();
  const country = ($("country").value || "").trim() || "马来西亚";
  chrome.storage.local.get({ settings: {} }, (s) => {
    chrome.storage.local.set({ settings: { ...(s.settings || {}), store, country } });
  });
}

/** 选了店铺就把国家填成该店所属国家（仍可手改，手改后以手填为准）。 */
function syncCountryFromStore() {
  const sel = $("store");
  if (!sel || !$("country")) return;
  const opt = sel.selectedOptions && sel.selectedOptions[0];
  const c = opt && opt.dataset ? opt.dataset.country : "";
  if (c) $("country").value = c;
}

/** 从本地桥拉店铺清单。桥没开时只剩「按国家算」，不至于让面板报错。 */
async function loadStores() {
  const sel = $("store");
  if (!sel) return;

  let data = null;
  try {
    const resp = await fetch("http://127.0.0.1:8765/stores");
    data = await resp.json();
  } catch (e) {
    data = null;
  }

  // 站点档案顺手存一份：后台按它决定「这家站要不要跳过规格接口」。
  // 存在扩展存储里，后台随时能读到，不用每次都问本地程序。
  if (data && data.sites) {
    chrome.storage.local.set({ sites: data.sites });
  }

  const hasStores = !!(data && data.ok && data.stores && data.stores.length);
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = hasStores ? "（不指定店铺，按国家算）" : "（桥未连接，按国家算）";
  sel.appendChild(blank);

  if (hasStores) {
    for (const s of data.stores) {
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = `${s.name}（${s.country}）`;
      o.dataset.country = s.country || "";
      sel.appendChild(o);
    }
  }

  // 国家做成下拉：本地程序认得的站点是固定的，手打容易打错一个字，
  // 而国家错 = 费率、汇率、佣金比例全错，算出来的净利看着正常其实是错的。
  const cs = $("country");
  if (cs && cs.tagName === "SELECT") {
    const list = (data && data.countries && data.countries.length)
      ? data.countries.slice()
      : ["马来西亚", "菲律宾", "泰国", "越南", "新加坡", "印尼",
         "美国", "英国", "德国", "法国", "意大利", "西班牙"];
    const keep = cs.value;
    cs.innerHTML = "";
    for (const c of list) {
      const o = document.createElement("option");
      o.value = c;
      o.textContent = c;
      cs.appendChild(o);
    }
    // 存下来的国家如果不在清单里（老配置/手填过），也保留，别把它的值弄丢
    if (keep && !list.includes(keep)) {
      const o = document.createElement("option");
      o.value = keep;
      o.textContent = keep;
      cs.appendChild(o);
    }
    if (keep) cs.value = keep;
  }

  // 必须等这个回调跑完再返回：它会把上次记住的国家填回输入框，
  // 如果不等就返回，调用方刚设好的「识别出来的国家」会被它覆盖成旧值。
  await new Promise((resolve) => {
    chrome.storage.local.get({ settings: {} }, (st) => {
      const set = st.settings || {};
      if (set.store && [...sel.options].some((o) => o.value === set.store)) {
        sel.value = set.store;
        syncCountryFromStore();
      } else if (set.country) {
        $("country").value = set.country;
      } else if (data && data.default) {
        $("country").value = data.default;
      }
      resolve();
    });
  });
}

/**
 * 问后台「现在这个浏览器登的是哪家店」。
 *
 * 为什么要自动问：店铺清单来自本地程序里已经建档的店铺，
 * 一家新店从没取过数，就永远不在下拉里——用户会觉得「又得手工加一次」。
 * 这里打开面板就识别，认出来就地建档、带出国家、补进下拉。
 */
async function identifyCurrent(silent) {
  const row = $("hosts");
  if (!row) return null;
  if (!silent) row.textContent = "识别中…";
  const resp = await new Promise((resolve) => {
    const ok = safeSend({ type: "identifyNow" }, (r) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(r || { ok: false, error: "后台没响应" });
    });
    if (!ok) resolve({ ok: false, error: "面板已过期" });
  });

  if (resp && resp.ok && resp.shop && resp.shop.shop_id) {
    const s = resp.shop;
    const c = resp.country || "";
    row.textContent = `${s.shop_name || s.shop_id}${c ? "·" + c : ""}`;
    row.className = "v ok";
    row.dataset.shop = s.shop_id;
    row.title = `shop_id：${s.shop_id}\n站点：${s.region || "?"}\n${c ? "国家：" + c : ""}`;
    // 顺序很关键：先刷店铺清单（新店这时才进下拉），
    // 再选店 + 填国家，最后一起存起来。
    // 反过来的话，loadStores 里回填的旧国家会把刚识别出的国家盖掉——
    // 菲律宾的店就会顶着「马来西亚」的费率去算。
    await loadStores();
    const sel = $("store");
    if (sel && [...sel.options].some((o) => o.value === s.shop_id)) {
      sel.value = s.shop_id;
    }
    if (c && $("country")) {
      $("country").value = c;
    } else {
      syncCountryFromStore();
    }
    saveStore();
    return s;
  }

  row.textContent = "未识别";
  row.className = "v bad";
  delete row.dataset.shop;
  row.title = (resp && resp.error) || "";
  // 无论是不是静默识别，都把原因写进日志：面板只显示"未识别"，
  // 没有原因就只能靠猜（以前只有手动点「重新识别」时才记）。
  if (resp && resp.error) log(`店铺识别失败：${resp.error}`);
  return null;
}

$("reident") && $("reident").addEventListener("click", () => identifyCurrent(false));

$("store") && $("store").addEventListener("change", () => {
  syncCountryFromStore();
  saveStore();
});
$("country") && $("country").addEventListener("change", saveStore);
// 面板一打开就先把"上次识别到的店铺"画出来，不等任何请求。
// 之后再问一次（后台若有更新会覆盖），体感就是"秒出店名"。
try {
  chrome.storage.local.get({ currentShop: null }, ({ currentShop }) => {
    const row = $("hosts");
    if (!row || !currentShop || !currentShop.shop_id) return;
    const c = currentShop.country || "";
    row.textContent = `${currentShop.shop_name || currentShop.shop_id}${c ? "·" + c : ""}`;
    row.className = "v ok";
    row.dataset.shop = currentShop.shop_id;
    row.title = `shop_id：${currentShop.shop_id}\n站点：${currentShop.region || "?"}`;
    const sel = $("store");
    if (sel && [...sel.options].some((o) => o.value === currentShop.shop_id)) {
      sel.value = currentShop.shop_id;
    }
  });
} catch (e) { /* 忽略 */ }

// 先拉店铺清单，再问一次「现在这家店是谁」：
// 新店就是靠这一步自动建档的，用户不需要先手工加店铺
loadStores().then(() => identifyCurrent(true));

$("calcOnly") && $("calcOnly").addEventListener("click", (e) => {
  const range = buildRange();
  if (!range) { log("请先选日期"); return; }
  saveStore();
  const country = ($("country").value || "").trim() || "马来西亚";
  const store = currentStore();
  e.target.disabled = true;
  log(`=== 只算净利：${country} ${range.startDate} ~ ${$("dto").value} ===`);
  safeSend({ type: "calcNow", range, country, store }, (resp) => {
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
// 注意：这里必须限定 [data-task]。grid 里还有「抓广告导出接口」这种
// 走自己一套逻辑的按钮，用通配选择器会让它们的 dataset.task 是 undefined，
// 于是后台报「不支持的取数任务：undefined」。
document.querySelectorAll(".grid button[data-task]").forEach((b) => {
  b.addEventListener("click", () => runTasks(b.dataset.task, b));
});
document.querySelectorAll(".presets button").forEach((b) => {
  b.addEventListener("click", () => applyPreset(b.dataset.preset));
});

// ---------------- 定时设置 ----------------

function renderSchedule(s) {
  // 面板上的定时设置已撤掉（交给程序的「计划任务」），
  // 这些元素不在时直接跳过，别让一个过期的回调把整个面板脚本搞停。
  if (!s || !$("wOn")) return;
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
  if (!$("wOn")) return;
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

// ---------------- 抓广告导出接口 ----------------
// 广告导出的「创建」接口在 ads 域，之前抓不到是因为抓包时人不在广告页面上。
// 这一步需要人配合：在广告页面点一次「导出」，抓到的请求体会单独落到
// data/capture/ads_export.jsonl，之后就能照着复现，不再需要人点。

const capAdsBtn = $("capAds");
if (capAdsBtn) {
    capAdsBtn.addEventListener("click", () => {
      capAdsBtn.disabled = true;
      log("开始监听广告请求…（现在去店铺的「广告」页面点一次「导出」，监听是全局的，切页不影响）");
      safeSend({ type: "captureAds", seconds: 240 }, (r) => {
        capAdsBtn.disabled = false;
        if (!r) { log("没有响应"); return; }
        if (r.ok) {
          const n = r.result?.requests ?? 0;
          log(n ? `抓到 ${n} 条广告请求，已存到 data/capture/ads_export.jsonl` : "这轮没抓到广告请求，请确认在广告页面点的导出");
        } else {
          log(`失败：${r.error}`);
        }
      });
    });
}

// ---------------- 手动导数据 ----------------
// 自动取数挂掉时的兜底：自己去店铺后台点导出。
// 两种模式：
//   陪同导出 —— 打开页面后顺便盯着浏览器下载，文件一下载就自动存进这家店
//   只开页面 —— 只帮你把页面打开，导出完到桌面端「手动导入」上传
// 为什么保留手动这条路：平台接口一改、一掉登录、弹风控，自动那条路就断，
// 但人总还能在页面上把表导出来。

const MANUAL_PAGES = {
  orders: { url: "https://seller-my.tiktok.com/", label: "卖家后台（订单管理）" },
  sku: { url: "https://seller-my.tiktok.com/", label: "卖家后台（商品管理）" },
  affiliate: { url: "https://affiliate.tiktok.com/affiliate/platform/homepage", label: "达人联盟后台" },
  ads: { url: "https://seller-my.tiktok.com/", label: "卖家后台（广告）" },
};

function updateManualHint() {
  const h = $("mHint");
  if (!h) return;
  const cap = $("mCapture") && $("mCapture").checked;
  h.textContent = cap
    ? "点下面的按钮会打开店铺页面；你在页面上点导出，文件一下载就自动存进这家店并生成快照。"
    : "点下面的按钮只打开店铺页面；导出后到桌面端「手动导入」上传，导入时要选国家和店铺。";
}

document.querySelectorAll("[data-manual]").forEach((b) => {
  b.addEventListener("click", () => {
    const kind = b.dataset.manual;
    const cap = !!( $("mCapture") && $("mCapture").checked );
    b.disabled = true;
    safeSend({ type: "manualExport", kind: kind, capture: cap }, (r) => {
      b.disabled = false;
      if (!r) { log("后台没响应，扩展可能刚被刷新，关掉面板重开一次"); return; }
      if (r.ok) {
        log(`已打开${r.label || kind}${cap ? "，现在在页面上点导出，我在这儿盯着下载" : ""}`);
      } else {
        log("失败：" + (r.error || "未知原因"));
      }
    });
  });
});

if ($("mCapture")) $("mCapture").onchange = updateManualHint;
updateManualHint();

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
