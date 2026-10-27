"""导入 SKU→商品 映射：合并进主表，并记录改绑流水。

为什么映射也要主表，而不是直接用最新抓下来的那份：
    链接一编辑，SKU 归属就可能变（一天变一次也正常）。最新那份只反映"现在"，
    一旦某个老 SKU 从里面消失，历史订单里那些行就再也找不到商品ID，
    会被整片归进「未识别商品」——报表看起来没错，实际少算了一个商品的全部净利。

所以这里做增量合并：
    · 新 SKU        → 追加
    · 商品ID 变绑    → 更新，并记一条 rebind 流水（旧商品 → 新商品）
    · 卖家SKU/商品名 → 变了就更新（不记流水，属描述性字段）
    · 主表永远保留全部见过的 SKU

用法：
    python -m scripts.import_mapping                       # 自动取 _inbox 里最新的 sku_list_*.json
    python -m scripts.import_mapping "sku_list_xxx.json"
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.loader import parse_mapping, parse_mapping_json, read_table  # noqa: E402

MAPPING_DIR = ROOT / "data" / "mapping"
MASTER = MAPPING_DIR / "master.csv"
HISTORY = MAPPING_DIR / "history.jsonl"
INBOX = ROOT / "data" / "exports" / "_inbox"
FIELDS = ["sku_id", "product_id", "seller_sku", "product_name", "style_code", "updated_at"]


def load_master() -> dict[str, dict]:
    if not MASTER.exists():
        return {}
    out: dict[str, dict] = {}
    with MASTER.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("sku_id") or "").strip()
            if not sid:
                continue
            out[sid] = {k: (row.get(k) or "") for k in FIELDS}
    return out


def write_master(data: dict[str, dict]) -> None:
    MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    with MASTER.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for sid in sorted(data):
            w.writerow(data[sid])


SUFFIXES = (".json", ".xlsx", ".xls", ".csv")


def find_sources() -> list[Path]:
    """所有可能的映射来源，按时间从旧到新（后者覆盖前者）。

    两种形态都收：
      · 扩展自动抓的 sku_list_*.json
      · 用户手工导的 product_sku_list.xlsx（接口取不到时的退路）
    """
    out: list[Path] = []
    for d in (MAPPING_DIR, INBOX):
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file() or f.suffix.lower() not in SUFFIXES:
                continue
            low = f.name.lower()
            if ("sku_list" in low or "sku list" in low or "sku_export" in low
                    or "产品" in f.name):
                out.append(f)
    return sorted(out, key=lambda f: f.stat().st_mtime)


def merge_file(path: Path, master: dict[str, dict], events: list[dict], now: str) -> tuple[int, int, int]:
    if path.suffix.lower() in (".xlsx", ".xls", ".csv"):
        idx = parse_mapping(read_table(path))
    else:
        idx = parse_mapping_json(path)
    added, rebound, same = 0, 0, 0

    for sid, item in idx.by_sku_id.items():
        row = {
            "sku_id": sid,
            "product_id": item["product_id"],
            "seller_sku": "",
            "product_name": item.get("product_name", ""),
            "style_code": item.get("style_code", ""),
            "updated_at": now,
        }
        # 反查卖家SKU（同一个 item 对象可能挂在多个卖家SKU下，取第一个即可）
        for sku, bucket in idx.by_seller_sku.items():
            if any(b is item for b in bucket):
                row["seller_sku"] = sku
                break

        old = master.get(sid)
        if old is None:
            master[sid] = row
            added += 1
            events.append({"at": now, "kind": "new", "sku_id": sid,
                           "old": None, "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
        elif old["product_id"] != row["product_id"]:
            # 链接改绑了商品：这是必须留痕的一类变更
            events.append({"at": now, "kind": "rebind", "sku_id": sid,
                           "old": old["product_id"], "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
            row["updated_at"] = now
            master[sid] = row
            rebound += 1
        else:
            # 同商品，只更新描述字段
            changed = any(old[k] != row[k] for k in ("seller_sku", "product_name", "style_code"))
            if changed:
                master[sid] = row
            same += 1
    return added, rebound, same


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    sources = [Path(a) for a in argv] if argv else find_sources()

    if not sources:
        print(f"没找到 sku_list_*.json：{INBOX}")
        return 2

    master = load_master()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events: list[dict] = []
    total = [0, 0, 0]

    for src in sources:
        if not src.exists():
            print(f"跳过不存在的文件：{src}")
            continue
        added, rebound, same = merge_file(src, master, events, now)
        total[0] += added
        total[1] += rebound
        total[2] += same
        print(f"  {src.name}：新增 {added}｜改绑 {rebound}｜未变 {same}")

    write_master(master)
    if events:
        MAPPING_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"\n合计：新增 {total[0]} 条｜改绑 {total[1]} 条｜未变 {total[2]} 条｜主表现有 {len(master)} 条")
    print(f"主表：{MASTER}")

    rebinds = [e for e in events if e["kind"] == "rebind"]
    if rebinds:
        print(f"\n⚠ 本次有 {len(rebinds)} 个 SKU 改绑了商品（历史订单会按新商品归属统计）：")
        for e in rebinds[:8]:
            print(f"  SKU {e['sku_id']}（{e['seller_sku'] or '无卖家SKU'}）：商品 {e['old']} → {e['new']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
