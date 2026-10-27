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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.loader import parse_costs, read_table  # noqa: E402

COST_DIR = ROOT / "data" / "costs"
MASTER = COST_DIR / "master.csv"
HISTORY = COST_DIR / "history.jsonl"
FIELDS = ["code", "cost", "effective_from", "updated_at"]


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
                "cost": float(row.get("cost") or 0),
                "effective_from": row.get("effective_from") or "",
                "updated_at": row.get("updated_at") or "",
            }
    return out


def write_master(data: dict[str, dict]) -> None:
    COST_DIR.mkdir(parents=True, exist_ok=True)
    with MASTER.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for code in sorted(data):
            w.writerow(data[code])


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
