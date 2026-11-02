"""从主工作簿的「链接资料」表提取 SKU→商品 映射，合并进映射主表。

「链接资料」是店铺后台导出的当前全部链接（商品ID / SKU ID / 卖家编码 / 规格 / 价格），
实测 1382 条 SKU ID，比接口取到的 235 条全得多。

为什么它不能单独用：
    删减 SKU 后，旧的 SKU ID 就不再出现在这张表里。历史订单里那些行会失去归属。
所以它的定位是「当前全量」，与「累积主表」配合：
    · 链接资料 → 提供当前所有 SKU 的归属
    · 累积主表 → 保住历史 SKU 不丢
两者合并后，既全又不丢。

用法：
    python -m scripts.import_link_info                # 自动找项目根目录下的主工作簿
    python -m scripts.import_link_info "xxx.xlsx"     # 指定工作簿
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT

from src.loader import parse_mapping, read_table  # noqa: E402

MAPPING_DIR = paths.MAPPING
OUT = MAPPING_DIR / "sku_list_链接资料.csv"
SHEET = "链接资料"
FIELDS = ["sku_id", "product_id", "seller_sku", "product_name", "style_code"]


def find_workbook() -> Path | None:
    """项目根目录下带「链接资料」表的工作簿，取最新修改的一个。"""
    best: Path | None = None
    for f in ROOT.glob("*.xlsx"):
        if f.name.startswith("~$") or not f.is_file():
            continue
        try:
            rows = read_table(f, sheet=SHEET)
        except Exception:
            continue
        if rows and len(rows) > 1:
            if best is None or f.stat().st_mtime > best.stat().st_mtime:
                best = f
    return best


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    wb = Path(argv[0]) if argv else find_workbook()
    if not wb or not Path(wb).exists():
        print("没找到含「链接资料」表的工作簿")
        return 2

    rows = read_table(Path(wb), sheet=SHEET)
    idx = parse_mapping(rows)
    if not len(idx):
        print(f"{Path(wb).name} 的「链接资料」表里没读出 SKU ID / Product ID 列")
        return 1

    MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 卖家编码反查：同一个编码可能挂多个商品，这里只做展示用，不参与定位
    sku_of_item: dict[int, str] = {}
    for sku, items in idx.by_seller_sku.items():
        for it in items:
            sku_of_item.setdefault(id(it), sku)

    import io

    from src import safeio

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    n = 0
    for sid, item in idx.by_sku_id.items():
        w.writerow({
            "sku_id": sid,
            "product_id": item["product_id"],
            "seller_sku": sku_of_item.get(id(item), ""),
            "product_name": item.get("product_name", ""),
            "style_code": item.get("style_code", ""),
        })
        n += 1
    safeio.write_text(OUT, buf.getvalue(), encoding="utf-8")

    print(f"来源：{Path(wb).name} / 「{SHEET}」表")
    print(f"导出 {n} 条 SKU 映射 → {OUT.name}")
    print(f"（已写入 {now}，下次算账时会自动合并进映射主表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
