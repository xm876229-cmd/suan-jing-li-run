"""链接维度逐条核对模板：GMV / 买家支付总额 / 分摊订单数 / 交易手续费 / 款式编码。

跟 verify_vs_template.py 的区别：
    那份核的是「钱算得对不对」（按模板 AE2=0 那一档对齐口径）；
    这份核的是「链接资料那一列（款式编码）对不对」——
    模板用的是 TEXTJOIN+UNIQUE+FILTER 的数组公式，
    程序用普通代码算，结果必须一字不差。

跑法：
    python -m scripts.verify_styles
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from src import calc, formula, paths  # noqa: E402
from src.cli import load_rates  # noqa: E402
from src.loader import load_from_workbook  # noqa: E402

TEMPLATE = "V1-净利  -15.xlsx"
COUNTRY = "马来西亚"


def main() -> int:
    tpl_path = Path(TEMPLATE)
    if not tpl_path.exists():
        print(f"找不到模板：{tpl_path.resolve()}")
        return 2

    rates, switches = load_rates(paths.RATES_FILE)
    params = rates[COUNTRY]
    fcfg = formula.load_config(paths.CONFIG / "formulas.json")
    cc = formula.country_config(fcfg, COUNTRY)
    data = load_from_workbook(
        tpl_path, params.usd_to_local, None,
        order_fields=cc["order_fields"], cost_fields=cc["cost_fields"])
    res = calc.compute(data, params, switches, None, None, "created", fcfg=fcfg)

    # 模板「净利润」页 A 列 = 款式编码
    wt = openpyxl.load_workbook(tpl_path, data_only=True)["净利润"]
    tpl: dict[str, str] = {}
    for r in range(3, wt.max_row + 1):
        pid = wt.cell(row=r, column=2).value
        if pid:
            tpl[str(pid).strip()] = str(wt.cell(row=r, column=1).value or "")

    ok = bad = 0
    diffs: list[tuple[str, str, str]] = []
    for p in res.products:
        pid = str(p.product_id).strip()
        if pid not in tpl:
            continue
        got = str(p.style_codes or "")
        exp = tpl[pid]
        if got == exp:
            ok += 1
        else:
            bad += 1
            diffs.append((pid, exp, got))

    print(f"参与比对：{ok + bad} 条链接（模板 {len(tpl)} 条，程序 {len(res.products)} 条）")
    print(f"款式编码一致 {ok} / 不一致 {bad}")
    for pid, e, g in diffs[:12]:
        print(f"  {pid}: 模板={e!r} 程序={g!r}")
    if not diffs:
        print("  → 逐条完全一致")

    # 顺带看看有几个链接有多个款式编码
    multi = [(p.product_id, p.style_codes) for p in res.products
             if "/" in str(p.style_codes or "")]
    print(f"含多个款式编码的链接：{len(multi)} 条")
    for pid, s in multi[:5]:
        print(f"  {pid}: {s}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
