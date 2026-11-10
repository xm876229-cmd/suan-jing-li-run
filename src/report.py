"""Excel 报表输出。

模板结构（自设计，非照搬原表）：
    汇总      每个商品一行，指标横向铺开，可筛选、可排序，带净利色阶
    费用拆解   长表形态（商品 × 费用项），方便透视和做图
    日趋势     按日期汇总 GMV / 净利，看波动
    异常      缺成本、缺映射、成本推导、未付款、取消，逐条给建议动作
    明细      订单行级原始数据，用于追查任何一个数字的来源
    口径      本次运行的参数快照与口径说明，保证结果可追溯
"""

from __future__ import annotations

from datetime import datetime
import os
import re
import sys
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .calc import CalcResult, Params, Switches, ZERO
from .loader import OrderLine

# ---------- 样式 ----------
HEAD_FILL = PatternFill("solid", fgColor="1F3B4D")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="1F3B4D")
SUB_FONT = Font(size=9, color="6B7B8C")
TOTAL_FILL = PatternFill("solid", fgColor="EDF2F7")
NEG_FONT = Font(color="C0392B")
THIN = Side(style="thin", color="D5DDE5")
BORDER = Border(bottom=THIN)
TOP_BORDER = Border(top=Side(style="medium", color="1F3B4D"))

MONEY = '#,##0.00;[Red]-#,##0.00'
INT = '#,##0'
PCT = '0.00%;[Red]-0.00%'


def _head(ws: Worksheet, headers: list[str], row: int = 2) -> None:
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def _title(ws: Worksheet, text: str, sub: str = "", span: int = 8) -> None:
    ws.cell(row=1, column=1, value=text).font = TITLE_FONT
    if sub:
        ws.cell(row=1, column=2, value=sub).font = SUB_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)


