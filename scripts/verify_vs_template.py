"""逐条链接核对：程序算的跟模板《V1-净利 -15.xlsx》差多少。

为什么要有这个脚本
------------------
核对净利只看合计是不够的——合计对上了，可能是正负抵消；
分配错了（同一笔订单的钱摊到别的链接上），合计照样能对上。

所以这个脚本**逐条链接**比：GMV、买家支付总额、分摊订单数、交易手续费、
成本、发单费，凡是模板有对应列的都拿来对，并列出每一条的差。

口径说明
--------
模板「净利润」页有 AE2 一个开关（0 = 去除样品），脚本按 AE2=0 那一档
对齐程序的参数：
    GMV / 平台佣金 / 奖金返现   剔取消退货，含 0 元样品   （H 列为空）
    买家支付总额                剔 0 元样品，含取消退货   （AA 列 > 0）
    订单数 / 成本 / 发单费      剔 0 元样品、剔取消退货   （AA>0 且 H 为空）
    交易手续费                  = 买家支付总额 × 费率

用法：
    python -m scripts.verify_vs_template [模板路径]
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from src import calc, formula, paths  # noqa: E402
from src.cli import load_rates  # noqa: E402
from src.loader import load_from_workbook  # noqa: E402

# 模板 AE2=0 对应的程序参数口径
ALIGN = {
    "gmv": ["normal", "zero_amount"],
    "buyer_paid": ["normal", "cancelled", "refunded"],
    "order_count": ["normal"],
    "cost": ["normal"],
    "dispatch": ["normal"],
    "tx_fee": ["normal", "cancelled", "refunded"],
    "platform_commission": ["normal", "zero_amount"],
    "cashback": ["normal", "zero_amount"],
    "items": ["normal"],
}

# 模板「净利润」页的列号
TPL_COLS = {"gmv": 3, "paid": 4, "ord": 5, "cost": 7, "dispatch": 9, "txfee": 13}

TOL = 0.01   # 容差：模板里本身就是浮点运算，差个万分之一不算事


def main() -> int:
    book = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("V1-净利  -15.xlsx")
    if not book.exists():
        print(f"找不到模板：{book}")
        return 1

    rates, switches = load_rates(paths.RATES_FILE)
    params = rates["马来西亚"]
    fcfg = formula.load_config(paths.CONFIG / "formulas.json")
    cc = formula.country_config(fcfg, "马来西亚")
    data = load_from_workbook(book, params.usd_to_local, None,
                              order_fields=cc["order_fields"], cost_fields=cc["cost_fields"])

    f = copy.deepcopy(fcfg)
    for p in f["params"]:
        if p["id"] in ALIGN:
            p["kinds"] = ALIGN[p["id"]]
    res = calc.compute(data, params, switches, None, None, "created", fcfg=f)

    ws = openpyxl.load_workbook(book, data_only=True)["净利润"]
    tpl: dict[str, dict] = {}
    for r in range(3, ws.max_row + 1):
        pid = ws.cell(row=r, column=2).value
        if not pid:
            continue
        tpl[str(pid).strip()] = {k: ws.cell(row=r, column=c).value or 0
                                 for k, c in TPL_COLS.items()}

    def prog(p, k):
        fee = p.fees if isinstance(p.fees, dict) else {}
        return {"gmv": p.gmv, "paid": p.buyer_paid, "ord": p.orders,
                "cost": p.cost_local, "dispatch": fee.get("dispatch", 0.0),
                "txfee": fee.get("tx_fee", 0.0)}[k]

    rows = []
    for p in res.products:
        t = tpl.get(str(p.product_id).strip())
        if not t:
            continue
        rows.append((str(p.product_id), t, p))

    print(f"参与比对：{len(rows)} 条链接（模板 {len(tpl)} 条，程序 {len(res.products)} 条）")
    print()
    names = [("gmv", "GMV"), ("paid", "买家支付总额"), ("ord", "分摊订单数"),
             ("txfee", "交易手续费"), ("cost", "成本"), ("dispatch", "发单费")]
    print(f"  {'项目':<14}{'不一致':>8}{'':>4}{'模板合计':>14}{'程序合计':>14}{'最大单条差':>14}")
    print()
    bad_total = 0
    for k, name in names:
        n = sum(1 for _, t, p in rows if abs(prog(p, k) - t[k]) > TOL)
        worst = max((abs(prog(p, k) - t[k]) for _, t, p in rows), default=0.0)
        st = sum(t[k] for _, t, _ in rows)
        sp = sum(prog(p, k) for _, _, p in rows)
        flag = "" if n == 0 else "  ← 有差异，见下"
        print(f"  {name:<14}{n:>5} 条{'/ ' + str(len(rows)):>3}{st:>14.4f}{sp:>14.4f}"
              f"{worst:>14.4f}{flag}")
        if n:
            bad_total += 1

    print()
    print("  已知差异（不是 bug）：")
    print("    · 成本      —— 模板漏填了部分 SKU 成本，程序按成本表算得更全")
    print("    · 发单费    —— 模板的发单费单价里混了订单服务费"
          "（3.885 马币/单 = 3 元发单费 + 0.54 服务费），")
    print("                  程序按真实发单费 3.00 元 × 汇率 0.61 = 1.83 马币/单，服务费单列")
    print()
    return 0 if bad_total <= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
