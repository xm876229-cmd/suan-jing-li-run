"""把报表里的公式按 Excel 语义算出来，用于核对（只支持本报表用到的子集）。

跑批核对用，不属于产品代码。支持：
    SUMIFS(明细!$X:$X, 明细!$C:$C, $C3 [, 明细!$R:$R, 0])
    IF(cond, a, b)   IFERROR(a/b, 0)   四则运算   $X$1 开关单元格
"""
import re
from collections import defaultdict

_SUMIFS = re.compile(
    r"SUMIFS\(明细!\$([A-Z]{1,2}):\$\1,明细!\$C:\$C,\$C(\d+)"
    r"(?:,明细!\$([A-Z]{1,2}):\$\3,0)?\)")


def build_detail(wb):
    ws = wb["明细"]
    heads = {str(c.value): c.column for c in ws[2] if c.value}
    rows = defaultdict(list)
    for r in range(3, ws.max_row + 1):
        pid = ws.cell(row=r, column=heads["商品ID"]).value
        if pid:
            rows[str(pid)].append(r)
    return ws, heads, rows


def _sumifs(wsd, rows_for_pid, col, need_nosample, sample_col):
    total = 0.0
    for r in rows_for_pid:
        if need_nosample and (wsd[f"{sample_col}{r}"].value or 0):
            continue
        v = wsd[f"{col}{r}"].value
        if isinstance(v, (int, float)):
            total += v
    return total


def eval_formula(f, wsd, rows_by_pid, sample_col, sw_col=None, sw_value="", ws_sum=None):
    def repl(m):
        # $C3 是**汇总页自己**的 C3（链接ID），不是明细页的
        col, row = m.group(1), int(m.group(2))
        src = ws_sum[f"C{row}"].value if ws_sum is not None else wsd[f"C{row}"].value
        pid = "" if src is None else str(src)
        return repr(_sumifs(wsd, rows_by_pid.get(pid, []), col,
                            bool(m.group(3)), sample_col))

    expr = _SUMIFS.sub(repl, f)

    # 剩下的 $X<n>：开关格取传进来的口径，其余取汇总页同格的值（辅助列）
    def repl_cell(m):
        col, row = m.group(1), int(m.group(2))
        if sw_col and col == sw_col and row == 1:
            return repr(sw_value)
        if ws_sum is not None:
            v = ws_sum[f"{col}{row}"].value
            return repr(float(v)) if isinstance(v, (int, float)) else "0.0"
        return repr(str(wsd[f"C{row}"].value or ""))

    expr = re.sub(r"\$([A-Z]{1,2})\$?(\d+)", repl_cell, expr)
    expr = expr.replace("IFERROR(", "__iferr(").replace("IF(", "__iif(")
    expr = expr.replace("=", "==")
    expr = expr.lstrip("=")

    try:
        return eval(expr, {"__builtins__": {}},  # noqa: S307
                    {"__iif": lambda c, a, b: a if c else b,
                     "__iferr": lambda a, b: a})
    except ZeroDivisionError:
        return 0.0
