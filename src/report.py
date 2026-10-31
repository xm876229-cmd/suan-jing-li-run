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
    keys = ["rank", "style", "product", "name", "gmv", "orders", "items",
            "aov", "buyer_paid", "cost", "gross_profit", "gross_margin"]
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
             report_titles: dict | None = None) -> None:
    # 一条链接（商品ID）一行：同一链接下的多个 SKU 合并，
    # 不同链接即使卖家编码相同也绝不合并——卖家编码可在多条链接间复用，
    # 只有商品ID 是链接的唯一标识。
    fees = fee_items(res)
    # 费用占比列：只对实际存在的费用项出列（用户把某项删了，列也没了）
    ratios = [(fid, name) for fid, name in RATIO_ITEMS
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
    if "rank" not in enabled:        # 排名始终在，避免空表
        enabled = ["rank"] + enabled

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

    _title(ws, "净利润汇总（每条链接一行）", subtitle, span=max(8, len(headers)))
    _head(ws, headers)

    # 行：合计 + 每条链接。合计行位置可配（顶部/底部）。
    t = res.total
    total_vals = [s["gett"]() for s in specs]
    data_vals = [[s["getp"](p, i) for s in specs]
                 for i, p in enumerate(res.products, 1)]
    if total_position == "top":
        rows = [total_vals] + data_vals
    else:
        rows = data_vals + [total_vals]

    last_text = 0
    for ci, s in enumerate(specs, 1):
        if s["kind"] == "text":
            last_text = ci
    net_rate_ci = next((ci for ci, s in enumerate(specs, 1) if s["key"] == "net_rate"), None)

    row = 3
    for ri, vals in enumerate(rows):
        is_total = (ri == 0 and total_position == "top") or \
                   (total_position != "top" and ri == len(rows) - 1)
        for ci, (s, v) in enumerate(zip(specs, vals), 1):
            cell = ws.cell(row=row, column=ci, value=v)
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
        if is_total:
            ws.cell(row=row, column=1).border = TOP_BORDER
        row += 1
    last = row - 1

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
            cell = ws.cell(row=r_i, column=c, value=v)
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
                ws.cell(row=row, column=c, value=v).border = BORDER
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
            ws.cell(row=row, column=c, value=v).border = BORDER
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
    ("缺成本", "成本表里查不到该 SKU，且无法推导；成本按 0 计，净利偏高", "补录成本表"),
    ("未付款", "订单实付为 0，未产生收入与成本", "确认是否为赠品或异常单"),
    ("已取消", "订单被取消，按当前口径处理（见右列）", "确认口径是否符合预期"),
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
            cat = "退货退款" if any(k in c for k in kw) else "已取消"
            summary[cat] = summary.get(cat, 0) + 1
        elif not l.is_paid:
            summary["未付款"] = summary.get("未付款", 0) + 1

    desc = {k: (d, a) for k, d, a in ISSUE_RULES}
    # 不要在这里下结论说「已排除」或「已计入」——现在每个参数各管各的，
    # 同一类订单的发单费可能算、成本可能不算。统一指到口径页去看。
    for cat in ("已取消", "退货退款", "未付款"):
        if cat in desc:
            d, a = desc[cat]
            desc[cat] = (f"{d}。各参数是否计入这类订单，见「口径」页逐条列出的适用范围", a)
    row = 3

    for (cat, obj), n in sorted(detail.items(), key=lambda x: (-x[1], x[0])):
        d, a = desc.get(cat, ("", ""))
        for c, v in enumerate([cat, obj, n, d, a], 1):
            ws.cell(row=row, column=c, value=v).border = BORDER
        ws.cell(row=row, column=3).number_format = INT
        ws.cell(row=row, column=2).font = Font(color="C0392B")
        row += 1

    for cat, n in summary.items():
        d, a = desc.get(cat, ("", ""))
        for c, v in enumerate([cat, "全部相关订单", n, d, a], 1):
            ws.cell(row=row, column=c, value=v).border = BORDER
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
DETAIL_AUX_HEADERS = ["订单类别", "是否样品", "净利贡献(含样品)", "净利贡献(去样品)"]


def _kind_of(l: OrderLine, refund_kw: tuple) -> str:
    """这行订单属于哪一类。明细页和核算用的是同一个判定，不能各写一份。"""
    from .calc import order_kind
    return order_kind(l, refund_kw)


def _row_net(l: OrderLine, kind: str, params: Params, cfg: list[dict]) -> float:
    """这一行在某一套口径下贡献的净利（不含广告/达人佣金）。

    广告和达人佣金是「整期直接取数」的，不按订单行分摊，
    所以在汇总那一层再减，这里只算跟这一行有关的收支。
    """
    fx = params.cny_to_local
    gmv = l.gmv if _in_kinds(cfg, "gmv", kind) else ZERO
    cost = (l.cost_total_cny or ZERO) * fx if _in_kinds(cfg, "cost", kind) else ZERO
    paid = l.allocated_paid if _in_kinds(cfg, "buyer_paid", kind) else ZERO
    orders = l.allocated_orders if _in_kinds(cfg, "order_count", kind) else ZERO

    dispatch = orders * params.dispatch_fee_cny * fx if _in_kinds(cfg, "dispatch", kind) else ZERO
    commission = gmv * params.platform_commission if _in_kinds(cfg, "platform_commission", kind) else ZERO
    tx = paid * params.tx_fee if _in_kinds(cfg, "tx_fee", kind) else ZERO
    cashback = gmv * params.cashback if _in_kinds(cfg, "cashback", kind) else ZERO
    service = orders * params.order_service if _in_kinds(cfg, "order_service", kind) else ZERO

    return gmv - cost - dispatch - commission - tx - cashback - service


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
    heads = list(DETAIL_HEADERS) + (list(DETAIL_AUX_HEADERS) if with_formula else [])
    _head(ws, heads)
    row = 3
    kw = tuple(res.refund_keywords or ())
    for l in lines:
        vals = [
            l.order_id, l.created, l.product_id or "无ID", l.style_codes,
            l.seller_sku, l.sku_id, l.product_name, l.qty,
            l.gmv, l.allocated_paid, l.allocated_orders, l.order_line_count,
            l.cost_unit_cny, l.cost_total_cny, l.status, l.cancel,
        ]
        if with_formula:
            k = _kind_of(l, kw)
            is_sample = 1 if k == "zero_amount" else 0
            vals += [k, is_sample,
                     _row_net(l, k, res.params, res.params_cfg),
                     _row_net(l, k, res.params, res.compare_params_cfg)]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row, column=c, value=v).border = BORDER
        _fmt_row(ws, row, [9, 10, 13, 14] + ([19, 20] if with_formula else []),
                [], [8, 12])
        row += 1
    ws.freeze_panes = "A3"
    last_col = "T" if with_formula else "P"
    ws.auto_filter.ref = f"A2:{last_col}{max(row - 1, 3)}"
    _widths(ws, {1: 22, 2: 20, 3: 22, 4: 18, 5: 22, 6: 22, 7: 30,
                 8: 8, 9: 11, 10: 15, 11: 13, 12: 10, 13: 14, 14: 14, 15: 12, 16: 12})


