// 常驻自检：订单待补记录 & 导出记录挑选。
//
// 两条铁律，破坏任何一条都会静默出错：
//   ① `pickOrderRecord` **只认本次任务号**，绝不拿平台上「以前那份」——
//      那样拿到的是上次那一刻的快照，中间退的款/取消的单全看不到。
//      （2026-11-12 我一度加过「改用同区间已生成的任务」，实测两次订单
//       文件字节数完全相同，就是旧快照，已回滚。）
//   ② `mergePendingRecord` 同店同区间**用最新任务号覆盖旧的**，
//      否则「运行→停止→运行」会一直等最早那个任务（实测白等 9 分钟）。
//
//   用法：node scripts/check_order_pick.js     退出码 0 = 全过
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..");

const src = fs.readFileSync(path.join(ROOT, "浏览器扩展", "background.js"), "utf8");

function cut(name, text) {
  const at = text.indexOf(`function ${name}(`);
  if (at < 0) return "";
  const rest = text.slice(at);
  const m = rest.match(/\r?\n\}\r?\n/);
  return m ? rest.slice(0, m.index + m[0].length) : "";
}

const pickSrc = cut("pickOrderRecord", src);
const mergeSrc = cut("mergePendingRecord", src);
if (!pickSrc || !mergeSrc) { console.error("找不到 pickOrderRecord / mergePendingRecord"); process.exit(1); }

// sameRange 直接复制源码里的实现（保证和线上一致）
const sameRangeSrc = cut("sameRange", src);
if (!sameRangeSrc) { console.error("找不到 sameRange"); process.exit(1); }

const sandbox = `
${sameRangeSrc}
${pickSrc}
${mergeSrc}
return { pickOrderRecord, mergePendingRecord };
`;
const { pickOrderRecord, mergePendingRecord } =
  new Function("shiftDate", sandbox)((d, n) => {
    const t = new Date(d + "T00:00:00"); t.setDate(t.getDate() + n);
    return t.toISOString().slice(0, 10);
  });

const okKey = (x) => !!(x && x.file_key && !/^\d+$/.test(String(x.file_key)));
const R = { startDate: "2026-10-13", to: "2026-11-11" };
const done = (tid, a = "20261013", b = "20261111") =>
  ({ export_task_id: tid, file_name: `全部 笔订单-${a}-${b}-2026-11-12-10_25`, file_key: "b595bc8c907f427bsafe" });
const pending = (tid, a = "20261013", b = "20261111") =>
  ({ export_task_id: tid, file_name: `全部 笔订单-${a}-${b}-2026-11-12-10_25`, file_key: "" });

let bad = 0;
const eq = (tag, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗ ${tag}：期望 ${JSON.stringify(want)}，实际 ${JSON.stringify(got)}`); }
  else console.log(`  ✓ ${tag}`);
};

console.log("— pickOrderRecord（只认本次任务号）—");
eq("自己的任务已生成 → 命中",
   !!pickOrderRecord([done("8132346875223984900")], { taskId: "8132346875223984900" }, okKey), true);
eq("自己的还没好，别的同区间已生成 → 不命中（关键）",
   pickOrderRecord([done("8132345590356266500")], { taskId: "8132346875223984900", range: R }, okKey), null);
eq("file_key 是纯数字 → 不算生成好",
   pickOrderRecord([{ export_task_id: "8132346875223984900", file_key: "8132346875223984900" }],
                   { taskId: "8132346875223984900" }, okKey), null);
eq("列表空 → 不命中", pickOrderRecord([], { taskId: "1" }, okKey), null);
eq("记录里有多条，只挑自己那个",
   (pickOrderRecord([done("A1"), done("B2")], { taskId: "B2" }, okKey) || {}).export_task_id, "B2");

console.log("\n— mergePendingRecord（同店同区间用最新号覆盖）—");
const base = { shop_id: "S1", shop_name: "店一", range: R, taskId: "OLD", at: 1000 };
const item = { shop_id: "S1", shop_name: "店一", range: R, taskId: "NEW", at: 2000 };
let m = mergePendingRecord([base], item);
eq("同店同区间 → 只剩 1 条", m.length, 1);
eq("任务号换成最新的", m[0].taskId, "NEW");

m = mergePendingRecord([base], Object.assign({}, item, { range: { startDate: "2026-10-01", to: "2026-10-31" } }));
eq("区间不同 → 两条都留", m.length, 2);

m = mergePendingRecord([base], Object.assign({}, item, { shop_id: "S2" }));
eq("不同店 → 两条都留", m.length, 2);

m = mergePendingRecord([base], Object.assign({}, item, { taskId: "OLD", at: 3000 }));
eq("同一个任务号重复登记 → 不新增", m.length, 1);

m = mergePendingRecord([], item);
eq("空列表 → 加一条", m.length, 1);

m = mergePendingRecord([base, { shop_id: "S9", shop_name: "别家", range: R, taskId: "X", at: 500 }], item);
eq("别家的记录不动", m.length, 2);
eq("别家的任务号没被改", m.find((x) => x.shop_id === "S9").taskId, "X");

if (bad) { console.log(`\n${bad} 项不符合`); process.exit(1); }
console.log("\n全部通过：任务号只认本次、待补记录用最新号覆盖");
