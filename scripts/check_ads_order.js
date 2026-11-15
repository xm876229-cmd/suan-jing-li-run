// 离线跑 TASKS.ads 的真身：把 background.js 里那段函数体抠出来，
// 注入假依赖，看它到底按什么顺序取数、失败后落到哪。
// 不需要浏览器，只验调度逻辑（顺序 / 兜底 / 站点档案）。
// 常驻自检：真跑一遍「广告取数」的调度（不是只看字符串顺序）。
//
// 把 background.js 里 TASKS.ads 的函数体抠出来，注入假依赖，逐场景断言
// 调用顺序和兜底落点。为什么值得单独做：顺序写反了，代码照样能跑、
// 报表照样出，只是广告费按 LOCAL 猜币种 —— 数字错得看不出来。
//
//   用法：node scripts/check_ads_order.js
//   退出码 0 = 全过；1 = 有场景不符合
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..");

const src = fs.readFileSync(path.join(ROOT, "浏览器扩展", "background.js"), "utf8");
const head = "  ads: async (range, log) => {";
const at = src.indexOf(head);
if (at < 0) { console.error("找不到 TASKS.ads"); process.exit(1); }
// 源码是 CRLF，行尾两种都认
const mEnd = src.slice(at).match(/\r?\n  \},\r?\n\};/);
if (!mEnd) { console.error("找不到 TASKS.ads 结尾"); process.exit(1); }
const body = src.slice(at + head.length, at + mEnd.index);

async function runCase(name, opts) {
  const calls = [];
  const fail = (what) => { throw new Error(what + " 失败"); };

  const ensureHost = async () => { calls.push("ensureHost"); return opts.host ?? "seller-my.tiktok.com"; };
  const siteProfile = async () => ({ key: opts.site || "my", ads_spec: opts.adsSpec });
  const exportAdsByApi = async () => {
    calls.push("11列-接口");
    if (opts.api11Fail) fail("11列接口");
    return { via: "导出接口", saved: "a.xlsx" };
  };
  const exportAdsByClickFresh = async () => {
    calls.push("11列-页面");
    if (opts.click11Fail) fail("11列页面");
    return { via: "页面导出", saved: "b.xlsx" };
  };
  const runSpecTask = async (task) => {
    calls.push("4列-规格");
    if (opts.spec4Fail) fail("4列规格");
    return { via: "规格驱动", files: ["c.csv"] };
  };
  const pullAdsDirect = async () => {
    calls.push("4列-直取");
    if (opts.direct4Fail) fail("4列直取");
    return { via: "4 列直取", saved: "d.csv" };
  };
  const runAds = async (r, l, o) => {
    calls.push("查任务" + (o && o.onlyQuery ? "(onlyQuery)" : ""));
    return { pending: false, none: true };
  };
  const log = () => {};

  const fn = new Function(
    "ensureHost", "siteProfile", "exportAdsByApi", "exportAdsByClickFresh",
    "runSpecTask", "pullAdsDirect", "runAds", "log",
    `return (async (range, log) => {${body}});`
  )(ensureHost, siteProfile, exportAdsByApi, exportAdsByClickFresh,
    runSpecTask, pullAdsDirect, runAds, log);

  const res = await fn({ startDate: "2026-11-03", to: "2026-11-09" }, log);
  console.log(`\n[${name}]`);
  console.log("  调用顺序:", calls.join(" → "));
  console.log("  结果:", JSON.stringify(res));
  return { calls, res };
}

(async () => {
  const R = [];
  R.push(await runCase("A 马来：11 列接口成功", {}));
  R.push(await runCase("B 马来：接口失败、页面导出成功", { api11Fail: true }));
  R.push(await runCase("C 马来：11 列都失败 → 4 列规格", { api11Fail: true, click11Fail: true }));
  R.push(await runCase("D 马来：4 列规格也失败 → 4 列直取",
    { api11Fail: true, click11Fail: true, spec4Fail: true }));
  R.push(await runCase("E 全失败 → 只查已完成任务",
    { api11Fail: true, click11Fail: true, spec4Fail: true, direct4Fail: true }));
  R.push(await runCase("F 菲律宾(ads_spec=false)：11 列失败 → 直接查任务",
    { site: "ph", adsSpec: false, api11Fail: true, click11Fail: true }));
  R.push(await runCase("G 认不出域名(host 为空) → 跳过 11 列，直接 4 列",
    { host: "", api11Fail: true, click11Fail: true }));
  R.push(await runCase("H 第一种方式一秒失败(接口立刻报错)", { api11Fail: true }));

  // ---- 断言 ----
  const bad = [];
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  if (!eq(R[0].calls, ["ensureHost", "11列-接口"])) bad.push("A：11 列成功时不该再试 4 列");
  if (!eq(R[1].calls, ["ensureHost", "11列-接口", "11列-页面"])) bad.push("B：页面导出没接上");
  if (!eq(R[2].calls, ["ensureHost", "11列-接口", "11列-页面", "4列-规格"])) bad.push("C：4 列规格兜底没接上");
  if (!eq(R[3].calls, ["ensureHost", "11列-接口", "11列-页面", "4列-规格", "4列-直取"])) bad.push("D：4 列直取兜底没接上");
  // 最后一层是「通用页面导出 + 查任务」（runAds），所以这里只要求收在 runAds 上
  if (R[4].calls[R[4].calls.length - 1] !== "查任务") bad.push("E：最后一层必须是 runAds（通用页面导出/查任务）");
  if (R[5].calls.includes("4列-规格") || R[5].calls.includes("4列-直取")) {
    bad.push("F：ads_spec=false 的站点不该再试 4 列");
  }
  // 有域名时，第一步必须是 11 列（G 那种认不出域名的除外）
  R.slice(0, 6).forEach((x, i) => {
    if (x.calls[1] !== "11列-接口") bad.push(String.fromCharCode(65 + i) + "：第一步不是 11 列");
  });
  // 认不出域名：不能去发 11 列的请求（会拼出空地址），也不能崩
  if (R[6].calls.includes("11列-接口") || R[6].calls.includes("11列-页面")) {
    bad.push("G：host 为空时还在试 11 列");
  }
  if (!R[6].calls.includes("4列-规格")) bad.push("G：host 为空时应该走 4 列规格兜底");
  // H：接口这一步失败后必须继续走（页面导出接上了就停，这是对的）
  if (R[7].calls.join() !== "ensureHost,11列-接口,11列-页面") {
    bad.push("H：接口失败后没继续走下一步兜底");
  }
  if (R[7].res.via !== "页面导出") bad.push("H：结果不对");

  console.log("\n--- 断言 ---");
  if (bad.length) { bad.forEach((b) => console.log("  ✗", b)); process.exit(1); }
  console.log(`  全部通过：${R.length} 种场景的顺序与兜底都符合「11 列优先、4 列兜底」`);
})();
