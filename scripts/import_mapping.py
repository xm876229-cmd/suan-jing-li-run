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

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT

from src.loader import (  # noqa: E402
    derive_style_code, parse_mapping, parse_mapping_json, read_table,
)

# 订单表里提取的 sku_id → 卖家SKU（main 里初始化）
ORDER_SKU: dict[str, str] = {}

MAPPING_DIR = paths.MAPPING
MASTER = MAPPING_DIR / "master.csv"
HISTORY = MAPPING_DIR / "history.jsonl"
INBOX = paths.INBOX
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
            # 兼容几种命名：扩展自动抓的 sku_list_*.json、
            # 页面导出改名后的 sku_export_*.xlsx、
            # 以及历史文件 sku_20261030.xlsx（早期命名，漏合并过，这里一并认）
            if ("sku_list" in low or "sku list" in low or "sku_export" in low
                    or low.startswith("sku_") or "产品" in f.name):
                out.append(f)
    return sorted(out, key=lambda f: f.stat().st_mtime)


# 来源优先级（用户明确）：
#   主来源 = 商品管理接口的全量 SKU 表（sku_id / product_id / seller_sku）
#   补充   = 主工作簿「链接资料」表、区间内的 SKU 分析导出
# 归属冲突时以主来源为准；补充来源只补主表里没有的 SKU，不改已有归属。
# 两边都找不到，才归入「无ID」。
PRIMARY_HINTS = ("商品管理",)


def is_primary(path: Path) -> bool:
    return any(h in path.name for h in PRIMARY_HINTS)


def load_seller_sku_from_orders(limit_files: int = 10) -> dict[str, str]:
    """从订单表里提取 sku_id → 卖家SKU。

    为什么从这里取：订单表每行都带「SKU ID」和「Seller SKU」，
    而款式编码（V27-White → V27）就藏在卖家SKU里。
    有了它就不用再单独导一张「链接资料 / 批量编辑商品」表了。

    只读最近 N 份订单表（每轮取数都会新增一份，全读会越来越慢）。
    """
    out: dict[str, str] = {}
    try:
        store_dirs = list(paths.STORES_ROOT.iterdir())
    except OSError:
        return out

    files: list[Path] = []
    for d in store_dirs:
        if not d.is_dir():
            continue
        for sub in ("订单表", "orders"):
            p = d / "inbox" / sub
            if not p.is_dir():
                continue
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in (".csv", ".xlsx", ".xls"):
                    files.append(f)
    if not files:
        return out
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    for f in files[:limit_files]:
        try:
            rows = read_table(f)
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        # 找表头里「SKU ID」「Seller SKU」两列
        hidx = -1
        c_sid = c_sku = -1
        for i, r in enumerate(rows[:6]):
            low = [str(x or "").strip().lower().replace(" ", "") for x in r]
            a = next((j for j, h in enumerate(low) if h in ("skuid", "sku_id")), -1)
            b = next((j for j, h in enumerate(low)
                      if h in ("sellersku", "seller_sku", "卖家sku", "卖家 SKU".lower())), -1)
            if a >= 0 and b >= 0:
                hidx, c_sid, c_sku = i, a, b
                break
        if hidx < 0:
            continue
        for r in rows[hidx + 1:]:
            if len(r) <= max(c_sid, c_sku):
                continue
            sid = str(r[c_sid] or "").strip()
            sku = str(r[c_sku] or "").strip()
            if sid and sku and sid not in out:
                out[sid] = sku
    return out


def merge_file(path: Path, master: dict[str, dict], events: list[dict], now: str,
               overwrite: bool = True) -> tuple[int, int, int]:
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
        # 表里没有卖家SKU（比如「商品数据分析」导出的表）→ 用订单表里的补上，
        # 再据此推出款式编码。这样就不用额外导「链接资料」那张表了。
        if not row["seller_sku"]:
            from_orders = ORDER_SKU.get(sid)
            if from_orders:
                row["seller_sku"] = from_orders
        if not row["style_code"] and row["seller_sku"]:
            row["style_code"] = derive_style_code(row["seller_sku"])

        old = master.get(sid)
        if old is None:
            master[sid] = row
            added += 1
            events.append({"at": now, "kind": "new", "sku_id": sid,
                           "old": None, "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
        elif old["product_id"] != row["product_id"]:
            if not overwrite:
                # 非主来源：只补缺，不改已有归属（归属以主来源为准）
                same += 1
                continue
            # 链接改绑了商品：这是必须留痕的一类变更
            events.append({"at": now, "kind": "rebind", "sku_id": sid,
                           "old": old["product_id"], "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
            row["updated_at"] = now
            master[sid] = row
            rebound += 1
        else:
            # 同商品，只更新描述字段。
            #
            # 关键：**空值绝不覆盖已有值**。
            # 商品数据分析接口返回的行没有「卖家SKU」（seller_sku 为空），
            # 如果按「有新值就整行替换」，它会把老数据里辛苦攒下的
            # seller_sku / style_code 全冲成空——报表的款式编码列就废了。
            desc = ("seller_sku", "product_name", "style_code")
            merged = dict(old)
            touched = False
            for k in desc:
                nv = (row.get(k) or "").strip()
                if nv and nv != (old.get(k) or ""):
                    merged[k] = row[k]
                    touched = True
            if touched:
                merged["updated_at"] = now
                master[sid] = merged
            same += 1
    return added, rebound, same


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    sources = [Path(a) for a in argv] if argv else find_sources()

    if not sources:
        print(f"没找到 sku_list_*.json：{INBOX}")
        return 2

    master = load_master()
    # 从订单表补卖家SKU（用来推款式编码），免得再导一张链接资料
    global ORDER_SKU
    ORDER_SKU = load_seller_sku_from_orders()
    if ORDER_SKU:
        print(f"从订单表读到 {len(ORDER_SKU)} 个 SKU 的卖家编码（用于推款式编码）")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events: list[dict] = []
    total = [0, 0, 0]

    for src in sources:
        if not src.exists():
            print(f"跳过不存在的文件：{src}")
            continue
        primary = is_primary(src)
        added, rebound, same = merge_file(src, master, events, now, overwrite=primary)
        print(f"      {'主来源' if primary else '补充（只补缺）'}")
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
