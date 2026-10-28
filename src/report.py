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

# 有 role 的参数是报表固定列，其余都算「费用」，一列一项。
ROLE_KEYS = {"gmv", "cost", "order_count", "items", "buyer_paid"}

BASE_HEADERS = [
    "排名", "款式编码", "链接（商品ID）", "商品名称",
    "GMV", "订单数", "客单价", "买家实付",
    "成本", "毛利", "毛利额",
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


def _summary(ws: Worksheet, res: CalcResult, subtitle: str) -> None:
    # 一条链接（商品ID）一行：同一链接下的多个 SKU 合并，
    # 不同链接即使卖家编码相同也绝不合并——卖家编码可在多条链接间复用，
    # 只有商品ID 是链接的唯一标识。
    fees = fee_items(res)
    headers = list(BASE_HEADERS) + [name for _, name in fees] + \
              ["费用合计", "净利率", "净利(本地货币)", "净利(人民币)"]
    n_fee = len(fees)
    c_fee0 = len(BASE_HEADERS) + 1          # 第一个费用列
    c_total = c_fee0 + n_fee                # 费用合计
    c_rate = c_total + 1                    # 净利率
    c_net = c_rate + 1                      # 净利(本地)
    c_net_cny = c_net + 1                   # 净利(人民币)

    _title(ws, "净利润汇总（每条链接一行）", subtitle, span=12)
    _head(ws, headers)

    money_cols = [5, 7, 8, 9, 11] + list(range(c_fee0, c_total + 1)) + [c_net, c_net_cny]
    pct_cols = [10, c_rate]

    def fee_vals(p) -> list[float]:
        return [float((p.fees or {}).get(fid, 0.0)) for fid, _ in fees]

    row = 3
    for i, p in enumerate(res.products, 1):
        fv = fee_vals(p)
        vals = [
            i, p.style_codes, p.product_id, p.product_name,
            p.gmv, p.orders, p.aov, p.buyer_paid,
            p.cost_local, p.gross_profit, p.gmv - p.cost_local,
            *fv,
            sum(fv), p.net_margin, p.net_local, p.net_cny(res.params.cny_to_local),
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.border = BORDER
        _fmt_row(ws, row, money_cols, pct_cols, [6])
        if p.net_local < 0:
            ws.cell(row=row, column=c_net).font = NEG_FONT
            ws.cell(row=row, column=c_net_cny).font = NEG_FONT
        row += 1

    t = res.total
    tfv = fee_vals(t)
    totals = [
        "", "合计", "", f"{res.order_count} 个订单 / {res.line_count} 行",
        t.gmv, t.orders, t.aov if t.orders else 0, t.buyer_paid,
        t.cost_local, t.gross_profit, t.gmv - t.cost_local,
        *tfv,
        sum(tfv), t.net_margin, t.net_local, res.net_cny_total,
    ]
    for c, v in enumerate(totals, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.fill = TOTAL_FILL
        cell.font = Font(bold=True)
        cell.border = TOP_BORDER
    _fmt_row(ws, row, money_cols, pct_cols, [6])

    # 净利率色阶：亏损越深越红，盈利越强越绿
    last = row - 1
    if last >= 3:
        from openpyxl.formatting.rule import ColorScaleRule
        col = get_column_letter(c_rate)
        ws.conditional_formatting.add(
            f"{col}3:{col}{last}",
            ColorScaleRule(start_type="num", start_value=-0.2, start_color="F4B6B6",
                           mid_type="num", mid_value=0, mid_color="FFFFFF",
                           end_type="num", end_value=0.25, end_color="A9D7B6"),
        )

    ws.freeze_panes = "E3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(headers))}{last}"
    _widths(ws, {1: 6, 2: 20, 3: 22, 4: 30})
    for c in range(5, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 12


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


def _detail(ws: Worksheet, lines: list[OrderLine], subtitle: str) -> None:
    _title(ws, "订单明细", subtitle, span=8)
    _head(ws, DETAIL_HEADERS)
    row = 3
    for l in lines:
        vals = [
            l.order_id, l.created, l.product_id or "无ID", l.style_codes,
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
