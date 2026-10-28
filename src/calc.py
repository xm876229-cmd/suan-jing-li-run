"""净利核算引擎。

口径完全对齐原工作簿「净利润」表：
    GMV      = Σ(订单!Q 折扣前小计) − Σ(订单!S 卖家折扣)，排除取消/退货行
    成本     = Σ(成本单价 × 数量) × 人民币→本地汇率
    毛利     = (GMV − 成本) / GMV
    发单费   = 订单数 × 发单费(本地)
    平台佣金 = GMV × 佣金率
    交易手续费 = 买家实付(分摊后) × 费率
    奖金返现 / 超级优惠券 / 商家成长 / 物流 = GMV × 各自费率
    达人佣金 = 联盟佣金按商品ID汇总
    广告花费 = 广告花费(美金)按商品ID汇总 × 美金→本地汇率
    净利率   = 毛利 − 各项费用占 GMV 的比例
    净利(本地) = GMV × 净利率
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

    @property
    def total(self) -> ProductMetrics:
        t = ProductMetrics(product_id="合计", style_codes="")
        agg: dict[str, float] = {}
        for p in self.products:
            t.gmv += p.gmv
            t.buyer_paid += p.buyer_paid
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
    inc_cancelled = bool(flt.get("include_cancelled", False))
    inc_refunded = bool(flt.get("include_refunded", False))
    inc_unpaid = bool(flt.get("include_unpaid", False))
    inc_zero = bool(flt.get("include_zero_amount", True))

    # 0 元样品单：整行是否参与。
    # 单独一个开关是因为发单费按订单数算，样品单加不加直接影响成本，
    # 不能只按金额一刀切，得让人自己决定。
    if not inc_zero:
        before = len(lines)
        lines = [l for l in lines if not (l.gmv == 0 and l.order_amount == 0)]
        dropped = before - len(lines)
        if dropped:
            issues.append(f"按设置排除了 {dropped} 行 0 元订单（样品单），如需计入请改 formulas.json 的 include_zero_amount")

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
        m.items += l.qty
        m.refund += l.order_amount if l.is_cancelled else 0.0

        # 取消 / 退货行是否计入，由 formulas.json 的 include_cancelled 决定
        alive = inc_cancelled or not l.is_cancelled

        # GMV：默认排除取消/退货
        if alive:
            m.gmv += l.gmv

        # 订单数：未取消 + 已付款（include_unpaid 打开就不看付款状态）
        paid_ok = inc_unpaid or l.is_paid or not switches.only_paid_orders
        if paid_ok and alive:
            m.orders += l.allocated_orders

        # 买家实付：只要求已付款（与原表一致，不看取消标记）
        if paid_ok:
            m.buyer_paid += alloc_paid(l)

        # 成本：未取消 + 已付款
        if paid_ok and alive:
            if l.cost_total_cny is None:
                missing_cost_lines += 1
            else:
                m.cost_local += l.cost_total_cny * params.cny_to_local

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
                      line_count=len(lines), order_count=len(order_ids), lines=lines)