def _widths(ws: Worksheet, widths: dict[int, int]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def _w(ws, row: int, col: int, value):
    """写一个**数据**单元格（不是公式）。

    openpyxl 见到以 `=` 开头的字符串就当成公式写进去：
    商品名写成 "=SALE= 清仓"，用 Excel 打开显示的是 #NAME?；
    更糟的是 "=cmd|' /C calc'!A0" 这种，会触发 Excel 的 DDE 安全警告。
    这些都是用户自己的数据，一律按纯文本写。

    注意：真正的公式（SUMIFS 那些）不要走这里，直接用 ws.cell(..., value=f)。
    """
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value[:1] == "=":
        cell.data_type = "s"
    return cell


def _fmt_row(ws: Worksheet, row: int, money_cols: list[int], pct_cols: list[int],
             int_cols: list[int] = ()) -> None:
    for c in money_cols:
        ws.cell(row=row, column=c).number_format = MONEY
    for c in pct_cols:
        ws.cell(row=row, column=c).number_format = PCT
    for c in int_cols:
        ws.cell(row=row, column=c).number_format = INT


# ---------------- 汇总 ----------------

# 有 role 的参数是报表固定列，其余都算「费用」，一列一项。
ROLE_KEYS = {"gmv", "cost", "order_count", "items", "buyer_paid"}

BASE_HEADERS = [
    "排名", "款式编码", "链接（商品ID）", "商品名称",
    "GMV", "订单数", "件数", "客单价", "买家实付",
    "成本", "毛利", "毛利额",
]

# 费用占比列：{费用项 id: 列名}
# 放在费用列后面，看「这项费用占 GMV 多少」一眼就明白。
# 口径跟模板《V1-净利》一致：费用 ÷ GMV。
RATIO_ITEMS: list[tuple[str, str]] = [
    ("dispatch", "发单费占比"),
    ("ads", "广告费比"),
]
# 上面这份只是历史上写死的两项。现在改成**按费用项动态生成**：
# 用户新增一项费用（联盟佣金、平台佣金、自定义…）都能有对应的占比列。
# 写死的时候，界面里能勾「联盟佣金占比」，报表却永远不出这一列 ——
# 因为 default_keys 里没有它，勾了也被静默过滤掉（2026-11-04 用户反馈）。


def ratio_items(fees: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """费用项 → 占比列（该项 ÷ GMV）。前两项沿用老标题，其余用「X占比」。"""
    special = {"dispatch": "发单费占比", "ads": "广告费比"}
    return [(fid, special.get(fid, f"{label}占比")) for fid, label in fees]


def fee_items(res: CalcResult) -> list[tuple[str, str]]:
    """费用项清单：{id, 中文名}。按参数表顺序，用户加一项就多一列/多一组。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for it in res.params_cfg or []:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "").strip()
        if not fid or fid in seen:
            continue
        role = str(it.get("role") or "").strip()
        if role in ROLE_KEYS or fid in ROLE_KEYS:
            continue      # GMV / 成本 / 订单数 / 件数 / 实付 已有固定列
        seen.add(fid)
        out.append((fid, str(it.get("label") or fid)))
    # 兜底：明细里有、参数表里没有的费用项（改过配置的旧数据）也要出来
    for p in res.products:
        for fid in (p.fees or {}):
            if fid not in seen:
                seen.add(fid)
                out.append((fid, fid))
    return out


def _default_report_columns(fees, ratios, has_alt) -> list[str]:
    """报表「汇总」页默认展示的所有列键（顺序即展示顺序）。

    用户可在「计算参数 → 报表列」里勾选，只显示自己关心的列。
    数值列标题会自动带上该国货币单位（马来=RM、菲律宾=PHP…）。
    """
    keys = ["rank", "style", "product", "name", "gmv", "orders",
            # 取消结构（用户 2026-11-05 要的 5 列），紧跟在订单数后面
            "pre_cancel_orders", "post_cancel_orders", "post_cancel_share",
            "all_cancel_share", "post_cancel_total_share",
            "items", "aov", "buyer_paid", "cost", "gross_profit", "gross_margin"]
    for fid, _ in fees:
        keys.append("fee:" + fid)
    for fid, _ in ratios:
        keys.append("ratio:" + fid)
    keys += ["fee_total", "net_rate", "net", "net_cny"]
    if has_alt:
        keys += ["alt_net", "alt_diff"]
    return keys


def _summary_specs(res: CalcResult, fees, ratios, has_alt, enabled,
                   titles: dict | None = None) -> list[dict]:
    """生成有序的列描述。每个描述：
    key / header / kind(text|int|money|pct) / getp(p,idx) / gett() / neg(红字) / cur

    titles 是「列键 → 自定义标题」：用户想给列改名（如「毛利率」→「链接毛利率」）
    就写在这里，其余列仍用默认标题。
    """
    t = res.total
    g = lambda p, k: float((p.fees or {}).get(k, 0.0))

    def ratio(p, fid):
        base = float(p.gmv or 0.0)
        return (float((p.fees or {}).get(fid, 0.0)) / base) if base else 0.0

    def tratio(fid):
        base = float(t.gmv or 0.0)
        return (float((t.fees or {}).get(fid, 0.0)) / base) if base else 0.0

    specs: list[dict] = []

    def add(key, header, kind, getp, gett, neg=False, cur="local"):
        # 自定义标题优先：改名只改这一列的表头，算法不动
        h = (titles or {}).get(key, header)
        specs.append({"key": key, "header": h, "kind": kind,
                      "getp": getp, "gett": gett, "neg": neg, "cur": cur})

    if "rank" in enabled:
        add("rank", "排名", "int", lambda p, i: i, lambda: "")
    if "style" in enabled:
        add("style", "款式编码", "text", lambda p, i: p.style_codes, lambda: "合计")
    if "product" in enabled:
        add("product", "链接（商品ID）", "text", lambda p, i: p.product_id, lambda: "")
    if "name" in enabled:
        add("name", "商品名称", "text", lambda p, i: p.product_name, lambda: "")
    if "gmv" in enabled:
        add("gmv", "GMV", "money", lambda p, i: p.gmv, lambda: t.gmv)
    if "orders" in enabled:
        add("orders", "订单数", "int", lambda p, i: p.orders, lambda: t.orders)

    # ── 取消结构（2026-11-05）──
    #
    # 这几列是**订单事实**，跟参数表里勾不勾没关系：勾掉售后，订单数列会变小，
    # 但「售后取消单数」照样显示——不然取消得多不多就看不出来了。
    #
    # 分母「总单数」= 正常 + 所有取消 + 退货退款（不含 0 元样品、不含未付款），
    # 这是用户给的口径；「订单数」列含 0 元样品，两者会有小差，属预期。
    def _cancel_nums(x):
        pre = float(getattr(x, "ord_pre_cancel", 0) or 0)
        post = float(getattr(x, "ord_post_cancel", 0) or 0)
        normal = float(getattr(x, "ord_normal", 0) or 0)
        refund = float(getattr(x, "ord_refunded", 0) or 0)
        allc = pre + post
        total = normal + refund + allc
        return pre, post, allc, total

    if "pre_cancel_orders" in enabled:
        add("pre_cancel_orders", "售前取消单数", "int",
            lambda p, i: _cancel_nums(p)[0], lambda: _cancel_nums(t)[0])
    if "post_cancel_orders" in enabled:
        add("post_cancel_orders", "售后取消单数", "int",
            lambda p, i: _cancel_nums(p)[1], lambda: _cancel_nums(t)[1])
    if "post_cancel_share" in enabled:
        add("post_cancel_share", "售后取消占比", "pct",
            lambda p, i: (_cancel_nums(p)[1] / _cancel_nums(p)[2]) if _cancel_nums(p)[2] else 0.0,
            lambda: (_cancel_nums(t)[1] / _cancel_nums(t)[2]) if _cancel_nums(t)[2] else 0.0)
    if "all_cancel_share" in enabled:
        add("all_cancel_share", "取消占总单比", "pct",
            lambda p, i: (_cancel_nums(p)[2] / _cancel_nums(p)[3]) if _cancel_nums(p)[3] else 0.0,
            lambda: (_cancel_nums(t)[2] / _cancel_nums(t)[3]) if _cancel_nums(t)[3] else 0.0)
    if "post_cancel_total_share" in enabled:
        add("post_cancel_total_share", "售后占总单比", "pct",
            lambda p, i: (_cancel_nums(p)[1] / _cancel_nums(p)[3]) if _cancel_nums(p)[3] else 0.0,
            lambda: (_cancel_nums(t)[1] / _cancel_nums(t)[3]) if _cancel_nums(t)[3] else 0.0)
    if "items" in enabled:
        add("items", "件数", "int", lambda p, i: p.items, lambda: t.items)
    if "aov" in enabled:
        add("aov", "客单价", "money", lambda p, i: p.aov, lambda: (t.aov if t.orders else 0))
    if "buyer_paid" in enabled:
        add("buyer_paid", "买家实付", "money", lambda p, i: p.buyer_paid, lambda: t.buyer_paid)
    if "cost" in enabled:
        add("cost", "成本", "money", lambda p, i: p.cost_local, lambda: t.cost_local)
    if "gross_profit" in enabled:
        # 毛利 = 金额（GMV − 成本）；毛利率（占比）走 gross_margin
        add("gross_profit", "毛利", "money",
            lambda p, i: p.gmv - p.cost_local, lambda: t.gmv - t.cost_local)
    if "gross_margin" in enabled:
        # 链接毛利率 = (链接GMV − 商品成本(换成本地货币后)) / 链接GMV
        # 关键：分子分母都用 GMV（折扣前小计 − 卖家折扣），**不是**买家实付。
        add("gross_margin", "链接毛利率", "pct",
            lambda p, i: ((p.gmv - p.cost_local) / p.gmv) if p.gmv else 0.0,
            lambda: ((t.gmv - t.cost_local) / t.gmv) if t.gmv else 0.0)
    for fid, label in fees:
        if ("fee:" + fid) in enabled:
            add("fee:" + fid, label, "money",
                (lambda k: (lambda p, i: g(p, k)))(fid),
                (lambda k: (lambda: g(t, k)))(fid))
    for fid, name in ratios:
        if ("ratio:" + fid) in enabled:
            add("ratio:" + fid, name, "pct",
                (lambda k: (lambda p, i: ratio(p, k)))(fid),
                (lambda k: (lambda: tratio(k)))(fid))
    if "fee_total" in enabled:
        add("fee_total", "费用合计", "money",
            lambda p, i: sum(g(p, fid) for fid, _ in fees),
            lambda: sum(g(t, fid) for fid, _ in fees))
    if "net_rate" in enabled:
        add("net_rate", "净利率", "pct", lambda p, i: p.net_margin, lambda: t.net_margin)
    if "net" in enabled:
        add("net", "净利", "money", lambda p, i: p.net_local, lambda: t.net_local, neg=True)
    if "net_cny" in enabled:
        add("net_cny", "净利", "money",
            lambda p, i: p.net_cny(res.params.cny_to_local),
            lambda: t.net_cny(res.params.cny_to_local), neg=True, cur="cny")
    if has_alt:
        if "alt_net" in enabled:
            add("alt_net", f"净利（{res.compare_label}）", "money",
                (lambda p, i: (res.compare.get(p.product_id).net_local
                               if res.compare.get(p.product_id) is not None else ZERO)),
                lambda: (res.compare_total or ZERO), neg=True)
        if "alt_diff" in enabled:
            add("alt_diff", "样品差额", "money",
                (lambda p, i: (p.net_local - (res.compare.get(p.product_id).net_local
                                           if res.compare.get(p.product_id) is not None else ZERO))),
                lambda: (t.net_local - (res.compare_total or ZERO)), neg=True)

    # 列的先后由用户在「报表列」里排：enabled 是有序的，照着它排序。
    # 上面那段 add(...) 的顺序只是默认值；不再在代码里写死，
    # 否则界面怎么拖、报表都是老样子。
    if isinstance(enabled, (list, tuple)) and enabled:
        order = {k: i for i, k in enumerate(enabled)}
        specs.sort(key=lambda s: order.get(s["key"], len(order)))
    return specs


def _summary(ws: Worksheet, res: CalcResult, subtitle: str,
             report_columns: list[str] | None = None,
             total_position: str = "bottom",
             report_titles: dict | None = None,
             with_formula: bool = False) -> None:
    # 一条链接（商品ID）一行：同一链接下的多个 SKU 合并，
    # 不同链接即使卖家编码相同也绝不合并——卖家编码可在多条链接间复用，
    # 只有商品ID 是链接的唯一标识。
    fees = fee_items(res)
    # 费用占比列：只对实际存在的费用项出列（用户把某项删了，列也没了）
    ratios = [(fid, name) for fid, name in ratio_items(fees)
              if any(fid in (p.fees or {}) for p in res.products)]
    has_alt = res.compare_total is not None

    # 用户可勾选要展示的列；没配置就展示全部（含款式编码、各费用、净利等）。
    default_keys = _default_report_columns(fees, ratios, has_alt)
    if report_columns:
        enabled = [k for k in report_columns if k in default_keys]
        if not enabled:
            enabled = default_keys
    else:
        enabled = default_keys
    # 排名以前是强制的（怕表格空着不好看），但它是纯装饰列：
    # 用户不需要的时候应该能关掉（2026-11-04 反馈）。
    # 上面已经保证 enabled 非空，所以去掉这段不会出现空表。

    # 但「链接（商品ID）」在带公式版里**不能关**：汇总页每条链接靠它
    # 去明细里 SUMIFS 匹配。关掉就没列可匹配 —— 2026-11-04 那次循环引用
    # 就是这类问题（列移位后拿 GMV 当条件）。带公式时自动补回来。
    if with_formula and "product" in default_keys and "product" not in enabled:
        if "style" in enabled:
            enabled.insert(enabled.index("style") + 1, "product")
        else:
            enabled.insert(0, "product")

    specs = _summary_specs(res, fees, ratios, has_alt, enabled, titles=report_titles)

    # 货币单位：金额列标题带上国家货币代码（马来=MYR、菲律宾=PHP…），看清是哪国钱
    cur_local = (res.params.currency or "").strip()
    headers = []
    for s in specs:
        h = s["header"]
        if s["kind"] == "money":
            c = cur_local if s.get("cur", "local") == "local" else "CNY"
            if c:
                h = f"{h}（{c}）"
        headers.append(h)

    span_used = max(8, len(headers))
    # 第 1 行留给「合计」，表头放第 2 行。
    # 打开文件第一眼就是总数，而且冻结窗格钉住前两行，往下翻也一直看得见。
    # 原来第 1 行是一句绿色标题「净利润汇总（每条链接一行）」——占位置、没信息量。
    _head(ws, headers)

    # ── 汇总口径开关：含 / 不含 0 元样品单 ──
    #
    # 放在标题行右侧、合并区之外，横着滑也不会跑到看不见的地方。
    # 带公式导出时是真开关（数据验证下拉），所有金额/订单数按它现算；
    # 数值版没有明细辅助列可引，只能显示当前口径并提示怎么才能切。
    # 辅助列（广告费/达人佣金）占 len(headers)+1、+2 且被隐藏，
    # 开关必须排在它们右边，否则标签会被写进隐藏列，用户根本看不到。
    # 控件必须排在**所有辅助列右边**：辅助列是 广告费/联盟佣金/售前/售后/正常/退货
    # 共 6 个（带公式时），只按 +2 算会正好压在第 4、5 个上——2026-11-05 实测：
    # 20 个可见列时 ctl_col=24 落在「售后取消单数(辅助)」，「含样品」开关落在
    # 「正常单数(辅助)」，随后这两个列被 hidden 隐藏 → 开关看不见；Excel 一旦
    # 选中被隐藏的单元格还会把整片辅助列拽出来，表格中间就多出 6 列乱七八糟的东西。
    _aux_end = len(headers) + (6 if with_formula else 0)
    ctl_col = max(span_used, _aux_end) + 2
    sw_col = ctl_col + 1
    # 表格与控件之间留一条窄空列，视觉上分开，别贴在一起
    try:
        ws.column_dimensions[get_column_letter(ctl_col - 1)].width = 2
    except Exception:  # noqa: BLE001
        pass
    SW = f"${get_column_letter(sw_col)}$2"
    cur_label = "不含样品" if res.exclude_zero_amount else "含样品"
    _lbl = ws.cell(row=2, column=ctl_col,
                   value="汇总口径" if with_formula else "汇总口径（导出带公式才能切）")
    _lbl.font = Font(size=9, color="8A8A8A")
    _lbl.alignment = Alignment(horizontal="right", vertical="center")
    _sw = ws.cell(row=2, column=sw_col, value=cur_label)
    _sw.font = Font(size=11, bold=True, color="8A5A00" if with_formula else "9A9A9A")
    _sw.fill = PatternFill("solid", fgColor="FFE9A8" if with_formula else "F0F0F0")
    _sw.alignment = Alignment(horizontal="center", vertical="center")
    _sw.border = BORDER
    ws.column_dimensions[get_column_letter(ctl_col)].width = 14 if with_formula else 30
    ws.column_dimensions[get_column_letter(sw_col)].width = 12
    # 成本缺失提醒：放在第 1 行、控制区那一列（不在表格范围内，不动任何列宽和公式）。
    # 用户要的是「打开报表第一眼就能看到」，所以不能只写进「异常」页。
    _miss_rows = int(getattr(res, "missing_cost_rows", 0) or 0)
    if _miss_rows:
        _codes = getattr(res, "missing_cost_codes", {}) or {}
        _warn = ws.cell(row=1, column=ctl_col,
                        value=f"⚠ 商品成本缺失：{len(_codes)} 个编码 / {_miss_rows} 行，"
                              f"成本按 0 计（净利偏高）→ 见「口径」页")
        _warn.font = Font(size=10, bold=True, color="C0392B")
        _warn.alignment = Alignment(horizontal="left", vertical="center")

    if with_formula:
        try:
            from openpyxl.worksheet.datavalidation import DataValidation
            _dv = DataValidation(type="list", formula1='"含样品,不含样品"',
                                 allow_blank=False, showDropDown=False)
            _dv.error = "只能选「含样品」或「不含样品」"
            _dv.errorTitle = "汇总口径"
            _dv.prompt = ("含样品：0 元样品单的发单费、成本、订单数都算进来；"
                          "不含样品：整行剔除（纯正常订单口径）")
            _dv.promptTitle = "切换汇总口径"
            _dv.showInputMessage = True
            _dv.showErrorMessage = True
            ws.add_data_validation(_dv)
            _dv.add(_sw)
        except Exception:  # noqa: BLE001  下拉加不上也不能让报表出不来
            pass

    # 行：合计 + 每条链接。合计行位置可配（顶部/底部）。
    t = res.total
    total_vals = [s["gett"]() for s in specs]
    data_vals = [[s["getp"](p, i) for s in specs]
                 for i, p in enumerate(res.products, 1)]
    total_row = None
    if total_position == "none":
        # 不显示合计行：只出每条链接，合计自己用 Excel 求和或者看别处
        rows = data_vals
    elif total_position == "top":
        # 合计放**第 1 行**（表头上面）：打开第一眼就是总数。
        # 数据从第 3 行开始，表头在第 2 行。
        rows = data_vals
        total_row = 1
    else:
        rows = data_vals + [total_vals]

    last_text = 0
    for ci, s in enumerate(specs, 1):
        if s["kind"] == "text":
            last_text = ci
    net_rate_ci = next((ci for ci, s in enumerate(specs, 1) if s["key"] == "net_rate"), None)

    # 「带公式导出」：汇总页不再写死数字，改成 SUMIFS 引用明细页。
    # 明细里那几列「计入GMV / 计入订单数 / 净利贡献」已经按当前口径过滤过，
    # 所以 SUMIFS 出来的数跟程序算的完全一致；改明细、改费率都能跟着动。
    col_of = {sp["key"]: ci for ci, sp in enumerate(specs, 1)}
    det_pids = {l.product_id or "无ID" for l in (res.lines or [])}
    fx = float(res.params.cny_to_local or 1) or 1.0
    # 广告费 / 达人佣金是「按链接整段取数」的，摊不到订单行上，
    # 所以净利公式 = 明细净利贡献 − 这两项；两项放在最右边两列（隐藏），
    # 不占用户的可见列。
    ads_fid = next((fid for fid, nm in fees if "广告" in str(nm)), None)
    # 这项 2026-11-04 从「达人佣金」改名成「联盟佣金」。只认「达人」的话
    # 改名后就匹配不到 → 佣金走死数分支、占比列也算不出、净利还少减一次。
    # 两个名字都认，谁先来都行。
    cm_fid = next((fid for fid, nm in fees
                   if ("达人" in str(nm) or "联盟" in str(nm))), None)
    aux_ci = len(headers) + 1
    ads_ci, cm_ci = aux_ci, aux_ci + 1
    # 取消结构的四个基数也走辅助列：合计行要写成公式（不然一切口径就僵住，
    # 打包体检也会报「合计行有常数」）。辅助列在表格右边、默认隐藏。
    pre_ci, post_ci = aux_ci + 2, aux_ci + 3
    nrm_ci, rfd_ci = aux_ci + 4, aux_ci + 5
    if with_formula:
        ws.cell(row=2, column=ads_ci, value="广告费(辅助)")
        ws.cell(row=2, column=cm_ci, value="联盟佣金(辅助)")
        for _ci, _nm in ((pre_ci, "售前取消单数(辅助)"), (post_ci, "售后取消单数(辅助)"),
                         (nrm_ci, "正常单数(辅助)"), (rfd_ci, "退货单数(辅助)")):
            ws.cell(row=2, column=_ci, value=_nm)

    # 明细页辅助列的位置跟着表头定义走，别写死字母（列一加就全错）。
    # 明细在「成本合计」后面插了 5 个费用列 + 费用合计，这里要一起算进去，
    # 否则汇总的 SUMIFS 会整天指向费用列，数字不会报错但会全错。
    _db = (len(DETAIL_HEADERS) + len(_row_fee_items(res.params_cfg)) + 1)
    _c_sample = get_column_letter(_db + 2)     # 是否样品：1/0
    _c_gmv = get_column_letter(_db + 3)        # 计入GMV
    _c_ord = get_column_letter(_db + 4)        # 计入订单数
    _c_item = get_column_letter(_db + 5)       # 计入件数
    _c_paid = get_column_letter(_db + 6)       # 计入买家实付
    _c_cost = get_column_letter(_db + 7)       # 计入成本 CNY
    _c_netw = get_column_letter(_db + 8)       # 净利贡献（含样品）
    _c_netwo = get_column_letter(_db + 9)      # 净利贡献（不含样品，样品行是 0）

    # 费用按什么量计：从参数表里的 expr 认。
    # 「本行订单数 * 发单费单价」按订单数，「本行GMV * 平台佣金率」按 GMV，
    # 「广告花费」这种整期取数的没有单量基准，只能写成死数。
    def _fee_base_of(fid: str) -> str:
        for it in (res.cfg_with_sample or res.params_cfg or []):
            if not isinstance(it, dict) or str(it.get("id") or "") != fid:
                continue
            e = str(it.get("expr") or "")
            if "订单数" in e:
                return "orders"
            if "买家实付" in e:
                return "paid"
            if "GMV" in e:
                return "gmv"
            return "fixed"
        return "gmv"

    def _fee_counts_sample(fid: str) -> bool:
        """这项费用原本算不算 0 元样品单（看含样品那套口径的 kinds）。

        不算的那几项（订单服务费、交易手续费）切口径时就不该动；
        一起按「含/不含样品」去缩放，会把它算多。
        """
        for it in (res.cfg_with_sample or []):
            if not isinstance(it, dict) or str(it.get("id") or "") != fid:
                continue
            ks = it.get("kinds")
            if not isinstance(ks, list):
                return False              # 不看类别（广告/佣金），与口径无关
            return "zero_amount" in ks
        return True

    def _fee_expr(fid: str, r: int, p) -> str | None:
        """这一行的某项费用，按当前口径算（单位价 × 口径下的量）。"""
        if p is None:
            return None
        if not _fee_counts_sample(fid):
            # 这项本来就不含样品单，切口径不影响金额，直接给常数
            return repr(float((p.fees or {}).get(fid, 0) or 0))
        base = _fee_base_of(fid)
        if base == "fixed":
            return None                      # 广告/达人佣金跟订单无关，用原值
        if base == "orders":
            bval, bexpr = float(p.orders or 0), _scope(_c_ord, r)
        elif base == "paid":
            bval, bexpr = float(p.buyer_paid or 0), _scope(_c_paid, r)
        else:
            bval, bexpr = float(p.gmv or 0), _scope(_c_gmv, r)
        if not bval:
            # 按量计费的费用，量为 0 就是 0。这里以前返回 None 让上层退回
            # 「导出时的死数」，于是口径切到「不含样品」、这条链接一单都不剩时，
            # 发单费还挂着原值——合计会比正确的多出一截。
            return "0.0"
        unit = float((p.fees or {}).get(fid, 0) or 0) / bval
        return f"({unit}*({bexpr}))"

    # 两个商品ID列都按**实际位置**算，绝不写死字母：
    #   _c_pid_sum 汇总页「链接（商品ID）」——用户能在报表列里勾掉「排名」，
    #              一勾掉整行往左移一格，商品ID 就从 C 变成 B；
    #   _c_pid_det 明细页「商品ID」——明细表头也加过费用列。
    # 写死的后果（2026-11-04 实测）：取消排名后 _sumifs 变成
    # `SUMIFS(明细!$Y:$Y,明细!$C:$C,$C3)`，而汇总页 C3 就是 GMV 本身 ——
    # 拿自己当条件匹配自己，Excel 报「循环引用」，整张表所有数字变 0。
    _c_pid_sum = get_column_letter(col_of.get("product", 3))
    _c_pid_det = get_column_letter(DETAIL_HEADERS.index("商品ID") + 1)

    def _sumifs(col: str, r: int, no_sample: bool = False) -> str:
        cond = f",明细!${_c_sample}:${_c_sample},0" if no_sample else ""
        return (f"SUMIFS(明细!${col}:${col},"
                f"明细!${_c_pid_det}:${_c_pid_det},${_c_pid_sum}{r}{cond})")

    def _col_total(col: str) -> str:
        """明细某一列**整表**求和（按当前口径要不要剔掉样品行）。

        合计行用它算「总成本」这类不属于单条链接的量，
        这样用户把「成本」列勾掉，合计行也不会退回死数。
        """
        # 「含样品」那半边必须是 SUM，不能写 SUMIFS(一个参数)：
        # SUMIFS 至少要 3 个参数（求和区 + 条件区 + 条件），只给求和区时
        # Excel/WPS 判成非法公式 —— 单元格直接空白，不报错也不显示 0。
        # 2026-11-05 实测：把「成本」列勾掉后，合计行的「链接毛利率」K1 全空，
        # 因为那条公式走 _col_total 拼成了 SUMIFS(明细!$AC:$AC)。
        return (f'IF({SW}="不含样品",SUMIFS(明细!${col}:${col},'
                f'明细!${_c_sample}:${_c_sample},0),SUM(明细!${col}:${col}))')

    def _scope(col: str, r: int, tail: str = "") -> str:
        """按汇总口径取数：含样品取全部行，不含样品只取「是否样品=0」的行。"""
        return (f'IF({SW}="不含样品",{_sumifs(col, r, True)},'
                f'{_sumifs(col, r, False)}){tail}')

    # 派生指标一律从明细列直算，**不引用同行可见单元格**。
    # 因为用户能勾掉「成本」「净利」这些列，一勾掉引用就断，数就成 0 了。
    A_L, M_L = get_column_letter(ads_ci), get_column_letter(cm_ci)

    def _net_total(last_row: int) -> str:
        """合计行的净利：明细净利贡献合计 − 广告费合计 − 联盟佣金合计。

        不引用同行的「净利」列 —— 那列可能被用户勾掉，
        一勾掉净利率的合计就退回导出时的死数，切口径不跟着动。
        """
        aux = f"+SUM(${A_L}3:${A_L}{last_row})+SUM(${M_L}3:${M_L}{last_row})"
        return (f'IF({SW}="不含样品",{_col_total(_c_netwo)},{_col_total(_c_netw)})'
                f'{aux}')

    def _net_expr(r: int) -> str:
        # 两套口径各有一列净利贡献，按开关取一列即可——
        # 「不含样品」那列里样品行本身就是 0，不用再加条件。
        return (f'IF({SW}="不含样品",'
                f'{_sumifs(_c_netwo, r)},{_sumifs(_c_netw, r)})'
                f'-${A_L}{r}-${M_L}{r}')

    def _f(key: str, r: int, pid_ok: bool, p=None) -> str | None:
        if not with_formula or not pid_ok:
            return None
        if key == "gmv":
            return f"={_scope(_c_gmv, r)}"
        if key == "orders":
            return f"={_scope(_c_ord, r)}"
        if key in ("net", "net_cny"):
            base = _net_expr(r)
            return f"=({base})" if key == "net" else f"=({base})/{fx}"
        if key == "cost":
            return f"={_scope(_c_cost, r, f'*{fx}')}"
        if key == "items":
            return f"={_scope(_c_item, r)}"
        if key == "aov":
            return f'=IFERROR({_scope(_c_gmv, r)}/{_scope(_c_ord, r)},0)'
        if key == "gross_profit":
            return f"={_scope(_c_gmv, r)}-{_scope(_c_cost, r, f'*{fx}')}"
        if key == "gross_margin":
            g = _scope(_c_gmv, r)
            k = _scope(_c_cost, r, f"*{fx}")
            return f"=IFERROR(({g}-{k})/({g}),0)"
        if key == "net_rate":
            return f"=IFERROR(({_net_expr(r)})/({_scope(_c_gmv, r)}),0)"
        if key == "fee_total":
            if p is None:
                return None
            parts: list[str] = []
            for fid, _nm in fees:
                if fid in (ads_fid, cm_fid):
                    # 这两项引辅助列，不写死数——合计行切口径时才不会僵着。
                    parts.append(f"${get_column_letter(ads_ci if fid == ads_fid else cm_ci)}{r}")
                    continue
                e = _fee_expr(fid, r, p)
                parts.append(e if e else
                             repr(float((p.fees or {}).get(fid, 0) or 0)))
            return "=" + "+".join(parts) if parts else None
        if key.startswith("fee:") or key.startswith("ratio:"):
            fid = key.split(":", 1)[1]
            # 广告费 / 达人佣金是「按链接整期取数」的，摊不到订单行，
            # 既不能 SUMIFS，也不该写成 `=7631.349` 这种只有一个数的假公式——
            # 那种公式看着像活的，其实改什么都不动。
            # 改成引用同行辅助列：改辅助列、换一批数据，它才跟着变。
            if fid in (ads_fid, cm_fid):
                aux = get_column_letter(ads_ci if fid == ads_fid else cm_ci)
                if key.startswith("fee:"):
                    return f"=${aux}{r}"
                return f"=IFERROR(${aux}{r}/({_scope(_c_gmv, r)}),0)"
            e = _fee_expr(fid, r, p)
            if e is None:
                return None
            if key.startswith("fee:"):
                return f"={e}"
            return f"=IFERROR({e}/({_scope(_c_gmv, r)}),0)"
        return None


    # 取消结构 5 列在合计行的公式：全部从隐藏辅助列求和，再算比值。
    # 好处：跟「这 5 列里用户勾掉了哪几列」无关，也不会留常数（体检会报）。
    _CANCEL_KEYS = ("pre_cancel_orders", "post_cancel_orders", "post_cancel_share",
                    "all_cancel_share", "post_cancel_total_share")

    def _cancel_total_expr(key: str, end: int) -> str | None:
        def sm(ci: int) -> str:
            L = get_column_letter(ci)
            return f"SUM({L}3:{L}{end})"

        pre, post = sm(pre_ci), sm(post_ci)
        nrm, rfd = sm(nrm_ci), sm(rfd_ci)
        allc = f"({pre}+{post})"
        total = f"({pre}+{post}+{nrm}+{rfd})"
        if key == "pre_cancel_orders":
            return f"={pre}"
        if key == "post_cancel_orders":
            return f"={post}"
        if key == "post_cancel_share":
            return f"=IFERROR({post}/{allc},0)"
        if key == "all_cancel_share":
            return f"=IFERROR({allc}/{total},0)"
        if key == "post_cancel_total_share":
            return f"=IFERROR({post}/{total},0)"
        return None

    row = 3
    for ri, vals in enumerate(rows):
        # 合计在第 1 行时，rows 里**全是数据**，不能再按 ri==0 判合计：
        # 那样第一条链接会被写成「假合计行」——灰底加粗、值是对的、但没有公式、
        # 也不带辅助列。平时看不出来，一切口径（含/不含样品）它就不跟着动，
        # 合计和明细对不上还不报错。最难查的一类错。
        is_total = (total_position == "bottom" and ri == len(rows) - 1)
        if is_total:
            pid = ""
        else:
            idx = ri
            pid = str(getattr(res.products[idx], "product_id", "")) if idx < len(res.products) else ""
        pid_ok = bool(pid) and pid in det_pids
        prod = None if is_total else (res.products[idx] if idx < len(res.products) else None)
        if with_formula and prod is not None:
            # 广告费 / 达人佣金摊不到订单行上，单放两列给净利公式扣。
            # 不管用户有没有勾「净利」列都要写，否则净利率那边引用到空值。
            ws.cell(row=row, column=ads_ci,
                    value=float((prod.fees or {}).get(ads_fid, 0) or 0) if ads_fid else 0.0)
            ws.cell(row=row, column=cm_ci,
                    value=float((prod.fees or {}).get(cm_fid, 0) or 0) if cm_fid else 0.0)
            ws.cell(row=row, column=pre_ci, value=float(getattr(prod, "ord_pre_cancel", 0) or 0))
            ws.cell(row=row, column=post_ci, value=float(getattr(prod, "ord_post_cancel", 0) or 0))
            ws.cell(row=row, column=nrm_ci, value=float(getattr(prod, "ord_normal", 0) or 0))
            ws.cell(row=row, column=rfd_ci, value=float(getattr(prod, "ord_refunded", 0) or 0))
        for ci, (s, v) in enumerate(zip(specs, vals), 1):
            f = _f(s["key"], row, pid_ok, prod)
            cell = ws.cell(row=row, column=ci, value=v if f is None else f)
            cell.border = BORDER
            if s["kind"] == "money":
                cell.number_format = MONEY
            elif s["kind"] == "pct":
                cell.number_format = PCT
            elif s["kind"] == "int":
                cell.number_format = INT
            if s["neg"] and isinstance(v, (int, float)) and v < 0:
                cell.font = NEG_FONT
            if is_total:
                cell.fill = TOTAL_FILL
                cell.font = (Font(bold=True, color="C0392B")
                             if (s["neg"] and isinstance(v, (int, float)) and v < 0)
                             else Font(bold=True))
        if is_total and with_formula:
            # 合计行也写成公式：能加的直接 SUM，派生列用合计之间的比值。
            # 不然一切口径，明细和各行都变了、合计还是导出时那组数。
            span_end = row - 1
            if span_end >= 3:
                def _cl(k):
                    ci = col_of.get(k)
                    return get_column_letter(ci) if ci else None

                def _s(k):
                    L = _cl(k)
                    return f"SUM({L}3:{L}{span_end})" if L else None

                _SUMMABLE = {"gmv", "orders", "items", "cost", "net", "net_cny",
                             "fee_total", "buyer_paid"}
                for ci2, sp in enumerate(specs, 1):
                    key = sp["key"]
                    f = None
                    if key in _SUMMABLE or key.startswith("fee:"):
                        e = _s(key)
                        f = f"={e}" if e else None
                    elif key in _CANCEL_KEYS:
                        f = _cancel_total_expr(key, span_end)
                    elif key == "aov":
                        g = _s("gmv") or _col_total(_c_gmv)
                        o = _s("orders") or _col_total(_c_ord)
                        f = f"=IFERROR({g}/{o},0)"
                    elif key == "gross_profit" and _cl("gmv"):
                        # 「成本」列被勾掉时 _s("cost") 是 None，原来整列退回死数。
                        # 改从明细「计入成本」直接求和，跟成本列勾不勾无关。
                        k = _s("cost") or f"({_col_total(_c_cost)}*{fx})"
                        f = f"={_s('gmv')}-{k}"
                    elif key == "gross_margin" and _cl("gmv"):
                        k = _s("cost") or f"({_col_total(_c_cost)}*{fx})"
                        f = f"=IFERROR(({_s('gmv')}-{k})/{_s('gmv')},0)"
                    elif key == "net_rate" and _cl("gmv"):
                        n = _s("net") or f"({_net_total(last)})"
                        f = f"=IFERROR({n}/{_s('gmv')},0)"
                    elif key.startswith("ratio:"):
                        fid = key.split(":", 1)[1]
                        g = _s("gmv") or _col_total(_c_gmv)
                        fe = _s("fee:" + fid)
                        if fe is None and fid == ads_fid:
                            fe = f"SUM(${A_L}3:${A_L}{last})"
                        elif fe is None and fid == cm_fid:
                            fe = f"SUM(${M_L}3:${M_L}{last})"
                        if fe and g:
                            f = f"=IFERROR({fe}/{g},0)"
                    if not f:
                        continue
                    tc = ws.cell(row=row, column=ci2, value=f)
                    tc.number_format = {"money": MONEY, "pct": PCT, "int": INT}.get(
                        sp["kind"], MONEY)
                    tc.font = Font(bold=True)
                    tc.fill = TOTAL_FILL
                    tc.border = BORDER
        if is_total:
            ws.cell(row=row, column=1).border = TOP_BORDER
        row += 1
    last = row - 1

    # ── 合计行写在第 1 行（表头上面）──
    if total_row:
        for ci, sp in enumerate(specs, 1):
            v = total_vals[ci - 1] if ci - 1 < len(total_vals) else None
            cell = _w(ws, total_row, ci, v)
            cell.fill = TOTAL_FILL
            cell.border = BORDER
            cell.font = (Font(bold=True, color="C0392B")
                         if (sp["neg"] and isinstance(v, (int, float)) and v < 0)
                         else Font(bold=True))
            cell.number_format = {"money": MONEY, "pct": PCT, "int": INT}.get(
                sp["kind"], MONEY)
        ws.cell(row=total_row, column=1).border = TOP_BORDER

        # 带公式时合计也写成公式：能加的直接 SUM，派生列用合计之间的比值。
        # 区间是 3..last（数据从第 3 行开始，合计在它上面，不参与求和）。
        if with_formula and last >= 3:
            def _cl2(k):
                ci = col_of.get(k)
                return get_column_letter(ci) if ci else None

            def _s2(k):
                L = _cl2(k)
                return f"SUM({L}3:{L}{last})" if L else None

            _SUMMABLE2 = {"gmv", "orders", "items", "cost", "net", "net_cny",
                          "fee_total", "buyer_paid"}
            for ci2, sp in enumerate(specs, 1):
                key = sp["key"]
                f = None
                if key in _SUMMABLE2 or key.startswith("fee:"):
                    e = _s2(key)
                    f = f"={e}" if e else None
                elif key in _CANCEL_KEYS:
                    f = _cancel_total_expr(key, last)
                elif key == "aov":
                    # 「订单数」列被勾掉时 _s2 是 None，原来整列退回死数。
                    # 直接从明细求和，勾不勾都算得出。
                    g = _s2("gmv") or _col_total(_c_gmv)
                    o = _s2("orders") or _col_total(_c_ord)
                    f = f"=IFERROR({g}/{o},0)"
                elif key == "gross_profit" and _cl2("gmv"):
                    k = _s2("cost") or f"({_col_total(_c_cost)}*{fx})"
                    f = f"={_s2('gmv')}-{k}"
                elif key == "gross_margin" and _cl2("gmv"):
                    k = _s2("cost") or f"({_col_total(_c_cost)}*{fx})"
                    f = f"=IFERROR(({_s2('gmv')}-{k})/{_s2('gmv')},0)"
                elif key == "net_rate" and _cl2("gmv"):
                    n = _s2("net") or f"({_net_total(last)})"
                    f = f"=IFERROR({n}/{_s2('gmv')},0)"
                elif key.startswith("ratio:"):
                    fid = key.split(":", 1)[1]
                    # 费用列没勾也要给公式：广告/佣金引辅助列，其余退回 GMV 比值
                    g = _s2("gmv") or _col_total(_c_gmv)
                    fe = _s2("fee:" + fid)
                    if fe is None and fid == ads_fid:
                        fe = f"SUM(${A_L}3:${A_L}{last})"
                    elif fe is None and fid == cm_fid:
                        fe = f"SUM(${M_L}3:${M_L}{last})"
                    if fe and g:
                        f = f"=IFERROR({fe}/{g},0)"
                if not f:
                    continue
                tc = ws.cell(row=total_row, column=ci2, value=f)
                tc.number_format = {"money": MONEY, "pct": PCT, "int": INT}.get(
                    sp["kind"], MONEY)
                tc.font = Font(bold=True)
                tc.fill = TOTAL_FILL
                tc.border = BORDER

    # 净利率色阶：亏损越深越红，盈利越强越绿
    if net_rate_ci:
        from openpyxl.formatting.rule import ColorScaleRule
        col = get_column_letter(net_rate_ci)
        ws.conditional_formatting.add(
            f"{col}3:{col}{last}",
            ColorScaleRule(start_type="num", start_value=-0.2, start_color="F4B6B6",
                           mid_type="num", mid_value=0, mid_color="FFFFFF",
                           end_type="num", end_value=0.25, end_color="A9D7B6"),
        )

    if with_formula:
        for _ci in (ads_ci, cm_ci, pre_ci, post_ci, nrm_ci, rfd_ci):
            ws.column_dimensions[get_column_letter(_ci)].hidden = True

    freeze_col = last_text + 1 if last_text else 1
    ws.freeze_panes = f"{get_column_letter(freeze_col)}3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(headers))}{last}"

    # 列宽：文本列宽一些，金额列统一 13
    for ci, s in enumerate(specs, 1):
        w = {"style": 20, "product": 22, "name": 30, "rank": 6}.get(s["key"], 13)
        ws.column_dimensions[get_column_letter(ci)].width = w


# ---------------- 样品单对比 ----------------

def _sample_compare(ws: Worksheet, res: CalcResult, subtitle: str) -> None:
    """两条口径并排：含 0 元样品单 vs 去除后。

    为什么要单独做一页：汇总页已经很宽了，再塞对比列会挤得没法看；
    而且「这一批样品单到底吃掉多少利润」这个数是单独拿出来看的，
    不是逐行对比的，放一页更清楚。
    """
    _title(ws, "样品单对比（含 0 元样品 vs 去除后）", subtitle, span=9)
    headers = ["链接（商品ID）", "商品名称",
               "GMV", "商品成本", "发单费",
               "净利（含样品）", "净利（去样品）", "样品影响"]
    _head(ws, headers)

    dropped = bool(res.exclude_zero_amount)
    rows: list[tuple] = []
    for p in res.products:
        alt = res.compare.get(p.product_id)
        if alt is None:
            continue
        # 主口径 / 对比口径 分别是谁，取决于报表当前这一版用的是哪套
        keep, drop = (alt, p) if dropped else (p, alt)
        rows.append((p.product_id, p.product_name,
                     keep.gmv, keep.cost_local, keep.dispatch_fee,
                     keep.net_local, drop.net_local,
                     keep.net_local - drop.net_local))

    t = res.total
    if res.compare_total is not None:
        keep_total = t.net_local if not dropped else res.compare_total
        drop_total = res.compare_total if not dropped else t.net_local
        rows.append(("合计", "", t.gmv, t.cost_local, t.dispatch_fee,
                     keep_total, drop_total, keep_total - drop_total))

    for r_i, vals in enumerate(rows, 3):
        is_total = vals[0] == "合计"
        for c, v in enumerate(vals, 1):
            cell = _w(ws, r_i, c, v)
            cell.border = BORDER
            if is_total:
                cell.fill = TOTAL_FILL
                cell.font = Font(bold=True)
                cell.border = TOP_BORDER
        _fmt_row(ws, r_i, [3, 4, 5, 6, 7, 8], [], [])

    last = 2 + len(rows)
    ws.freeze_panes = "C3"
    if last >= 3:
        ws.auto_filter.ref = f"A2:{get_column_letter(len(headers))}{last}"
    _widths(ws, {1: 22, 2: 34})
    for c in range(3, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13


# ---------------- 费用拆解 ----------------

def _fee_breakdown(ws: Worksheet, res: CalcResult, subtitle: str) -> None:
    # 费用项不写死：参数表里加一项，这里就自动多一组，
    # 否则用户自己加的「订单服务费」在报表里会凭空消失。
    _title(ws, "费用拆解（长表，可直接透视）", subtitle, span=6)
    _head(ws, ["商品ID", "款式编码", "费用项", "金额", "占GMV", "占费用合计"])
    fees = fee_items(res)
    row = 3
    for p in res.products:
        vals_map = [(name, float((p.fees or {}).get(fid, 0.0))) for fid, name in fees]
        total_fee = sum(v for _, v in vals_map) or 0
        for name, val in vals_map:
            if abs(val) < 1e-9:
                continue
            vals = [p.product_id, p.style_codes, name, val,
                    (val / p.gmv if p.gmv else 0),
                    (val / total_fee if total_fee else 0)]
            for c, v in enumerate(vals, 1):
                _w(ws, row, c, v).border = BORDER
            _fmt_row(ws, row, [4], [5, 6])
            row += 1
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:F{max(row - 1, 3)}"
    _widths(ws, {1: 22, 2: 20, 3: 16, 4: 14, 5: 12, 6: 14})


# ---------------- 日趋势 ----------------

def _daily(ws: Worksheet, lines: list[OrderLine], res: CalcResult, subtitle: str) -> None:
    from .calc import date_order, order_kind, parse_dt
    _title(ws, "日趋势", subtitle, span=6)
    _head(ws, ["日期", "GMV", "订单数", "买家实付", "净利(本地)", "净利率"])
    # 日期是「日/月」还是「月/日」按整批判断，不能逐行猜
    dorder = date_order(l.created for l in lines[:300])

    day_gmv: dict[str, float] = {}
    day_paid: dict[str, float] = {}
    day_orders: dict[str, float] = {}
    # 「去除 0 元样品订单」打开时，日趋势也要跟着去掉，不然和汇总页对不上
    drop_zero = bool(getattr(res, "exclude_zero_amount", False))
    refund_kw = tuple(getattr(res, "refund_keywords", None) or ())
    for l in lines:
        if l.is_cancelled:
            continue
        if drop_zero and order_kind(l, refund_kw) == "zero_amount":
            continue
        dt = parse_dt(l.created, dorder)
        key = dt.date().isoformat() if dt else "无日期"
        day_gmv[key] = day_gmv.get(key, 0) + l.gmv
        day_paid[key] = day_paid.get(key, 0) + l.allocated_paid
        day_orders[key] = day_orders.get(key, 0) + l.allocated_orders

    total_gmv = sum(day_gmv.values()) or 0
    overall_margin = res.total.net_margin
    row = 3
    for key in sorted(day_gmv):
        gmv = day_gmv[key]
        vals = [key, gmv, day_orders.get(key, 0), day_paid.get(key, 0),
                gmv * overall_margin, overall_margin]
        for c, v in enumerate(vals, 1):
            _w(ws, row, c, v).border = BORDER
        _fmt_row(ws, row, [2, 4, 5], [6])
        row += 1
    ws.freeze_panes = "A3"
    _widths(ws, {1: 14, 2: 14, 3: 10, 4: 14, 5: 16, 6: 12})
    ws.cell(row=row, column=1, value="注").font = SUB_FONT
    ws.cell(row=row, column=2,
            value="按日期分摊净利为估算（用整体净利率折算），精确到天的净利需按天重算").font = SUB_FONT


# ---------------- 异常 ----------------

# 参数表里 base 的中文名
BASE_LABELS = {
    "gmv": "GMV",
    "buyer_paid": "买家实付",
    "order_count": "订单数",
    "items": "件数",
    "cost": "商品成本",
    "refund": "退款金额",
    "commission": "达人佣金（直接取数）",
    "ads": "广告花费（直接取数）",
}

ISSUE_RULES = [
    ("缺成本", "成本表里查不到该 SKU（不按编码推导）；成本按 0 计，净利偏高", "补录成本表"),
    ("未付款", "订单实付为 0，未产生收入与成本", "确认是否为赠品或异常单"),
    ("售前取消", "还没打包、没有物流单号就取消了，货没动", "一般什么都不该算"),
    ("售后取消（已发货）", "有物流单号且已发货后取消；货已发出", "按口径算成本与发单费"),
    ("售后取消（未发货）", "有物流单号但还没发货就取消（包裹已打包）", "要不要算成本/发单费由店主决定"),
    ("退货退款", "已成交后退货/退款，按当前口径处理（见右列）", "确认口径是否符合预期"),
]

# 四个口径项的中文名，顺序固定：收入 / 成本 / 发单费 / 平台抽成
_SCOPE_FLAGS = (("income", "收入"), ("cost", "成本"),
                ("order_count", "发单费"), ("fee_base", "平台抽成"))


def order_scope_desc(spec: dict | None) -> str:
    """把一组四项开关说成人话。报表多处要用，抽出来避免两处写法不一致。"""
    if not spec:
        return "—"
    on = [n for k, n in _SCOPE_FLAGS if spec.get(k)]
    off = [n for k, n in _SCOPE_FLAGS if not spec.get(k)]
    if not off:
        return "整行计入（收入+成本+发单费+平台抽成）"
    if not on:
        return "整行排除"
    return f"计入：{'/'.join(on)}；不计：{'/'.join(off)}"


def _issues(ws: Worksheet, lines: list[OrderLine], res: CalcResult, subtitle: str) -> None:
    _title(ws, "异常与待办", subtitle, span=5)
    _head(ws, ["类别", "对象", "影响行数", "说明", "建议动作"])

    # 缺成本：逐个 SKU 列出，需要人工补数据
    detail: dict[tuple[str, str], int] = {}
    # 未付款 / 取消退货：只汇总，不需要处理
    summary: dict[str, int] = {}

    of = res.order_filter or {}
    for l in lines:
        if l.cost_unit_cny is None:
            detail[("缺成本", l.seller_sku)] = detail.get(("缺成本", l.seller_sku), 0) + 1
            continue
        if l.is_cancelled:
            c = (l.cancel or "").lower()
            kw = res.refund_keywords or ("return", "refund")
            if any(k in c for k in kw):
                cat = "退货退款"
            elif (l.tracking_id or "").strip() or (l.shipping_provider or "").strip():
                cat = ("售后取消（已发货）"
                       if ((l.shipped_time or "").strip() or (l.delivered_time or "").strip())
                       else "售后取消（未发货）")
            else:
                cat = "售前取消"
            summary[cat] = summary.get(cat, 0) + 1
        elif not l.is_paid:
            summary["未付款"] = summary.get("未付款", 0) + 1

    desc = {k: (d, a) for k, d, a in ISSUE_RULES}
    # 不要在这里下结论说「已排除」或「已计入」——现在每个参数各管各的，
    # 同一类订单的发单费可能算、成本可能不算。统一指到口径页去看。
    for cat in ("售前取消", "售后取消（已发货）", "售后取消（未发货）",
                "退货退款", "未付款"):
        if cat in desc:
            d, a = desc[cat]
            desc[cat] = (f"{d}。各参数是否计入这类订单，见「口径」页逐条列出的适用范围", a)
    row = 3

    for (cat, obj), n in sorted(detail.items(), key=lambda x: (-x[1], x[0])):
        d, a = desc.get(cat, ("", ""))
        for c, v in enumerate([cat, obj, n, d, a], 1):
            _w(ws, row, c, v).border = BORDER
        ws.cell(row=row, column=3).number_format = INT
        ws.cell(row=row, column=2).font = Font(color="C0392B")
        row += 1

    for cat, n in summary.items():
        d, a = desc.get(cat, ("", ""))
        for c, v in enumerate([cat, "全部相关订单", n, d, a], 1):
            _w(ws, row, c, v).border = BORDER
        ws.cell(row=row, column=3).number_format = INT
        row += 1

    if row == 3:
        ws.cell(row=3, column=1, value="本期无异常").font = Font(color="2E7D32")
        row = 4

    row += 1
    ws.cell(row=row, column=1, value="引擎提示").font = Font(bold=True, color="1F3B4D")
    row += 1
    for s in res.issues:
        ws.cell(row=row, column=1, value="·")
        ws.cell(row=row, column=2, value=s)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        row += 1

    ws.freeze_panes = "A3"
    _widths(ws, {1: 14, 2: 26, 3: 10, 4: 56, 5: 40})


# ---------------- 明细 ----------------

DETAIL_HEADERS = [
    "订单号", "创建时间", "商品ID", "款式编码", "Seller SKU", "SKU ID", "商品名称",
    "数量", "GMV", "买家实付(分摊)", "订单数(分摊)", "订单行数",
    "成本单价(CNY)", "成本合计(CNY)", "状态", "取消/退货",
]

# 「带公式导出」时追加到明细页末尾的辅助列。
# 有了这几列，Excel 里就能用 SUMIFS 按「含样品 / 去样品」两套口径重算，
# 不用回程序再跑一遍。列顺序：类别 / 是否样品 / 净利(含) / 净利(去)
# 明细页最右边这 7 列是给「带公式」版本的汇总页用的：
#   口径开关 = 含样品时，汇总取 S/T/U/V 列；= 不含样品时，取 W 列（样品行在这里是 0）。
# 所以 S/T/U 一律按**含样品**口径写值，不要在导出时就把样品行清零，
# 否则那个开关切过去是一半是数、一半是 0。
DETAIL_AUX_HEADERS = ["订单类别", "是否样品",
                      "计入GMV", "计入订单数", "计入件数", "计入买家实付",
                      "计入成本(CNY)",
                      "净利贡献(含样品)", "净利贡献(不含样品)"]


def _role_kinds(cfg: list[dict], role: str) -> list[str] | None:
    """参数表里承担某个角色（gmv / order_count / cost）的那一项认哪些订单类别。

    返回 None 表示「不限制」。为什么不用 order_filter：那是「订单口径」的总开关，
    而报表上的 GMV / 订单数 / 成本 是**参数**算出来的——两者的 kinds 可以不一样
    （比如发单费不含取消单，所以订单数也不含）。用错一份就会对不上账。
    """
    for it in cfg or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("role") or "").strip() != role and str(it.get("id") or "").strip() != role:
            continue
        ks = it.get("kinds")
        return None if not isinstance(ks, list) else [str(x) for x in ks]
    return None


def _kind_of(l: OrderLine, refund_kw: tuple) -> str:
    """这行订单属于哪一类。明细页和核算用的是同一个判定，不能各写一份。"""
    from .calc import order_kind
    return order_kind(l, refund_kw)


# 能按「订单行」精确算的费用项。广告费/达人佣金是按链接整期取的，摊到行上只能是
# 估算，所以不在明细里列（列了会让人以为是平台真实扣款）。
_ROW_FEE_IDS = ("dispatch", "platform_commission", "tx_fee", "cashback", "order_service")
_ROW_FEE_CN = {"dispatch": "发单费", "platform_commission": "平台佣金",
               "tx_fee": "交易手续费", "cashback": "奖金返现",
               "order_service": "订单服务费"}


def _row_fee_items(cfg: list[dict]) -> list[tuple[str, str]]:
    """明细页要显示的费用列：按参数表顺序，取能按行算的那几项。

    参数表里没写的也补回来——_in_kinds 找不到该项时按「不限制类别」处理，
    也就是说它照样参与净利计算；明细不列，净利和费用列就对不上。
    """
    labels: dict[str, str] = {}
    order: list[str] = []
    for it in cfg or []:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "").strip()
        if fid in _ROW_FEE_IDS and fid not in labels:
            labels[fid] = str(it.get("label") or _ROW_FEE_CN[fid])
            order.append(fid)
    for fid in _ROW_FEE_IDS:
        if fid not in labels:
            labels[fid] = _ROW_FEE_CN[fid]
            order.append(fid)
    return [(fid, labels[fid]) for fid in order]


def _row_fees(l: OrderLine, kind: str, params: Params, cfg: list[dict]) -> dict[str, float]:
    """这一行在某一套口径下的各项订单级费用（**本地货币**）。

    跟 _row_net 共用同一套判定，不能各写一份：两边一岔开，明细里
    「GMV − 成本 − 费用合计」就跟净利差一个说不清的数。
    """
    fx = params.cny_to_local
    gmv = l.gmv if _in_kinds(cfg, "gmv", kind) else ZERO
    paid = l.allocated_paid if _in_kinds(cfg, "buyer_paid", kind) else ZERO
    orders = l.allocated_orders if _in_kinds(cfg, "order_count", kind) else ZERO
    out: dict[str, float] = {}
    if _in_kinds(cfg, "dispatch", kind):
        out["dispatch"] = orders * params.dispatch_fee_cny * fx
    if _in_kinds(cfg, "platform_commission", kind):
        out["platform_commission"] = gmv * params.platform_commission
    if _in_kinds(cfg, "tx_fee", kind):
        out["tx_fee"] = paid * params.tx_fee
    if _in_kinds(cfg, "cashback", kind):
        out["cashback"] = gmv * params.cashback
    if _in_kinds(cfg, "order_service", kind):
        out["order_service"] = orders * params.order_service
    return out


def _row_net(l: OrderLine, kind: str, params: Params, cfg: list[dict]) -> float:
    """这一行在某一套口径下贡献的净利（不含广告/达人佣金）。

    广告和达人佣金是「整期直接取数」的，不按订单行分摊，
    所以在汇总那一层再减，这里只算跟这一行有关的收支。
    """
    fx = params.cny_to_local
    gmv = l.gmv if _in_kinds(cfg, "gmv", kind) else ZERO
    cost = (l.cost_total_cny or ZERO) * fx if _in_kinds(cfg, "cost", kind) else ZERO
    return gmv - cost - sum(_row_fees(l, kind, params, cfg).values())


def _in_kinds(cfg: list[dict], pid: str, kind: str) -> bool:
    """某个参数在这一套口径下算不算这一类订单。"""
    for it in cfg or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("id") or "").strip() != pid:
            continue
        ks = it.get("kinds")
        if not isinstance(ks, list):
            return True                 # 不写 kinds = 不看订单类别
        return kind in ks
    return True


def _detail(ws: Worksheet, lines: list[OrderLine], res: CalcResult,
            subtitle: str, with_formula: bool = False) -> None:
    _title(ws, "订单明细", subtitle, span=8)
    # 费用列插在「成本合计(CNY)」后面：一行订单到底被扣了哪些钱，不用再去翻汇总。
    # 只列**能按订单行精确算**的几项；广告费/达人佣金是按链接整期取的，
    # 摊到行上只能是估算，列在这里会让人误以为是平台真实扣款，所以不放。
    fee_items = _row_fee_items(res.params_cfg)
    base = list(DETAIL_HEADERS)
    split = base.index("成本合计(CNY)") + 1
    heads = (base[:split]
             + [f"{lab}(本地)" for _fid, lab in fee_items]
             + ["费用合计(本地)"]
             + base[split:])
    if with_formula:
        heads += list(DETAIL_AUX_HEADERS)
    _head(ws, heads)

    # 列号一律按表头反推。写死数字的话，插一列就全串位，
    # 而且不会报错——只是金额格式和汇总公式悄悄指错列。
    def ci(name: str) -> int:
        return heads.index(name) + 1

    money_cols = [ci("GMV"), ci("买家实付(分摊)"),
                  ci("成本单价(CNY)"), ci("成本合计(CNY)")]
    money_cols += [ci(f"{lab}(本地)") for _fid, lab in fee_items]
    money_cols += [ci("费用合计(本地)")]
    int_cols = [ci("数量"), ci("订单行数")]
    if with_formula:
        money_cols += [ci(x) for x in ("计入GMV", "计入买家实付", "计入成本(CNY)",
                                       "净利贡献(含样品)", "净利贡献(不含样品)")]
        int_cols += [ci(x) for x in ("是否样品", "计入订单数", "计入件数")]

    row = 3
    kw = tuple(res.refund_keywords or ())
    # 固定两套口径：一套含 0 元样品单，一套不含。跟当前全局开关无关，
    # 否则开关一打开，「含样品」那套就全成 0 了，汇总页的口径切不回去。
    _cfg_w = res.cfg_with_sample or res.params_cfg
    _cfg_wo = res.cfg_without_sample or res.compare_params_cfg
    _g = _role_kinds(_cfg_w, "gmv")
    _o = _role_kinds(_cfg_w, "order_count")
    _i = _role_kinds(_cfg_w, "items")
    _p = _role_kinds(_cfg_w, "buyer_paid")
    _c = _role_kinds(_cfg_w, "cost")
    for l in lines:
        k = _kind_of(l, kw)
        fees = _row_fees(l, k, res.params, _cfg_w)
        vals = [
            l.order_id, l.created, l.product_id or "无ID", l.style_codes,
            l.seller_sku, l.sku_id, l.product_name, l.qty,
            l.gmv, l.allocated_paid, l.allocated_orders, l.order_line_count,
            l.cost_unit_cny, l.cost_total_cny,
        ]
        vals += [fees.get(fid, 0.0) for fid, _lab in fee_items]
        vals += [sum(fees.values())]
        vals += [l.status, l.cancel]
        if with_formula:
            is_sample = 1 if k == "zero_amount" else 0
            gmv_in = 0.0 if (_g is not None and k not in _g) else l.gmv
            ord_in = 0.0 if (_o is not None and k not in _o) else l.allocated_orders
            item_in = 0.0 if (_i is not None and k not in _i) else l.qty
            paid_in = 0.0 if (_p is not None and k not in _p) else l.allocated_paid
            if l.cost_total_cny is None or (_c is not None and k not in _c):
                cost_in = 0.0
            else:
                # 列名写着 CNY，就得是 CNY。以前这里乘了汇率，
                # 汇总页公式再乘一次 → 成本整整多出一个汇率的倍数。
                cost_in = l.cost_total_cny
            vals += [k, is_sample, gmv_in, ord_in, item_in, paid_in, cost_in,
                     _row_net(l, k, res.params, _cfg_w),
                     _row_net(l, k, res.params, _cfg_wo)]
        for c, v in enumerate(vals, 1):
            _w(ws, row, c, v).border = BORDER
        _fmt_row(ws, row, money_cols, [], int_cols)
        row += 1
    ws.freeze_panes = "A3"
    last_col = get_column_letter(len(heads))
    ws.auto_filter.ref = f"A2:{last_col}{max(row - 1, 3)}"
    widths = {1: 22, 2: 20, 3: 22, 4: 18, 5: 22, 6: 22, 7: 30,
              8: 8, 9: 11, 10: 15, 11: 13, 12: 10,
              13: 14, 14: 14}
    widths[ci("订单号")] = 22
    widths[ci("创建时间")] = 20
    widths[ci("商品名称")] = 30
    widths[ci("状态")] = 12
    widths[ci("取消/退货")] = 12
    for fid, lab in fee_items:
        widths[ci(f"{lab}(本地)")] = 13
    widths[ci("费用合计(本地)")] = 14
    _widths(ws, widths)


# ---------------- 口径 ----------------

def _meta(ws: Worksheet, res: CalcResult, params: Params, switches: Switches,
          subtitle: str, extra: dict[str, str], with_formula: bool = False) -> None:
    _title(ws, "口径与参数快照", subtitle, span=4)
    _head(ws, ["项目", "值", "说明", ""])

    p = params
    # 广告费与 GMV 明显不成比例时，多半是「广告表币种」选错了（差一个汇率倍数）。
    # 这种错不会报错，报表每列都有数，只能在口径页当面提醒。
    try:
        _ads_tot = float(((res.total.fees or {}) if res.total else {}).get("ads", 0) or 0)
        _gmv_tot = float((res.total.gmv if res.total else 0) or 0)
    except Exception:  # noqa: BLE001
        _ads_tot = _gmv_tot = 0.0
    _ads_warn = ""
    # 只在 GMV 有规模的月份才比这个比例：3 单、GMV 36 的月份，几百块广告费
    # 就能算出几千 %，会误报「币种选错了」。净利为负本身是正常结果，不是错误。
    if _gmv_tot >= 5000 and _ads_tot > _gmv_tot * 0.6:
        _ads_warn = (f"广告费 {_ads_tot:,.0f} 是 GMV 的 {_ads_tot / _gmv_tot * 100:.0f}%，"
                     f"远高于正常水平，币种可能选错了（去「费率」页改每国的「广告表币种」）")

    rows = [
        ("核算国家", p.country, "汇率与费率均取该国参数"),
        *([("⚠ 广告费异常", _ads_warn, "")] if _ads_warn else []),
        ("汇率来源", p.fx_note or "配置里的固定值",
         "auto=按核算日前一天（T-1）的汇率（取不到依次退回本机存档/最新价/配置值），"
         "manual=手动固定，见 config/rates.json 的 fx 段"),
        ("汇率：人民币→本地", p.cny_to_local, f"1 人民币 = {p.cny_to_local} 本地货币"),
        ("汇率：美金→本地", p.usd_to_local, f"1 美金 = {p.usd_to_local} 本地货币"),
        ("发单费(本地)", p.dispatch_fee_local, "按订单数计，不按金额"),
        ("平台佣金率", p.platform_commission,
         "基数为平台抽成计费基数（正常单与 0 元样品单；取消/退货不含）"),
        ("交易手续费率", p.tx_fee, "基数为平台抽成计费基数（实付）"),
        ("奖金返现费率", p.cashback, "基数为 GMV"),
        ("超级优惠券费率", p.super_coupon if switches.super_coupon_enabled else 0,
         "开关：" + ("启用" if switches.super_coupon_enabled else "关闭（按 0 计）")),
        ("商家成长费率", p.growth_fee, "基数为 GMV"),
        ("物流费率", p.logistics, "基数为 GMV（比例法，非实际运费账单）"),
        ("分组维度", "链接（商品ID）", "一条链接一行；同一链接下多个 SKU 合并计算，不同链接不合并"),
        ("SKU 归属", "平台 SKU ID（唯一）", "卖家编码可在多条链接间复用，只在缺 SKU ID 时兜底使用"),
        ("GMV 口径", "折扣前小计 − 卖家折扣", "不含平台补贴；取消/退货行按下方「订单口径」决定"),
        ("订单数口径", "订单级计数按行数平分", "一个订单的分配数合计为 1"),
        ("买家实付口径", f"{switches.allocation_paid}", "订单实付在订单内各行的分配方式"),
        ("成本口径", "成本表单价 × 数量 × 汇率",
         "只认成本表里的编码，不按编码规律推导；查不到的按 0 计并在本页点名"),
        ("达人佣金口径", "联盟报表预估佣金", "退款部分未额外扣减"),
        ("广告口径", "广告花费 × 币种汇率",
         "按商品ID汇总。导出表写 USD/MYR 就照它算；写 LOCAL（账号本位币）或空，"
         "则按「费率」页每国的「广告表币种」：留空=自动识别，LOCAL=不换算，"
         "也可直接填任意币种代码（USD/THB/PHP…）按交叉汇率折算"),
    ]

    # 总开关：报表上必须写明，否则事后看数会以为样品单被漏掉了
    if with_formula:
        _sw_hint = ("汇总页右上角有「汇总口径」下拉：切成「不含样品」整表立刻换成"
                    "纯正常订单口径，切成「含样品」又换回来，不用重新跑一遍")
    else:
        _sw_hint = "导出时勾「导出带公式」，汇总页右上角就能现场切换含/不含样品"
    rows.append((
        "0 元样品单",
        "整表剔除（已打开「去除 0 元样品订单」）" if res.exclude_zero_amount else "按各参数自己的勾选",
        ("发单费、商品成本、订单数、件数都不含样品单，算出来的是纯正常订单（含退货退款）的净利。"
         if res.exclude_zero_amount else
         "在「计算参数」页可一键打开「去除 0 元样品订单」。")
        + _sw_hint,
    ))

    # ── 参数表：这次每个参数算的是哪些订单类别 ──
    # 必须按实际生效的配置生成。写死说明会让报表「说明和数不符」，
    # 事后对账越查越乱。
    stats = res.kind_stats or {}
    labels = res.kind_labels or {}
    order_seq = ("normal", "cancelled", "cancelled_shipped", "cancelled_unshipped",
                 "refunded", "zero_amount", "unpaid")

    if stats:
        brief = "；".join(
            f"{labels.get(k, k)} {stats[k]} 行"
            for k in order_seq if stats.get(k)
        )
        rows.append(("本次订单构成", brief,
                     "每一行订单只归一类，优先级：取消（售前/售后）> 退货退款 > "
                     "0元样品 > 未付款 > 正常"))
        rows.append((
            "取消结构列口径",
            "售前取消单数 = 无物流单号的取消；售后取消单数 = 有单号/物流商的取消"
            "（判据列：Tracking ID / Shipping Provider Name 任一有值 = 已打单出库；"
            "再用 Shipped Time / Delivered Time 有没有值区分已发/未发）",
            "售后取消占比 = 售后 ÷（售前+售后）；取消占总单比 =（售前+售后）÷ 总单数；"
            "售后占总单比 = 售后 ÷ 总单数。"
            "总单数 = 正常 + 全部取消 + 退货退款（不含 0 元样品、未付款）；"
            "各列都是「分摊订单数」，一个订单 4 行、3 行属某链接就是 0.75 单",
        ))

    for it in res.params_cfg or []:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "")
        if not pid:
            continue
        label = str(it.get("label") or pid)
        kinds = it.get("kinds")
        if kinds is None:
            scope = "全部订单类别"
        elif not kinds:
            scope = "不算（关掉了）"
        else:
            scope = "、".join(labels.get(k, k) for k in kinds)
        expr = it.get("expr")
        expr_txt = str(expr).strip() if expr is not None else ""
        if expr_txt in ("ads", "commission"):
            # 这两项来自广告表/联盟表，按商品ID汇总，跟订单类别无关
            scope = "来自数据源，不看订单类别"
        if expr_txt:
            formula_txt = expr_txt
        else:
            base = str(it.get("base") or "gmv")
            rate = it.get("rate")
            if rate in (None, 1, 1.0, "1", "1.0"):
                formula_txt = BASE_LABELS.get(base, base)
            else:
                rt = str(rate)
                if rt.startswith("params."):
                    rt = "国家费率 " + rt.split(".", 1)[1]
                formula_txt = f"{BASE_LABELS.get(base, base)} × {rt}"
        if not it.get("enabled", True):
            scope = "已停用"
        rows.append((label, scope, formula_txt))
    row = 3
    for k, v, note in rows:
        ws.cell(row=row, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=row, column=2, value=v)
        if isinstance(v, float):
            c.number_format = PCT if abs(v) < 1 else MONEY
        ws.cell(row=row, column=3, value=note).font = SUB_FONT
        for cc in range(1, 4):
            ws.cell(row=row, column=cc).border = BORDER
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="本次运行").font = Font(bold=True, color="1F3B4D")
    row += 1
    for k, v in extra.items():
        ws.cell(row=row, column=1, value=k).font = Font(bold=True)
        ws.cell(row=row, column=2, value=v)
        for cc in range(1, 3):
            ws.cell(row=row, column=cc).border = BORDER
        row += 1

    # ── 成本缺失点名（用户要求：报表里要写明是哪个编码没成本） ──
    _miss_rows = int(getattr(res, "missing_cost_rows", 0) or 0)
    _codes = getattr(res, "missing_cost_codes", {}) or {}
    if _miss_rows and _codes:
        row += 1
        _h = ws.cell(row=row, column=1, value="⚠ 商品成本缺失（成本按 0 计，净利偏高）")
        _h.font = Font(bold=True, color="C0392B")
        row += 1
        ws.cell(row=row, column=1, value="影响行数").font = Font(bold=True)
        ws.cell(row=row, column=2, value=f"{_miss_rows} 行 / {len(_codes)} 个编码")
        row += 1
        # 「怎么补」放在编码清单**上面**，清单放整页最底下 —— 编码可能几十上百条，
        # 夹在中间会把后面的说明顶到很远。
        ws.cell(row=row, column=1, value="怎么补").font = Font(bold=True)
        ws.cell(row=row, column=2,
                value="在「成本表」里加上这些编码（编码要和订单表的 Seller SKU 一字不差，"
                      "含大小写），存盘后重算即可；补之前这些行的成本按 0 计，净利会偏高")
        row += 1
        # 编码竖着一个一行，只写编码本身。
        # 不再拼成「A×4、B×3…」长串：那个 ×4 是**行数**，挤在一格里既看不清，
        # 又容易被当成编码的一部分（用户 2026-11-05 提的）。
        ws.cell(row=row, column=1, value="没成本的编码").font = Font(bold=True)
        row += 1
        for _code in sorted(_codes, key=lambda k: (-_codes[k], str(k))):
            ws.cell(row=row, column=1, value=str(_code))
            row += 1

    _widths(ws, {1: 22, 2: 34, 3: 60})


# ---------------- 入口 ----------------

_REF_RE = re.compile(r"\$?([A-Z]{1,2})\$?(\d+)")


def audit_formulas(ws: Worksheet, detail: Worksheet) -> list[str]:
    """检查汇总页的公式有没有「自己算自己」或「指到空列」。

    为什么要在出表时查：报表列是用户随时能勾的，勾掉一列整行就往左移一格。
    2026-11-04 就是因为「排名」被取消、商品ID 从 C 变成 B，而公式里写死了 C，
    变成 `SUMIFS(…, 明细!$C:$C, $C3)` —— 拿自己的 GMV 当匹配条件，
    Excel 判循环引用，整张表变成 0.00，肉眼完全看不出是算错还是真没单。
    现在所有列字母都按实际位置算，但**改动总会再犯**，所以每次出表都自查一遍。
    """
    problems: list[str] = []
    maxc, maxr = ws.max_column, ws.max_row
    det_max = detail.max_column
    det_hdr = {c for c in range(1, det_max + 1) if detail.cell(2, c).value}
    hdr = {c: ws.cell(2, c).value for c in range(1, maxc + 1)}

    try:
        from openpyxl.utils import column_index_from_string as to_idx
    except Exception:  # noqa: BLE001
        return problems

    seen: set[str] = set()
    for r in range(1, maxr + 1):
        for c in range(1, maxc + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str) or not v.startswith("="):
                continue
            own_col = get_column_letter(c)
            # ① 自引用：公式里出现自己这一格
            for m in _REF_RE.finditer(v):
                col, row = m.group(1), m.group(2)
                if row and col == own_col and int(row) == r:
                    key = f"{own_col}{r}"
                    if key not in seen:
                        seen.add(key)
                        problems.append(f"自引用 {key}（{str(hdr.get(c) or '')}）：改了也没用，Excel 会报循环引用")
                    break
            # ② 明细引用越界（列数变了但公式没跟上）
            for m in re.finditer(r"明细!\$([A-Z]{1,2}):\$([A-Z]{1,2})", v):
                try:
                    ci = to_idx(m.group(1))
                except Exception:  # noqa: BLE001
                    continue
                if ci > det_max or ci not in det_hdr:
                    key = f"{own_col}{r}→明细!{m.group(1)}"
                    if key not in seen:
                        seen.add(key)
                        problems.append(f"{own_col}{r} 引用了明细页的空列 {m.group(1)}")
            # ③ 汇总页引用越界
            for m in _REF_RE.finditer(v):
                try:
                    ci = to_idx(m.group(1))
                except Exception:  # noqa: BLE001
                    continue
                if ci > maxc:
                    key = f"{own_col}{r}→{m.group(1)}"
                    if key not in seen:
                        seen.add(key)
                        problems.append(f"{own_col}{r} 引用了不存在的列 {m.group(1)}")
            # ④ 函数参数个数不对（只有 SUMIFS 一个参数这种）
            #
            # SUMIFS 至少要 3 个参数（求和区 + 条件区 + 条件）。只给求和区时
            # Excel/WPS 不会报错、也不给 #VALUE，而是**整格空白** —— 看起来就是
            # 「这一列没算」，最容易蒙混过去。2026-11-05 实测：勾掉「成本」列后，
            # 合计行的「链接毛利率」全空，就是 _col_total 拼出了 SUMIFS(明细!$AC:$AC)。
            for m in re.finditer(r"SUMIFS\(([^,()]*)\)", v):
                key = f"{own_col}{r}→SUMIFS参数"
                if key not in seen:
                    seen.add(key)
                    problems.append(
                        f"{own_col}{r} 的 SUMIFS 只有 1 个参数（{m.group(1)[:20]}）："
                        f"Excel/WPS 会留空白，应改成 SUM")
    return problems


def _append_issue_rows(ws: Worksheet, probs: list[str]) -> None:
    """把体检结果追加到「异常」页，用户打开报表就能看到。"""
    try:
        row = ws.max_row + 1
        for i, msg in enumerate(probs[:20], 1):
            ws.cell(row=row, column=1, value="公式体检")
            ws.cell(row=row, column=2, value=f"第 {i} 项")
            ws.cell(row=row, column=3, value=0)
            ws.cell(row=row, column=4, value=msg)
            ws.cell(row=row, column=5,
                    value="这是程序 bug，请把这份报表发给开发者；数字先别用")
            row += 1
    except Exception:  # noqa: BLE001
        pass


def build_report(res: CalcResult, lines: list[OrderLine], out_path: Path,
                 switches: Switches, subtitle: str = "",
                 extra: Optional[dict[str, str]] = None,
                 with_compare: bool = True,
                 with_formula: bool = False,
                 report_columns: list[str] | None = None,
                 total_position: str = "bottom",
                 report_titles: dict | None = None) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    _summary(ws, res, subtitle, report_columns=report_columns,
             total_position=total_position, report_titles=report_titles,
             with_formula=with_formula)

    _fee_breakdown(wb.create_sheet("费用拆解"), res, subtitle)
    _daily(wb.create_sheet("日趋势"), lines, res, subtitle)
    _issues(wb.create_sheet("异常"), lines, res, subtitle)
    _detail(wb.create_sheet("明细"), lines, res, subtitle,
           with_formula=with_formula)
    # 两套样品口径并排一页，想看剔不剔样品的差别直接翻这页
    if with_compare and res.compare_total is not None:
        _sample_compare(wb.create_sheet("样品对比"), res, subtitle)
    _meta(wb.create_sheet("口径"), res, res.params, switches, subtitle,
          extra or {"生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
          with_formula=with_formula)

    wb.properties.title = "净利润报表"
    wb.properties.creator = "算净利润引擎"

    # 出表前自查公式：列是用户随时能勾的，勾掉一列整行就左移，
    # 万一哪处还在写死列字母，这里能当场抓住，而不是等 Excel 弹「循环引用」。
    if with_formula:
        _probs = audit_formulas(ws, wb["明细"])
        if _probs:
            for _x in _probs[:5]:
                print(f"[warn] 公式体检：{_x}", file=sys.stderr)
            print(f"[warn] 公式体检共 {len(_probs)} 项问题，已写进报表「异常」页",
                  file=sys.stderr)
            _append_issue_rows(wb["异常"], _probs)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _save_atomic(target: Path) -> None:
        """先写临时文件、落盘，再原子改名。

        为什么要这么绕：openpyxl 是边算边往文件里写的，几万行明细要写十几秒。
        正好写到一半断电/强杀，报表目录里就多一个**打不开的 xlsx**——
        界面能看见、点「查看」报错，用户以为报表坏了。
        写临时文件的话，断电只会留下一个 .saving 临时文件（启动时会清掉），
        原报表要么是上一版完整的，要么完全不存在，不会出现半截。
        """
        tmp = target.with_name(f".{target.stem}.saving{target.suffix}")
        try:
            with tmp.open("wb") as f:
                wb.save(f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # 目标文件可能正被 Excel/WPS 打开（Windows 下会独占锁），
    # 直接写会抛 PermissionError 让整次核算白跑。这里逐级换个名字再存。
    try:
        _save_atomic(out_path)
        return out_path
    except PermissionError:
        pass

    for i in range(2, 10):
        alt = out_path.with_name(f"{out_path.stem}_v{i}{out_path.suffix}")
        try:
            _save_atomic(alt)
            print(f"提示：{out_path.name} 正被占用，已改存为 {alt.name}")
            return alt
        except PermissionError:
            continue
    raise PermissionError(f"无法写入报表：{out_path}（该文件可能被 Excel 打开，请关闭后重试）")
