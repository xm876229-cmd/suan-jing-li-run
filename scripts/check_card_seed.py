"""卡片「补文件名」的常驻自检（基线快照版）。

背景（2026-11-12，两次翻车）：
- 旧版靠**时间窗口**猜"这个文件是不是这一轮落的"（6 小时 → 600 秒 → 5 秒）。
  留大了：上一轮 20 分钟前的文件被当成「这一轮已完成」，点运行 1 秒满屏是勾；
  留小了：这一轮真落的文件补不上，卡片 ✓ 却没有文件名。
- 现在改成**基线快照**：派单那一刻记下磁盘上每类最新的文件，
  之后只补「跟基线不一样」的（名字换了 = 这一轮落的），完全不看时间。

这个脚本真跑四种情形，退出码 0 = 全过。

    python scripts/check_card_seed.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    home = ROOT / "release" / "净利助手"
    if not (home / "数据" / "店铺").is_dir():
        print("跳过：找不到 release/净利助手/数据/店铺")
        return 0
    os.environ["JINGLI_HOME"] = str(home)
    sys.path.insert(0, str(ROOT))
    import bridge.server as S

    # 挑一家「磁盘上有广告表」的店
    sid, adfile = "", None
    for meta in (home / "数据" / "店铺").glob("*/meta.json"):
        try:
            this_sid = str(json.loads(meta.read_text(encoding="utf-8")).get("shop_id") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        d = meta.parent / "收件箱" / "广告表"
        if not this_sid or not d.is_dir():
            continue
        fs = [f for f in d.glob("*.xlsx") if not f.name.startswith(".")]
        if fs:
            sid = this_sid
            adfile = max(fs, key=lambda x: x.stat().st_mtime)
            break
    if not sid or not adfile:
        print("跳过：没有可用的广告表来构造场景")
        return 0

    now = time.time()
    snap = S._latest_kind_files(sid, 0.0)     # 当前磁盘上每类最新的
    print(f"样本店 {sid}，当前快照：{ {k: v[:34] for k, v in snap.items()} }")
    bad: list[str] = []

    def trial(baseline, queued: bool = False) -> dict:
        rec = {"steps": {"sku": {"state": "running", "detail": "已排上，等浏览器领任务",
                                 "seeded": True}},
               "current": "sku", "started": now, "updated": now, "queued": queued}
        if baseline is not None:
            rec["baseline"] = baseline
        S._fill_step_files(sid, rec)
        return {k: v.get("detail") for k, v in rec["steps"].items()
                if (v or {}).get("state") == "done"}

    # ① 派单那一刻拍下的基线 = 现在磁盘上的 → 一个都不许补（否则 1 秒打勾）
    got = trial(dict(snap))
    print(f"  ① 基线跟磁盘一样（= 上一轮的文件）→ 补成 done：{got or '无'}")
    if got:
        bad.append(f"上一轮的文件被补成本轮（点运行就满屏勾）：{list(got)}")

    # ② 基线里是旧名字（模拟这一轮换了新文件）→ 必须补上文件名
    old_base = {k: f"OLD-{k}.xlsx" for k in ("sku", "ads", "orders", "affiliate")}
    got = trial(old_base)
    print(f"  ② 基线是旧名字（= 这一轮换了新文件）→ 补成 done：{got or '无'}")
    if "ads" not in got:
        bad.append("这一轮换了新文件，名字补不上（卡片 ✓ 却没有文件名）")

    # ③ 没有基线 / 基线是空字典 → 都不许乱补
    got = trial(None)
    print(f"  ③ 没有基线字段 → 补成 done：{got or '无'}")
    if got:
        bad.append(f"没有基线时乱补：{list(got)}")
    got = trial({})
    print(f"  ③-b 基线是空字典（拍快照时扫描失败）→ 补成 done：{got or '无'}")
    if got:
        bad.append(f"基线为空时乱补：{list(got)}")

    # ④ 把文件 mtime 改成 10 天前，但基线里是旧名字 → 仍要补
    #    （证明判断依据是「名字换了」，跟时间无关）
    old_mt = adfile.stat().st_mtime
    os.utime(adfile, (now - 10 * 86400, now - 10 * 86400))
    try:
        got = trial(old_base)
    finally:
        os.utime(adfile, (old_mt, old_mt))
    print(f"  ④ 名字换了但 mtime=10 天前 → 补成 done：{got or '无'}")
    if "ads" not in got:
        bad.append("还在看时间：名字换了、但 mtime 旧，就补不上")

    # ⑤-b 扩展报了 done 却没给文件名，且文件跟基线同名 → 也必须补上名字。
    #     2026-11-12 13:31 截图：商品SKU表/联盟佣金表 打了勾，右边一片空白。
    rec = {"steps": {"sku": {"state": "done", "detail": None},
                     "affiliate": {"state": "done", "detail": ""}},
           "current": "", "started": now, "updated": now, "baseline": dict(snap)}
    S._fill_step_files(sid, rec)
    got = {k: v.get("detail") for k, v in rec["steps"].items()}
    print(f"  ⑤-b 打了勾但没名字（文件跟基线同名）→ 补后：{ {k: str(v)[:26] for k, v in got.items()} }")
    for k in ("sku", "affiliate"):
        if k in snap and not got.get(k):
            bad.append(f"{k} 打了勾却没有文件名（卡片会显示空白）")

    # ⑤ 补订单后自动核算（/run）走的是 reset=False，**不许清掉已取到的步骤**
    #    2026-11-12 回归：PHT-B1 算完了，卡片上四个取数步骤全变空圈。
    S._PROGRESS.pop(sid, None)
    S._seed_progress(sid, "sku", "已排上", reset=True)
    for k in ("sku", "ads", "affiliate"):
        S._PROGRESS[sid]["steps"][k] = {"state": "done", "detail": f"{k}-new.xlsx"}
    S._PROGRESS[sid]["steps"]["orders"] = {"state": "running", "detail": "已提交，等平台生成"}
    S._seed_progress(sid, "calc", "正在本机核算…")        # 补订单后的重算
    left = set(S._PROGRESS[sid]["steps"])
    print(f"  ⑤ 重算后剩下的步骤：{sorted(left)}")
    missing = {"sku", "ads", "affiliate", "orders"} - left
    if missing:
        bad.append(f"重算把已取到的步骤清掉了：{sorted(missing)}（卡片会变成空圈）")
    if not S._PROGRESS[sid].get("baseline"):
        bad.append("重算把基线快照弄丢了（之后补不了文件名）")
    # ⑥ 「点运行」派单时不许沿用上一轮的 run_id。
    #    扩展报上来的是它自己那一轮的 id，一旦跟记录里的对不上，
    #    _post_progress 会把整份记录清空重建，baseline 跟着丢 →
    #    卡片就出现「打了勾没有文件名」（2026-11-12 14:10 PHT-B1 实测）。
    S._PROGRESS.pop(sid, None)
    S._PROGRESS[sid] = {
        "run_id": "上一轮的id", "steps": {"ads": {"state": "done", "detail": "old"}},
        "baseline": {"ads": "old-ad.xlsx"}, "started": 1.0, "updated": 1.0,
    }
    S._seed_progress(sid, "sku", "已排上", reset=True)
    rec = S._PROGRESS[sid]
    print(f"  ⑥ 派单后 run_id={rec.get('run_id')!r} baseline={list((rec.get('baseline') or {}))}")
    if rec.get("run_id"):
        bad.append("派单沿用了上一轮的 run_id（扩展一报进度就会把 baseline 清掉）")
    if not rec.get("baseline"):
        bad.append("派单后基线快照是空的（之后补不了文件名）")
    if "ads" in (rec.get("steps") or {}):
        bad.append("派单没清掉上一轮的步骤（刚点运行就挂着旧状态）")
    S._PROGRESS.pop(sid, None)

    # ⑦ 静态：_post_progress 在「run 切换」清空时必须把 baseline 带回来
    src = (ROOT / "bridge" / "server.py").read_text(encoding="utf-8")
    if "_keep_base" not in src:
        bad.append("run 切换时没保留 baseline（_keep_base 不见了，卡片会打勾却没名字）")

    for b in bad:
        print(f"✗ {b}")
    if bad:
        return 1
    print("全部通过：旧文件不冒充、新文件补得上、没有基线不乱补、完全不看时间")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
