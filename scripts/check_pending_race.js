// 常驻自检：补订单写回时不能把「刚刚新写进来的任务号」抹掉。
//
// 这条守的是一个静默失败：补订单线程进循环时先读一份 pendingOrders 快照，
// 处理完（每个任务要开页面、发请求，十几秒）再写回。如果写回是拿旧快照整体
// 覆盖，用户在中间点「运行」提交的新任务号就不在快照里，会被当场抹掉——
// 之后一直等上一轮那个可能永远不生成的旧号，订单表停在上一份，报表是旧数据，
// 界面上还看不出问题。2026-11-12 就是这么怀疑过一回（后来查清那轮没中招，
// 但代码结构上这个窗口客观存在：谁先谁后全看时序，不是店铺差异）。
//
// 用法：node scripts/check_pending_race.js     退出码 0 = 全过
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..");
const FILE = path.join(ROOT, "浏览器扩展", "background.js");
const src = fs.readFileSync(FILE, "utf8");

function cut(name, text) {
  const at = text.indexOf(`function ${name}(`);
  if (at < 0) return "";
  const rest = text.slice(at);
  const m = rest.match(/\r?\n\}\r?\n/);
  return m ? rest.slice(0, m.index + m[0].length) : "";
}

const pureSrc = cut("pendingAfterDrain", src);
if (!pureSrc) {
  console.error("✗ 找不到 pendingAfterDrain（补订单差集写回）");
  process.exit(1);
}
const pendingAfterDrain = new Function(`${pureSrc}\nreturn pendingAfterDrain;`)();

let bad = 0;
function eq(tag, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { bad++; console.log(`  ✗ ${tag}：期望 ${w}，实际 ${g}`); }
  else console.log(`  ✓ ${tag}`);
}

const rec = (tid) => ({ taskId: String(tid), shop_id: "7495895952415426722" });
const ids = (list) => (list || []).map((x) => String(x.taskId));

console.log("— pendingAfterDrain（差集写回）—");

// ① 竞态正题：drain 进循环时快照只有 4 条旧任务；处理这十几秒里，
//    用户点了「运行」，rememberPendingOrder 写进第 5 条新任务。
//    处理完写回时，新任务必须还在。
{
  const snapshot = [1, 2, 3, 4].map(rec);
  const newTask = rec(99);                       // 期间新写入
  const curLatest = [newTask, ...snapshot];      // 写回时读到的最新列表
  const gone = new Set();                        // 一个都没收到，继续等
  const keep = snapshot;                         // 旧逻辑会写成这份
  eq("① 处理期间新写入的任务号必须留下",
     ids(pendingAfterDrain(curLatest, gone, keep)),
     ["99", "1", "2", "3", "4"]);
}

// ② 收走一条：只删收走的那条，其余（含新写入的）都留
{
  const curLatest = [rec(99), rec(1), rec(2), rec(3), rec(4)];
  const gone = new Set(["1"]);
  eq("② 只摘掉本次收走的那条", ids(pendingAfterDrain(curLatest, gone, curLatest)),
     ["99", "2", "3", "4"]);
}

// ③ 过期的要摘掉（不然每分钟都白查一遍）
{
  const curLatest = [rec(99), rec(1)];
  const gone = new Set(["1"]);
  eq("③ 过期/域名不对的摘掉", ids(pendingAfterDrain(curLatest, gone, curLatest)), ["99"]);
}

// ④ 读不到最新列表时退回 keep（宁可多留一条，也别抹掉新的）
{
  const keep = [rec(1), rec(2)];
  eq("④ cur 读不到 → 退回 keep",
     ids(pendingAfterDrain(undefined, new Set(), keep)), ["1", "2"]);
  eq("④ cur 是空数组（真的都收完了）→ 空",
     ids(pendingAfterDrain([], new Set(["1"]), [rec(1)])), []);
}

// ⑤ gone 传数组也要认（别把类型写死成 Set）
{
  const curLatest = [rec(1), rec(2)];
  eq("⑤ gone 传数组也能用", ids(pendingAfterDrain(curLatest, ["1"], [])), ["2"]);
}

// ⑥ 纯函数不能修改入参（调用方还要用 keep 兜底）
{
  const keep = [rec(1), rec(2)];
  const copy = JSON.stringify(keep);
  pendingAfterDrain([rec(1), rec(2), rec(3)], new Set(["3"]), keep);
  eq("⑥ 不修改入参", JSON.stringify(keep), copy);
}

// ——— 静态断言：harvestPendingOrdersInner 结尾不许再整体覆盖 ———
console.log("— 源码结构 —");
{
  const at = src.indexOf("async function harvestPendingOrdersInner(");
  const end = src.indexOf("\nasync function ", at + 10);
  const body = at < 0 ? "" : src.slice(at, end > 0 ? end : at + 12000);
  if (!body) {
    bad++; console.log("  ✗ 找不到 harvestPendingOrdersInner");
  } else {
    // 只数真代码：注释里提到老写法是正常的（就是在解释为什么改）
    const code = body.split(/\r?\n/)
      .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
      .join("\n");
    const full = code.match(/skSet\(\{\s*pendingOrders:\s*keep\s*\}\)/g) || [];
    const pure = body.includes("pendingAfterDrain(");
    // 允许出现一次，但只能作为「读不到最新列表」的兜底，前面必须有 try
    if (!pure) { bad++; console.log("  ✗ 结尾没用 pendingAfterDrain（又退回整体覆盖了？）"); }
    else if (full.length > 1) { bad++; console.log(`  ✗ skSet(pendingOrders: keep) 出现 ${full.length} 次，只该剩 catch 里那一次`); }
    else console.log(`  ✓ 结尾走 pendingAfterDrain，整体覆盖只剩 ${full.length} 处兜底`);
  }
}

console.log(bad ? `\n✗ ${bad} 项没通过` : "\n全过");
process.exit(bad ? 1 : 0);
