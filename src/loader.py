"""原始数据读取与标准化。

设计原则：**按表头名定位，不按列号**。
平台导出模板会变、人工会在前面插列，写死列号必然错位；只有表头名相对稳定。

支持三类输入：
    1. 独立导出文件（订单 / 广告 / 联盟佣金 / SKU 映射），自动化产线与手工导出都走这条
    2. 一个装满导出文件的目录，按文件名关键字自动识别
    3. 主工作簿（旧版，前面插了人工列），用于历史数据与口径校验

统一输出 OrderLine 列表 + SKU 映射索引 + 成本表，后续模块不关心数据从哪来。
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from numbers import Number
from pathlib import Path
from typing import Iterable, Optional

import openpyxl


# ---------------- 基础转换 ----------------

_CURRENCY_RE = re.compile(r"[^\d.\-,]")


def to_num(v, default: float = 0.0) -> float:
    """宽容解析数字：处理 'RM1,498.68'、'1 234.5'、'12.3%'、空值、None。"""
    if v is None or v == "":
        return default
    if isinstance(v, Number):
        return float(v)
    s = str(v).strip()
    if not s:
        return default
    neg = s.startswith("(") and s.endswith(")")
    s = _CURRENCY_RE.sub("", s)
    if s in ("", "-", ".", ","):
        return default
    try:
        val = float(s.replace(",", ""))
    except ValueError:
        return default
    return -val if neg else val


def to_str(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def norm_header(s) -> str:
    """表头归一：去掉空白、下划线、连字符、全角转半角、小写，便于稳定匹配。

    必须去掉下划线：主表用的列名是 sku_id / product_id，
    而别名表写的是 skuid / productid，不去掉就永远匹配不上
    （表现为主表 235 条一条都读不出来）。
    """
    s = to_str(s)
    s = s.replace("\u3000", " ")
    s = s.translate(str.maketrans("　（）／", " ()/"))
    return re.sub(r"[\s_\-]+", "", s).lower()


# ---------------- 列映射配置 ----------------

# 内置默认：config/mapping.json 缺失或坏了时的兜底。
# 正常情况下用的是配置文件——那里的候选标题可以随便加。
ALIASES: dict[str, list[str]] = {
    "order_id": ["orderid", "订单号", "订单id", "平台订单号"],
    "status": ["orderstatus", "订单状态"],
    "substatus": ["ordersubstatus", "订单子状态"],
    "cancel": ["cancelation/returntype", "cancellation/returntype", "取消/退货类型", "取消退货类型"],
    "sku_id": ["skuid", "sku id"],
    "seller_sku": ["sellersku", "卖家sku", "商家sku"],
    "product_id": ["productid", "商品id", "商品id"],
    "product_name": ["productname", "商品名称", "商品名", "商品"],
    "variation": ["variation", "规格"],
    "qty": ["quantity", "数量", "购买数量"],
    "subtotal_before": ["skusubtotalbeforediscount", "折扣前小计", "sku折扣前小计"],
    "seller_discount": ["skusellerdiscount", "卖家折扣", "sku卖家折扣"],
    "order_amount": ["orderamount", "订单金额", "买家实付"],
    "refund_amount": ["orderrefundamount", "退款金额"],
    "created": ["createdtime", "创建时间", "下单时间"],
    "paid": ["paidtime", "付款时间", "支付时间"],
    # 广告
    "ad_cost": ["成本", "cost", "spend", "花费", "广告花费"],
    "ad_currency": ["货币", "currency"],
    # 联盟佣金
    "commission": ["est.commission", "estcommission", "预估佣金", "佣金"],
    "commission_refund": ["refunds", "退款"],
    # 统计表 / 映射表
    "style_code": ["款式编码", "款式", "stylecode"],
}


MAPPING_FILE = Path(__file__).resolve().parents[1] / "config" / "mapping.json"
_ALIASES_CACHE: dict = {"stamp": None, "data": None}


def get_aliases() -> dict[str, list[str]]:
    """读 config/mapping.json 里的列映射。

    每个逻辑字段可以配**任意多个**候选标题，按顺序取第一个能找到的：
        第一个标题找不到 → 试第二个 → 一直试到找到为止。
    所以平台改列名时，在配置里加一条候选就行，不用改代码。

    文件缺失或格式坏了就退回内置的 ALIASES（保证还能跑）。
    按 mtime 缓存，改完配置不用重启程序。
    """
    try:
        st = MAPPING_FILE.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return ALIASES
    if _ALIASES_CACHE["stamp"] == stamp:
        return _ALIASES_CACHE["data"]

    try:
        doc = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ALIASES

    out: dict[str, list[str]] = {}
    for key, v in (doc.get("fields") or {}).items():
        if isinstance(v, dict):
            cands = v.get("candidates") or v.get("names") or []
        elif isinstance(v, list):
            cands = v
        else:
            continue
        # 去重但保持顺序（第一个优先级最高）
        seen: set = set()
        clean: list[str] = []
        for c in cands:
            c = str(c).strip()
            if not c:
                continue
            k = norm_header(c)
            if k in seen:
                continue
            seen.add(k)
            clean.append(c)
        if clean:
            out[key] = clean

    if not out:
        return ALIASES
    _ALIASES_CACHE["stamp"] = stamp
    _ALIASES_CACHE["data"] = out
    return out


def build_colmap(header_row: Iterable) -> dict[str, int]:
    """把表头行映射成 {逻辑字段: 列号}。

    每个逻辑字段按候选顺序逐个试：第一个找不到就试下一个，
    直到找到为止（所以可以配很多个标题防表格改版）。
    """
    norm = [norm_header(c) for c in header_row]
    colmap: dict[str, int] = {}
    for key, names in get_aliases().items():
        for want in names:
            w = norm_header(want)
            hit = next((i for i, h in enumerate(norm) if h == w), None)
            if hit is None:
                hit = next((i for i, h in enumerate(norm) if w and w in h), None)
            if hit is not None and key not in colmap:
                colmap[key] = hit
                break
    return colmap


def find_header_row(rows: list[tuple], required: tuple[str, ...], max_scan: int = 8) -> tuple[int, dict[str, int]]:
    """在前若干行里找最像表头的那一行：包含 required 字段最多者。

    平台导出常见套路：第 1 行英文表头 + 第 2 行英文说明；或第 1 行标题、第 3 行才是表头。
    """
    best = (-1, -1, {})
    for i, row in enumerate(rows[:max_scan]):
        cm = build_colmap(row)
        score = sum(1 for k in required if k in cm)
        if score > best[0] or (score == best[0] and len(cm) > len(best[2])):
            best = (score, i, cm)
    score, idx, cm = best
    # 至少命中 2 个（或全部，若要求少于 2 个）才算找到表头
    if score < min(2, len(required)):
        return -1, {}
    return idx, cm


# ---------------- 数据结构 ----------------

@dataclass
class SkuIndex:
    """SKU → 商品 的映射。

    by_sku_id 优先：平台 SKU ID 全局唯一。
    by_seller_sku 兜底：卖家自定义 SKU 可能被多个商品复用，需按商品名消歧。
    """
    by_sku_id: dict[str, dict] = field(default_factory=dict)
    by_seller_sku: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    # 卖家编码可能被多条链接共用（同一编码出现在多个商品下），
    # 这种编码无法靠编码本身定位商品，记下来供报表提示。
    ambiguous: set[str] = field(default_factory=set)
    # 同一个 SKU ID 指向不同商品：映射表脏数据，需要人工确认
    conflicts: set[str] = field(default_factory=set)

    def __len__(self) -> int:
        return len(self.by_sku_id) + len(self.by_seller_sku)

    def merge(self, other: "SkuIndex") -> "SkuIndex":
        """把 other 里本索引没有的条目补进来（self 优先）。

        用途：累积主表 + 本次导出目录里的映射表合并使用。
        主表保证老 SKU 不丢，目录表保证本次抓取完整——任一方缺失都不会算错。
        """
        for sid, item in other.by_sku_id.items():
            self.by_sku_id.setdefault(sid, item)
        for sku, items in other.by_seller_sku.items():
            bucket = self.by_seller_sku[sku]
            for it in items:
                if it not in bucket:
                    bucket.append(it)
        return self

    def lookup(self, sku_id: str, seller_sku: str, product_name: str) -> tuple[str, str]:
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

        # 组合装：任一组件能定位就用它
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
        # 走到这里说明：卖家编码相同，但对应了多个商品。
        # 业务上卖家编码是可以复用的（同一编码挂在多条链接下常见），
        # 只有 SKU ID 才是唯一的。所以这种情况绝不能默认取第一个——
        # 取错会把这条链接的 GMV 与全部费用记到另一条链接上，报表表面还正常。
        # 只有商品名能唯一确定时才用，否则留空（由报表归入「无ID」）。
        norm = re.sub(r"\s+", "", product_name or "").lower()
        if norm:
            hits = [c for c in cands
                    if re.sub(r"\s+", "", c["product_name"]).lower() == norm]
            if len(hits) == 1:
                return hits[0]["product_id"], hits[0]["style_code"]
        self.ambiguous.add(seller_sku)
        return "", ""


MULTIPACK_RE = re.compile(r"^(?P<base>.+?)[-\s](?P<n>\d+)\s*[Pp][Cc][Ss]$")


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
        """这一行是否算作有效订单（影响订单数、成本、买家实付）。

        不能只看金额。赠品、换货补发、纯运费单这类 0 元单金额为 0，
        但已经发货，同样会产生发单费（按单收）和成本。
        只按金额判断会把它们整行排除，店铺总净利因此偏高。

        所以以状态为准：只要进入履约流程就算有效，明确失效的才排除。
        """
        if self.order_amount > 0:
            return True

        s = (self.status or "").strip()
        if not s:
            return False          # 状态缺失时保守处理，不擅自计入
        dead = ("未支付", "待付款", "已取消", "已关闭", "已退款", "取消", "失败")
        return not any(k in s for k in dead)

    @property
    def gmv(self) -> float:
        return self.subtotal_before - self.seller_discount

    @property
    def allocated_orders(self) -> float:
        return 1.0 / self.order_line_count if self.order_line_count else 0.0

    @property
    def allocated_paid(self) -> float:
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
    ads: dict[str, float]      # product_id -> 广告花费（已换算成本地货币）
    commissions: dict[str, float]
    issues: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


# ---------------- 文件读取 ----------------

def read_table(path: Path, sheet: Optional[str] = None) -> list[tuple]:
    path = Path(path)
    if path.suffix.lower() in (".csv", ".txt"):
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with path.open("r", encoding=enc, newline="") as f:
                    return [tuple(r) for r in csv.reader(f)]
            except UnicodeDecodeError:
                continue
        return []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


# ---------------- 各类数据解析 ----------------

def parse_costs(rows: list[tuple]) -> dict[str, float]:
    """成本表：编码 → 成本（人民币）。表头可有可无。"""
    out: dict[str, float] = {}
    start = 0
    if rows:
        cm = build_colmap(rows[0])
        if "seller_sku" in cm or "product_id" in cm:
            start = 1
            sku_col = cm.get("seller_sku", 0)
            cost_col = next((c for c in range(len(rows[0])) if c != sku_col), 1)
        else:
            sku_col, cost_col = 0, 1
    else:
        sku_col, cost_col = 0, 1

    for r in rows[start:]:
        if len(r) <= max(sku_col, cost_col):
            continue
        code = to_str(r[sku_col])
        if not code:
            continue
        val = r[cost_col]
        if val is None or to_str(val) == "":
            continue
        # 表头行（成本列写的是「成本」「cost」这类文字）不能当数据
        if isinstance(val, str) and not any(ch.isdigit() for ch in val):
            continue
        out[code] = to_num(val)
    return out


def parse_mapping(rows: list[tuple]) -> SkuIndex:
    """解析 SKU→商品 映射表。

    兼容两种形态：
      · 链接资料模板：款式编码 | product_id | ... | sku_id | ... | seller_sku
      · SKU 统计表  ：SKU ID | Product ID | 商品 | GMV | 订单数 | 件数（表头可能在第 3 行）
    """
    idx = SkuIndex()
    hidx, cm = find_header_row(rows, ("sku_id", "product_id"))
    if hidx < 0:
        return idx
    c_prod = cm.get("product_id")
    if c_prod is None:
        return idx
    c_skuid = cm.get("sku_id")
    c_sku = cm.get("seller_sku")
    c_name = cm.get("product_name")
    c_style = cm.get("style_code")

    for r in rows[hidx + 1:]:
        if len(r) <= c_prod:
            continue
        pid = to_str(r[c_prod])
        if not pid:
            continue
        item = {
            "product_id": pid,
            "style_code": to_str(r[c_style]) if c_style is not None and c_style < len(r) else "",
            "product_name": to_str(r[c_name]) if c_name is not None and c_name < len(r) else "",
        }
        sid = to_str(r[c_skuid]) if c_skuid is not None and c_skuid < len(r) else ""
        if sid:
            old = idx.by_sku_id.get(sid)
            if old is None:
                idx.by_sku_id[sid] = item
            elif old["product_id"] != item["product_id"]:
                # SKU ID 是平台唯一的，同一个 ID 指向两个商品只可能是映射表本身脏了
                # （比如两次导出的数据拼在一起）。留着第一个，但记下来。
                idx.conflicts.add(sid)
        if c_sku is not None and c_sku < len(r):
            sku = to_str(r[c_sku])
            if sku:
                bucket = idx.by_seller_sku[sku]
                if item not in bucket:
                    bucket.append(item)
    return idx


def _norm_key(k) -> str:
    """键名归一化：product_id / productId / Product ID 都变成 productid。"""
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def parse_mapping_json(path: Path) -> SkuIndex:
    """解析扩展直接抓下来的商品 SKU JSON（sku_list_*.json）。

    平台这个接口返回的 item 结构层级较深（items[].meta.xxx），而且字段名
    随版本会变，所以不写死路径：把每个 item 递归摊平，再按键名归一化匹配。
    """
    idx = SkuIndex()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return idx

    items = (raw.get("data") or {}).get("items") or raw.get("items") or []

    # 商品管理接口 /api/v1/product/stock/sku/list 返回的是 {"skus":[...]}，
    # 字段名是 sku_id / product_id / seller_sku / product_title，一样要收。
    if not items:
        items = raw.get("skus") or (raw.get("data") or {}).get("skus") or []

    for it in items:
        if not isinstance(it, dict):
            continue
        flat: dict[str, str] = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, (dict, list)):
                        walk(v)
                    elif isinstance(v, (str, int)) and not isinstance(v, bool):
                        flat.setdefault(_norm_key(k), str(v))
            elif isinstance(o, list):
                for v in o[:3]:      # 变体数组只取前几个，避免同 item 反复覆盖
                    walk(v)

        walk(it)

        pid = flat.get("productid", "")
        if not pid:
            continue
        item = {
            "product_id": pid,
            "style_code": flat.get("stylecode", "") or flat.get("sellerstylecode", ""),
            "product_name": flat.get("productname", "") or flat.get("producttitle", ""),
        }
        sid = flat.get("skuid", "")
        if sid:
            old = idx.by_sku_id.get(sid)
            if old is None:
                idx.by_sku_id[sid] = item
            elif old["product_id"] != item["product_id"]:
                idx.conflicts.add(sid)
        ssk = flat.get("sellersku", "") or flat.get("skuname", "") or flat.get("sku", "")
        if ssk:
            bucket = idx.by_seller_sku[ssk]
            if item not in bucket:
                bucket.append(item)
    return idx


def parse_orders(rows: list[tuple]) -> list[OrderLine]:
    hidx, cm = find_header_row(rows, ("order_id", "sku_id", "seller_sku", "qty"))
    if hidx < 0:
        return []
    need = ("order_id", "sku_id", "seller_sku", "qty", "subtotal_before")
    if sum(1 for k in need if k in cm) < 3:
        return []

    def get(r, key, default=None):
        i = cm.get(key)
        if i is None or i >= len(r):
            return default
        return r[i]

    # 旧版工作簿在订单表最左边插了一列人工填的「商品ID」，表头不叫 Product ID，
    # 而是「这列常态保留 勿删和覆盖」。这里把它认出来，直接采信。
    legacy_pid_col: Optional[int] = None
    if "product_id" not in cm:
        norm_hdr = [norm_header(c) for c in rows[hidx]]
        for i, h in enumerate(norm_hdr):
            if "常态保留" in h:
                legacy_pid_col = i
                break

    def legacy_pid(r) -> str:
        if legacy_pid_col is None or legacy_pid_col >= len(r):
            return ""
        raw = to_str(r[legacy_pid_col])
        return raw if re.fullmatch(r"\d{6,}", raw) else ""  # 排除「无ID」这类手填占位

    lines: list[OrderLine] = []
    for r in rows[hidx + 1:]:
        sku = to_str(get(r, "seller_sku", ""))
        sku_id = to_str(get(r, "sku_id", ""))
        if not sku and not sku_id:
            continue  # 说明行 / 空行

        oid = to_str(get(r, "order_id", ""))
        qty = to_num(get(r, "qty"), 1.0) or 1.0
        before = to_num(get(r, "subtotal_before"))
        amount = to_num(get(r, "order_amount"))

        # 平台导出第 2 行是英文说明（"Platform unique order ID." 之类），
        # 它同样落在 SKU 列上，必须排掉：既没有数字订单号，也没有任何金额
        if not re.search(r"\d{6,}", oid) and qty <= 1 and before == 0 and amount == 0:
            continue

        lines.append(
            OrderLine(
                order_id=oid,
                seller_sku=sku,
                sku_id=sku_id,
                product_name=to_str(get(r, "product_name", "")),
                qty=qty,
                subtotal_before=before,
                seller_discount=to_num(get(r, "seller_discount")),
                order_amount=amount,
                cancel=to_str(get(r, "cancel", "")),
                status=to_str(get(r, "status", "")),
                created=to_str(get(r, "created", "")),
                paid=to_str(get(r, "paid", "")),
                product_id=to_str(get(r, "product_id", "")) or legacy_pid(r),
            )
        )

    per_order = Counter(l.order_id for l in lines)
    for l in lines:
        l.order_line_count = per_order.get(l.order_id, 1) or 1
    return lines


def parse_ads(rows: list[tuple], usd_to_local: float = 1.0) -> tuple[dict[str, float], list[str]]:
    """广告报表：按 商品ID 汇总花费，并按货币换算成本地货币。"""
    issues: list[str] = []
    hidx, cm = find_header_row(rows, ("product_id", "ad_cost"))
    if hidx < 0 or "product_id" not in cm or "ad_cost" not in cm:
        return {}, ["广告表未识别出「商品 ID」与「成本」列，广告费按 0 计"]

    out: dict[str, float] = defaultdict(float)
    currencies: Counter = Counter()
    unknown = 0.0
    for r in rows[hidx + 1:]:
        pid = to_str(r[cm["product_id"]]) if cm["product_id"] < len(r) else ""
        if not pid:
            continue
        cost = to_num(r[cm["ad_cost"]]) if cm["ad_cost"] < len(r) else 0.0
        cur = to_str(r[cm["ad_currency"]]).upper() if cm.get("ad_currency") is not None and cm["ad_currency"] < len(r) else ""
        currencies[cur or "未标注"] += 1
        if cur in ("", "USD", "US$", "$"):
            out[pid] += cost * usd_to_local
        elif cur in ("MYR", "RM"):
            out[pid] += cost          # 已是本地货币
        else:
            unknown += cost
    if unknown:
        issues.append(f"广告表有 {unknown:.2f} 金额的货币未识别，已按原值计入")
    detail = "、".join(f"{k}×{v}" for k, v in currencies.most_common(3))
    if detail:
        issues.append(f"广告表货币分布：{detail}（已按美金→本地汇率换算）")
    return dict(out), issues


def parse_commissions(rows: list[tuple]) -> tuple[dict[str, float], list[str]]:
    """联盟佣金报表：按 商品ID 汇总预估佣金。值可能是 'RM2,678.75' 这类字符串。"""
    hidx, cm = find_header_row(rows, ("product_id", "commission"))
    if hidx < 0 or "product_id" not in cm or "commission" not in cm:
        return {}, ["联盟佣金表未识别出「Product ID」与「Est. commission」列，达人佣金按 0 计"]

    out: dict[str, float] = defaultdict(float)
    for r in rows[hidx + 1:]:
        pid = to_str(r[cm["product_id"]]) if cm["product_id"] < len(r) else ""
        if not pid:
            continue
        out[pid] += to_num(r[cm["commission"]])
    return dict(out), []


# ---------------- 成本解析 ----------------

def resolve_cost(sku: str, costs: dict[str, float]) -> tuple[Optional[float], str]:
    """返回 (成本单价, 命中方式)。方式：exact / multipack / combo / miss。"""
    if sku in costs:
        return costs[sku], "exact"

    m = MULTIPACK_RE.match(sku)
    if m:
        base, n = m.group("base"), int(m.group("n"))
        if base in costs:
            return costs[base] * n, "multipack"

    if "+" in sku:
        parts = [p.strip() for p in sku.split("+") if p.strip()]
        if parts and all(p in costs for p in parts):
            return sum(costs[p] for p in parts), "combo"
        vals = []
        for p in parts:
            c, _ = resolve_cost(p, costs)
            if c is None:
                vals = []
                break
            vals.append(c)
        if vals:
            return sum(vals), "combo"

    return None, "miss"


def attach_product_ids(lines: list[OrderLine], sku_index: SkuIndex, issues: list[str]) -> None:
    unresolved = Counter()
    ambiguous_rows = 0
    styles_by_product: dict[str, set[str]] = defaultdict(set)
    for l in lines:
        if l.product_id:
            pid, style = l.product_id, sku_index.by_sku_id.get(l.sku_id, {}).get("style_code", "")
        else:
            pid, style = sku_index.lookup(l.sku_id, l.seller_sku, l.product_name)
        l.product_id, l.style_codes = pid, style
        if not pid:
            if l.seller_sku and l.seller_sku in sku_index.ambiguous:
                ambiguous_rows += 1
            else:
                unresolved[l.seller_sku or l.sku_id] += 1
        elif style:
            styles_by_product[pid].add(style)

    # 卖家编码可以挂在多条链接下（用户明确说过：编码不唯一，只有 SKU ID 唯一），
    # 这种行无法靠编码判定归属，单独说明，不要混进「真的找不到」里。
    if sku_index.conflicts:
        sample = "、".join(sorted(sku_index.conflicts)[:3])
        issues.append(
            f"映射表里 {len(sku_index.conflicts)} 个 SKU ID 指向了不同商品（如 {sample}），"
            f"已取第一条；这属于映射数据冲突，建议重新导一次映射表"
        )
    if ambiguous_rows:
        issues.append(
            f"{ambiguous_rows} 行的卖家编码同时挂在多条链接下（编码可复用），"
            f"已归入「无ID」；有 SKU ID 的行不受影响"
        )
    for sku, n in unresolved.most_common(8):
        issues.append(f"「{sku}」找不到商品ID（{n} 行），已归入「无ID」")
    if len(unresolved) > 8:
        issues.append(f"另有 {len(unresolved) - 8} 个 SKU 同样找不到商品ID，未逐条列出")
    for l in lines:
        if l.product_id and not l.style_codes:
            l.style_codes = "/".join(sorted(styles_by_product.get(l.product_id, [])))


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
        issues.append(f"成本表查不到「{sku}」且无法推导（{n} 行），成本按 0 计，净利偏高")
    for sku, n in derived.most_common():
        issues.append(f"「{sku}」成本由规则推导（{n} 行），建议补录成本表")


# ---------------- 对外入口 ----------------

def load_from_files(order_file: Path, ads_file: Optional[Path] = None,
                    commission_file: Optional[Path] = None,
                    mapping_file: Optional[Path] = None,
                    costs_file: Optional[Path] = None,
                    usd_to_local: float = 1.0) -> RawData:
    """从四个独立导出文件装配数据。"""
    issues: list[str] = []
    sources: dict[str, str] = {}

    order_rows = read_table(order_file)
    sources["订单"] = Path(order_file).name
    orders = parse_orders(order_rows)
    if not orders:
        issues.append(f"订单文件解析不出数据行：{Path(order_file).name}")

    sku_index = SkuIndex()
    if mapping_file:
        if Path(mapping_file).exists():
            sku_index = parse_mapping(read_table(mapping_file))
            sources["映射"] = Path(mapping_file).name
            if not len(sku_index):
                issues.append(f"映射文件里没有识别出 SKU ID / Product ID 列：{Path(mapping_file).name}")
        else:
            issues.append(f"映射文件不存在：{mapping_file}")

    costs: dict[str, float] = {}
    if costs_file and Path(costs_file).exists():
        rows = read_table(costs_file, sheet="成本") if costs_file.suffix.lower() in (".xlsx", ".xls") else read_table(costs_file)
        costs = parse_costs(rows)
        sources["成本"] = Path(costs_file).name
        if not costs:
            issues.append(f"成本文件里没读到编码与成本：{Path(costs_file).name}")
    elif costs_file:
        issues.append(f"成本文件不存在：{costs_file}")

    ads: dict[str, float] = {}
    if ads_file and Path(ads_file).exists():
        ads, ad_issues = parse_ads(read_table(ads_file), usd_to_local)
        issues += ad_issues
        sources["广告"] = Path(ads_file).name

    commissions: dict[str, float] = {}
    if commission_file and Path(commission_file).exists():
        commissions, com_issues = parse_commissions(read_table(commission_file))
        issues += com_issues
        sources["联盟佣金"] = Path(commission_file).name

    attach_product_ids(orders, sku_index, issues)
    attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues, sources=sources)


# 每类表的特征字段：只要这几个字段同时出现在同一行，就能确定类型
TABLE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("order", ("order_id", "subtotal_before")),
    ("ads", ("product_id", "ad_cost")),
    ("affiliate", ("product_id", "commission")),
    ("mapping", ("sku_id", "product_id")),
)


def classify_rows(rows: list[tuple]) -> Optional[str]:
    """按表头特征判断这是一张什么表。

    不靠文件名，因为平台导出的默认文件名千奇百怪
    （"全部 笔订单-2026-10-27-16_59.csv"、"creative data for product campaigns ..."），
    而表头结构是稳定的。
    """
    for kind, required in TABLE_SIGNATURES:
        hidx, cm = find_header_row(rows, required)
        if hidx >= 0 and all(k in cm for k in required):
            return kind
    return None


def load_from_dir(folder: Path, costs_file: Optional[Path] = None,
                  usd_to_local: float = 1.0,
                  mapping_file: Optional[Path] = None) -> RawData:
    """从目录自动识别四类文件：逐张读表头判断类型，同名类型取最新的一张。"""
    folder = Path(folder)
    buckets: dict[str, list[tuple[Path, list[tuple]]]] = {
        "order": [], "ads": [], "affiliate": [], "mapping": []
    }
    skipped: list[str] = []
    json_maps: list[tuple[Path, SkuIndex]] = []   # 扩展直接抓的 sku_list_*.json

    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".xlsx", ".xls", ".csv", ".txt", ".json"):
            continue
        if f.name.startswith("~$"):
            continue
        if f.suffix.lower() == ".json":
            jm = parse_mapping_json(f)
            if len(jm):
                json_maps.append((f, jm))
            else:
                skipped.append(f.name)
            continue
        try:
            rows = read_table(f)
        except Exception:
            skipped.append(f.name)
            continue
        kind = classify_rows(rows)
        if kind:
            buckets[kind].append((f, rows))
        else:
            skipped.append(f.name)

    if not buckets["order"]:
        raise FileNotFoundError(
            f"目录里没识别出订单表：{folder}\n"
            f"（跳过：{'、'.join(skipped) if skipped else '无'}）"
        )

    def newest(kind: str):
        items = buckets[kind]
        if not items:
            return None
        return max(items, key=lambda x: x[0].stat().st_mtime)

    order_f, order_rows = newest("order")
    issues: list[str] = []
    sources: dict[str, str] = {"订单": order_f.name}
    orders = parse_orders(order_rows)

    sku_index = SkuIndex()
    m = newest("mapping")
    # 映射主表优先：它是历次抓取累积合并出来的，链接改绑后老 SKU 也不会丢。
    # 单次抓下来的那份只反映"现在"，用它会让历史订单里消失的 SKU 归入未识别。
    # 映射来源可以有多份，全部合并（主表优先），而不是挑一份用：
    # 主表保证链接改过后老 SKU 不丢归属，目录里的表保证本次抓取完整。
    parts: list[str] = []
    if mapping_file and Path(mapping_file).exists():
        midx = parse_mapping(read_table(Path(mapping_file)))
        if len(midx):
            sku_index.merge(midx)
            parts.append(f"{Path(mapping_file).name}（累积主表）")
        else:
            issues.append(f"映射主表里没识别出 SKU ID / Product ID 列：{Path(mapping_file).name}")

    m = newest("mapping")
    if m:
        tidx = parse_mapping(m[1])
        if len(tidx):
            sku_index.merge(tidx)
            parts.append(m[0].name)
        else:
            issues.append(f"映射表里没识别出 SKU ID / Product ID 列：{m[0].name}")

    if json_maps:
        for jf, jidx in sorted(json_maps, key=lambda x: x[0].stat().st_mtime):
            sku_index.merge(jidx)
            parts.append(jf.name)

    if parts:
        sources["映射"] = " + ".join(parts)

    costs: dict[str, float] = {}
    if costs_file and Path(costs_file).exists():
        cpath = Path(costs_file)
        if cpath.is_dir():
            for cf in cpath.iterdir():
                if cf.suffix.lower() in (".xlsx", ".xls", ".csv") and not cf.name.startswith("~$"):
                    c = parse_costs(read_table(cf, sheet="成本") if cf.suffix.lower().startswith(".xls") else read_table(cf))
                    if c:
                        costs.update(c)
                        sources["成本"] = cf.name
                        break
        else:
            rows = read_table(cpath, sheet="成本") if cpath.suffix.lower().startswith(".xls") else read_table(cpath)
            costs = parse_costs(rows)
            sources["成本"] = cpath.name
        if not costs:
            issues.append(f"成本文件里没读到编码与成本：{costs_file}")
    elif costs_file:
        issues.append(f"成本文件不存在：{costs_file}")

    ads: dict[str, float] = {}
    a = newest("ads")
    if a:
        ads, ad_issues = parse_ads(a[1], usd_to_local)
        issues += ad_issues
        sources["广告"] = a[0].name

    commissions: dict[str, float] = {}
    c = newest("affiliate")
    if c:
        commissions, com_issues = parse_commissions(c[1])
        issues += com_issues
        sources["联盟佣金"] = c[0].name

    for kind, label in (("ads", "广告表"), ("affiliate", "联盟佣金表"), ("mapping", "商品映射表")):
        if not buckets[kind]:
            issues.append(f"目录里没识别出{label}，相关字段按 0 计")

    attach_product_ids(orders, sku_index, issues)
    attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues, sources=sources)


def load_from_workbook(path: Path, usd_to_local: float = 1.0,
                       costs_file: Optional[Path] = None) -> RawData:
    """兼容旧版主工作簿（订单表前面插了四列人工列，链接资料是完整模板）。"""
    path = Path(path)
    issues: list[str] = []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)

    def sheet_rows(name: str) -> list[tuple]:
        if name not in wb.sheetnames:
            return []
        return [tuple(r) for r in wb[name].iter_rows(values_only=True)]

    cost_rows = sheet_rows("成本")
    link_rows = sheet_rows("链接资料")
    order_rows = sheet_rows("订单")
    ad_rows = sheet_rows("广告")
    com_rows = sheet_rows("联盟佣金")
    wb.close()

    orders = parse_orders(order_rows)
    sku_index = parse_mapping(link_rows)

    costs = parse_costs(cost_rows)
    # 如果 data/costs 里有更新的一份，用它覆盖工作簿里的成本表
    if costs_file and Path(costs_file).exists():
        cf = Path(costs_file)
        rows = read_table(cf, sheet="成本") if cf.suffix.lower().startswith(".xls") else read_table(cf)
        override = parse_costs(rows)
        if override:
            costs = override
    ads, ad_issues = parse_ads(ad_rows, usd_to_local)
    commissions, com_issues = parse_commissions(com_rows)
    issues += ad_issues + com_issues

    attach_product_ids(orders, sku_index, issues)
    attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues,
                   sources={"工作簿": path.name})
