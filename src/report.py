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

from .calc import CalcResult, Params, Switches
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

SUMMARY_HEADERS = [
    "排名", "款式编码", "商品ID", "商品名称",
    "GMV", "订单数", "客单价", "买家实付",
    "成本", "毛利", "毛利额",
    "发单费", "平台佣金", "交易手续费", "奖金返现", "超级优惠券",
    "商家成长费", "物流费", "达人佣金", "广告费", "费用合计",
    "净利率", "净利(本地货币)", "净利(人民币)",
]
MONEY_COLS = [5, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24]
PCT_COLS = [10, 22]
INT_COLS = [6]


def _summary(ws: Worksheet, res: CalcResult, subtitle: str) -> None:
    _title(ws, "净利润汇总（按商品）", subtitle, span=12)
    _head(ws, SUMMARY_HEADERS)
    row = 3
    for i, p in enumerate(res.products, 1):
        vals = [
            i, p.style_codes, p.product_id, p.product_name,
            p.gmv, p.orders, p.aov, p.buyer_paid,
            p.cost_local, p.gross_profit, p.gmv - p.cost_local,
            p.dispatch_fee, p.platform_commission, p.tx_fee, p.cashback, p.super_coupon,
            p.growth_fee, p.logistics, p.creator_commission, p.ad_spend,
            (p.dispatch_fee + p.platform_commission + p.tx_fee + p.cashback + p.super_coupon
             + p.growth_fee + p.logistics + p.creator_commission + p.ad_spend),
            p.net_margin, p.net_local, p.net_cny(res.params.cny_to_local),
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.border = BORDER
        _fmt_row(ws, row, MONEY_COLS, PCT_COLS, INT_COLS)
        if p.net_local < 0:
            ws.cell(row=row, column=23).font = NEG_FONT
            ws.cell(row=row, column=24).font = NEG_FONT
        row += 1

    t = res.total
    totals = [
        "", "合计", "", f"{res.order_count} 个订单 / {res.line_count} 行",
        t.gmv, t.orders, t.aov if t.orders else 0, t.buyer_paid,
        t.cost_local, t.gross_profit, t.gmv - t.cost_local,
        t.dispatch_fee, t.platform_commission, t.tx_fee, t.cashback, t.super_coupon,
        t.growth_fee, t.logistics, t.creator_commission, t.ad_spend,
        (t.dispatch_fee + t.platform_commission + t.tx_fee + t.cashback + t.super_coupon
         + t.growth_fee + t.logistics + t.creator_commission + t.ad_spend),
        t.net_margin, t.net_local, res.net_cny_total,
    ]
    for c, v in enumerate(totals, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.fill = TOTAL_FILL
        cell.font = Font(bold=True)
        cell.border = TOP_BORDER
    _fmt_row(ws, row, MONEY_COLS, PCT_COLS, INT_COLS)

    # 净利率色阶：亏损越深越红，盈利越强越绿
    last = row - 1
    if last >= 3:
        rng = f"V3:V{last}"
        from openpyxl.formatting.rule import ColorScaleRule
        ws.conditional_formatting.add(
            rng,
            ColorScaleRule(start_type="num", start_value=-0.2, start_color="F4B6B6",
                           mid_type="num", mid_value=0, mid_color="FFFFFF",
                           end_type="num", end_value=0.25, end_color="A9D7B6"),
        )

    ws.freeze_panes = "E3"
    ws.auto_filter.ref = f"A2:X{last}"
    _widths(ws, {1: 6, 2: 20, 3: 22, 4: 30})
    for c in range(5, 25):
        ws.column_dimensions[get_column_letter(c)].width = 12


# ---------------- 费用拆解 ----------------

FEE_ITEMS = [
    ("发单费", "dispatch_fee"),
    ("平台佣金", "platform_commission"),
    ("交易手续费", "tx_fee"),
    ("奖金返现", "cashback"),
    ("超级优惠券", "super_coupon"),
    ("商家成长费", "growth_fee"),
    ("物流费", "logistics"),
    ("达人佣金", "creator_commission"),
    ("广告费", "ad_spend"),
]


def _fee_breakdown(ws: Worksheet, res: CalcResult, subtitle: str) -> None:
    _title(ws, "费用拆解（长表，可直接透视）", subtitle, span=6)
    _head(ws, ["商品ID", "款式编码", "费用项", "金额", "占GMV", "占费用合计"])
    row = 3
    for p in res.products:
        fees = [(n, getattr(p, k)) for n, k in FEE_ITEMS]
        total_fee = sum(v for _, v in fees) or 0
        for name, val in fees:
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
    from .calc import parse_dt
    _title(ws, "日趋势", subtitle, span=6)
    _head(ws, ["日期", "GMV", "订单数", "买家实付", "净利(本地)", "净利率"])

    day_gmv: dict[str, float] = {}
    day_paid: dict[str, float] = {}
    day_orders: dict[str, float] = {}
    for l in lines:
        if l.is_cancelled:
            continue
        dt = parse_dt(l.created)
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

ISSUE_RULES = [
    ("缺成本", "成本表里查不到该 SKU，且无法推导；成本按 0 计，净利偏高", "补录成本表"),
    ("缺商品映射", "链接资料里没有该 SKU，无法定位商品ID", "补链接资料，或在 config/manual_map.json 手工指定"),
    ("未付款", "订单实付为 0，已按口径排除在订单数和成本之外", "确认是否为赠品或异常单"),
    ("取消退货", "带取消/退货标记，已从 GMV 与成本中排除", "无需处理，仅作提示"),
]


def _issues(ws: Worksheet, lines: list[OrderLine], res: CalcResult, subtitle: str) -> None:
    _title(ws, "异常与待办", subtitle, span=5)
    _head(ws, ["类别", "对象", "影响行数", "说明", "建议动作"])

    # 缺成本 / 缺映射：逐个 SKU 列出，需要人工补数据
    detail: dict[tuple[str, str], int] = {}
    # 未付款 / 取消退货：只汇总，不需要处理
    summary: dict[str, int] = {}

    for l in lines:
        if l.cost_unit_cny is None:
            detail[("缺成本", l.seller_sku)] = detail.get(("缺成本", l.seller_sku), 0) + 1
        elif not l.product_id:
            detail[("缺商品映射", l.seller_sku)] = detail.get(("缺商品映射", l.seller_sku), 0) + 1
        elif l.is_cancelled:
            summary["取消退货"] = summary.get("取消退货", 0) + 1
        elif not l.is_paid:
            summary["未付款"] = summary.get("未付款", 0) + 1

    desc = {k: (d, a) for k, d, a in ISSUE_RULES}
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


def _detail(ws: Worksheet, lines: list[OrderLine], subtitle: str) -> None:
    _title(ws, "订单明细", subtitle, span=8)
    _head(ws, DETAIL_HEADERS)
    row = 3
    for l in lines:
        vals = [
            l.order_id, l.created, l.product_id or "未识别", l.style_codes,
            l.seller_sku, l.sku_id, l.product_name, l.qty,
            l.gmv, l.allocated_paid, l.allocated_orders, l.order_line_count,
            l.cost_unit_cny, l.cost_total_cny, l.status, l.cancel,
        ]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row, column=c, value=v).border = BORDER
        _fmt_row(ws, row, [9, 10, 13, 14], [], [8, 12])
        row += 1
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:P{max(row - 1, 3)}"
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
        ("汇率：人民币→本地", p.cny_to_local, f"1 人民币 = {p.cny_to_local} 本地货币"),
        ("汇率：美金→本地", p.usd_to_local, f"1 美金 = {p.usd_to_local} 本地货币"),
        ("发单费(本地)", p.dispatch_fee_local, "按订单数计，不按金额"),
        ("平台佣金率", p.platform_commission, "基数为 GMV"),
        ("交易手续费率", p.tx_fee, "基数为买家实付"),
        ("奖金返现费率", p.cashback, "基数为 GMV"),
        ("超级优惠券费率", p.super_coupon if switches.super_coupon_enabled else 0,
         "开关：" + ("启用" if switches.super_coupon_enabled else "关闭（按 0 计）")),
        ("商家成长费率", p.growth_fee, "基数为 GMV"),
        ("物流费率", p.logistics, "基数为 GMV（比例法，非实际运费账单）"),
        ("GMV 口径", "折扣前小计 − 卖家折扣", "不含平台补贴；排除取消/退货行"),
        ("订单数口径", "订单级计数按行数平分", "一个订单的分配数合计为 1"),
        ("买家实付口径", f"{switches.allocation_paid}", "订单实付在订单内各行的分配方式"),
        ("成本口径", "成本表单价 × 数量 × 汇率", "多件装按基座价×件数推导，组合装按组件相加"),
        ("达人佣金口径", "联盟报表预估佣金", "退款部分未额外扣减"),
        ("广告口径", "广告花费(美金) × 美金汇率", "按商品ID汇总"),
        ("未付款订单", "排除" if switches.only_paid_orders else "计入",
         "影响订单数、成本与买家实付"),
    ]
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
                 extra: Optional[dict[str, str]] = None) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    _summary(ws, res, subtitle)

    _fee_breakdown(wb.create_sheet("费用拆解"), res, subtitle)
    _daily(wb.create_sheet("日趋势"), lines, res, subtitle)
    _issues(wb.create_sheet("异常"), lines, res, subtitle)
    _detail(wb.create_sheet("明细"), lines, subtitle)
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
