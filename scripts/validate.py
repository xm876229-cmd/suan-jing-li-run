"""口径校验：把引擎算出来的结果，逐项和主工作簿「净利润」表的人工结果比对。

校验四层，任何一层不过都说明口径没复刻对：
    1. 商品ID 映射      —— 由 SKU 反查的商品ID，是否等于人工填的 A 列
    2. 订单数分摊       —— 1/订单行数，是否等于人工 C 列
    3. 买家实付分摊     —— 订单实付/行数，是否等于人工 D 列
    4. 成本             —— 成本表单价 × 数量，是否等于人工 B 列
    5. 净利             —— 全链路用程序算，与「净利润」表 AC/AD 列比

用法：python -m scripts.validate [工作簿路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

import openpyxl

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT

from src.calc import compute, load_rates  # noqa: E402
from src.loader import load_from_workbook, to_num, to_str  # noqa: E402

TOL_MONEY = 0.02      # 金额容差
TOL_QTY = 1e-6        # 数量容差


def id_key(v) -> str:
    """商品ID 归一：Excel 把 18 位数字存成浮点会丢末位，只比对前 15 位有效数字。"""
    s = to_str(v)
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[:15]


def pct(a: float, b: float) -> str:
    return f"{a / b * 100:.2f}%" if b else "-"


def main() -> int:
    wb_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "V1-净利  -15.xlsx"
    if not wb_path.exists():
        print(f"找不到工作簿：{wb_path}")
        return 2

    print(f"校验对象：{wb_path.name}")
    rates, switches = load_rates(paths.RATES_FILE)
    params = rates["马来西亚"]
    data = load_from_workbook(wb_path, params.usd_to_local)
    print(f"订单行数 {len(data.orders)}｜成本编码 {len(data.costs)}｜SKU 索引 {len(data.sku_index)}｜"
          f"广告商品 {len(data.ads)}｜联盟商品 {len(data.commissions)}")

    # ---------- 人工表 ----------
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    od = wb["订单"]
    manual_rows = [r for r in od.iter_rows(min_row=3, values_only=True)]

    print("\n===== 1. 商品ID 映射 =====")
    hit = miss = blank = 0
    samples: list[tuple[str, str, str]] = []
    for l, r in zip(data.orders, manual_rows):
        ma = id_key(r[0])
        mine = id_key(l.product_id)
        if not ma:
            blank += 1
            continue
        if ma == mine:
            hit += 1
        else:
            miss += 1
            if len(samples) < 5:
                samples.append((l.seller_sku, ma, mine))
    total_cmp = hit + miss
    print(f"一致 {hit} / 可比 {total_cmp}（{pct(hit, total_cmp)}）｜表内 A 列为空或「无ID」 {blank} 行")
    for s in samples:
        print(f"   不一致：SKU={s[0]} 人工={s[1]} 程序={s[2]}")

    print("\n===== 2. 订单数分摊（C 列）=====")
    bad = 0
    worst = 0.0
    for l, r in zip(data.orders, manual_rows):
        mc, oc = to_num(r[2]), l.allocated_orders
        d = abs(mc - oc)
        worst = max(worst, d)
        if d > 1e-6:
            bad += 1
    print(f"偏差行数 {bad} / {len(data.orders)}｜最大偏差 {worst:.8f}")

    print("\n===== 3. 买家实付分摊（D 列）=====")
    bad = 0
    worst = 0.0
    for l, r in zip(data.orders, manual_rows):
        md, od_ = to_num(r[3]), l.allocated_paid
        d = abs(md - od_)
        worst = max(worst, d)
        if d > TOL_MONEY:
            bad += 1
    print(f"偏差行数 {bad} / {len(data.orders)}｜最大偏差 {worst:.4f}")

    print("\n===== 4. 成本（B 列 = 单价 × 数量）=====")
    bad = 0
    worst = 0.0
    diff_skus: list[str] = []
    for l, r in zip(data.orders, manual_rows):
        mb = to_num(r[1])
        mine = l.cost_total_cny
        if mine is None:
            bad += 1
            if l.seller_sku not in diff_skus:
                diff_skus.append(l.seller_sku)
            continue
        d = abs(mb - mine)
        worst = max(worst, d)
        if d > TOL_MONEY:
            bad += 1
            if l.seller_sku not in diff_skus:
                diff_skus.append(l.seller_sku)
    print(f"偏差行数 {bad} / {len(data.orders)}｜最大偏差 {worst:.4f}")
    if diff_skus:
        print(f"   涉及 SKU（前 10）：{diff_skus[:10]}")

    # ---------- 5. 净利 ----------
    result = compute(data, params, switches)

    sheet = wb["净利润"]
    manual: dict[str, dict] = {}
    for r in sheet.iter_rows(min_row=3, values_only=True):
        key = id_key(r[1])
        if not key:
            continue
        manual[key] = {
            "gmv": to_num(r[2]), "paid": to_num(r[3]), "orders": to_num(r[4]),
            "cost": to_num(r[6]), "creator": to_num(r[23]), "ads": to_num(r[25]),
            "net_local": to_num(r[28]), "net_cny": to_num(r[29]),
            "style": to_str(r[0]),
        }

    ours = {id_key(p.product_id): p for p in result.products if p.product_id != "__未识别商品__"}

    print("\n===== 5. 净利全链路（按商品ID）=====")
    print(f"{'商品ID(前15)':<18}{'GMV':>12}{'净利(本地)':>14}{'差异':>12}{'差异率':>9}")
    matched = 0
    gmv_ok = net_ok = 0
    worst_net = 0.0
    rows_out = []
    for key, m in manual.items():
        p = ours.get(key)
        if p is None:
            rows_out.append(f"{key:<18}{'缺失':>12}{'':>14}{'':>12}{'':>9}")
            continue
        matched += 1
        dg = p.gmv - m["gmv"]
        dn = p.net_local - m["net_local"]
        worst_net = max(worst_net, abs(dn))
        if abs(dg) <= TOL_MONEY:
            gmv_ok += 1
        if abs(dn) <= max(TOL_MONEY, abs(m["net_local"]) * 0.005):
            net_ok += 1
        rows_out.append(f"{key:<18}{m['gmv']:>12.2f}{m['net_local']:>14.2f}{dn:>12.2f}"
                        f"{pct(abs(dn), abs(m['net_local'])) if m['net_local'] else '-':>9}")

    for line in rows_out[:15]:
        print(line)
    if len(rows_out) > 15:
        print(f"... 共 {len(rows_out)} 个商品")

    print(f"\n可比商品 {matched}/{len(manual)}｜GMV 一致 {gmv_ok}（{pct(gmv_ok, matched)}）"
          f"｜净利一致 {net_ok}（{pct(net_ok, matched)}）｜净利最大偏差 {worst_net:.2f}")

    t = result.total
    sheet_total_net = sum(m["net_local"] for m in manual.values())
    sheet_total_gmv = sum(m["gmv"] for m in manual.values())
    print("\n===== 合计对比 =====")
    print(f"{'':<16}{'程序':>16}{'人工表':>16}{'差异':>12}")
    print(f"{'GMV':<16}{t.gmv:>16.2f}{sheet_total_gmv:>16.2f}{t.gmv - sheet_total_gmv:>12.2f}")
    print(f"{'净利(本地)':<16}{t.net_local:>16.2f}{sheet_total_net:>16.2f}{t.net_local - sheet_total_net:>12.2f}")
    print(f"{'净利(人民币)':<16}{result.net_cny_total:>16.2f}"
          f"{sum(m['net_cny'] for m in manual.values()):>16.2f}"
          f"{result.net_cny_total - sum(m['net_cny'] for m in manual.values()):>12.2f}")

    if result.issues:
        print("\n===== 引擎提示（前 10 条）=====")
        for s in result.issues[:10]:
            print("  ·", s)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
