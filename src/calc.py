"""净利核算引擎。

口径由 config/formulas.json 驱动，默认值如下（与原工作簿一致，但已可配）：

    订单口径（四关独立，按订单类别分别控制）
        收入       GMV = Σ(折扣前小计) − Σ(卖家折扣)
        成本       Σ(成本单价 × 数量) × 人民币→本地汇率
        发单费     订单数 × 发单费(本地)
        平台抽成   按比例的费用（平台佣金/交易手续费）的计费基数
        为什么拆开：取消单、退货单、0 元样品单的收入是 0，
        但成本、发单费、平台抽成照样发生。整行排除会让成本被低估、净利虚高。

    各项费用（fees 列表，想加几条加几条）
        发单费     订单数 × 发单费(本地)
        平台佣金   平台抽成计费基数 × 佣金率
        交易手续费 平台抽成计费基数(实付) × 费率
        返现/券/成长/物流 = GMV × 各自费率
        达人佣金   联盟佣金按商品ID汇总
        广告花费   广告花费(美金)按商品ID汇总 × 美金→本地汇率

    净利(本地) = GMV − 成本 − 各项费用合计
    净利(人民币) = 净利(本地) ÷ 人民币→本地汇率
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from . import formula, paths
from .loader import OrderLine, RawData, merged_aliases

ZERO = 0.0


# ---------------- 参数 ----------------

@dataclass
class Params:
    country: str
    cny_to_local: float
    usd_to_local: float
    dispatch_fee_cny: float
    dispatch_fee_local: float
    platform_commission: float
    tx_fee: float
    cashback: float
    super_coupon: float
    growth_fee: float
    logistics: float
    # 订单服务费：每个国家不一样，本地货币、按单收
    order_service: float = 0.0
    # 其他费用：按 GMV 比例收的杂项
    other_fee: float = 0.0
    # 以下为配置辅助字段，不参与计算
    currency: str = ""
    region: str = ""
    # False 表示该国费率是占位值，需要在店铺后台核对；报表里会明确提示
    rates_verified: bool = True
    # 本次实际用的汇率来源说明（由 cli 写入，供报表口径页使用）
    fx_note: str = ""
    # 费率项的中文名（rates.json 的 labels 段），公式里可以直接用中文引用
    cn_labels: dict = field(default_factory=dict)


@dataclass
class Switches:
    super_coupon_enabled: bool = False
    only_paid_orders: bool = True
    # 订单实付在订单内各行的分配方式：
    #   equal        按行数平分（与原工作簿人工列一致，校验可得 100% 命中）
    #   proportional 按各行 GMV 占比分配（经济含义更准，但与人工表会有细微差异）
    allocation_paid: str = "equal"


def _f(c: dict, key: str, default: float = 0.0) -> float:
    """读一个数值，缺失或非法就当 0。新加的国家可以先只填汇率。"""
    v = c.get(key, default)
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _build_field_titles(order_aliases: dict,
                        order_fields: dict | None = None) -> dict[str, list[str]]:
    """把「字段 → 备胎标题」摊平成 {归一化标题: [归一化备胎...]}。

    公式里写 [折扣前小计] 这种主标题时，compile_expr 用它去查真正的列：
    先按主标题找，找不到就按顺序试这个字段配的其它标题。

    同时把「字段名」本身（如 subtotal_before）和它的中文名（如 折扣前小计）
    也映射到同一组候选——这样公式可以直接写字段名，表头变了只要在
    「表头识别」里加候选，公式一个字都不用改。
    """
    out: dict[str, list[str]] = {}
    of = order_fields or {}
    for key, titles in (order_aliases or {}).items():
        cands = [formula._norm(t) for t in titles if str(t).strip()]
        if not cands:
            continue
        for t in cands:
            out.setdefault(t, cands)
        # 逻辑字段名 / 中文标签 → 同一组候选
        out.setdefault(formula._norm(key), cands)
        meta = of.get(key) if isinstance(of, dict) else None
        if isinstance(meta, dict):
            lbl = str(meta.get("label") or "").strip()
            if lbl:
                out.setdefault(formula._norm(lbl), cands)
    return out


def load_rates(path: Path) -> tuple[dict[str, Params], Switches]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, Params] = {}
    for name, c in data.get("countries", {}).items():
        cny = _f(c, "cny_to_local")
        dcny = _f(c, "dispatch_fee_cny")

        # 发单费按**人民币**定价，再按汇率换成本地货币。
        # 所以汇率一变，发单费跟着变——这是业务上的实际规则（发单费在国内结算）。
        # 只在没填人民币价时才退回本地值，兼容老配置。
        dlocal = round(dcny * cny, 6) if (dcny and cny) else _f(c, "dispatch_fee_local")

        out[name] = Params(
            country=name,
            cny_to_local=cny,
            usd_to_local=_f(c, "usd_to_local"),
            dispatch_fee_cny=dcny,
            dispatch_fee_local=dlocal,
            platform_commission=_f(c, "platform_commission"),
            tx_fee=_f(c, "tx_fee"),
            cashback=_f(c, "cashback"),
            super_coupon=_f(c, "super_coupon"),
            growth_fee=_f(c, "growth_fee"),
            logistics=_f(c, "logistics"),
            order_service=_f(c, "order_service"),
            other_fee=_f(c, "other_fee"),
            currency=str(c.get("currency") or ""),
            region=str(c.get("region") or ""),
            rates_verified=bool(c.get("rates_verified", True)),
            cn_labels=dict(data.get("labels") or {}),
        )
    sw = data.get("switches", {})
    switches = Switches(
        super_coupon_enabled=bool(sw.get("super_coupon_enabled", False)),
        only_paid_orders=bool(sw.get("only_paid_orders", True)),
        allocation_paid=str(sw.get("allocation_paid", "equal")),
    )
    return out, switches


# ---------------- 日期过滤 ----------------

# 两种写法都要认，而且**顺序不能反着猜**：
#   马来站导出  08/10/2026 23:58:53      → 日/月/年
#   菲律宾站导出 09/30/2026 11:35:45 PM  → 月/日/年 + 12 小时制
# 以前只认「日/月/年」且不带 AM/PM，菲律宾店的日期一条都解析不出来，
# 于是日期过滤整个失效——要 10 月的数据，实际把整份文件都算进去了，
# 报表还只在异常页轻描淡写提一句。
_ISO_PATTERNS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
)
_DMY_PATTERNS = (
    "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
)
_MDY_PATTERNS = (
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def date_order(samples: Iterable[str]) -> str:
    """判断这批日期是「日/月/年」还是「月/日/年」。

    单看一行分不出来（08/10 两种读法都成立），但一整个月的导出必然有
    日 > 12 的行：看到 30/09 就是日在前，看到 09/30 就是月在前。
    两边的证据都没有就按日在前（马来站是主站，历史上也是这么读的）。
    """
    dmy = mdy = 0
    for s in samples:
        m = re.match(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", str(s or ""))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            dmy += 1
        elif b > 12 >= a:
            mdy += 1
    return "mdy" if mdy > dmy else "dmy"


def parse_dt(s: str, order: str = "") -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    s = re.sub(r"\s+", " ", s)
    for p in _ISO_PATTERNS:
        try:
            return datetime.strptime(s, p)
        except ValueError:
            continue
    first, second = (_MDY_PATTERNS, _DMY_PATTERNS) if order == "mdy" else (_DMY_PATTERNS, _MDY_PATTERNS)
    for p in first:
        try:
            return datetime.strptime(s, p)
        except ValueError:
            continue
    for p in second:
        try:
            return datetime.strptime(s, p)
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def filter_by_date(lines: list[OrderLine], dfrom: Optional[date], dto: Optional[date],
                   field: str = "created") -> list[OrderLine]:
    if not dfrom and not dto:
        return lines
    # 先拿前 300 行判断这批数据是「日/月」还是「月/日」，再统一按那个顺序解析。
    # 逐行各猜各的会把 08/10 拆成两个月份，报表就错得没边了。
    order = date_order((l.created if field == "created" else l.paid) for l in lines[:300])
    out = []
    for l in lines:
        dt = parse_dt(l.created if field == "created" else l.paid, order)
        if dt is None:
            # 没有时间字段的行不丢弃，留给异常页提示
            out.append(l)
            continue
        d = dt.date()
        if dfrom and d < dfrom:
            continue
        if dto and d > dto:
            continue
        out.append(l)
    return out


# ---------------- 结果 ----------------

@dataclass
class ProductMetrics:
    product_id: str
    style_codes: str = ""
    product_name: str = ""
    gmv: float = ZERO
    buyer_paid: float = ZERO
    orders: float = ZERO
    items: float = ZERO
    refund: float = ZERO
    cost_local: float = ZERO
    # 计费基数（保留兼容旧配置，新配置请直接用费用的 kinds）
    fee_base_gmv: float = ZERO
    fee_base_paid: float = ZERO
    # 按订单类别分开记的原始明细：{类别: {gmv, buyer_paid, orders, items, cost, refund}}
    #
    # 为什么要这么存：用户要能「发单费只算正常单和退货单，取消单不算」，
    # 也就是每一项费用各自挑订单类别。汇总成一个数就再也拆不开了，
    # 所以这里把每类订单的原始量都留着，算费用时按需合计。
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)
    # 费用明细：费用 id → 金额。id 与 config/formulas.json 的 fees 对应，
    # 所以用户加一项「订单服务费」这里就自动多一个键，不用改代码。
    fees: dict[str, float] = field(default_factory=dict)
    # 以下老字段保留：报表模板与历史代码在用，算完费用后从 fees 同步过来
    dispatch_fee: float = ZERO
    platform_commission: float = ZERO
    tx_fee: float = ZERO
    cashback: float = ZERO
    super_coupon: float = ZERO
    growth_fee: float = ZERO
    logistics: float = ZERO
    creator_commission: float = ZERO
    ad_spend: float = ZERO
    lines: int = 0
    # 公式算出来的净利。由 config/formulas.json 的 net 决定；
    # 为 None 时退回下面的默认算法（保证没有配置也能跑）。
    net_value: float | None = None

    @property
    def aov(self) -> float:
        return self.gmv / self.orders if self.orders else ZERO

    @property
    def gross_profit(self) -> float:
        return (self.gmv - self.cost_local) / self.gmv if self.gmv else ZERO

    @property
    def fee_total(self) -> float:
        # 有明细就用明细（支持用户自定义费用），没有就退回老字段之和
        if self.fees:
            return sum(self.fees.values())
        return (self.dispatch_fee + self.platform_commission + self.tx_fee + self.cashback
                + self.super_coupon + self.growth_fee + self.logistics
                + self.creator_commission + self.ad_spend)

    @property
    def net_margin(self) -> float:
        return self.net_local / self.gmv if self.gmv else ZERO

    @property
    def net_local(self) -> float:
        """净利（本地货币）。

        优先用 config/formulas.json 里 net 公式算出的值——这样用户改公式就真的改了算法。
        没配公式时退回默认：(GMV − 成本) − 各项费用。
        用绝对额而不是「GMV × 净利率」，这样零 GMV 但有广告花费的链接
        也能正确算出亏损，而不是被算成 0。
        """
        if self.net_value is not None:
            return self.net_value
        return (self.gmv - self.cost_local) - self.fee_total

    def net_cny(self, cny_to_local: float) -> float:
        return self.net_local / cny_to_local if cny_to_local else ZERO


@dataclass
class CalcResult:
    params: Params
    products: list[ProductMetrics]
    issues: list[str] = field(default_factory=list)
    line_count: int = 0
    order_count: int = 0
    lines: list[OrderLine] = field(default_factory=list)
    # 本次实际生效的口径（报表「口径」页要展示，事后对账靠它）
    order_filter: dict = field(default_factory=dict)
    # 各类订单的行数：{类别: 行数}
    kind_stats: dict = field(default_factory=dict)
    # 判定「退货」的关键词（配置里可改，报表分类要用同一套，不能各写一份）
    refund_keywords: tuple = ()
    # 本次生效的参数表（报表口径页要逐行展示：每个参数只算哪些订单类别）
    params_cfg: list = field(default_factory=list)
    # 类别中文名（配置里可改）
    kind_labels: dict = field(default_factory=dict)
    # 总开关：本次是否整张表都不算 0 元样品单（报表口径页要写明）
    exclude_zero_amount: bool = False
    # 另一套口径的结果，用来在报表里做「含样品 / 去样品」对比。
    # 键是商品ID，值是那套口径下算出来的整组指标（GMV/成本/发单费/净利…）。
    # 只有一份口径时（比如没传 fcfg）就是 None，报表上不出现对比列。
    compare_label: str = ""
    compare: dict = field(default_factory=dict)
    compare_total: float | None = None
    # 对比口径下生效的参数表（样子跟 params_cfg 一样，只是样品单那格相反）。
    # 「带公式导出」要按它算另一套行级分摊值，所以得留在结果里。
    compare_params_cfg: list = field(default_factory=list)

    @property
    def total(self) -> ProductMetrics:
        t = ProductMetrics(product_id="合计", style_codes="")
        agg: dict[str, float] = {}
        net_sum = ZERO
        net_any = False
        for p in self.products:
            # 净利是公式算出来的（net_value），必须单独累加。
            # 不累加的话「合计」这一行会退回硬编码算法：
            # 改了公式，明细行变了、报表总额却没变。
            if p.net_value is not None:
                net_sum += p.net_value
                net_any = True
            t.gmv += p.gmv
            t.buyer_paid += p.buyer_paid
            t.fee_base_gmv += p.fee_base_gmv
            t.fee_base_paid += p.fee_base_paid
            for k, d in p.by_kind.items():
                td = t.by_kind.get(k)
                if td is None:
                    td = {kk: ZERO for kk in d}
                    t.by_kind[k] = td
                for kk, vv in d.items():
                    td[kk] = td.get(kk, ZERO) + vv
            t.orders += p.orders
            t.items += p.items
            t.refund += p.refund
            t.cost_local += p.cost_local
            t.dispatch_fee += p.dispatch_fee
            t.platform_commission += p.platform_commission
            t.tx_fee += p.tx_fee
            t.cashback += p.cashback
            t.super_coupon += p.super_coupon
            t.growth_fee += p.growth_fee
            t.logistics += p.logistics
            t.creator_commission += p.creator_commission
            t.ad_spend += p.ad_spend
            t.lines += p.lines
            for k, v in (p.fees or {}).items():
                agg[k] = agg.get(k, 0.0) + v
        t.fees = agg
        if net_any:
            t.net_value = net_sum
        return t

    @property
    def net_cny_total(self) -> float:
        return self.total.net_cny(self.params.cny_to_local)


# ---------------- 主计算 ----------------

# ---------------- 订单口径：按「行类别 × 四项」分别控制 ----------------
#
# 为什么不能只用一个开关控制整行：
#   取消单 / 退货单 / 0 元样品单的收入通常是 0（甚至要冲回），
#   但它们的**成本是真实发生的**——货采购了、发出了，发单费按订单数已经收了，
#   平台抽成也照收（样品单除外）。如果把它们整行排除，
#   成本就被低估，净利被高估。所以收入、成本、订单数、计费基数必须分开控。
#
# 一行订单可能同时符合多个类别，按这个优先级归一类：
#   已取消 > 退货退款 > 0元样品 > 未付款 > 正常
KINDS = ("cancelled", "refunded", "zero_amount", "unpaid", "normal")

KIND_LABELS = {
    "cancelled": "已取消订单",
    "refunded": "退货退款订单",
    "zero_amount": "0 元样品单",
    "unpaid": "未付款订单",
    "normal": "正常订单",
}

# 四个口径项：
#   income       收入（GMV 与买家实付算不算）
#   cost         商品成本（含件数）
#   order_count  订单数（发单费等按单计费的基数）
#   fee_base     平台抽成的计费基数（按比例的佣金/返现/物流等）
ALL_ON = {"income": True, "cost": True, "order_count": True, "fee_base": True}
ALL_OFF = {"income": False, "cost": False, "order_count": False, "fee_base": False}

# 默认口径（按业务实情）：
#   取消 / 退货：收入不算（钱退了），但成本、发单费、平台抽成照算
#   0 元样品：收入本来就是 0，成本与发单费照算；平台抽成不算（没有成交额）
#   未付款：什么都没发生，全不算
DEFAULT_ORDER_FILTER: dict[str, dict[str, bool]] = {
    "cancelled": {"income": False, "cost": True, "order_count": True, "fee_base": True},
    "refunded": {"income": False, "cost": True, "order_count": True, "fee_base": True},
    "zero_amount": {"income": True, "cost": True, "order_count": True, "fee_base": False},
    "unpaid": {"income": False, "cost": False, "order_count": False, "fee_base": False},
    "normal": dict(ALL_ON),
}

# 旧配置只有 include_xxx 一个总开关，映射成「四项同开/同关」保持兼容
_LEGACY_KEYS = {
    "include_cancelled": "cancelled",
    "include_refunded": "refunded",
    "include_zero_amount": "zero_amount",
    "include_unpaid": "unpaid",
}


def order_kind(l: OrderLine, refund_keywords: tuple[str, ...] = ("return", "refund")) -> str:
    """判断一行订单属于哪一类（决定用哪组口径开关）。"""
    if l.is_cancelled:
        c = (l.cancel or "").lower()
        if any(k in c for k in refund_keywords):
            return "refunded"
        return "cancelled"
    if l.gmv == 0 and l.order_amount == 0:
        return "zero_amount"
    if not l.is_paid:
        return "unpaid"
    return "normal"


def resolve_order_filter(flt: dict | None) -> dict[str, dict[str, bool]]:
    """把 order_filter 配置解析成 {类别: 四项开关}。

    支持两种写法，可以混用：
      新写法（推荐）  "cancelled": {"income":false,"cost":true,...}
      旧写法          "include_cancelled": false   → 四项同开/同关
    没写的项沿用默认值，所以只写想改的那一项就行。
    """
    flt = flt or {}
    out: dict[str, dict[str, bool]] = {}
    for kind in KINDS:
        default = DEFAULT_ORDER_FILTER[kind]
        spec = flt.get(kind)
        if isinstance(spec, dict):
            item = dict(default)
            for k in ("income", "cost", "order_count", "fee_base"):
                if k in spec:
                    item[k] = bool(spec[k])
            out[kind] = item
            continue
        # 没有分类配置 → 看旧的总开关
        legacy = next((lk for lk, lkind in _LEGACY_KEYS.items()
                       if lkind == kind and lk in flt), None)
        if legacy is not None:
            out[kind] = dict(ALL_ON if flt[legacy] else ALL_OFF)
        else:
            out[kind] = dict(default)
    return out


def _compute_once(data: RawData, params: Params, switches: Switches,
                  dfrom: Optional[date] = None, dto: Optional[date] = None,
                  date_field: str = "created", fcfg: dict | None = None) -> CalcResult:
    issues = list(data.issues)
    lines = filter_by_date(data.orders, dfrom, dto, date_field)

    # 口径配置（config/formulas.json）：订单怎么筛、费用有哪些、净利怎么算。
    # 默认值与之前的写死逻辑完全一致，所以不配这个文件时结果不变。
    if fcfg is None:
        fcfg = formula.load_config(paths.CONFIG / "formulas.json")
    flt = fcfg.get("order_filter") or {}
    # 「退货」关键词可配：有些店铺导出里退款类型写 Refund 之外的字样
    refund_kw = tuple(
        str(x).strip().lower() for x in (flt.get("refund_keywords") or ["return", "refund"])
        if str(x).strip()
    )
    kfilter = resolve_order_filter(flt)
    # 四关全关的类别直接整行不要（等价于「这一行完全不参与」）
    kind_stats: dict[str, int] = {k: 0 for k in KINDS}

    # 这个国家生效的一套：参数表、订单表字段标题、成本表列标题、净利公式。
    cc = formula.country_config(fcfg, params.country)
    order_aliases = merged_aliases(cc["order_fields"])
    # 把「订单表识别」里的字段 → 备胎标题 摊平成 {归一化标题: [归一化备胎...]}，
    # 公式里写 [折扣前小计] 这类主标题时，程序据此去找真正的列。
    field_titles = _build_field_titles(order_aliases, cc.get("order_fields"))

    # 订单实付分配：按 GMV 占比时，先算出每个订单的 GMV 合计。
    # 用全量订单（不受日期过滤影响），避免跨期的订单被截断后分配比例失真。
    order_gmv_total: dict[str, float] = defaultdict(float)
    if switches.allocation_paid == "proportional":
        for l in data.orders:
            order_gmv_total[l.order_id] += max(l.gmv, 0.0)

    def alloc_paid(l: OrderLine) -> float:
        if switches.allocation_paid != "proportional":
            return l.allocated_paid
        tot = order_gmv_total.get(l.order_id, 0.0)
        if tot <= 0:
            return l.allocated_paid
        return l.order_amount * (max(l.gmv, 0.0) / tot)

    buckets: dict[str, ProductMetrics] = {}
    names: dict[str, str] = {}
    no_pid = 0
    missing_cost_lines = 0
    untimed = 0
    # 这批订单的日期是「日/月」还是「月/日」，跟 filter_by_date 用同一套判断，
    # 否则会出现「按日/月筛选了，但缺时间的行按另一套算」的错位。
    _dorder = date_order((l.created if date_field == "created" else l.paid) for l in lines[:300])

    for l in lines:
        if parse_dt(l.created if date_field == "created" else l.paid, _dorder) is None:
            untimed += 1

        # 分组键只有一个：链接（商品ID）。订单表里没有商品ID 很正常，
        # 商品ID 是靠订单行的「SKU ID」去商品SKU表里查出来的（SKU ID 唯一）。
        #
        # 以前这里有个兜底：一条商品ID都没有时，按「商品名称」归并。
        # 已经删掉——标题会被改，同一条链接在订单里会出现好几个标题，
        # 按标题归并会把一条链接拆成好几行（Chroma Home 实测 101 条链接里
        # 有 7 条被拆过），而广告费只认商品ID、挂不到这些拆出来的行上，
        # 于是「每条链接的 GMV / 净利」全是错的，表面还很正常。
        # 宁可如实归进「无ID」，也不能给一份对不上的链接表。
        pid = l.product_id
        if not pid:
            pid = "无ID"
            no_pid += 1
        m = buckets.get(pid)
        if m is None:
            m = ProductMetrics(product_id=pid, style_codes=l.style_codes)
            buckets[pid] = m
        if l.style_codes and not m.style_codes:
            m.style_codes = l.style_codes
        # 商品名只用来显示，不参与归并。
        # 后来出现的覆盖前面的：链接标题被改过时，报表上显示最近一次的叫法。
        if l.product_name:
            names[pid] = l.product_name

        m.lines += 1

        # 这一行属于哪类订单，决定用哪组口径开关
        kind = order_kind(l, refund_kw)
        sw_k = kfilter[kind]
        kind_stats[kind] += 1

        # 取消 / 退货金额：只是统计量（用来在报表上说明「退了多少钱」），
        # 不受口径开关影响，否则对账时看不出退了多少
        refund_line = l.refund_amount or (l.order_amount if kind in ("cancelled", "refunded") else 0.0)
        if refund_line:
            m.refund += refund_line

        paid_amt = alloc_paid(l)

        # 不管口径怎么设，都先把这一行的原始量按类别记下来。
        # 计算参数各自的 kinds 就是从这里取数的——
        # 这样「发单费只算正常+退货」「平台佣金只算正常」才能各自成立。
        #
        # 这里刻意「不」按 order_filter 清零：口径只由每个参数自己的 kinds 决定，
        # 两套开关叠加会让人算不清（改了参数没反应，或反应了但不知道是谁改的）。
        bk = m.by_kind.get(kind)
        if bk is None:
            bk = {}
            m.by_kind[kind] = bk
        # 订单表的每一列都按标题记下来：公式里写 [折扣前小计] 就从这里取。
        # 这样平台加一列、改列名、换语言，都不用动代码。
        #
        # 买家实付是「订单级」的金额，同一订单的每一行都写着整单的数，
        # 直接按行相加会重复计（三行订单就把实付算成三倍）。
        # 所以这一列按订单内行数分摊，和订单数的分摊口径一致。
        paid_titles = {formula._norm(c) for c in (order_aliases.get("order_amount") or [])}
        for title, val in l.raw_num.items():
            bk[title] = bk.get(title, ZERO) + (paid_amt if title in paid_titles else val)
        bk["__orders"] = bk.get("__orders", ZERO) + l.allocated_orders
        bk["__qty"] = bk.get("__qty", ZERO) + l.qty
        cost_cny = l.cost_total_cny
        bk["__cost_cny"] = bk.get("__cost_cny", ZERO) + (cost_cny if cost_cny is not None else ZERO)
        bk["__refund"] = bk.get("__refund", ZERO) + refund_line
        # 老字段留着：报表的毛利/异常页还在用
        bk["gmv"] = bk.get("gmv", ZERO) + l.gmv
        bk["buyer_paid"] = bk.get("buyer_paid", ZERO) + paid_amt
        bk["orders"] = bk.get("orders", ZERO) + l.allocated_orders
        bk["items"] = bk.get("items", ZERO) + l.qty
        bk["cost"] = bk.get("cost", ZERO) + (cost_cny or ZERO)
        bk["refund"] = bk.get("refund", ZERO) + refund_line

        # ① 收入：GMV 与买家实付
        if sw_k["income"]:
            m.gmv += l.gmv
            m.buyer_paid += paid_amt

        # ② 成本与件数（货已经采购/发出，钱就是花了）
        if sw_k["cost"]:
            m.items += l.qty
            if l.cost_total_cny is None:
                missing_cost_lines += 1
            else:
                m.cost_local += l.cost_total_cny * params.cny_to_local

        # ③ 订单数：发单费等按单计费的基数
        if sw_k["order_count"]:
            m.orders += l.allocated_orders

        # ④ 计费基数：平台抽成等按比例费用用（可与收入不同）
        if sw_k["fee_base"]:
            m.fee_base_gmv += l.gmv
            m.fee_base_paid += paid_amt

    # 有广告花费或达人佣金、但本期没有订单的商品
    # 钱已经花了，不能因为"没出单"就从报表里消失，否则总成本被低估
    orphans: list[str] = []
    for pid in set(data.ads) | set(data.commissions):
        if pid and pid not in buckets:
            buckets[pid] = ProductMetrics(product_id=pid)
            orphans.append(pid)

    # ── 参数表：GMV、买家实付、订单数、件数、商品成本、各项费用，全在这张表里 ──
    #
    # 每一项都自带 kinds（只算哪些订单类别），所以「发单费只算正常单和退货单」
    # 「平台佣金只算正常单」这类口径，都是改配置里的一个数组，不改代码。
    # 加一项新支出也是加一条，自动参与 net 的计算。
    param_cfg = list(cc["params"])
    net_formulas = cc["formulas"]
    # 老开关仍然生效：超级优惠券原来由 switches 控制
    if switches.super_coupon_enabled:
        for it in param_cfg:
            if isinstance(it, dict) and it.get("id") == "super_coupon":
                it["enabled"] = True

    # 参数表里这几项有固定去处（报表要按列展示），其余都进「费用明细」。
    # 优先认参数上写的 role；没写就按 id 猜（老配置没有 role）。
    _ROLE_ATTR = {
        "gmv": "gmv",
        "cost": "cost_local",
        "order_count": "orders",
        "items": "items",
        "buyer_paid": "buyer_paid",
    }
    role_of: dict[str, str] = {}
    for it in param_cfg:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "").strip()
        if not fid:
            continue
        role = str(it.get("role") or "").strip()
        attr = _ROLE_ATTR.get(role) or _ROLE_ATTR.get(fid)
        if attr:
            role_of[fid] = attr
    reserved_ids = set(role_of)

    fee_issues: list[str] = []
    for pid, m in buckets.items():
        m.product_name = names.get(pid, "")
        ctx = {
            "commission": data.commissions.get(pid, ZERO),
            "ads": data.ads.get(pid, ZERO),
            # 每一行订单按类别记的原始量，计算参数的 kinds 就是从这里取数
            "by_kind": m.by_kind,
            "params": params,
            # 公式里写 [标题] 时，靠这两个找列：
            #   titles  本次订单表的真实表头（裸写标题也能匹配）
            #   aliases 该国「基础字段 → 标题候选」（gmv/实付这些派生量用）
            "titles": data.order_titles,
            "aliases": order_aliases,
            # 公式里写 [主标题] 时，按「订单表识别」里配的备胎标题去找列
            "field_titles": field_titles,
            # 兜底单价：某个参数 kinds 里一件都没有时，公式里的 cost_unit 不至于变 0
            "cost_unit": (m.cost_local / m.items) if m.items else ZERO,
            # 中文别名：公式里可以直接写「平台佣金」「订单数」，不用记英文
            "rate_aliases": formula.rate_aliases(getattr(params, "cn_labels", None)),
        }
        vals, one_issues = formula.compute_params(param_cfg, ctx)
        if one_issues:
            fee_issues.extend(one_issues[:1])

        # 归位：报表按列展示的那几个量单独放，其余都算「费用」
        for fid, attr in role_of.items():
            if fid in vals:
                setattr(m, attr, vals[fid])
        m.fees = {k: v for k, v in vals.items() if k not in reserved_ids}

        # 退款金额只是统计量，永远取原始值（不受 kinds 影响，否则对账看不出退了多少）
        m.refund = sum(d.get("refund", ZERO) for d in m.by_kind.values())

        # 同步到老字段：报表模板与历史代码还在读这些名字
        m.dispatch_fee = m.fees.get("dispatch", ZERO)
        m.platform_commission = m.fees.get("platform_commission", ZERO)
        m.tx_fee = m.fees.get("tx_fee", ZERO)
        m.cashback = m.fees.get("cashback", ZERO)
        m.super_coupon = m.fees.get("super_coupon", ZERO)
        m.growth_fee = m.fees.get("growth_fee", ZERO)
        m.logistics = m.fees.get("logistics", ZERO)
        m.creator_commission = m.fees.get("affiliate", ZERO)
        m.ad_spend = m.fees.get("ads", ZERO)

        # 公式：gross_profit / total_fees / net / net_rate 都在这算。
        # 之前这段代码根本没被调用过（formulas 配了却不生效），
        # 净利一直是写死的属性。现在接上，改公式才真的改了算法。
        # 公式：gross_profit / total_fees / net / net_rate 都在这算。
        # 除了费用明细，还把每个参数算出的值、以及原始字段合计都放进去，
        # 所以净利公式想改成 "gmv - cost - platform_commission - dispatch" 也可以。
        fctx = {**formula.pick_kinds(m.by_kind, None), **vals, **m.fees,
                "gmv": m.gmv, "cost": m.cost_local,
                "buyer_paid": m.buyer_paid, "order_count": m.orders,
                "items": m.items, "refund": m.refund,
                # 内置量的英文名也放进来，中文别名才找得到对应值
                "orders": m.orders, "qty": m.items, "paid": m.buyer_paid,
                "cost_cny": m.cost_local / (params.cny_to_local or 1.0),
                "commission": m.fees.get("affiliate", ZERO),
                "ads": m.fees.get("ads", ZERO),
                "fees": m.fees, "params": params}
        # 净利这几行也支持中文写法：GMV - 商品成本 - 发单费 - ...
        # 优先级：参数表那一行的名字 > 费率项的中文名 > 内置量的中文名。
        # 「平台佣金」指参数表里算出来的那一项；费率另有名字（平台佣金率）。
        f_aliases = dict(ctx.get("rate_aliases") or {})
        for it in param_cfg:
            if not isinstance(it, dict):
                continue
            _fid = str(it.get("id") or "").strip()
            _flb = str(it.get("label") or "").strip()
            if _fid and _flb:
                f_aliases[_flb] = _fid
        # 公式结果名也能用中文：net_rate 写「净利 / GMV」
        for _fid, _fcn in formula.FORMULA_CN.items():
            f_aliases.setdefault(_fcn, _fid)
        fvals, f_issues = formula.compute_formulas(net_formulas, fctx, f_aliases)
        if f_issues:
            fee_issues.extend(f_issues[:1])
        if "net" in fvals:
            m.net_value = fvals["net"]

    for msg in dict.fromkeys(fee_issues):
        issues.append(msg)

    products = sorted(buckets.values(), key=lambda x: x.gmv, reverse=True)

    if no_pid:
        if no_pid >= len(lines):
            issues.append(
                "整批订单都定位不到链接ID，报表只能给出一行「无ID」汇总。"
                "原因是没有商品SKU表：链接ID 只能靠「SKU ID → 商品ID」查出来。"
                "处理办法：让扩展取一次商品数据（后台「商品数据分析 → 按 SKU → 导出」，"
                "程序会自动存到店铺目录下的「商品SKU表」），再重算"
            )
        else:
            issues.append(
                f"{no_pid} 行订单的 SKU ID 在商品SKU表里查不到对应链接，已归入「无ID」照常计入合计。"
                f"处理办法：重新取一次商品数据（商品SKU表），新上架的 SKU 才会进映射"
            )
    if missing_cost_lines:
        issues.append(f"{missing_cost_lines} 行订单缺成本，按 0 计，这些行的净利会偏高")
    if untimed:
        issues.append(f"{untimed} 行订单缺少可解析的时间字段，未按日期过滤，已全部计入")
    if orphans:
        cost = sum(data.ads.get(p, 0) + data.commissions.get(p, 0) for p in orphans)
        issues.append(f"{len(orphans)} 个商品本期无订单但有广告/佣金支出（合计 {cost:.2f}），已单列，净利为负")

    # 订单表本身是按日期过滤的，不用整段计入——但要看得出「这份文件覆盖到哪」，
    # 否则面板选了 9/1~9/30、文件夹里躺着一份导到 10/28 的订单，会让人以为算错了。
    _ranges = getattr(data, "ranges", None) or {}
    if (dfrom or dto) and data.orders:
        _miss = len(data.orders) - len(lines)
        if _miss > 0:
            lo = dfrom.isoformat() if dfrom else "不限"
            hi = dto.isoformat() if dto else "不限"
            orng = _ranges.get("订单表")
            tail = f"；这份订单表覆盖 {orng[0]}~{orng[1]}" if orng else ""
            issues.append(
                f"订单表里有 {_miss:,} 行不在报表区间 {lo}~{hi} 内（订单创建时间），"
                f"已排除，不计入 GMV / 订单数 / 成本{tail}"
            )

    # 广告 / 佣金是「整段一个数」的表（没有日期列），只能整段计入。
    # 一旦导出区间和报表区间对不上，那段日子的费用就被错算进这一期了——
    # 这件事没法自动修，但必须让人看见，否则净利率会莫名其妙偏低。
    for label in ("广告表", "联盟佣金表"):
        if label not in _ranges:
            continue
        a, b = _ranges[label]
        lo = dfrom.isoformat() if dfrom else ""
        hi = dto.isoformat() if dto else ""
        if not lo and not hi:
            continue
        out = []
        if lo and a < lo:
            out.append(f"{a}~{lo} 早于报表起始日")
        if hi and b > hi:
            out.append(f"{hi}~{b} 晚于报表结束日")
        if out:
            issues.append(
                f"{label}覆盖 {a}~{b}，报表区间是 {lo or '不限'}~{hi or '不限'}"
                f"（{'、'.join(out)}）。这张表没有日期列，费用只能整段计入，"
                f"这一项的净利会偏；建议按报表区间重新导一次"
            )

    order_ids = {l.order_id for l in lines if l.order_id}
    return CalcResult(params=params, products=products, issues=issues,
                      line_count=len(lines), order_count=len(order_ids), lines=lines,
                      order_filter=kfilter, kind_stats=dict(kind_stats),
                      refund_keywords=refund_kw,
                      params_cfg=[it for it in param_cfg if isinstance(it, dict)],
                      kind_labels=dict(fcfg.get("kinds") or KIND_LABELS),
                      exclude_zero_amount=bool(cc.get("exclude_zero_amount")))


def compute(data: RawData, params: Params, switches: Switches,
            dfrom: Optional[date] = None, dto: Optional[date] = None,
            date_field: str = "created", fcfg: dict | None = None,
            with_compare: bool = True) -> CalcResult:
    """核算一次，并顺带把「另一套样品口径」的净利也算出来。

    为什么要两套一起算：以前想看「剔除样品单之后差多少」，得先切开关、
    跑一次、记下数字，再切回来跑一次。来回两遍还容易记错。
    现在一次跑完，报表里「净利(含样品) / 净利(去样品) / 差额」并排列出来，
    不用来回切，也不会算错。

    两套口径互为补集：主口径按配置里「去除 0 元样品订单」的开关来，
    对比口径就是它的反面，所以报表上一定能看出剔不剔的差别。
    """
    res = _compute_once(data, params, switches, dfrom, dto, date_field, fcfg)
    if not with_compare:
        return res

    try:
        alt_cfg = json.loads(json.dumps(fcfg)) if fcfg else {}
        alt_cfg["exclude_zero_amount"] = not bool(res.exclude_zero_amount)
        alt = _compute_once(data, params, switches, dfrom, dto, date_field, alt_cfg)
    except Exception:  # noqa: BLE001
        # 对比口径算不出来就算了，主结果照出，不影响正常核算
        return res

    res.compare_label = "含 0 元样品单" if res.exclude_zero_amount else "去除 0 元样品单"
    res.compare = {p.product_id: p for p in alt.products}
    res.compare_total = float(alt.total.net_local)
    res.compare_params_cfg = [it for it in (alt.params_cfg or []) if isinstance(it, dict)]
    return res
