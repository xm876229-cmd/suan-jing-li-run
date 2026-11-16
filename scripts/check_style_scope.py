"""款式编码口径自检：明细只看本行、汇总只看本期出单。

为什么单独写这一支（2026-11-16）：
    汇总页原来报的是「这条链接下全部 SKU 的款式」，而映射主表是累积表、
    只增不删，下架的 SKU 会永远留着。实测 PHT-M4/PHT-M2 各有 6 条链接
    多出了早就不存在的 `M14`（那个 SKU 11-05 就下架了）。
    用户定的口径是「汇总和明细都从订单里提取」，即：
      明细 → 这一行自己的款式
      汇总 → 这条链接**本期出过单**的款式，去重后用 / 连起来

这一支盯住四件事，任何一条被改回去都会红：
    ① 明细行不写别人的款式（不能出现 `M18/M4` 这种单行多值）
    ② 映射里有、本期订单里没有的款式（已下架）不能出现在汇总里
    ③ 映射里查不到该 SKU 时，靠本行 Seller SKU 推出来
    ④ 推不出款式时安静留空，不许抛异常

跑法：
    python -m scripts.check_style_scope
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.loader import OrderLine, SkuIndex, attach_product_ids  # noqa: E402

bad: list[str] = []


def line(oid: str, sku_id: str, seller_sku: str, product_id: str = "") -> OrderLine:
    return OrderLine(
        order_id=oid, seller_sku=seller_sku, sku_id=sku_id,
        product_name="测试商品", qty=1, subtotal_before=100, seller_discount=0,
        order_amount=100, cancel="", status="已签收", created="2026-11-10 10:00",
        paid="2026-11-10 10:05", product_id=product_id,
    )


def build_index() -> SkuIndex:
    idx = SkuIndex()
    rows = [
        ("S1", "P1", "M2-1-Red", "M2"),
        ("S2", "P1", "M4-1-Blue", "M4"),
        ("S3", "P2", "M1-1-Red", "M1"),
        # 已下架的 SKU：映射里还在，本期订单里没有 → 绝不能出现在报表里
        ("S9", "P1", "M14-1pc", "M14"),
    ]
    for sid, pid, ss, sc in rows:
        item = {"sku_id": sid, "product_id": pid, "seller_sku": ss,
                "product_name": "测试商品", "style_code": sc}
        idx.by_sku_id[sid] = item
        idx.by_seller_sku[ss].append(item)
        idx.add_style(pid, sc)
    return idx


def check() -> None:
    # ① 明细只写自己、汇总=本期并集
    lines = [line("O1", "S1", "M2-1-Red"), line("O2", "S2", "M4-1-Blue")]
    attach_product_ids(lines, build_index(), [])

    if lines[0].style_codes != "M2":
        bad.append(f"① 明细第一行应为 M2，实际 {lines[0].style_codes!r}"
                   f"（说明又写成了整条链接的并集）")
    if lines[1].style_codes != "M4":
        bad.append(f"① 明细第二行应为 M4，实际 {lines[1].style_codes!r}")
    for l in lines:
        if "/" in l.style_codes:
            bad.append(f"① 明细行出现单行多值 {l.style_codes!r}"
                       f"（一行只有一个 SKU，不该显示两个款式）")
        if l.link_styles != "M2/M4":
            bad.append(f"① 汇总字段应为 M2/M4，实际 {l.link_styles!r}")

    # ② 已下架的 M14 不能跑进来
    if "M14" in lines[0].link_styles:
        bad.append("② 已下架的 M14 出现在了本期汇总里"
                   "（映射累积表那一路又被加回来了？）")

    # ③ 映射里没有的 SKU：靠本行 Seller SKU 推
    idx = build_index()
    l3 = [line("O3", "S-NEW", "V27-White-10Pcs")]
    attach_product_ids(l3, idx, [])
    if l3[0].style_codes != "V27":
        bad.append(f"③ 映射里没有的 SKU 没从卖家编码推出来，实际 {l3[0].style_codes!r}")

    # ④ 什么都推不出：留空，不抛异常
    l4 = [line("O4", "S-EMPTY", "")]
    attach_product_ids(l4, build_index(), [])
    if l4[0].style_codes:
        bad.append(f"④ Seller SKU 为空时不该凭空造款式，实际 {l4[0].style_codes!r}")
    if l4[0].link_styles:
        bad.append(f"④ 认不出商品的行不该有链接级并集，实际 {l4[0].link_styles!r}")

    # ⑤ 一条链接本期只出一个款式时，汇总也该只有一个（不能把整条链接都列上）
    l5 = [line("O5", "S3", "M1-1-Red")]
    attach_product_ids(l5, build_index(), [])
    if l5[0].link_styles != "M1":
        bad.append(f"⑤ 只出了 M1 的链接，汇总应为 M1，实际 {l5[0].link_styles!r}")


def main() -> int:
    check()
    if bad:
        print("[缺失] 款式编码口径：")
        for b in bad:
            print("       -", b)
    else:
        print("[OK]   款式编码：明细只看本行、汇总只看本期出单（下架款式不会混进来）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
