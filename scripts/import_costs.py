"""导入成本表：合并进主表，并记录变更流水。

为什么不直接"用最新那份文件覆盖"：
    1. 成本表常有合并单元格、多级表头、商品编码列位置漂移，直接覆盖容易整表读空；
    2. 覆盖式会丢掉历史价，事后想追"这个 SKU 上个月成本多少"就没了；
    3. 新增商品时，旧表里的编码不能因为新表没带而消失。

所以这里做增量合并：
    · 新编码 → 追加
    · 已有编码 → 价格变了才更新，并记一条流水
    · 主表永远保留全部已知编码

用法：
    python -m scripts.import_costs "成本表.xlsx"
    python -m scripts.import_costs            # 自动导入 data/costs 下最新的表
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT

from src.loader import parse_costs, read_table  # noqa: E402

COST_DIR = paths.COSTS
MASTER = COST_DIR / "master.csv"
HISTORY = COST_DIR / "history.jsonl"
FIELDS = ["code", "cost", "effective_from", "updated_at"]


def _num(v) -> float:
    """把成本列转成数字。

    表格里常见「1,234.50」「¥12」「12 元」这种写法，直接 float() 会抛异常，
    一个格子有问题就让整张成本表导不进来（然后全店净利算错）。
    这里尽量救：去掉货币符号和千分位；实在认不出就当 0，并在导入时提示。
    """
    if isinstance(v, (int, float)):
        return float(v)
    raw = str(v or "").strip()
    if not raw:
        return 0.0
    for ch in ("¥", "￥", "RMB", "rmb", "元", ",", " ", "\u00a0"):
        raw = raw.replace(ch, "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def load_master() -> dict[str, dict]:
    if not MASTER.exists():
        return {}
    out: dict[str, dict] = {}
    with MASTER.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            out[code] = {
                "code": code,
                "cost": _num(row.get("cost")),
                "effective_from": row.get("effective_from") or "",
                "updated_at": row.get("updated_at") or "",
            }
    return out


def write_master(data: dict[str, dict]) -> None:
    """写成本主表：先在内存里拼好，再原子替换。

    直接 open("w") 是先清空再写，断电/强杀会留下半截 CSV——
    成本表读不全，全店净利都会算错，而且看不出来。
    """
    import io

    from src import safeio

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    for code in sorted(data):
        w.writerow(data[code])
    safeio.write_text(MASTER, buf.getvalue(), encoding="utf-8")


def pick_source(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit
    if not COST_DIR.exists():
        return None
    files = [
        f for f in COST_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv")
        and f.name != MASTER.name and not f.name.startswith("~$")
    ]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    src = pick_source(Path(argv[0]) if argv else None)
    if not src or not Path(src).exists():
        print(f"找不到成本表：{src or COST_DIR}")
        return 2

    pairs = parse_costs(read_table(Path(src), sheet="成本") if str(src).lower().endswith(".xls") else read_table(Path(src)))
    if not pairs:
        print(f"这张表里没读出「编码 + 成本」两列：{src}")
        return 1

    master = load_master()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    added, changed, same = 0, 0, 0
    events: list[dict] = []
    for code, cost in pairs.items():
        old = master.get(code)
        if old is None:
            master[code] = {"code": code, "cost": cost, "effective_from": today, "updated_at": now}
            added += 1
            events.append({"at": now, "code": code, "old": None, "new": cost, "kind": "new"})
        elif abs(old["cost"] - cost) > 1e-6:
            events.append({"at": now, "code": code, "old": old["cost"], "new": cost, "kind": "change"})
            old["cost"] = cost
            old["effective_from"] = today
            old["updated_at"] = now
            changed += 1
        else:
            same += 1

    write_master(master)
    if events:
        COST_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"来源：{Path(src).name}")
    print(f"新增 {added} 条｜变动 {changed} 条｜未变 {same} 条｜主表现有 {len(master)} 条")
    print(f"主表：{MASTER}")
    if events[:5]:
        print("本次变动（前 5 条）：")
        for e in events[:5]:
            old = "—" if e["old"] is None else round(e["old"], 4)
            print(f"  {e['code']}：{old} → {round(e['new'], 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
