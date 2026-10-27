"""原始数据读取与标准化。

支持两种输入：
1. 主工作簿（V1-净利.xlsx）里的原始工作表 —— 用于历史数据与口径校验
2. 独立导出文件（订单/商品/广告/联盟各一份 xlsx 或 csv）—— 用于自动化产线

统一输出 OrderLine 列表与各类索引，后续模块不关心数据从哪来。
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from numbers import Number
from pathlib import Path
from typing import Iterable, Optional

import openpyxl

# ---------- 订单导出表的列约定（按平台导出模板的列字母，0 基） ----------
COL = {
    "order_id": 4,        # E  Order ID
    "status": 5,          # F  Order Status
    "substatus": 6,       # G  Order Substatus
    "cancel": 7,          # H  Cancelation/Return Type（非空 = 取消或退货）
    "sku_id": 9,          # J  SKU ID
    "seller_sku": 10,     # K  Seller SKU
    "product_name": 11,   # L  Product Name
    "variation": 12,      # M  Variation
    "qty": 13,            # N  Quantity
    "subtotal_before": 16,  # Q  SKU Subtotal Before Discount
    "seller_discount": 18,  # S  SKU Seller Discount
    "order_amount": 26,   # AA Order Amount（订单级，逐行重复）
    "refund_amount": 27,  # AB Order Refund Amount
    "created": 28,        # AC Created Time
    "paid": 29,           # AD Paid Time
}

MULTIPACK_RE = re.compile(r"^(?P<base>.+?)[-\s](?P<n>\d+)\s*[Pp][Cc][Ss]$")


def to_num(v, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    if isinstance(v, Number):
        return float(v)
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return default


def to_str(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # Excel 把 18 位 ID 存成浮点会带 .0 或丢精度，这里尽量还原成纯整数字符串
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


@dataclass
class SkuIndex:
    """SKU 到商品的映射。

    by_sku_id 优先：平台 SKU ID 全局唯一，一个 SKU ID 只对应一个商品。
    by_seller_sku 兜底：卖家自定义 SKU 可能被多个商品复用，需要消歧。
    """
    by_sku_id: dict[str, dict] = field(default_factory=dict)
    by_seller_sku: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    def __len__(self) -> int:
        return len(self.by_sku_id) + len(self.by_seller_sku)

    def lookup(self, sku_id: str, seller_sku: str, product_name: str) -> tuple[str, str]:
        """返回 (商品ID, 款式编码)。

        解析顺序：平台 SKU ID → 卖家 SKU → 多件装/组合装拆解后的基座 SKU。
        """
        hit = self.by_sku_id.get(sku_id) if sku_id else None
        if hit:
            return hit["product_id"], hit["style_code"]

        found = self._by_seller(seller_sku, product_name)
        if found:
            return found

        # 多件装：V26-C-21cm-2Pcs 继承基座 V26-C-21cm 的商品归属
        m = MULTIPACK_RE.match(seller_sku or "")
        if m:
            found = self._by_seller(m.group("base").strip(), product_name)
            if found:
                return found

        # 组合装：任一组件能定位就用它（组合装通常挂在主商品下）
        if "+" in (seller_sku or ""):
            for part in (p.strip() for p in seller_sku.split("+")):
                m = MULTIPACK_RE.match(part)
                base = m.group("base").strip() if m else part
                found = self._by_seller(base, product_name)
                if found:
                    return found
        return "", ""

    def _by_seller(self, seller_sku: str, product_name: str) -> tuple[str, str]:
        cands = self.by_seller_sku.get(seller_sku) or []
        if not cands:
            return "", ""
        if len(cands) == 1:
            return cands[0]["product_id"], cands[0]["style_code"]
        norm = re.sub(r"\s+", "", product_name or "").lower()
        if norm:
            for c in cands:
                if re.sub(r"\s+", "", c["product_name"]).lower() == norm:
                    return c["product_id"], c["style_code"]
        return cands[0]["product_id"], cands[0]["style_code"]


@dataclass
class OrderLine:
    order_id: str
    seller_sku: str
    sku_id: str
    product_name: str
    qty: float
    subtotal_before: float
    seller_discount: float
    order_amount: float
    cancel: str
    status: str
    created: str
    paid: str
    product_id: str = ""
    style_codes: str = ""
    cost_unit_cny: Optional[float] = None
    order_line_count: int = 1

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancel and self.cancel.strip())

    @property
    def is_paid(self) -> bool:
        return self.order_amount > 0

    @property
    def gmv(self) -> float:
        """GMV 口径：折扣前小计 − 卖家折扣（不含平台补贴）。"""
        return self.subtotal_before - self.seller_discount

    @property
    def allocated_orders(self) -> float:
        """分配到的订单数：订单级计数按行数平分，与人工表口径一致。"""
        return 1.0 / self.order_line_count if self.order_line_count else 0.0

    @property
    def allocated_paid(self) -> float:
        """分配到的买家实付：订单实付按行数平分，与人工表口径一致。"""
        return self.order_amount / self.order_line_count if self.order_line_count else 0.0

    @property
    def cost_total_cny(self) -> Optional[float]:
        if self.cost_unit_cny is None:
            return None
        return self.cost_unit_cny * self.qty


@dataclass
class RawData:
    orders: list[OrderLine]
    costs: dict[str, float]
    sku_index: SkuIndex
    ads: dict[str, float]          # product_id -> 广告花费（美金）
    commissions: dict[str, float]  # product_id -> 达人佣金（本地货币）
    issues: list[str]


# ---------------- 通用小工具 ----------------

def _read_sheet(path: Path, sheet: str):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if sheet not in wb.sheetnames:
        wb.close()
        return None
    ws = wb[sheet]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def _norm_header(s) -> str:
    return re.sub(r"\s+", "", to_str(s)).lower()


def _find_col(header_row: Iterable, candidates: Iterable[str]) -> Optional[int]:
    norm = [_norm_header(c) for c in header_row]
    for want in candidates:
        w = _norm_header(want)
        for i, h in enumerate(norm):
            if h == w:
                return i
    for want in candidates:
        w = _norm_header(want)
        for i, h in enumerate(norm):
            if w and w in h:
                return i
    return None


# ---------------- 各类数据解析 ----------------

def parse_costs(rows) -> dict[str, float]:
    """成本表：第 1 列编码，第 2 列成本（人民币）。"""
    out: dict[str, float] = {}
    for r in rows:
        if len(r) < 2:
            continue
        code, val = to_str(r[0]), r[1]
        if not code or val is None:
            continue
        try:
            out[code] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def parse_sku_index(rows) -> SkuIndex:
    """链接资料：款式编码 / 商品ID / 商品名 / SKU ID / Seller SKU。

    表头在第 5 行、数据从第 6 行开始（前 4 行是平台模板说明）。
    这里按列名定位，模板行数变了也能认。
    """
    idx = SkuIndex()
    if not rows:
        return idx
    header_idx = 0
    for i, r in enumerate(rows[:10]):
        if _find_col(r, ["product_id"]) is not None:
            header_idx = i
            break
    header = rows[header_idx]
    c_prod = _find_col(header, ["product_id"])
    c_sku = _find_col(header, ["seller_sku"])
    c_skuid = _find_col(header, ["sku_id"])
    c_name = _find_col(header, ["product_name"])
    c_style = 0  # 第 1 列固定是卖家自己的款式编码
    if c_prod is None or c_sku is None:
        return idx

    for r in rows[header_idx + 1:]:
        if len(r) <= max(c_prod, c_sku):
            continue
        sku = to_str(r[c_sku])
        pid = to_str(r[c_prod])
        if not sku or not pid:
            continue
        item = {
            "product_id": pid,
            "style_code": to_str(r[c_style]) if c_style < len(r) else "",
            "product_name": to_str(r[c_name]) if c_name is not None and c_name < len(r) else "",
        }
        sku_id = to_str(r[c_skuid]) if c_skuid is not None and c_skuid < len(r) else ""
        if sku_id:
            idx.by_sku_id[sku_id] = item
        bucket = idx.by_seller_sku[sku]
        if item not in bucket:
            bucket.append(item)
    return idx


def parse_ads(rows) -> dict[str, float]:
    """广告报表：按 商品ID 汇总花费（美金）。"""
    if not rows:
        return {}
    header = rows[0]
    c_prod = _find_col(header, ["商品 ID", "商品ID", "Product ID"])
    c_cost = _find_col(header, ["成本", "Cost", "Spend"])
    if c_prod is None or c_cost is None:
        return {}
    out: dict[str, float] = defaultdict(float)
    for r in rows[1:]:
        if len(r) <= max(c_prod, c_cost):
            continue
        pid = to_str(r[c_prod])
        if pid:
            out[pid] += to_num(r[c_cost])
    return dict(out)


def parse_commissions(rows) -> dict[str, float]:
    """联盟佣金报表：按 商品ID 汇总预估佣金（本地货币）。"""
    if not rows:
        return {}
    header = rows[0]
    c_prod = _find_col(header, ["Product ID", "商品ID", "商品 ID"])
    c_com = _find_col(header, ["Est. commission", "预估佣金", "Commission"])
    if c_prod is None or c_com is None:
        return {}
    out: dict[str, float] = defaultdict(float)
    for r in rows[1:]:
        if len(r) <= max(c_prod, c_com):
            continue
        pid = to_str(r[c_prod])
        if pid:
            out[pid] += to_num(r[c_com])
    return dict(out)


def parse_orders(rows, issues: list[str]) -> list[OrderLine]:
    """订单表：表头在第 1 行，第 2 行是列说明，数据从第 3 行开始。"""
    if not rows:
        return []
    lines: list[OrderLine] = []
    for r in rows[2:]:
        if len(r) <= COL["order_amount"]:
            r = tuple(r) + (None,) * (COL["order_amount"] + 1 - len(r))
        sku = to_str(r[COL["seller_sku"]])
        if not sku:
            continue
        lines.append(
            OrderLine(
                order_id=to_str(r[COL["order_id"]]),
                seller_sku=sku,
                sku_id=to_str(r[COL["sku_id"]]),
                product_name=to_str(r[COL["product_name"]]),
                qty=to_num(r[COL["qty"]], 1.0) or 1.0,
                subtotal_before=to_num(r[COL["subtotal_before"]]),
                seller_discount=to_num(r[COL["seller_discount"]]),
                order_amount=to_num(r[COL["order_amount"]]),
                cancel=to_str(r[COL["cancel"]]),
                status=to_str(r[COL["status"]]),
                created=to_str(r[COL["created"]]),
                paid=to_str(r[COL["paid"]]),
            )
        )

    # 同一订单的行数 → 用于订单数/实付的平分
    per_order = Counter(l.order_id for l in lines)
    for l in lines:
        l.order_line_count = per_order.get(l.order_id, 1) or 1
    return lines


# ---------------- 商品ID 解析 ----------------

def attach_product_ids(lines: list[OrderLine], sku_index: SkuIndex, issues: list[str]) -> None:
    unresolved = Counter()
    styles_by_product: dict[str, set[str]] = defaultdict(set)
    for l in lines:
        pid, style = sku_index.lookup(l.sku_id, l.seller_sku, l.product_name)
        l.product_id, l.style_codes = pid, style
        if not pid:
            unresolved[l.seller_sku] += 1
        elif style:
            styles_by_product[pid].add(style)
    for sku, n in unresolved.most_common():
        issues.append(f"SKU「{sku}」在链接资料里找不到商品ID（{n} 行），已归入「未识别商品」")
    # 一个商品对应多个款式编码时，用 "/" 连接（与原表 TEXTJOIN("/") 一致）
    for l in lines:
        if l.product_id and not l.style_codes:
            l.style_codes = "/".join(sorted(styles_by_product.get(l.product_id, [])))


# ---------------- 成本解析 ----------------

def resolve_cost(sku: str, costs: dict[str, float]) -> tuple[Optional[float], str]:
    """返回 (成本单价, 命中方式)。命中方式：exact / multipack / combo / miss。"""
    if sku in costs:
        return costs[sku], "exact"

    # 多件装：V26-C-21cm-2Pcs = V26-C-21cm 单价 × 2
    m = MULTIPACK_RE.match(sku)
    if m:
        base, n = m.group("base"), int(m.group("n"))
        if base in costs:
            return costs[base] * n, "multipack"

    # 组合装：A+B = 各自单价相加
    if "+" in sku:
        parts = [p.strip() for p in sku.split("+") if p.strip()]
        if parts and all(p in costs for p in parts):
            return sum(costs[p] for p in parts), "combo"
        # 允许组内也是多件装
        vals = []
        for p in parts:
            c, how = resolve_cost(p, costs)
            if c is None:
                vals = []
                break
            vals.append(c)
        if vals:
            return sum(vals), "combo"

    return None, "miss"


def attach_costs(lines: list[OrderLine], costs: dict[str, float], issues: list[str]) -> None:
    missing = Counter()
    derived: Counter = Counter()
    for l in lines:
        unit, how = resolve_cost(l.seller_sku, costs)
        l.cost_unit_cny = unit
        if unit is None:
            missing[l.seller_sku] += 1
        elif how != "exact":
            derived[f"{l.seller_sku}（{how}）"] += 1
    for sku, n in missing.most_common():
        issues.append(f"SKU「{sku}」在成本表里查不到，且无法推导（{n} 行），成本按 0 计，净利会偏高")
    for sku, n in derived.most_common():
        issues.append(f"SKU「{sku}」成本由规则推导（{n} 行），建议补录到成本表")


# ---------------- 主入口 ----------------

def load_from_workbook(path: Path) -> RawData:
    """从主工作簿读取全部原始表。"""
    path = Path(path)
    issues: list[str] = []

    cost_rows = _read_sheet(path, "成本") or []
    link_rows = _read_sheet(path, "链接资料") or []
    order_rows = _read_sheet(path, "订单") or []
    ad_rows = _read_sheet(path, "广告") or []
    com_rows = _read_sheet(path, "联盟佣金") or []

    costs = parse_costs(cost_rows)
    sku_index = parse_sku_index(link_rows)
    orders = parse_orders(order_rows, issues)
    ads = parse_ads(ad_rows)
    commissions = parse_commissions(com_rows)

    if not orders:
        issues.append("订单表为空，请确认工作表名称与列结构")
    attach_product_ids(orders, sku_index, issues)
    attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index,
                   ads=ads, commissions=commissions, issues=issues)


def _read_table_file(path: Path) -> list[tuple]:
    """读独立导出文件（xlsx 取第一个工作表，csv 按 utf-8-sig）。"""
    path = Path(path)
    if path.suffix.lower() in (".csv", ".txt"):
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with path.open("r", encoding=enc, newline="") as f:
                    return [tuple(r) for r in csv.reader(f)]
            except UnicodeDecodeError:
                continue
        return []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def load_from_dir(folder: Path, costs_file: Optional[Path] = None) -> RawData:
    """从导出目录读取：按文件名关键字识别四类文件。

    约定文件名包含：订单 / order、商品|链接 / product|sku、广告 / ad、联盟|佣金 / affiliate
    """
    folder = Path(folder)
    issues: list[str] = []
    found: dict[str, Optional[Path]] = {"order": None, "product": None, "ad": None, "affiliate": None}

    rules = [
        ("order", ("订单", "order")),
        ("product", ("商品", "链接", "product", "sku")),
        ("ad", ("广告", "ad_", "ads", "advert")),
        ("affiliate", ("联盟", "佣金", "affiliate", "commission")),
    ]
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".xlsx", ".xls", ".csv", ".txt"):
            continue
        name = f.name.lower()
        for key, kws in rules:
            if found[key] is None and any(k.lower() in name for k in kws):
                found[key] = f
                break

    for key, label in (("order", "订单"), ("product", "商品/链接资料"), ("ad", "广告"), ("affiliate", "联盟佣金")):
        if found[key] is None:
            issues.append(f"未找到{label}导出文件，相关字段按 0 计")

    order_rows = _read_table_file(found["order"]) if found["order"] else []
    link_rows = _read_table_file(found["product"]) if found["product"] else []
    ad_rows = _read_table_file(found["ad"]) if found["ad"] else []
    com_rows = _read_table_file(found["affiliate"]) if found["affiliate"] else []

    costs = {}
    if costs_file and Path(costs_file).exists():
        costs = parse_costs(_read_table_file(Path(costs_file)))
    elif costs_file:
        issues.append(f"成本文件不存在：{costs_file}")

    sku_index = parse_sku_index(link_rows)
    orders = parse_orders(order_rows, issues)
    attach_product_ids(orders, sku_index, issues)
    attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index,
                   ads=parse_ads(ad_rows), commissions=parse_commissions(com_rows),
                   issues=issues)
