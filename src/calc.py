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
from typing import Optional

from . import formula, paths
from .loader import OrderLine, RawData

ZERO = 0.0


# ---------------- 参数 ----------------

@dataclass
class Params:
    country: str
    cny_to_local: float
    usd_to_local: float
    dispatch_fee_local: float
    platform_commission: float
    tx_fee: float
    cashback: float
    super_coupon: float
    growth_fee: float
    logistics: float
    # 以下为配置辅助字段，不参与计算
    currency: str = ""
    region: str = ""
    # False 表示该国费率是占位值，需要在店铺后台核对；报表里会明确提示
    rates_verified: bool = True
    # 本次实际用的汇率来源说明（由 cli 写入，供报表口径页使用）
    fx_note: str = ""


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


def load_rates(path: Path) -> tuple[dict[str, Params], Switches]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, Params] = {}
    for name, c in data.get("countries", {}).items():
        out[name] = Params(
            country=name,
            cny_to_local=_f(c, "cny_to_local"),
            usd_to_local=_f(c, "usd_to_local"),
            dispatch_fee_local=_f(c, "dispatch_fee_local"),
            platform_commission=_f(c, "platform_commission"),
            tx_fee=_f(c, "tx_fee"),
            cashback=_f(c, "cashback"),
            super_coupon=_f(c, "super_coupon"),
            growth_fee=_f(c, "growth_fee"),
            logistics=_f(c, "logistics"),
            currency=str(c.get("currency") or ""),
            region=str(c.get("region") or ""),
            rates_verified=bool(c.get("rates_verified", True)),
        )
    sw = data.get("switches", {})
    switches = Switches(
        super_coupon_enabled=bool(sw.get("super_coupon_enabled", False)),
        only_paid_orders=bool(sw.get("only_paid_orders", True)),
        allocation_paid=str(sw.get("allocation_paid", "equal")),
    )
    return out, switches


# ---------------- 日期过滤 ----------------

_DATE_PATTERNS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
)


def parse_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    for p in _DATE_PATTERNS:
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
    out = []
    for l in lines:
        dt = parse_dt(l.created if field == "created" else l.paid)
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
    # 计费基数：算平台抽成等按比例费用的基数，可以与 gmv / buyer_paid 不同。
    # 为什么需要它：取消单和退货单的收入不算（钱退了），但平台抽成往往照收，
    # 所以「收入」和「计费基数」必须是两个独立的量，否则一关就全关了。
    fee_base_gmv: float = ZERO
    fee_base_paid: float = ZERO
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
        """绝对额口径：(GMV − 成本) − 各项费用。

        用绝对额而不是「GMV × 净利率」，这样零 GMV 但有广告花费的链接
        也能正确算出亏损，而不是被算成 0。
        """
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

    @property
    def total(self) -> ProductMetrics:
        t = ProductMetrics(product_id="合计", style_codes="")
        agg: dict[str, float] = {}
        for p in self.products:
            t.gmv += p.gmv
            t.buyer_paid += p.buyer_paid
            t.fee_base_gmv += p.fee_base_gmv
            t.fee_base_paid += p.fee_base_paid
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


def compute(data: RawData, params: Params, switches: Switches,
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

    for l in lines:
        if parse_dt(l.created if date_field == "created" else l.paid) is None:
            untimed += 1

        # 找不到商品ID 的行不丢弃、也不算异常：按「无ID」单独成组照常计入合计。
        # 这类行通常是没有映射的新商品，不影响整体净利。
        pid = l.product_id or "无ID"
        if not l.product_id:
            no_pid += 1
        m = buckets.get(pid)
        if m is None:
            m = ProductMetrics(product_id=pid, style_codes=l.style_codes)
            buckets[pid] = m
        if l.style_codes and not m.style_codes:
            m.style_codes = l.style_codes
        if l.product_name and pid not in names:
            names[pid] = l.product_name

        m.lines += 1

        # 这一行属于哪类订单，决定用哪组口径开关
        kind = order_kind(l, refund_kw)
        sw_k = kfilter[kind]
        kind_stats[kind] += 1

        # 取消 / 退货金额：只是统计量（用来在报表上说明「退了多少钱」），
        # 不受口径开关影响，否则对账时看不出退了多少
        if kind in ("cancelled", "refunded"):
            m.refund += l.order_amount

        paid_amt = alloc_paid(l)

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

    # 费用项：按 formulas.json 的 fees 列表逐项算。
    # 用户想加一项「订单服务费」，在那个列表里加一条就会自动参与净利计算，
    # 不用改这里的代码。
    fee_cfg = list(fcfg.get("fees") or [])
    # 老开关仍然生效：超级优惠券原来由 switches 控制，这里映射到费用项的 enabled
    if switches.super_coupon_enabled:
        for it in fee_cfg:
            if isinstance(it, dict) and it.get("id") == "super_coupon":
                it["enabled"] = True

    fee_issues: list[str] = []
    for pid, m in buckets.items():
        m.product_name = names.get(pid, "")
        ctx = {
            "gmv": m.gmv,
            "buyer_paid": m.buyer_paid,
            # 计费基数：与收入不同（取消单/退货单收入不算，但平台抽成照收）
            "fee_base": m.fee_base_gmv,
            "fee_base_paid": m.fee_base_paid,
            "order_count": m.orders,
            "orders": m.orders,
            "items": m.items,
            "cost": m.cost_local,
            "refund": m.refund,
            "commission": data.commissions.get(pid, ZERO),
            "ads": data.ads.get(pid, ZERO),
            "params": params,
        }
        fees, one_issues = formula.compute_fees(fee_cfg, ctx)
        m.fees = fees
        if one_issues:
            fee_issues.extend(one_issues[:1])
        # 同步到老字段：报表模板与历史代码还在读这些名字
        m.dispatch_fee = fees.get("dispatch", ZERO)
        m.platform_commission = fees.get("platform_commission", ZERO)
        m.tx_fee = fees.get("tx_fee", ZERO)
        m.cashback = fees.get("cashback", ZERO)
        m.super_coupon = fees.get("super_coupon", ZERO)
        m.growth_fee = fees.get("growth_fee", ZERO)
        m.logistics = fees.get("logistics", ZERO)
        m.creator_commission = fees.get("affiliate", ZERO)
        m.ad_spend = fees.get("ads", ZERO)

    for msg in dict.fromkeys(fee_issues):
        issues.append(msg)

    products = sorted(buckets.values(), key=lambda x: x.gmv, reverse=True)

    if no_pid:
        issues.append(
            f"{no_pid} 行订单在所有映射来源里都找不到商品归属，已归入「无ID」照常计入合计。"
            f"处理办法：更新主工作簿的「链接资料」表（覆盖当前全量），或重跑一次商品SKU取数"
        )
    if missing_cost_lines:
        issues.append(f"{missing_cost_lines} 行订单缺成本，按 0 计，这些行的净利会偏高")
    if untimed:
        issues.append(f"{untimed} 行订单缺少可解析的时间字段，未按日期过滤，已全部计入")
    if orphans:
        cost = sum(data.ads.get(p, 0) + data.commissions.get(p, 0) for p in orphans)
        issues.append(f"{len(orphans)} 个商品本期无订单但有广告/佣金支出（合计 {cost:.2f}），已单列，净利为负")

    order_ids = {l.order_id for l in lines if l.order_id}
    return CalcResult(params=params, products=products, issues=issues,
                      line_count=len(lines), order_count=len(order_ids), lines=lines,
                      order_filter=kfilter, kind_stats=dict(kind_stats),
                      refund_keywords=refund_kw)
