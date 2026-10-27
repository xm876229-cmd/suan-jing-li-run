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


@dataclass
class Switches:
    super_coupon_enabled: bool = False
    only_paid_orders: bool = True
    # 订单实付在订单内各行的分配方式：
    #   equal        按行数平分（与原工作簿人工列一致，校验可得 100% 命中）
    #   proportional 按各行 GMV 占比分配（经济含义更准，但与人工表会有细微差异）
    allocation_paid: str = "equal"


def load_rates(path: Path) -> tuple[dict[str, Params], Switches]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, Params] = {}
    for name, c in data.get("countries", {}).items():
        out[name] = Params(
            country=name,
            cny_to_local=float(c["cny_to_local"]),
            usd_to_local=float(c["usd_to_local"]),
            dispatch_fee_local=float(c["dispatch_fee_local"]),
            platform_commission=float(c["platform_commission"]),
            tx_fee=float(c["tx_fee"]),
            cashback=float(c["cashback"]),
            super_coupon=float(c["super_coupon"]),
            growth_fee=float(c["growth_fee"]),
            logistics=float(c["logistics"]),
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
    cost_local: float = ZERO
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
        for p in self.products:
            t.gmv += p.gmv
            t.buyer_paid += p.buyer_paid
            t.orders += p.orders
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
        return t

    @property
    def net_cny_total(self) -> float:
        return self.total.net_cny(self.params.cny_to_local)


# ---------------- 主计算 ----------------

def compute(data: RawData, params: Params, switches: Switches,
            dfrom: Optional[date] = None, dto: Optional[date] = None,
            date_field: str = "created") -> CalcResult:
    issues = list(data.issues)
    lines = filter_by_date(data.orders, dfrom, dto, date_field)

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

        # GMV：排除取消/退货
        if not l.is_cancelled:
            m.gmv += l.gmv

        # 订单数：未取消 + 已付款
        paid_ok = l.is_paid or not switches.only_paid_orders
        if paid_ok and not l.is_cancelled:
            m.orders += l.allocated_orders

        # 买家实付：只要求已付款（与原表一致，不看取消标记）
        if paid_ok:
            m.buyer_paid += alloc_paid(l)

        # 成本：未取消 + 已付款
        if paid_ok and not l.is_cancelled:
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

    # 费用项
    for pid, m in buckets.items():
        m.product_name = names.get(pid, "")
        m.cost_local = m.cost_local  # 已是本地货币
        m.dispatch_fee = m.orders * params.dispatch_fee_local
        m.platform_commission = m.gmv * params.platform_commission
        m.tx_fee = m.buyer_paid * params.tx_fee
        m.cashback = m.gmv * params.cashback
        m.super_coupon = m.gmv * params.super_coupon if switches.super_coupon_enabled else ZERO
        m.growth_fee = m.gmv * params.growth_fee
        m.logistics = m.gmv * params.logistics
        m.creator_commission = data.commissions.get(pid, ZERO)
        # data.ads 在解析层已按「货币」列换算成本地货币，这里直接用，不能再乘汇率
        m.ad_spend = data.ads.get(pid, ZERO)

    products = sorted(buckets.values(), key=lambda x: x.gmv, reverse=True)

    if no_pid:
        issues.append(f"{no_pid} 行订单无法确定商品ID，已归入「未识别商品」，请检查链接资料是否缺 SKU")
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