# ---------------- 口径 ----------------

def _meta(ws: Worksheet, res: CalcResult, params: Params, switches: Switches,
          subtitle: str, extra: dict[str, str]) -> None:
    _title(ws, "口径与参数快照", subtitle, span=4)
    _head(ws, ["项目", "值", "说明", ""])

    p = params
    rows = [
        ("核算国家", p.country, "汇率与费率均取该国参数"),
        ("汇率来源", p.fx_note or "配置里的固定值",
         "auto=联网抓实时（抓不到退回配置值），manual=手动固定，见 config/rates.json 的 fx 段"),
        ("汇率：人民币→本地", p.cny_to_local, f"1 人民币 = {p.cny_to_local} 本地货币"),
        ("汇率：美金→本地", p.usd_to_local, f"1 美金 = {p.usd_to_local} 本地货币"),
        ("发单费(本地)", p.dispatch_fee_local, "按订单数计，不按金额"),
        ("平台佣金率", p.platform_commission, "基数为平台抽成计费基数（含取消/退货，不含 0 元样品单）"),
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
        ("成本口径", "成本表单价 × 数量 × 汇率", "多件装按基座价×件数推导，组合装按组件相加"),
        ("达人佣金口径", "联盟报表预估佣金", "退款部分未额外扣减"),
        ("广告口径", "广告花费(美金) × 美金汇率", "按商品ID汇总"),
    ]

    # 总开关：报表上必须写明，否则事后看数会以为样品单被漏掉了
    rows.append((
        "0 元样品单",
        "整表剔除（已打开「去除 0 元样品订单」）" if res.exclude_zero_amount else "按各参数自己的勾选",
        ("发单费、商品成本、订单数、件数都不含样品单，算出来的是纯正常订单（含退货退款）的净利"
         if res.exclude_zero_amount else
         "在「计算参数」页可一键打开「去除 0 元样品订单」"),
    ))

    # ── 参数表：这次每个参数算的是哪些订单类别 ──
    # 必须按实际生效的配置生成。写死说明会让报表「说明和数不符」，
    # 事后对账越查越乱。
    stats = res.kind_stats or {}
    labels = res.kind_labels or {}
    order_seq = ("normal", "cancelled", "refunded", "zero_amount", "unpaid")

    if stats:
        brief = "；".join(
            f"{labels.get(k, k)} {stats[k]} 行"
            for k in order_seq if stats.get(k)
        )
        rows.append(("本次订单构成", brief, "每一行订单只归一类，优先级：已取消 > 退货退款 > 0元样品 > 未付款 > 正常"))

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

    _widths(ws, {1: 22, 2: 34, 3: 60})


# ---------------- 入口 ----------------

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
             total_position=total_position, report_titles=report_titles)

    _fee_breakdown(wb.create_sheet("费用拆解"), res, subtitle)
    _daily(wb.create_sheet("日趋势"), lines, res, subtitle)
    _issues(wb.create_sheet("异常"), lines, res, subtitle)
    _detail(wb.create_sheet("明细"), lines, res, subtitle,
           with_formula=with_formula)
    # 两套样品口径并排一页，想看剔不剔样品的差别直接翻这页
    if with_compare and res.compare_total is not None:
        _sample_compare(wb.create_sheet("样品对比"), res, subtitle)
    _meta(wb.create_sheet("口径"), res, res.params, switches, subtitle,
          extra or {"生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    wb.properties.title = "净利润报表"
    wb.properties.creator = "算净利润引擎"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 目标文件可能正被 Excel/WPS 打开（Windows 下会独占锁），
    # 直接写会抛 PermissionError 让整次核算白跑。这里逐级换个名字再存。
    try:
        wb.save(out_path)
        return out_path
    except PermissionError:
        pass

    for i in range(2, 10):
        alt = out_path.with_name(f"{out_path.stem}_v{i}{out_path.suffix}")
        try:
            wb.save(alt)
            print(f"提示：{out_path.name} 正被占用，已改存为 {alt.name}")
            return alt
        except PermissionError:
            continue
    raise PermissionError(f"无法写入报表：{out_path}（该文件可能被 Excel 打开，请关闭后重试）")
