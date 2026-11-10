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
from datetime import date, datetime, timedelta
from numbers import Number
from pathlib import Path
from typing import Iterable, Optional

import openpyxl

from . import paths
from . import provenance as prove


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
    # 物流：判定「售前取消」还是「售后取消」。
    # 有单号/物流商 = 已经打包出单，取消发生在履约之后——
    # 这种订单货已经发了，发单费和商品成本是实打实花掉的。
    "tracking_id": ["trackingid", "物流单号", "运单号", "跟踪号"],
    "shipping_provider": ["shippingprovidername", "物流商", "快递公司", "承运商"],
    "rts_time": ["rtstime", "打包时间", "备货时间"],
    "shipped_time": ["shippedtime", "发货时间", "揽收时间"],
    "delivered_time": ["deliveredtime", "签收时间", "送达时间"],
    # 广告
    "ad_cost": ["成本", "cost", "spend", "花费", "广告花费"],
    "ad_currency": ["货币", "currency"],
    # 联盟佣金
    "commission": ["est.commission", "estcommission", "预估佣金", "佣金"],
    "commission_refund": ["refunds", "退款"],
    # 统计表 / 映射表
    "style_code": ["款式编码", "款式", "stylecode",
                   # 店铺后台导出的「链接资料」表，第 1 列是款式编码，
                   # 但表头那格写的是「这列常态保留 勿删和覆盖」——
                   # 平台不给你正经列名，只能按这个说法认。
                   "这列常态保留", "常态保留", "勿删和覆盖"],
}


# 配置文件目录（打包后叫「配置」）
MAPPING_FILE = paths.CONFIG / "mapping.json"
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


def build_colmap(header_row: Iterable, aliases: dict[str, list[str]] | None = None) -> dict[str, int]:
    """把表头行映射成 {逻辑字段: 列号}。

    每个逻辑字段按候选顺序逐个试：第一个找不到就试下一个，
    直到找到为止（所以可以配很多个标题防表格改版）。

    aliases 传了就用它（界面上按国家配的标题），没传就用 config/mapping.json。
    """
    norm = [norm_header(c) for c in header_row]
    colmap: dict[str, int] = {}
    for key, names in (aliases or get_aliases()).items():
        for want in names:
            w = norm_header(want)
            hit = next((i for i, h in enumerate(norm) if h == w), None)
            if hit is None:
                hit = next((i for i, h in enumerate(norm) if w and w in h), None)
            if hit is not None and key not in colmap:
                colmap[key] = hit
                break
    return colmap


def find_header_row(rows: list[tuple], required: tuple[str, ...], max_scan: int = 8,
                    aliases: dict[str, list[str]] | None = None) -> tuple[int, dict[str, int]]:
    """在前若干行里找最像表头的那一行：包含 required 字段最多者。

    平台导出常见套路：第 1 行英文表头 + 第 2 行英文说明；或第 1 行标题、第 3 行才是表头。
    """
    best = (-1, -1, {})
    for i, row in enumerate(rows[:max_scan]):
        cm = build_colmap(row, aliases)
        score = sum(1 for k in required if k in cm)
        if score > best[0] or (score == best[0] and len(cm) > len(best[2])):
            best = (score, i, cm)
    score, idx, cm = best
    # 至少命中 2 个（或全部，若要求少于 2 个）才算找到表头
    if score < min(2, len(required)):
        return -1, {}
    return idx, cm


def merged_aliases(order_fields: dict | None) -> dict[str, list[str]]:
    """把界面上配的「字段 → 标题候选」合并进内置别名。

    order_fields 两种写法都认：
        {"subtotal_before": ["折扣前小计", "SKU Subtotal"]}
        {"subtotal_before": {"label": "折扣前小计", "titles": [...]}}
    配了的字段以配置为准（顺序即优先级），没配的沿用内置默认。
    """
    out = dict(get_aliases())
    # 内置别名兜底：config/mapping.json 一旦存在，get_aliases 就**只**返回配置里
    # 写了的字段。新增的字段（物流单号这些）没同步进配置时会整列读不到，
    # 而且不报错——表现是「所有订单都算售前取消」。这里补齐缺的键。
    for _k, _v in ALIASES.items():
        out.setdefault(_k, list(_v))
    for key, v in (order_fields or {}).items():
        if isinstance(v, dict):
            titles = v.get("titles") or v.get("candidates") or []
        elif isinstance(v, list):
            titles = v
        else:
            continue
        clean = [str(t).strip() for t in titles if str(t).strip()]
        if clean:
            out[str(key)] = clean
    return out


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
    # 商品ID → 该链接下所有 SKU 的款式编码（按首次出现顺序，去重）
    #
    # 这跟模板《V1-净利》的逻辑一致：
    #   TEXTJOIN("/", TRUE, UNIQUE(FILTER(链接资料!A:A, 链接资料!B:B=商品ID, "")))
    # 也就是「这条链接下所有款式编码，去重后用 / 连起来」。
    # 注意是链接下的**全部** SKU，不只是本期出了单的——
    # 没出单的 SKU 同样属于这条链接。
    by_product_styles: dict[str, list[str]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.by_sku_id) + len(self.by_seller_sku)

    def add_style(self, product_id: str, style_code: str) -> None:
        """登记一个款式编码。按首次出现顺序去重（不区分大小写）。"""
        pid = to_str(product_id)
        code = to_str(style_code)
        if not pid or not code:
            return
        lst = self.by_product_styles.setdefault(pid, [])
        # Excel 的 UNIQUE 不区分大小写（V36 与 v36 算同一个），保留先出现的那个写法
        for old in lst:
            if old.lower() == code.lower():
                return
        lst.append(code)

    def styles_of(self, product_id: str) -> str:
        """某条链接的款式编码串：去重后用 / 连接。"""
        return "/".join(self.by_product_styles.get(to_str(product_id), []))

    def merge(self, other: "SkuIndex", override: bool = False) -> "SkuIndex":
        """合并另一份索引。

        override=False（默认）：self 优先，只补 self 没有的 —— 用于「主表垫底」。
        override=True：other 优先，直接盖掉 —— 用于「按区间的 SKU 表说了算」。

        ⚠ 前提：**SKU ID 是平台内唯一的，一个 SKU 只属于一条链接**。
        把同一个规格加到另一条链接上，平台会生成一个新的 SKU ID，
        所以正常情况下绝对不会出现「同一个 SKU ID 指向两条不同链接」。

        真出现了，只有两种可能：映射表拼错了（两次导出叠在一起）、
        或者文件被手工改过。这种一定要报出来——以前是静默取第一条，
        谁知道哪条是对的。
        """
        for sid, item in other.by_sku_id.items():
            old = self.by_sku_id.get(sid)
            if old is not None and to_str(old.get("product_id")) != to_str(item.get("product_id")):
                self.conflicts.add(sid)
            if override or old is None:
                self.by_sku_id[sid] = item
        for sku, items in other.by_seller_sku.items():
            bucket = self.by_seller_sku[sku]
            for it in items:
                if it not in bucket:
                    bucket.append(it)
        # 款式编码也要合并：主表里的和目录表里的取并集
        for pid, codes in other.by_product_styles.items():
            for c in codes:
                self.add_style(pid, c)
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

# 从卖家编码推导款式编码。
#
# 店铺后台下载的「链接资料」表里没有款式编码那一列（用户手工加的那列
# 只有在模板里才有），但款式编码就藏在卖家编码的第一段里：
#     V27-White-10Pcs      → V27
#     V1【Set-LOVE】       → V1
#     MY-J68-22cm+clip     → MY-J68
#     M15*30pcs            → M15   （* 后面也是数量，不是编码的一部分）
# 规则：取第一个分隔符（- * × 【 （ ( 空格）之前的部分，
# 但「MY-J68」这种两段式要保留两段，所以先试「字母前缀-字母+数字」的整段。
_STYLE_TWO_SEG = re.compile(r"^([A-Za-z]{1,3}-[A-Za-z]+\d*)(?=[\-*×\[【（(\s]|$)")
_STYLE_FIRST_SEG = re.compile(r"^([^\-*×\[【（(\s]+)")


def derive_style_code(seller_sku: str) -> str:
    """从卖家编码推款式编码。推不出来就返回空，由调用方留空。"""
    s = str(seller_sku or "").strip()
    if not s:
        return ""
    m = _STYLE_TWO_SEG.match(s)
    if m:
        return m.group(1).strip()
    m = _STYLE_FIRST_SEG.match(s)
    return m.group(1).strip() if m else ""


def derive_styles(seller_sku: str) -> list[str]:
    """一个卖家编码里可能捆着好几个款式（MY-J33/J74/J28/J71-Beige）。

    整串直接推会得到「MY」这种半截编码，所以先按 / 、 拆开再逐个推。
    """
    out: list[str] = []
    for part in split_styles(seller_sku):
        c = derive_style_code(part)
        if c and c not in out:
            out.append(c)
    return out


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
    # 物流三件套：判断「售前取消 / 售后取消」，见 order_kind()
    tracking_id: str = ""
    shipping_provider: str = ""
    rts_time: str = ""
    shipped_time: str = ""
    delivered_time: str = ""
    product_id: str = ""
    style_codes: str = ""
    cost_unit_cny: Optional[float] = None
    refund_amount: float = 0.0
    order_line_count: int = 1
    # 这一行订单在原始表里的「列标题 → 数值」。
    # 公式里写 [折扣前小计] 这种标题时，就从这里取数——
    # 所以公式不用关心列在第几列，平台改列顺序也不影响。
    raw_num: dict[str, float] = field(default_factory=dict)

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
    # 订单表的原始表头。计算参数里裸写标题时靠它匹配。
    order_titles: list[str] = field(default_factory=list)
    # 各类表覆盖的导出区间 {「广告」: (起, 止)}。
    # 广告 / 佣金表没有日期列，费用是按整段计入的；
    # 报表区间和导出区间对不上时，净利会偏，得让人看得见。
    ranges: dict = field(default_factory=dict)
    # 这次**真正参与计算**的是哪几份文件、盖的哪一段、多少行（见 src/provenance.py）。
    # sources 只能说明「认出了这类表」，不等于「数据真的用上了」——
    # 出表闸门看的是这里，不是 sources。
    uses: dict = field(default_factory=dict)
    # 这家店显式声明「本来就没有」的类别（no_ads / no_affiliate）
    declared: set = field(default_factory=set)
    # 这次核算的区间（闸门要用它判「盖不盖得住」）
    window: tuple[str, str] | None = None
    # 成本表里查不到的编码 → 行数。报表要点名提醒，不能只写进「异常」页
    missing_costs: dict = field(default_factory=dict)


# ---------------- 文件读取 ----------------

def read_table(path: Path, sheet: Optional[str] = None) -> list[tuple]:
    path = Path(path)
    try:
        return _read_table_inner(path, sheet)
    except OSError as e:
        # 最常见：这张表正在 Excel/WPS 里开着（Windows 独占锁），
        # 或者文件被杀毒软件拦着。直接抛一个能看懂的话，
        # 别让用户对着一串 WinError 32 发呆。
        raise RuntimeError(
            f"读不了「{path.name}」：可能正在 Excel/WPS 里打开，或没有访问权限（{e}）"
        ) from e


# csv 默认单个字段上限 128KB。订单表的「商品名/备注/客户留言」偶尔会超，
# 一超就抛 _csv.Error，而那个异常没人接 —— 整次核算直接失败，
# 报的还是英文 "field larger than field limit"，用户完全不知道发生了什么。
# 这里放宽到 10MB（足够任何一行文本），内存代价可以忽略。
_CSV_FIELD_LIMIT = 10 * 1024 * 1024
try:
    csv.field_size_limit(_CSV_FIELD_LIMIT)
except (OverflowError, OSError):      # 32 位解释器上限更低
    csv.field_size_limit(1024 * 1024)


def _read_table_inner(path: Path, sheet: Optional[str] = None) -> list[tuple]:
    if path.suffix.lower() in (".csv", ".txt"):
        # 编码依次试。utf-16 排在最后：Excel「Unicode 文本」和部分导出工具
        # 给的就是 UTF-16，以前不在列表里，读出来是**空表** ——
        # 于是「该时段没有订单」，数字全是 0，而且不报任何错。
        # 先看文件头两个字节：UTF-16 有 BOM 才试。
        # 无 BOM 时硬按 UTF-16 解会「成功」解出一堆乱码（不报错），
        # 列名全认不出 —— 比直接报错更难查。
        # gb18030 要排在 gbk 前面：它是 GBK 的超集，能解 GBK 解不了的字节
        # （0x80 这类）。2026-11-05 实测：店主从后台手动导的订单表就是 GB18030，
        # 只试 gbk 会 UnicodeDecodeError → 整份文件被跳过 → 报「目录里没识别出
        # 订单表」，而文件明明是好的。
        encs = ("utf-8-sig", "gb18030", "gbk", "utf-8")
        try:
            head = path.open("rb").read(2)
            # 用 "utf-16" 而不是 "utf-16-le"：前者会读 BOM 定字节序
            # 并把 BOM **去掉**，后者会把 BOM 留在第一个单元格里 ——
            # 于是首列表头变成 "\ufeff订单号"，跟配置里的「订单号」对不上，
            # 整张表一列都认不出。
            if head in (b"\xff\xfe", b"\xfe\xff"):
                encs = ("utf-16",)
        except OSError:
            pass

        last_err: Exception | None = None
        for enc in encs:
            try:
                with path.open("r", encoding=enc, newline="") as f:
                    try:
                        rows = [tuple(r) for r in csv.reader(f)]
                        # 兜一层：首行首列如果还带着 BOM，去掉再返回。
                        # 表头认不出会导致「整表列都对不上」，但不会报错。
                        if rows and rows[0] and isinstance(rows[0][0], str):
                            first = rows[0][0].lstrip("\ufeff")
                            if first != rows[0][0]:
                                rows[0] = (first,) + tuple(rows[0][1:])
                        return rows
                    except csv.Error as e:
                        raise RuntimeError(
                            f"「{path.name}」表格格式有问题（{e}）："
                            "可能是某一行断了引号，或者有超长单元格。"
                            "用 Excel 打开另存一次 CSV 通常就好了"
                        ) from e
            except UnicodeDecodeError as e:
                last_err = e
                continue

        # 严格解码全失败 → 用「坏字节替换成 �」再读一遍。
        #
        # 真实场景（2026-11-05，店主手动导的 MYT-J1 订单表，42 MB）：整份是 GBK，
        # 但某个客户名字段里混进了一个 0x80，位置 13,114,709。严格解码直接抛
        # UnicodeDecodeError，整张表被跳过 → 报「目录里没识别出订单表」，
        # 而那份文件 99.9999% 是好的。一个脏字节不该让整个月的账算不出来。
        # 换成 � 之后，坏的那一格最多显示成问号，金额、日期、订单号全都还在。
        for enc in ("gb18030", "utf-8"):
            try:
                with path.open("r", encoding=enc, errors="replace", newline="") as f:
                    rows = [tuple(r) for r in csv.reader(f)]
            except (UnicodeError, csv.Error, OSError):
                continue
            if rows and rows[0] and isinstance(rows[0][0], str):
                rows[0] = (rows[0][0].lstrip("\ufeff"),) + tuple(rows[0][1:])
            return rows

        # 连替换解码都失败：明确报错，不要静默返回空表。
        # 静默返回空的后果是「这家店这个月没订单」，看起来像正常结论。
        raise RuntimeError(
            f"「{path.name}」编码认不出来（试过 UTF-8 / GB18030 / GBK / UTF-16）："
            f"{last_err}。用 Excel 另存为「CSV UTF-8」再试"
        )
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


# ---------------- 各类数据解析 ----------------

def parse_costs(rows: list[tuple], cost_fields: dict | None = None) -> dict[str, float]:
    """成本表：编码 → 成本（人民币）。表头可有可无。

    cost_fields 可以指定「编码列」「成本列」的标题候选（界面上可改）：
        {"code": ["商品编码", "SKU"], "cost": ["成本", "采购成本"]}
    """
    out: dict[str, float] = {}
    start = 0
    sku_col, cost_col = 0, 1
    cf = cost_fields or {}
    code_titles = [norm_header(x) for x in (cf.get("code") or []) if str(x).strip()]
    cost_titles = [norm_header(x) for x in (cf.get("cost") or []) if str(x).strip()]

    if rows:
        norm = [norm_header(c) for c in rows[0]]
        hit_code = next((norm.index(t) for t in code_titles if t in norm), None)
        hit_cost = next((norm.index(t) for t in cost_titles if t in norm), None)
        if hit_code is None or hit_cost is None:
            cm = build_colmap(rows[0])
            if "seller_sku" in cm or "product_id" in cm:
                start = 1
                if hit_code is None:
                    hit_code = cm.get("seller_sku", cm.get("product_id"))
                if hit_cost is None:
                    hit_cost = next((c for c in range(len(rows[0])) if c != hit_code), 1)
        if hit_code is not None and hit_cost is not None:
            sku_col, cost_col = hit_code, hit_cost
            start = 1
        elif hit_code is not None:
            sku_col = hit_code
            cost_col = next((c for c in range(len(rows[0])) if c != sku_col), 1)
            start = 1

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


def parse_mapping(rows: list[tuple], mapping_fields: dict | None = None) -> SkuIndex:
    """解析 SKU→商品 映射表。

    兼容两种形态：
      · 链接资料模板：款式编码 | product_id | ... | sku_id | ... | seller_sku
      · SKU 统计表  ：SKU ID | Product ID | 商品 | GMV | 订单数 | 件数（表头可能在第 3 行）

    店铺后台下载的「链接资料」表没有款式编码列，
    这时从卖家编码推导（见 derive_style_code）——款式编码就藏在
    卖家编码的第一段里，V27-White-10Pcs → V27。
    """
    idx = SkuIndex()
    aliases = merged_aliases(mapping_fields) if mapping_fields else None
    hidx, cm = find_header_row(rows, ("sku_id", "product_id"), aliases=aliases)
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
        # 表头说明行（"Mandatory" / "Uneditable" / "商品 ID" 这类）不是数据
        if not pid.isdigit():
            continue
        seller_sku = to_str(r[c_sku]) if c_sku is not None and c_sku < len(r) else ""
        style = to_str(r[c_style]) if c_style is not None and c_style < len(r) else ""
        if not style:
            # 表里没有款式编码列（或这行是空的）→ 从卖家编码推
            style = derive_style_code(seller_sku)
        item = {
            "product_id": pid,
            "style_code": style,
            "product_name": to_str(r[c_name]) if c_name is not None and c_name < len(r) else "",
        }
        # 款式编码登记到「商品ID → 款式编码」的索引上，
        # 报表那列要的是「这条链接下所有款式编码」，不只是出了单的那几个。
        idx.add_style(pid, item["style_code"])
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
        ssk = flat.get("sellersku", "") or flat.get("skuname", "") or flat.get("sku", "")
        style = (flat.get("stylecode", "") or flat.get("sellerstylecode", "")
                 or derive_style_code(ssk))
        item = {
            "product_id": pid,
            "style_code": style,
            "product_name": flat.get("productname", "") or flat.get("producttitle", ""),
        }
        idx.add_style(pid, style)
        sid = flat.get("skuid", "")
        if sid:
            old = idx.by_sku_id.get(sid)
            if old is None:
                idx.by_sku_id[sid] = item
            elif old["product_id"] != item["product_id"]:
                idx.conflicts.add(sid)
        if ssk:
            bucket = idx.by_seller_sku[ssk]
            if item not in bucket:
                bucket.append(item)
    return idx


def _is_number(v) -> bool:
    """这个单元格像不像数字（决定要不要进「标题 → 数值」表）。

    订单号、SKU ID 这类长数字串会顶到公式里，所以超过 12 位的一律当标识符，
    不算金额也不进聚合表。
    """
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return abs(float(v)) < 1e12
    s = to_str(v).replace(",", "")
    if not s or not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return False
    return len(s.replace("-", "").split(".")[0]) <= 12


def parse_orders(rows: list[tuple], order_fields: dict | None = None,
                 titles_out: list[str] | None = None) -> list[OrderLine]:
    """解析订单表。

    order_fields 是界面上按国家配的「字段 → 标题候选」，配了就优先用。
    """
    aliases = merged_aliases(order_fields)
    hidx, cm = find_header_row(rows, ("order_id", "sku_id", "seller_sku", "qty"),
                               aliases=aliases)
    if hidx < 0:
        return []
    need = ("order_id", "sku_id", "seller_sku", "qty", "subtotal_before")
    if sum(1 for k in need if k in cm) < 3:
        return []
    # 原始表头（归一化）：公式里写标题时靠它匹配
    raw_titles = [norm_header(c) for c in rows[hidx]]
    if titles_out is not None:
        titles_out.extend(str(c).strip() for c in rows[hidx] if str(c).strip())

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
        # 没有订单号就不是订单：平台导出的**合计行**（SKU 列也空，金额是总额）、
        # 说明行都落在这里。2026-11-05 实测：店主手动导的订单表末尾就有一行
        # 「SKU Subtotal 2,740,004.98 / Discount 1,502,818.20」的合计行，
        # 万一它带着 SKU 或件数被读进来，会凭空多出一条订单、GMV 直接翻倍。
        if not oid:
            continue
        qty = to_num(get(r, "qty"), 1.0) or 1.0
        before = to_num(get(r, "subtotal_before"))
        amount = to_num(get(r, "order_amount"))

        # 平台导出第 2 行是英文说明（"Platform unique order ID." 之类），
        # 它同样落在 SKU 列上，必须排掉：既没有数字订单号，也没有任何金额
        if not re.search(r"\d{6,}", oid) and qty <= 1 and before == 0 and amount == 0:
            continue

        line = OrderLine(
            order_id=oid,
            seller_sku=sku,
            sku_id=sku_id,
            product_name=to_str(get(r, "product_name", "")),
            qty=qty,
            subtotal_before=before,
            seller_discount=to_num(get(r, "seller_discount")),
            order_amount=amount,
            refund_amount=to_num(get(r, "refund_amount")),
            cancel=to_str(get(r, "cancel", "")),
            status=to_str(get(r, "status", "")),
            created=to_str(get(r, "created", "")),
            paid=to_str(get(r, "paid", "")),
            tracking_id=to_str(get(r, "tracking_id", "")),
            shipping_provider=to_str(get(r, "shipping_provider", "")),
            rts_time=to_str(get(r, "rts_time", "")),
            shipped_time=to_str(get(r, "shipped_time", "")),
            delivered_time=to_str(get(r, "delivered_time", "")),
            product_id=to_str(get(r, "product_id", "")) or legacy_pid(r),
        )
        # 公式里写 [折扣前小计] 这种标题时，就从这里取数。
        # 只收「像数字」的列：订单号、SKU、时间这些不进，省内存也不会被误当金额。
        raw: dict[str, float] = {}
        for t, v in zip(raw_titles, r):
            if t and _is_number(v):
                raw[t] = raw.get(t, 0.0) + to_num(v)
        line.raw_num = raw
        lines.append(line)

    per_order = Counter(l.order_id for l in lines)
    for l in lines:
        l.order_line_count = per_order.get(l.order_id, 1) or 1
    return lines


# 广告账号的本位币 / 店铺本地币：由 cli 在知道国家之后设一次。
# 为什么不从行里读：平台导出把币种写成 LOCAL，而 LOCAL 指的是**账号本位币**，
# 不是店铺所在国货币 —— 这两者不一致（账号 USD、店铺 MYR）时，差了整整一个汇率。
_ADS_CURRENCY = "AUTO"
_LOCAL_CURRENCY = ""
_ADS_RATES: dict = {}          # CNY 基准的汇率表，用来算任意币种的交叉汇率
# 上一次 parse_ads 里「币种认不出、只能按 0 计」的金额。
# 这个数不为 0 时出表闸门会拦住——钱静默变 0 比算错更危险，报表上完全看不出来。
_ADS_UNKNOWN = 0.0


def last_unknown_ads() -> float:
    """上一次解析广告表时，有多少金额因为币种认不出被按 0 计了。"""
    return float(_ADS_UNKNOWN or 0.0)


def set_ads_currency(acct: str = "", local: str = "", rates: dict | None = None) -> None:
    """核算前告诉加载器：广告表按什么币种算、店铺本地币是什么、汇率表是什么。

    acct 取值：""/"AUTO" 自动识别；"LOCAL" 不换算；或任意币种代码（USD/THB/PHP…）。
    """
    global _ADS_CURRENCY, _LOCAL_CURRENCY, _ADS_RATES, _ADS_UNKNOWN
    _ADS_UNKNOWN = 0.0
    _ADS_CURRENCY = (acct or "AUTO").strip().upper()
    _LOCAL_CURRENCY = (local or "").strip().upper()
    _ADS_RATES = dict(rates or {}) if isinstance(rates, dict) else {}



def _ads_rate_to_local(code: str, usd_to_local: float, local: str) -> float | None:
    """1 单位 code 折成多少本地币。查不到返回 None（调用方记「未识别」，不默认 1）。"""
    code = (code or "").strip().upper()
    if not code or code in ("LOCAL", "RM") or (local and code == local):
        return 1.0
    if code in ("USD", "US$", "$"):
        return float(usd_to_local or 1.0)
    rf = _ADS_RATES.get(code)
    rl = _ADS_RATES.get(local) if local else None
    if rf and rl:
        return float(rl) / float(rf)      # 交叉汇率：CNY→本地 ÷ CNY→该币
    return None


def parse_ads(rows: list[tuple], usd_to_local: float = 1.0,
              ads_fields: dict | None = None,
              ads_currency: str | None = None,
              local_currency: str | None = None) -> tuple[dict[str, float], list[str]]:
    """广告报表：按 商品ID 汇总花费，并按货币换算成本地货币。

    ads_fields 是界面上配的「广告表字段 → 标题候选」，配了优先用，
    这样平台把「成本」改成「花费」之类的也能自己加候选，不必改代码。
    """
    global _ADS_UNKNOWN
    _ADS_UNKNOWN = 0.0
    issues: list[str] = []
    aliases = merged_aliases(ads_fields) if ads_fields else None
    hidx, cm = find_header_row(rows, ("product_id", "ad_cost"), aliases=aliases)
    if hidx < 0 or "product_id" not in cm or "ad_cost" not in cm:
        return {}, ["广告表未识别出「商品 ID」与「成本」列，广告费按 0 计"]

    out: dict[str, float] = defaultdict(float)
    currencies: Counter = Counter()
    unknown = 0.0
    for r in rows[hidx + 1:]:
        pid = to_str(r[cm["product_id"]]) if cm["product_id"] < len(r) else ""
        if not pid:
            continue
        # 广告表里会出现 N/A、NULL、- 这种伪商品ID（账户层级的未归属花费）。
        # 把它们当链接会凭空多出叫「N/A」的产品行，统一归到「无ID」，
        # 钱不丢，也不会伪造一条链接。
        if not re.fullmatch(r"\d{6,}", pid):
            pid = "无ID"
        cost = to_num(r[cm["ad_cost"]]) if cm["ad_cost"] < len(r) else 0.0
        cur = to_str(r[cm["ad_currency"]]).upper() if cm.get("ad_currency") is not None and cm["ad_currency"] < len(r) else ""
        currencies[cur or "未标注"] += 1
        # 币种判定（2026-11-04 修正，实测后台）：
        #   「LOCAL」和空值都表示**广告账号的本位币**，不等于店铺所在国的货币。
        #   实测 DXM：后台成本 12,450.49 USD，导出表 Currency 写的是 LOCAL，
        #   以前把 LOCAL 当成马币直接用 → 广告费少算 4 倍、净利虚高 3.8 万。
        #   账号本位币由 ads_currency 配置（rates.json 每国可写），默认 USD。
        acct = (ads_currency or _ADS_CURRENCY or "AUTO").strip().upper()
        local = (local_currency or _LOCAL_CURRENCY or "").strip().upper()
        # 这一行按什么币种算：
        #   AUTO        表里写了真实币种就用它；写 LOCAL/空（账号本位币）按美金
        #   LOCAL       不换算
        #   USD/THB/…   强制按这个币种（汇率表里查交叉汇率）
        c = (cur or "").strip().upper()
        if acct == "AUTO":
            code = "USD" if c in ("", "LOCAL") else c
        elif acct in ("LOCAL", "本地", "本地币"):
            code = local or "LOCAL"
        else:
            code = acct
        rate = _ads_rate_to_local(code, usd_to_local, local)
        if rate is None:
            unknown += cost
        else:
            out[pid] += cost * rate
    detail = "、".join(f"{k}×{v}" for k, v in currencies.most_common(3))
    _m = (ads_currency or _ADS_CURRENCY or "AUTO").upper()
    _local = (local_currency or _LOCAL_CURRENCY or "").upper()
    if detail:
        if _m in ("LOCAL", "MYR", "RM") or (_local and _m == _local):
            _how = "按本地币直接用"
        elif _m in ("", "AUTO"):
            _how = "自动识别；表里写 LOCAL/空的按美金折算"
        else:
            _how = f"按 {_m} 折成本地币"
        issues.append(f"广告表货币分布：{detail}（{_how}）")
    if unknown:
        # 这条必须放最后：币种填错/汇率表里没有时，钱会静默变 0，比少算更危险。
        issues.append(f"⚠ 有 {unknown:,.2f} 的金额币种认不出（设置：{_m or '自动'}），"
                      f"这部分广告费按 0 计，净利会偏高")
    _ADS_UNKNOWN = float(unknown or 0.0)
    return dict(out), issues


def parse_commissions(rows: list[tuple],
                      affiliate_fields: dict | None = None) -> tuple[dict[str, float], list[str]]:
    """联盟佣金报表：按 商品ID 汇总预估佣金。值可能是 'RM2,678.75' 这类字符串。

    affiliate_fields 是界面上配的「联盟佣金表字段 → 标题候选」。
    """
    aliases = merged_aliases(affiliate_fields) if affiliate_fields else None
    hidx, cm = find_header_row(rows, ("product_id", "commission"), aliases=aliases)
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

# 成本能不能「按编码规律推」——2026-11-04 起默认**不推**。
#
# 实测 J22-1-18pCS 会被推成 J22-1(1.55) × 18 = 27.9。乘法本身没错，
# 但卖家编码是人工起的，「18pCS」不一定是 18 件，也不一定就是那个基座——
# 一旦推错，成本静默变高变低，报表上看不出来。用户定的规矩：
# 宁可按 0 计 + 在报表里点名「这个编码没成本」，也不要猜。
# 想恢复推导把这里改回 True（老逻辑原样留在下面）。
ALLOW_COST_DERIVE = False


def cost_index(costs: dict[str, float]) -> dict[str, float]:
    """「忽略大小写」的成本索引：`V26-T-21cm-2PCS` → `V26-T-21CM-2PCS`。

    为什么需要（2026-11-05 实测）：店主成本表里录的是 `V26-T-21cm-2PCS`（大写 PCS），
    订单里卖家编码是 `V26-T-21cm-2Pcs`（小写 s）。字符串不等就查不到，
    报表就报「24 个编码没成本」——可人眼一看就是同一条。MYT-V1 一个月 42 行、
    MYT-W1 13 个编码都是这么来的。

    只做「大小写」这一种归一，不做别的：去掉 `*N`、去掉前缀这类都属于「猜」，
    用户定的规矩是宁可按 0 计 + 点名，也不要猜。

    归一后撞车的（两个不同编码只差大小写）**不放进索引** —— 分不清就不猜。
    """
    out: dict[str, float] = {}
    dup: set[str] = set()
    for k, v in (costs or {}).items():
        key = str(k).strip().upper()
        if not key:
            continue
        if key in out and out[key] != v:
            dup.add(key)          # 同键不同价：两条都不能用
        out[key] = v
    for k in dup:
        out.pop(k, None)
    return out


def resolve_cost(sku: str, costs: dict[str, float],
                 index: dict[str, float] | None = None
                 ) -> tuple[Optional[float], str]:
    """返回 (成本单价, 命中方式)。方式：exact / case / multipack / combo / miss。"""
    if sku in costs:
        return costs[sku], "exact"
    # 忽略大小写再试一次（不算「推导」，只是同一串字符的不同写法）
    idx = index if index is not None else cost_index(costs)
    hit = idx.get(str(sku).strip().upper())
    if hit is not None:
        return hit, "case"
    if not ALLOW_COST_DERIVE:
        return None, "miss"

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


def split_styles(s: str) -> list[str]:
    """把「款式编码」拆成单值。

    卖家编码里有时一段就捆着好几个款式（MY-J33/J74/J28/J71-Beige），
    直接被当成一个字符串用，报表上就会和别的行对不上——
    同一条链接这行写 "MY-J33/J74/J28/J71"、那行只写 "J28"。
    """
    return [x for x in re.split(r"[/,，、;；]+", to_str(s)) if x]


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
            styles_by_product[pid].update(split_styles(style))
    # 卖家编码可以挂在多条链接下（用户明确说过：编码不唯一，只有 SKU ID 唯一），
    # 这种行无法靠编码判定归属，单独说明，不要混进「真的找不到」里。
    if sku_index.conflicts:
        sample = "、".join(sorted(sku_index.conflicts)[:3])
        issues.append(
            f"有 {len(sku_index.conflicts)} 个 SKU ID 在不同来源里指向了不同商品（如 {sample}）。"
            f"SKU ID 是平台唯一的、不会改挂到别的链接，所以这只能是映射数据拼错了"
            f"（多次导出叠在一起 / 文件被手工改过）；已按最新那份取，建议清一次映射主表重导"
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

    # 款式编码：一条链接一个值，全链接的行必须拿到**同一串**编码。
    #
    # 模板《V1-净利》的算法是：
    #   TEXTJOIN("/", TRUE, UNIQUE(FILTER(链接资料!A:A, 链接资料!B:B=商品ID, "")))
    # 也就是「这条链接下全部 SKU 的款式编码」，跟本期有没有出单无关。
    #
    # 以前这里只做了一半：能查到映射的行拿到的是整串列表，查不到的行走兜底
    # 只留自己这一个编码，于是同一条链接里「这行写 MY-J33/J74/J28/J71、
    # 那行只写 J28」，报表取第一行就成了随机结果。
    # 现在把三处来源（映射里该链接的全部编码 / 本行映射编码 / 卖家编码推出的编码）
    # 求并集，再回写给这个链接的每一行，结果跟行的先后无关。
    for l in lines:
        if not l.product_id:
            continue
        codes = styles_by_product[l.product_id]
        codes.update(split_styles(sku_index.styles_of(l.product_id)))
        codes.update(derive_styles(l.seller_sku))
    for l in lines:
        if not l.product_id:
            continue
        codes = styles_by_product.get(l.product_id)
        if codes:
            l.style_codes = "/".join(sorted(codes))

    # 最后兜底：商品没认出来的行，款式编码直接从卖家编码推。
    #
    # 款式编码本来就写在卖家 SKU 里（V27-White-10Pcs → V27，
    # M15-RoseRed-100P → M15），不需要映射表也不需要商品 ID。
    # 以前这条只在映射里查到商品时才顺带带上，新店铺没有映射就整列空着，
    # 看着像"程序没读到"，其实是顺序反了。
    for l in lines:
        if not l.style_codes:
            derived = "/".join(derive_styles(l.seller_sku))
            if derived:
                l.style_codes = derived


def attach_costs(lines: list[OrderLine], costs: dict[str, float],
                 issues: list[str]) -> "Counter[str]":
    """把成本单价挂到每一行上，返回「查不到成本的 SKU → 行数」。

    返回值是给报表用的：成本查不到就是按 0 计，净利会虚高，
    这件事必须在报表第一眼就能看到，不能只躺在「异常」页第八条。
    """
    missing = Counter()
    derived: Counter = Counter()
    case_hit: Counter = Counter()
    idx = cost_index(costs)
    orig_by_upper = {str(k).strip().upper(): str(k).strip() for k in (costs or {})}
    for l in lines:
        unit, how = resolve_cost(l.seller_sku, costs, idx)
        l.cost_unit_cny = unit
        if unit is None:
            missing[l.seller_sku] += 1
        elif how == "case":
            case_hit[l.seller_sku] += 1
        elif how != "exact":
            derived[f"{l.seller_sku}（{how}）"] += 1
    for sku, n in missing.most_common(20):
        issues.append(f"成本表查不到「{sku}」（{n} 行），成本按 0 计，净利偏高——补录这条编码即可")
    if len(missing) > 20:
        issues.append(f"另有 {len(missing) - 20} 个编码也查不到成本，详见报表「口径」页")
    if case_hit:
        # 只差大小写：成本算进去了，但两边写法不一致，早晚还要再踩一次
        _n = sum(case_hit.values())
        _eg = []
        for sku in list(case_hit)[:3]:
            _o = orig_by_upper.get(str(sku).strip().upper(), "")
            _eg.append(f"{sku} ↔ {_o}" if _o and _o != sku else sku)
        issues.append(
            f"{len(case_hit)} 个编码是「忽略大小写」才匹配到成本的（{_n} 行）："
            + "、".join(_eg)
            + "——成本已算进去，建议把订单编码和成本表改成一致的大小写")
    for sku, n in derived.most_common():
        issues.append(f"「{sku}」成本由规则推导（{n} 行），建议补录成本表")
    return missing


# ---------------- 对外入口 ----------------

def load_from_files(order_file: Path, ads_file: Optional[Path] = None,
                    commission_file: Optional[Path] = None,
                    mapping_file: Optional[Path] = None,
                    costs_file: Optional[Path] = None,
                    usd_to_local: float = 1.0,
                    order_fields: dict | None = None,
                    cost_fields: dict | None = None,
                    ads_fields: dict | None = None,
                    affiliate_fields: dict | None = None,
                    mapping_fields: dict | None = None) -> RawData:
    """从四个独立导出文件装配数据。"""
    issues: list[str] = []
    sources: dict[str, str] = {}

    order_rows = read_table(order_file)
    sources["订单"] = Path(order_file).name
    titles: list[str] = []
    orders = parse_orders(order_rows, order_fields, titles)
    if not orders:
        issues.append(f"订单文件解析不出数据行：{Path(order_file).name}")

    sku_index = SkuIndex()
    if mapping_file:
        if Path(mapping_file).exists():
            sku_index = parse_mapping(read_table(mapping_file), mapping_fields)
            sources["映射"] = Path(mapping_file).name
            if not len(sku_index):
                issues.append(f"映射文件里没有识别出 SKU ID / Product ID 列：{Path(mapping_file).name}")
        else:
            issues.append(f"映射文件不存在：{mapping_file}")

    costs: dict[str, float] = {}
    if costs_file and Path(costs_file).exists():
        rows = read_table(costs_file, sheet="成本") if costs_file.suffix.lower() in (".xlsx", ".xls") else read_table(costs_file)
        costs = parse_costs(rows, cost_fields)
        sources["成本"] = Path(costs_file).name
        if not costs:
            issues.append(f"成本文件里没读到编码与成本：{Path(costs_file).name}")
    elif costs_file:
        issues.append(f"成本文件不存在：{costs_file}")

    ads: dict[str, float] = {}
    if ads_file and Path(ads_file).exists():
        ads, ad_issues = parse_ads(read_table(ads_file), usd_to_local, ads_fields)
        issues += ad_issues
        if ads:
            sources["广告"] = Path(ads_file).name

    commissions: dict[str, float] = {}
    if commission_file and Path(commission_file).exists():
        commissions, com_issues = parse_commissions(read_table(commission_file), affiliate_fields)
        issues += com_issues
        if commissions:
            sources["联盟佣金"] = Path(commission_file).name

    attach_product_ids(orders, sku_index, issues)
    _mc = attach_costs(orders, costs, issues)

    def _u(kind, label, data, path_obj) -> prove.TableUse:
        rows = len(data or {})
        st = prove.ST_OK if rows else prove.ST_EMPTY
        return prove.TableUse(kind, label, st,
                              [Path(path_obj).name] if path_obj else [], None, rows)

    uses = {
        "order": _u("order", "订单表", {i: 1 for i in range(len(orders))}, order_file),
        "ads": _u("ads", "广告表", ads, ads_file),
        "affiliate": _u("affiliate", "联盟佣金表", commissions, commission_file),
        "mapping": _u("mapping", "商品SKU表", dict.fromkeys(sku_index.by_sku_id, 1), mapping_file),
    }
    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues, sources=sources,
                   order_titles=titles, uses=uses, missing_costs=dict(_mc))


# 每类表的特征字段：只要这几个字段同时出现在同一行，就能确定类型
TABLE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("order", ("order_id", "subtotal_before")),
    ("ads", ("product_id", "ad_cost")),
    ("affiliate", ("product_id", "commission")),
    ("mapping", ("sku_id", "product_id")),
)


def classify_rows(rows: list[tuple], aliases: dict | None = None) -> Optional[str]:
    """按表头特征判断这是一张什么表。

    不靠文件名，因为平台导出的默认文件名千奇百怪
    （"全部 笔订单-2026-10-27-16_59.csv"、"creative data for product campaigns ..."），
    而表头结构是稳定的。

    aliases 可以传该国家配置的列标题候选：不同国家导出的表头不一样
    （马来站叫「订单金额」，菲律宾站可能叫别的），
    手动导入时按所选国家认表才准。
    """
    amap = aliases if aliases else get_aliases()
    for kind, required in TABLE_SIGNATURES:
        hidx, cm = find_header_row(rows, required, aliases=amap)
        if hidx >= 0 and all(k in cm for k in required):
            return kind
    return None


def _scan_folder(folder: Path) -> tuple[dict, list, list]:
    """扫一个目录（递归），把文件按类型分桶。

    递归是为了支持「inbox/orders、inbox/ads…」这种分子目录。
    类型仍然靠**表头**判断，不靠目录名——目录名只是给人看的，
    靠名字认表早晚会认错。
    """
    buckets: dict[str, list[tuple[Path, list[tuple]]]] = {
        "order": [], "ads": [], "affiliate": [], "mapping": []
    }
    json_maps: list[tuple[Path, SkuIndex]] = []
    skipped: list[str] = []
    folder = Path(folder)
    if not folder.is_dir():
        return buckets, json_maps, skipped

    def _mt(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    files = [f for f in folder.rglob("*") if f.is_file()]
    # 按修改时间从旧到新：同一个订单同时出现在「9/1~9/30」和「9/1~10/28」两份导出里时，
    # 后合并的那份会把状态盖上去——必须是**更新**的那份说了算（退款/取消要反映出来）。
    # 以前按文件名排序，恰好把旧的那份排在后面，状态被改回老样子。
    files.sort(key=lambda p: (_mt(p), str(p)))

    for f in files:
        if f.suffix.lower() not in (".xlsx", ".xls", ".csv", ".txt", ".json"):
            continue
        if f.name.startswith("~$") or f.name.startswith("."):
            continue
        # 数据身份证（<原文件名>.srcmeta.json）不是数据表，别拿去解析
        if f.name.endswith(prove.META_SUFFIX):
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
    return buckets, json_maps, skipped


def _find_date_col(header) -> int:
    """表头里找「像日期」的列，找不到返回 -1。

    有日期列时，去重可以精确到「同一个商品同一天」；
    没有就退化成整行内容比对——宁可少去重，也不该把两段不重叠的区间删掉。
    """
    for i, h in enumerate(header):
        s = norm_header(str(h or ""))
        if not s:
            continue
        if any(t in s for t in ("date", "日期", "day", "time", "时间")):
            return i
    return -1


def _merge_same_kind(items: list[tuple[Path, list[tuple]]],
                     kind: str) -> tuple[list[tuple], int, int]:
    """把多份同类表并成一张表。

    返回 (合并后的行, 去掉的重复行数, 同商品但内容不同的行数)。

    去重规则：整行内容相同才算重复。这对「按天/按订单明细」的表正好等于
    「按日期+投放去重」；而对「整个区间汇总成一行」的表，跨区间抓两次
    内容本来就不同，不会被误删——那种表重复计入的情况会在 issues 里提醒。
    """
    if not items:
        return [], 0, 0
    header: list[tuple] | None = None
    merged: list[tuple] = []
    seen: set = set()
    dropped = 0
    same_pid_diff = 0
    pid_seen: dict[str, set] = {}

    for path, rows in items:
        hidx, cm = find_header_row(rows, ("product_id",))
        if hidx < 0:
            continue
        if header is None:
            header = rows[hidx]
        dcol = _find_date_col(header)
        pid_col = cm.get("product_id", -1)
        for r in rows[hidx + 1:]:
            vals = tuple(to_str(x) for x in r)
            key = (vals,)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            merged.append(r)
            pid = to_str(r[pid_col]) if 0 <= pid_col < len(r) else ""
            dv = to_str(r[dcol]) if 0 <= dcol < len(r) else ""
            sig = (pid, dv)
            if pid in pid_seen and sig not in pid_seen[pid]:
                same_pid_diff += 1
            pid_seen.setdefault(pid, set()).add(sig)

    if header is None:
        return [], 0, 0
    return [header] + merged, dropped, same_pid_diff


_RANGE_RE = re.compile(
    r"(\d{4})[-_./]?(\d{2})[-_./]?(\d{2}).{0,12}?(\d{4})[-_./]?(\d{2})[-_./]?(\d{2})"
)


def _d(iso: str) -> date:
    """'2026-09-30' → date，用来比区间宽窄。"""
    return datetime.strptime(iso, "%Y-%m-%d").date()


def table_range(path: Path, rows: list[tuple]) -> tuple[str, str] | None:
    """读出这份导出覆盖的日期区间（'20260901_20261028' / '2026-09-01 00 ~ 2026-09-30 23'）。

    **先看身份证**（上传落盘时写的 <文件名>.srcmeta.json，里面是扩展当时
    请求的区间），再看文件名和首行标题。

    为什么身份证优先：文件名是平台起的，改个名字、以「(2)」结尾、
    或者手动另存一次，区间就没了——那时候这份表会被当成「读不出区间」，
    而「读不出区间」以前是「全部一起读」，正是串月的老路。
    身份证是落盘那一刻写死的，改名也甩不掉。
    """
    meta = prove.read_meta(path)
    rng = meta.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        a, b = str(rng[0] or ""), str(rng[1] or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", b):
            return (a, b) if a <= b else (b, a)

    texts: list[str] = [Path(path).name]
    for r in rows[:3]:
        for c in r:
            if isinstance(c, str) and 0 < len(c) < 160:
                texts.append(c)
    for t in texts:
        for m in _RANGE_RE.finditer(t):
            y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
            if not (2000 <= y1 <= 2100 and 2000 <= y2 <= 2100):
                continue
            if not (1 <= m1 <= 12 and 1 <= m2 <= 12 and 1 <= d1 <= 31 and 1 <= d2 <= 31):
                continue
            a = f"{y1:04d}-{m1:02d}-{d1:02d}"
            b = f"{y2:04d}-{m2:02d}-{d2:02d}"
            if b < a:
                a, b = b, a
            return a, b
    return None


# 文件名尾部的导出时刻：`全部 笔订单-20261001-20261031-2026-11-05-10_18.csv`
# 注意别和前面的区间（20261001-20261031）搞混——这里要求带横线和下划线。
_NAME_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})_(\d{2})\.[A-Za-z]+$")


def export_time(path) -> float:
    """这份表是**哪一刻导出的**。

    为什么不能只看 mtime（2026-11-05 实测踩到）：
    把数据目录拷到另一台电脑、解压一个备份、网盘同步回来——文件的 mtime
    全会变成"复制那一刻"。于是一堆老表看起来一样新（甚至比今天的还新），
    「用最新导出那份」就退化成随便挑一份。测试里 `cp -r` 一次就复现了。

    优先级（前两条都能扛住复制/改名）：
      1. 身份证 `<文件名>.srcmeta.json` 里的 at —— 落盘那一刻写死
      2. 文件名尾部的导出时刻（区间后面那一段 `2026-11-05-10_18`）
      3. 文件 mtime —— 兜底

    返回时间戳（秒）。取不到返回 0，调用方按"最旧"处理。
    """
    try:
        from . import provenance
        _at = str(provenance.read_meta(Path(path)).get("at") or "")
        if _at:
            try:
                return datetime.strptime(_at, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        m = _NAME_TS.search(Path(path).name)
        if m:
            y, mo, d, hh, mm = (int(x) for x in m.groups())
            return datetime(y, mo, d, hh, mm).timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(Path(path).stat().st_mtime)
    except OSError:
        return 0.0


def _rng_text(rng) -> str:
    return f"{rng[0]}~{rng[1]}" if rng else "全量/无区间"


def _mt_text(ts) -> str:
    """把文件时间写成「11-05 10:18」，给「用了哪份、什么时候导的」那句提示用。

    刻意写月日而不是只写区间：文件名里那串日期是**数据范围**，
    不是导出时刻。选文件时比的是导出时刻，提示里也得把它露出来，
    不然人对不上「为什么用的是这份」。
    """
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "时间未知"


def _union_cover(ranges: list[tuple[str, str] | None],
                 window: tuple[str, str],
                 allow_overlap: bool = False) -> list[int] | None:
    """几份区间能不能拼成完整一段。

    为什么要有：计划任务可以选「按周取数」，收件箱里就变成
    10-05~10-11、10-12~10-18、10-19~10-25、10-26~10-31 四份，
    谁都不单独盖住整个 10 月。以前的规则是「一份盖不住就一份都不用」，
    结果是月底按周取过数的店铺直接算不出来——而数据其实是齐的。

    **重叠怎么处理，两类表完全相反**（2026-11-05 分清）：

    · 广告 / 联盟佣金是「按区间汇总」的，一行就是一个商品在这段区间的合计。
      两份重叠的表相加 = 同一段钱算两遍，报表上看不出来，只是净利偏低。
      所以默认 allow_overlap=False：**挑了之后还要再验一遍两两不重叠**，
      有重叠宁可不用。
      （原来的实现只按「谁能把光标推得更远」贪心，压根没验重叠——
       传进两份重叠的周表，它照样拼、照样相加，是个静默的重复计入。）

    · 订单是逐行明细，合并时按「订单号 + SKU」去重、还会重算每单行数，
      重叠部分后取的那份盖掉旧的（后来退款了就该按新的算）。
      所以订单传 allow_overlap=True：重叠不是问题，反而能修正状态。

    返回选中的下标（按时间顺序），拼不成返回 None。
    """
    picked: list[int] = []
    cursor = window[0]
    idx = sorted((i for i, r in enumerate(ranges) if r),
                 key=lambda i: (ranges[i][0], ranges[i][1]))
    while cursor <= window[1]:
        cand = [i for i in idx if ranges[i][0] <= cursor <= ranges[i][1]]
        if not cand:
            return None
        # 从当前缺口开始、能伸到最远的那份
        best = max(cand, key=lambda i: ranges[i][1])
        if best in picked:
            return None
        picked.append(best)
        nxt = (_d(ranges[best][1]) + timedelta(days=1)).isoformat()
        cursor = nxt

    if not allow_overlap:
        rs = sorted((ranges[i] for i in picked), key=lambda r: (r[0], r[1]))
        for i in range(len(rs) - 1):
            if rs[i][1] >= rs[i + 1][0]:
                # 汇总类表重叠 = 重复计入，不能拼
                return None
    return picked


def _pick_latest(items: list[tuple[Path, list[tuple]]], window, label: str,
                 issues: list[str]) -> tuple[list[tuple[Path, list[tuple]]], list[str]]:
    """从一堆同类导出里挑出这次能用的**那一份**。

    规则（2026-11-05 店主定稿，两条必须同时满足）：

      1. **区间百分百对上**——文件写的导出区间要和报表区间一字不差。
         算 10/1~10/7 的报表，只用区间正好是 10/1~10/7 的文件；
         10/1~10/31 那份**不算对得上**，哪怕它盖住了这 7 天。
         区间不同就是另一次取数：广告/佣金是整段汇总，用宽区间会把别的月份
         算进来；订单虽然是明细，但那份报表也不是"这次区间"的导出。
      2. 同样对得上的有几份 → 用**导出时间最新**那份（见 export_time：
         身份证 at → 文件名尾部时刻 → mtime，拷来拷去都不失真）。

    一份区间都不完全相等 → 一份都不用，并列出目录里现有哪些区间。
    宁可不出口报，也不拿别的区间凑。

    唯一的例外：目录里**一份都读不出区间**、而且就一份老文件时，用它并
    在异常页写明（早版本留下的表，没有更好的依据）。

    返回 (选中的文件, 没选中的说明)。
    """
    if not items:
        return [], []

    stamped: list[tuple[float, Path, list[tuple], tuple | None]] = [
        (export_time(p), p, rows, table_range(p, rows)) for p, rows in items
    ]
    stamped.sort(key=lambda x: (x[0], str(x[1])))

    if not window:
        pick = stamped[-1]
        dropped = stamped[:-1]
        if dropped:
            issues.append(f"{label}：只用了最新导出的 {pick[1].name}，"
                          f"忽略 {len(dropped)} 份更早的导出")
        return [(pick[1], pick[2])], [f"{s[1].name}（更早的导出）" for s in dropped]

    lo, hi = window

    # ① 区间完全相等的才算候选；同样相等取导出时间最新的
    exact = [s for s in stamped if s[3] and s[3][0] == lo and s[3][1] == hi]
    if exact:
        exact.sort(key=lambda x: (x[0], str(x[1])))
        pick = exact[-1]
        dropped = [s for s in stamped if s is not pick]
        issues.append(
            f"{label}：用 {pick[1].name}（{_rng_text(pick[3])}，"
            f"导出于 {_mt_text(pick[0])}）"
            + (f"，忽略 {len(dropped)} 份其它导出" if dropped else ""))
        return [(pick[1], pick[2])], [
            f"{s[1].name}（{_rng_text(s[3])}）" for s in dropped
        ]

    ranged = [s for s in stamped if s[3]]

    # ② 一份区间都读不出来：只有唯一一份时才敢用，且写明让人自己核
    if not ranged:
        if len(stamped) == 1:
            issues.append(
                f"{label}：{stamped[0][1].name} 读不出导出区间，按唯一一份使用——"
                f"请自己核对这份是不是 {lo}~{hi} 的")
            return [(stamped[0][1], stamped[0][2])], []
        issues.append(
            f"⚠ {label}：目录里 {len(stamped)} 份都读不出导出区间，分不清是哪个月的，"
            f"这次一份都不用（{lo}~{hi}）。请重新导一次，文件名里带日期")
        return [], [f"{s[1].name}（无区间）" for s in stamped]

    # ③ 有区间、但没有一份正好等于报表区间 → 不用
    have = "、".join(
        f"{s[1].name}（{_rng_text(s[3])}，导出于 {_mt_text(s[0])}）"
        for s in ranged[-3:])
    issues.append(
        f"⚠ {label}：没有区间正好是 {lo}~{hi} 的导出，这次不用任何一份。现有：{have}")
    return [], [f"{s[1].name}（{_rng_text(s[3])}）" for s in stamped]


def _map_rank(rng, window, mtime: float) -> tuple:
    """映射来源的合并顺序：键越大越晚合并 = 优先级越高。

    分三档（用户定的：按区间的优先，全量垫底）：
      2  区间盖住了报表区间 —— 其中**越窄越优先**（最贴合的那份最后合并）
      1  有区间但盖不住（部分重叠、或完全不沾边）
      0  读不出区间（商品管理全量 JSON）—— 只做底，不许盖掉按区间导的表
    同档里再按修改时间，越新越靠后。
    """
    if rng and window:
        lo, hi = window
        if rng[0] <= lo and rng[1] >= hi:
            span = (_d(rng[1]) - _d(rng[0])).days
            return (2, -span, mtime)
        return (1, 0, mtime)
    if rng:
        return (1, 0, mtime)
    return (0, 0, mtime)


def _merge_aggregate(items: list[tuple[Path, list[tuple]]], label: str,
                     window: tuple[str, str] | None = None
                     ) -> tuple[list[tuple], int, int, list[str],
                                tuple[str, str] | None, list[str]]:
    """合并**按区间汇总**的表（广告 / 联盟佣金）。

    这类表没有日期列，一行就是一个商品在整个导出区间的合计。
    两份区间有重叠的表直接相加，等于把同一个月算两遍——
    实测 Chroma Home 就是这样：9/1~9/30 和 9/1~10/28 两份并存，
    广告费从 71,170 变成 105,224，每条链接的净利全部偏低。

    所以先看导出区间（文件名 / 首行标题里都写着）：
      · 区间被另一份完全包住 → 整份丢掉（9 月那份被 9/1~10/28 包住）
      · 区间不重叠 → 相加（9 月一份 + 10 月一份，本来就该加）
      · 部分重叠 → 保留，但明确提示可能重复计入
    读不出区间、又有多份的，退化成「同一商品以最后一份为准」——宁可少算不重复算。

    返回 (合并后的行, 去掉的重复行数, 同商品不同值的行数, 提示,
          这次用的区间, 这次**真正用到的文件名**)。

    最后那一项是给闸门用的：广告费算成 0 有两种完全不同的原因——
    「这份表本来就没有花费」和「这份表根本不是这个月的、被丢掉了」，
    后者必须拦住出表，不能和前者一样静默按 0 计。
    """
    issues: list[str] = []
    if not items:
        return [], 0, 0, issues, None, []

    # ① 多份快照里常常是同一个文件，内容一模一样，先按内容去重
    uniq: list[tuple[Path, list[tuple]]] = []
    seen_sig: set = set()
    for path, rows in sorted(items, key=lambda x: x[0].stat().st_mtime):
        sig = tuple(tuple(to_str(c) for c in r) for r in rows)
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        uniq.append((path, rows))

    # ② 先按本次核算区间挑：刚好盖住这一段的那份优先
    #
    # 比如同时有「9/1~9/30」和「9/1~10/28」两份，算 9 月就该用前者，
    # 用后者的结果是把 10 月的广告也摊进 9 月，净利凭空变低。
    # 这一步必须排在「区间被包住就丢掉」之前——否则小的那份先被丢掉，
    # 就再也挑不到它了。
    ranges = [table_range(p, r) for p, r in uniq]
    keep: list[int] = list(range(len(uniq)))
    skipped: list[str] = []
    if window:
        lo, hi = window
        covering = [i for i, r in enumerate(ranges) if r and r[0] <= lo and r[1] >= hi]
        if covering:
            def _span(i: int) -> tuple:
                a, b = ranges[i]
                # 同样窄就挑**最新**那份：同一个月往往导过好几次（实测一家店
                # 躺着 5 份 10/1~10/31 的广告表），最旧那份是当时的状态，
                # 后来补的导出才包含新加的计划。以前这里只比天数，
                # 并列时按 mtime 升序取第一个 = **最旧**，正好反了。
                try:
                    mt = uniq[i][0].stat().st_mtime
                except OSError:
                    mt = 0.0
                return ((_d(b) - _d(a)).days, -mt)

            best = min(covering, key=_span)
            others = [uniq[j][0].name for j in range(len(uniq)) if j != best]
            keep = [best]
            if others:
                issues.append(f"{label}：按报表区间 {lo}~{hi} 选用了 {uniq[best][0].name}，"
                              f"另有 {len(others)} 份区间不同的表未参与")
        else:
            # 一份都盖不住这次区间 → **一份都不用**。
            #
            # 以前这里 keep 保持全部，于是「收件箱里只有 8 月那份广告表」时算 7 月，
            # 会直接把 8 月的花费当成 7 月的：实测 PHT-W1，8 月表 3,602.16 USD
            # × 63.46 = 228,601 全部记进 7 月报表，广告费一分不差，但月份是错的。
            # 这种错不报任何异常，报表每列都有数。
            known = [i for i in range(len(uniq)) if ranges[i]]
            if known:
                # 按周/按半月取数的店：几份互不重叠的拼起来正好盖住整段
                # （10-05~10-11 + 10-12~10-18 + …）。数据是齐的，不该判死。
                parts = _union_cover(ranges, (lo, hi))
                if parts:
                    keep = parts
                    issues.append(
                        f"{label}：没有单份盖住 {lo}~{hi}，用 {len(parts)} 份不重叠的拼起来——"
                        + "、".join(uniq[i][0].name for i in parts[:4]))
                else:
                    have = "、".join(
                        f"{uniq[i][0].name}（{ranges[i][0]}~{ranges[i][1]}）" for i in known[:3]
                    )
                    issues.append(
                        f"⚠ {label}：没有区间正好是 {lo}~{hi} 的导出，"
                        f"这次不用任何一份。现有：{have}"
                    )
                    return [], 0, 0, issues, None, []
            # 全都读不出区间：**不能「合并全部」**。
            #
            # 合并全部 = 把几个月的数据相加，而且不报错、每列都有数。
            # 手上有几份却认不出是哪个月，唯一安全的选择是不用——
            # 上层会拦住出表，用户重新导一次（文件名带日期）就好。
            if len(uniq) == 1:
                issues.append(
                    f"{label}：{uniq[0][0].name} 读不出导出区间，按唯一一份使用——"
                    f"请自己核对这份是不是 {lo}~{hi} 的")
            else:
                issues.append(
                    f"⚠ {label}：目录里 {len(uniq)} 份都读不出导出区间，分不清是哪个月的，"
                    f"这次一份都不用（{lo}~{hi}）")
                return [], 0, 0, issues, None, []

    # ②' 没有指定区间（或没有一份能盖住整段）时，按区间包含关系去重
    if len(keep) > 1:
        rest: list[int] = []
        for i in keep:
            ri = ranges[i]
            p = uniq[i][0]
            out = False
            if ri:
                for j in keep:
                    if i == j or not ranges[j]:
                        continue
                    q, rj = uniq[j][0], ranges[j]
                    if rj[0] <= ri[0] and rj[1] >= ri[1]:
                        if rj != ri or q.stat().st_mtime > p.stat().st_mtime:
                            out = True
                            break
            if out:
                skipped.append(f"{p.name}（{ri[0]}~{ri[1]} 已被更大区间覆盖）")
            else:
                rest.append(i)
        if rest:
            keep = rest
    if skipped:
        issues.append(f"{label}：跳过了 {len(skipped)} 份区间被覆盖的旧表，避免重复计入——"
                      + "、".join(skipped[:3]))

    kept = [uniq[i] for i in keep]
    kept_ranges = [ranges[i] for i in keep]

    # ③ 部分重叠（谁也不包含谁）没法自动判断该算哪段，如实提示
    overlap = []
    for i in range(len(kept_ranges)):
        for j in range(i + 1, len(kept_ranges)):
            a, b = kept_ranges[i], kept_ranges[j]
            if a and b and a[0] <= b[1] and b[0] <= a[1]:
                overlap.append(f"{a[0]}~{a[1]} 与 {b[0]}~{b[1]}")
    if overlap:
        issues.append(f"{label}：有 {len(overlap)} 组导出区间部分重叠（{'、'.join(overlap[:2])}），"
                      f"重叠那几天的费用可能被算了两遍；建议每次只导一个区间")

    # ④ 行级合并：完全相同的行只留一条
    merged: list[tuple] = []
    seen: set = set()
    dropped = 0
    same_pid_diff = 0
    pid_seen: dict[str, set] = {}
    header: list[tuple] | None = None
    for path, rows in kept:
        hidx, cm = find_header_row(rows, ("product_id",))
        if hidx < 0:
            continue
        if header is None:
            header = rows[hidx]
        pid_col = cm.get("product_id", -1)
        dcol = _find_date_col(header)
        for r in rows[hidx + 1:]:
            vals = tuple(to_str(x) for x in r)
            if vals in seen:
                dropped += 1
                continue
            seen.add(vals)
            merged.append(r)
            pid = to_str(r[pid_col]) if 0 <= pid_col < len(r) else ""
            dv = to_str(r[dcol]) if 0 <= dcol < len(r) else ""
            sig = (pid, dv)
            if pid in pid_seen and sig not in pid_seen[pid]:
                same_pid_diff += 1
            pid_seen.setdefault(pid, set()).add(sig)

    used = None
    _rs = [r for r in kept_ranges if r]
    if _rs:
        used = (min(r[0] for r in _rs), max(r[1] for r in _rs))
    used_files = [p.name for p, _r in kept]
    if header is None:
        return [], dropped, same_pid_diff, issues, used, used_files
    return [header] + merged, dropped, same_pid_diff, issues, used, used_files


def _merge_orders(items: list[tuple[Path, list[tuple]]],
                  order_fields: dict | None) -> tuple[list[OrderLine], int, list[str]]:
    """多份订单表合并。

    去重键：**订单号 + SKU**（同一订单里同一个 SKU 只该有一条）。
    重叠时以**靠后的目录**为准——调用方把目录按从旧到新排好，
                这样后取的状态（比如后来退款了）能反映进净利。
    """
    lines: list[OrderLine] = []
    index: dict[tuple, tuple[int, int]] = {}   # key -> (文件序号, 行序号)
    overlaps = 0
    titles: list[str] = []
    for fi, (path, rows) in enumerate(items):
        for l in parse_orders(rows, order_fields, titles):
            key = (l.order_id, l.sku_id or l.seller_sku or l.product_id)
            hit = index.get(key)
            if hit and hit[0] != fi:
                # 跨文件撞了：同一订单的同一 SKU，后取的那份状态更新（比如后来退款了），
                # 用它盖掉旧的。同一文件内部的重复行不动——那是表本身的样子。
                overlaps += 1
                lines[hit[1]] = l
                index[key] = (fi, hit[1])
            elif not hit:
                index[key] = (fi, len(lines))
                lines.append(l)

    # 合并后**重算**每个订单的行数。
    #
    # 这一步不能省：订单金额是按「订单内 SKU 行数」分摊到每一行的
    # （一个订单 3 行，买家实付就分成 3 份）。而行数是每份文件各自统计的——
    # 一个订单的 3 行如果被切在两个快照里，各算各的就是 1 行和 2 行，
    # 分摊基数错了，买家实付和交易手续费都会跟着偏。
    # 合并成完整的一份之后重新统计，才能跟「整份数据单独算」完全一致。
    per_order = Counter(l.order_id for l in lines)
    for l in lines:
        l.order_line_count = per_order.get(l.order_id, 1) or 1
    return lines, overlaps, titles


def has_orders_table(folder) -> bool:
    """目录里有没有能当订单表用的文件。

    只做分类、不解析内容——核算前用它拦一句"没订单就别算了"，
    比让子进程跑到 loader 抛异常、界面干等十几分钟要好。
    """
    try:
        b, _jm, _sk = _scan_folder(Path(folder))
        return bool(b.get("order"))
    except Exception:  # noqa: BLE001
        return True          # 判不出来就别拦，交给正常流程


def orders_covering(folder, window=None, fresh_since: float | None = None
                    ) -> tuple[bool, str]:
    """目录里有没有**区间正好等于本次报表区间**的订单表，顺便说清现有什么。

    判据和核算里 `_pick_latest` 完全一致：文件写的导出区间必须和报表区间
    一字不差（算 10/1~10/7 就只认 10/1~10/7 的文件），同样相等取最新的。
    这样预检说"有"，核算那边就一定有；两边不会各说一套。

    为什么不能只看"有没有订单文件"（2026-11-05 实测）：
    店里常年留着 6/7/8 月的老导出，算 10 月时 has_orders_table 照样返回
    True，预检放行 → 核算跑到 loader 才因为"没有 10 月的导出"抛错。
    那一步报错走的是"拦截"分支，**不登记待补**，于是订单真到了也没人算，
    卡片永远停在「等订单：到了自动算」——承诺了不兑现。

    fresh_since 给了就再要求这几份里最新的那份是**这次才落的**：
    区间对上但文件是上一轮导的，同样不能用（内容可能是旧的）。

    返回 (有没有, 说明)。window 为空时退化成"有没有订单文件"。
    """
    try:
        b, _jm, _sk = _scan_folder(Path(folder))
    except Exception:  # noqa: BLE001
        return True, ""          # 判不出来就别拦，交给正常流程
    items = b.get("order") or []
    if not items:
        return False, "目录里没有订单表"
    if not window or len(window) != 2:
        return True, ""

    lo, hi = str(window[0]), str(window[1])
    known = []
    for p, rows in items:
        r = table_range(p, rows)
        if r:
            known.append((p, r))
    # 一份区间都读不出来：核算那边会按唯一一份兜底，预检就别拦
    if not known:
        return True, ""

    exact = [p for p, r in known if r[0] == lo and r[1] == hi]
    if not exact:
        known.sort(key=lambda x: x[1][1])
        have = "、".join(f"{p.name}（{r[0]}~{r[1]}）" for p, r in known[:3])
        return False, (f"目录里没有区间正好是 {lo}~{hi} 的订单表"
                       + (f"；现有：{have}" if have else ""))

    if fresh_since:
        newest = max(export_time(p) for p in exact)
        if newest < float(fresh_since):
            names = "、".join(p.name for p in exact[:2])
            return False, (f"有 {lo}~{hi} 的订单表，但还是上一次取的那份"
                           f"（{names}）——这次重新取数的订单还没落地")

    return True, ""


def load_from_dirs(folders: list[Path], costs_file: Optional[Path] = None,
                   usd_to_local: float = 1.0,
                   mapping_file: Optional[Path] = None,
                   order_fields: dict | None = None,
                   cost_fields: dict | None = None,
                   ads_fields: dict | None = None,
                   affiliate_fields: dict | None = None,
                   mapping_fields: dict | None = None,
                   window: tuple[str, str] | None = None,
                   latest_only: bool = True,
                   shop_id: str = "",
                   declared: Optional[set[str]] = None,
                   only: Optional[set[str]] = None) -> RawData:
    """从**多个**目录一起读数据，跨目录合并去重。

    folders 按「从旧到新」排序传入。单个目录时行为和从前一样。
    window 是这次要核算的日期区间（ISO 字符串），用来在几份广告/佣金表里
    挑出最贴合的那一份——这类表是整段汇总的，选错一份就等于把别的月份
    的费用也算进来了。

    shop_id 给了就核一遍身份证：文件落盘时记的是别家店的，一律不用。
    这是「串店」唯一能自动拦住的办法——文件名看不出来，表头更看不出来。

    only 是「只用这几份文件」（按 resolve 后的绝对路径）。界面上点
    「用这批重算」时传进来：那一批取了哪几份就只读哪几份，收件箱里
    后来又落的文件一概不参与——这是唯一能 100% 复现某一次取数的方式。
    """
    folders = [Path(f) for f in folders if Path(f) and Path(f).is_dir()]
    if not folders:
        raise FileNotFoundError("没有可读的数据目录")

    buckets: dict[str, list[tuple[Path, list[tuple]]]] = {
        "order": [], "ads": [], "affiliate": [], "mapping": []
    }
    json_maps: list[tuple[Path, SkuIndex]] = []
    skipped: list[str] = []
    for d in folders:
        b, jm, sk = _scan_folder(d)
        for k in buckets:
            buckets[k].extend(b[k])
        json_maps.extend(jm)
        skipped.extend(sk)

    issues: list[str] = []
    sources: dict[str, str] = {}
    titles: list[str] = []

    # ---- 只认指定的那几份（「用这批重算」）----
    if only:
        def _norm(p) -> str:
            try:
                return str(Path(p).resolve())
            except OSError:
                return str(p)

        allow = {_norm(p) for p in only}
        kept_any = 0
        _cn = {"order": "订单表", "ads": "广告表",
               "affiliate": "联盟佣金表", "mapping": "商品SKU表"}
        for kind in ("order", "ads", "affiliate", "mapping"):
            before = len(buckets[kind])
            buckets[kind] = [(p, r) for p, r in buckets[kind] if _norm(p) in allow]
            kept_any += len(buckets[kind])
            if before and not buckets[kind]:
                issues.append(f"{_cn[kind]}：这一批取数里没有它的文件")
        json_maps = [(p, i) for p, i in json_maps if _norm(p) in allow]
        if not kept_any and not json_maps:
            raise FileNotFoundError(
                "这一批取数的文件都不在了（被删、被移走或被隔离）。"
                "请重新取一次数，或者换一批")

    # 广告 / 佣金实际用的是哪一段区间（这类表没有日期列，只能整段计入）
    ranges: dict = {}
    uses: dict[str, prove.TableUse] = {
        "order": prove.TableUse("order", "订单表"),
        "ads": prove.TableUse("ads", "广告表"),
        "affiliate": prove.TableUse("affiliate", "联盟佣金表"),
        "mapping": prove.TableUse("mapping", "商品SKU表"),
    }

    # ---- 身份证核对：别家店的文件不参与 ----
    if shop_id:
        for kind in ("order", "ads", "affiliate", "mapping"):
            keep_f, wrong = [], []
            for p, rows in buckets[kind]:
                meta = prove.read_meta(p)
                sid = str(meta.get("shop_id") or "").strip()
                if sid and sid != str(shop_id):
                    wrong.append(f"{p.name}（属于 {sid}）")
                else:
                    keep_f.append((p, rows))
            if wrong:
                buckets[kind] = keep_f
                uses[kind].rejected.extend(wrong)
                issues.append(
                    f"⚠ {uses[kind].label}：有 {len(wrong)} 份文件属于别的店铺，"
                    f"这次不用——{'、'.join(wrong[:3])}")
        json_maps = [(p, i) for p, i in json_maps
                     if not (prove.read_meta(p).get("shop_id")
                             and str(prove.read_meta(p).get("shop_id")) != str(shop_id))]

    # 订单 / 广告 / 佣金一律只用**最新导出的那一份**（用户要求：不复用旧文件）。
    # 显式指定多份快照要合并时（--snapshots）例外，那种场景就是要拼起来。
    if latest_only:
        for kind, label in (("order", "订单表"), ("ads", "广告表"),
                            ("affiliate", "联盟佣金表")):
            picked, rejected = _pick_latest(buckets[kind], window, label, issues)
            buckets[kind] = picked
            uses[kind].rejected.extend(rejected)
            if not picked and rejected and kind != "order":
                # 有文件但一份都没选上：这是「区间不对」，不是「没有」。
                # 具体有哪些区间、哪份更新，_pick_latest 已经写进 issues 了。
                uses[kind].status = prove.ST_OUT_OF_RANGE

    # ---- 订单：多份合并去重 ----
    orders: list[OrderLine] = []
    if buckets["order"]:
        # 先按本次报表区间筛文件：区间完全不沾边的老导出不必读进来
        # （大店一份就 40MB，读一遍十几秒，而且会让"订单怎么到 10/28"更费解）
        if window:
            lo, hi = window
            keep_o, drop_o = [], []
            for of, orows in buckets["order"]:
                r = table_range(of, orows)
                if r and (r[1] < lo or r[0] > hi):
                    drop_o.append(f"{of.name}（{r[0]}~{r[1]}）")
                else:
                    keep_o.append((of, orows))
            if drop_o and keep_o:
                buckets["order"] = keep_o
                uses["order"].rejected.extend(drop_o)
                issues.append("订单表：跳过了 %d 份与报表区间 %s~%s 不重叠的导出——%s"
                              % (len(drop_o), lo, hi, "、".join(drop_o[:3])))

        # 记下订单数据实际覆盖的区间：报表页要拿它跟统计区间对照
        _ors = [table_range(p, r) for p, r in buckets["order"]]
        _ors = [x for x in _ors if x]
        if _ors:
            ranges["订单表"] = (min(x[0] for x in _ors), max(x[1] for x in _ors))

        orders, overlaps, titles = _merge_orders(buckets["order"], order_fields)
        names = " + ".join(p.name for p, _ in buckets["order"])
        sources["订单"] = names
        uses["order"].files = [p.name for p, _ in buckets["order"]]
        uses["order"].range = ranges.get("订单表")
        uses["order"].rows = len(orders)
        uses["order"].status = prove.ST_OK if orders else prove.ST_EMPTY
        if len(buckets["order"]) > 1:
            msg = (f"订单表合并：{len(buckets['order'])} 份快照共 "
                   f"{len(orders) + overlaps} 行，去重 {overlaps} 行（重叠部分以最新快照为准）")
            issues.append(msg)
    else:
        uses["order"].status = (
            prove.ST_OUT_OF_RANGE if uses["order"].rejected else prove.ST_MISSING)
        # 说清楚是"根本没有订单表"还是"有、但都不覆盖这次区间"——
        # 后者是这次才拦下来的：以前会拿别的月份凑合算，出的是废表。
        _why = next((x for x in issues if x.startswith("⚠ 订单表")), "")
        if _why:
            raise FileNotFoundError(_why)
        raise FileNotFoundError(
            f"目录里没识别出订单表：{folders[0]}\n"
            f"（跳过：{'、'.join(skipped) if skipped else '无'}）"
        )

    # ---- 映射：本轮导出的 SKU 表是**第一选择**，累积主表只做兜底 ----
    #
    # 用户定的优先级，不能颠倒：
    #   ① 这一轮取数刚导的「商品数据分析 → 按 SKU」表（**区间对得上**的那份）
    #   ② 以前导过的 SKU 表 / 抓包 JSON（越新越优先）
    #   ③ 累积主表 master.csv —— 只在 SKU 表里查不到这个 sku_id 时才用
    #
    # 「区间对得上」怎么判：SKU 表第一行就写着它导的是哪一段
    # （`2026-09-01 ~ 2026-09-30`），按这个跟报表区间比。
    # 能盖住报表区间的排最后合并（数字越大越晚 = 优先级越高），
    # 其中区间更窄的又更优先；带不上区间的（商品管理全量 JSON）排最前只做底。
    sku_index = SkuIndex()
    parts: list[str] = []
    fresh: list[tuple[tuple, str, SkuIndex, str]] = []
    for mf, mrows in buckets["mapping"]:
        tidx = parse_mapping(mrows, mapping_fields)
        if len(tidx):
            try:
                mt = mf.stat().st_mtime
            except OSError:
                mt = 0.0
            rng = table_range(mf, mrows)
            fresh.append((_map_rank(rng, window, mt), mf.name, tidx, _rng_text(rng)))
        else:
            issues.append(f"映射表里没识别出 SKU ID / Product ID 列：{mf.name}")
    for jf, jidx in json_maps:
        try:
            jt = jf.stat().st_mtime
        except OSError:
            jt = 0.0
        # 商品管理全量 JSON 不带区间：只能垫底，不能盖掉按区间导的表
        fresh.append((_map_rank(None, window, jt), jf.name, jidx, "全量"))

    if not fresh:
        issues.append(
            "本轮没有导到「商品SKU表」：订单行只能靠历史映射认链接，"
            "新上架的 SKU 会归到「无ID」。取数时这一步不该被跳过"
        )
    for _key, _name, _idx, _rng in sorted(fresh, key=lambda x: x[0]):
        sku_index.merge(_idx, override=True)      # 排在后面的赢
        parts.append(f"{_name}（{_rng}）")

    # 看看最后真正说了算的是哪一份，写进异常页——归属错了要能一眼看出用的是谁
    if fresh and window:
        _best = sorted(fresh, key=lambda x: x[0])[-1]
        issues.append(
            f"链接归属用的是 {_best[1]}（区间 {_best[3]}，报表区间 {window[0]}~{window[1]}）"
        )

    if mapping_file and Path(mapping_file).exists():
        midx = parse_mapping(read_table(Path(mapping_file)), mapping_fields)
        if len(midx):
            sku_index.merge(midx)                 # 只补缺，绝不覆盖新表
            parts.append(f"{Path(mapping_file).name}（兜底）")
        else:
            issues.append(f"映射主表里没识别出 SKU ID / Product ID 列：{Path(mapping_file).name}")

    if parts:
        sources["映射"] = " + ".join(parts)
        uses["mapping"].files = [n for _k, n, _i, _r in sorted(fresh, key=lambda x: x[0])]
        if mapping_file and Path(mapping_file).exists():
            uses["mapping"].files.append(Path(mapping_file).name)
        uses["mapping"].rows = len(getattr(sku_index, "by_sku_id", {}) or {})
        uses["mapping"].status = prove.ST_OK if uses["mapping"].rows else prove.ST_EMPTY
    else:
        uses["mapping"].status = (
            prove.ST_EMPTY if buckets["mapping"] or json_maps else prove.ST_MISSING)

    # 这一轮的 SKU 表是不是新导的？拿它跟本轮订单表的修改时间比。
    #
    # 注意时间差要给足：取数顺序是先导 SKU 表、再导订单，而大店订单在平台
    # 生成要 8 分钟（Chroma 实测），所以订单表天生比 SKU 表"新"好几分钟。
    # 阈值只用来抓「这一天压根没取 SKU 表」的情况，2 小时足够。
    if fresh and buckets["order"]:
        map_times: list[float] = []
        for _mf, _r in buckets["mapping"]:
            try:
                map_times.append(_mf.stat().st_mtime)
            except OSError:
                pass
        for _jf, _i in json_maps:
            try:
                map_times.append(_jf.stat().st_mtime)
            except OSError:
                pass
        times = []
        for of, _r in buckets["order"]:
            try:
                times.append(of.stat().st_mtime)
            except OSError:
                pass
        if times and map_times and max(map_times) < max(times) - 7200:
            issues.append(
                "这次没有新导的「商品SKU表」（比订单表还旧），链接归属用的是历史数据；"
                "本期新上架的 SKU 会归到「无ID」，重新取一次数即可"
            )

    # ---- 成本 ----
    costs: dict[str, float] = {}
    if costs_file and Path(costs_file).exists():
        cpath = Path(costs_file)
        if cpath.is_dir():
            for cf in cpath.iterdir():
                if cf.suffix.lower() in (".xlsx", ".xls", ".csv") and not cf.name.startswith("~$"):
                    c = parse_costs(read_table(cf, sheet="成本") if cf.suffix.lower().startswith(".xls") else read_table(cf),
                                    cost_fields)
                    if c:
                        costs.update(c)
                        sources["成本"] = cf.name
                        break
        else:
            rows = read_table(cpath, sheet="成本") if cpath.suffix.lower().startswith(".xls") else read_table(cpath)
            costs = parse_costs(rows, cost_fields)
            sources["成本"] = cpath.name
        if not costs:
            issues.append(f"成本文件里没读到编码与成本：{costs_file}")
    elif costs_file:
        issues.append(f"成本文件不存在：{costs_file}")

    # ---- 广告 / 联盟：多份合并去重后汇总 ----
    #
    # 关键：`sources[...]` 只在**数据真的用上了**的时候才写。
    # 以前不管结果是不是空的都写，于是「广告表在、但被区间筛掉了」这种情况
    # 在 sources 里看着像「有广告数据」，缺表闸门直接放行，广告费按 0 计，
    # 净利虚高还没人知道。闸门现在看的是 uses，这里必须保持一致。
    ads: dict[str, float] = {}
    if buckets["ads"]:
        (mrows, dropped, diff, agg_issues,
         used, used_files) = _merge_aggregate(buckets["ads"], "广告表", window)
        issues += agg_issues
        used_files = used_files or []
        ads, ad_issues = parse_ads(mrows, usd_to_local, ads_fields)
        issues += ad_issues
        if used:
            ranges["广告表"] = used
        u = uses["ads"]
        u.files = used_files
        u.range = used
        u.rows = len(ads)
        if not used_files:
            u.status = prove.ST_OUT_OF_RANGE
        elif not ads:
            u.status = prove.ST_EMPTY
        else:
            u.status = prove.ST_OK
            sources["广告"] = " + ".join(used_files)
            _unk = last_unknown_ads()
            if _unk:
                # 币种认不出 = 这块钱按 0 计了。报表上看不出任何异常，
                # 但净利实打实偏高——必须拦住，让人去「参数」页把广告账户币种填对。
                u.blocker = (f"广告费里有 {_unk:,.2f} 的金额币种认不出，"
                             f"已被按 0 计入（净利会偏高）。"
                             f"请到「参数 → 广告账号币种」选对币种后重算")
            if len(buckets["ads"]) > 1:
                issues.append(f"广告表合并：{len(buckets['ads'])} 份快照，去重 {dropped} 行")
                if diff:
                    issues.append(f"广告表有 {diff} 行「同商品+同日期但金额不同」，"
                                  f"若该表是按区间汇总而非按天明细，请核对是否重复计入")

    commissions: dict[str, float] = {}
    if buckets["affiliate"]:
        (mrows, dropped, diff, agg_issues,
         used, used_files) = _merge_aggregate(
            buckets["affiliate"], "联盟佣金表", window)
        issues += agg_issues
        used_files = used_files or []
        commissions, com_issues = parse_commissions(mrows, affiliate_fields)
        issues += com_issues
        if used:
            ranges["联盟佣金表"] = used
        u = uses["affiliate"]
        u.files = used_files
        u.range = used
        u.rows = len(commissions)
        if not used_files:
            u.status = prove.ST_OUT_OF_RANGE
        elif not commissions:
            u.status = prove.ST_EMPTY
        else:
            u.status = prove.ST_OK
            sources["联盟佣金"] = " + ".join(used_files)
            if len(buckets["affiliate"]) > 1:
                issues.append(f"联盟佣金表合并：{len(buckets['affiliate'])} 份快照，去重 {dropped} 行")

    # 没被采用的同类文件，记进 rejected（出表被拦时原样展示，避免"我明明导了"）
    for kind in ("ads", "affiliate"):
        u = uses[kind]
        if buckets[kind]:
            if not u.files:
                u.rejected = [f"{p.name}（区间对不上，未采用）" for p, _ in buckets[kind]]
            else:
                u.rejected.extend(f"{p.name}（本次未采用）" for p, _ in buckets[kind]
                                  if p.name not in u.files)
        u.rejected = list(dict.fromkeys(u.rejected))

    for kind, label in (("ads", "广告表"), ("affiliate", "联盟佣金表"), ("mapping", "商品映射表")):
        if buckets[kind]:
            continue
        # 映射表现在是全局主表（数据/链接映射/master.csv），不放在店目录里，
        # 所以「店目录没有映射表」是正常的——只要已经有 SKU→商品 数据就不该报警。
        if kind == "mapping":
            if len(getattr(sku_index, "by_sku_id", {}) or {}):
                continue
            issues.append(
                "没有商品SKU表（SKU ID → 商品ID）：订单表里本来就没有链接ID，"
                "少了这张表所有订单只能归到「无ID」，看不出哪条链接赚了钱。"
                "扩展取商品数据时会自动导出这张表，存到店铺目录下的「商品SKU表」"
            )
            continue
        # 有文件但被区间筛掉的，别说成"没识别出"——那会让人以为文件坏了
        if uses[kind].rejected:
            continue
        tgt = "ads" if kind == "ads" else "affiliate"
        if uses[tgt].status not in (prove.ST_DECLARED,):
            uses[tgt].status = prove.ST_MISSING
        issues.append(f"目录里没识别出{label}，相关字段按 0 计")

    attach_product_ids(orders, sku_index, issues)
    missing_costs = attach_costs(orders, costs, issues)

    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues, sources=sources,
                   missing_costs=dict(missing_costs),
                   order_titles=titles, ranges=ranges, uses=uses,
                   declared=set(declared or ()), window=window)


def load_from_dir(folder: Path, costs_file: Optional[Path] = None,
                  usd_to_local: float = 1.0,
                  mapping_file: Optional[Path] = None,
                  order_fields: dict | None = None,
                  cost_fields: dict | None = None,
                  ads_fields: dict | None = None,
                  affiliate_fields: dict | None = None,
                  mapping_fields: dict | None = None,
                  shop_id: str = "",
                  declared: Optional[set[str]] = None) -> RawData:
    """从目录自动识别四类文件（单个目录的快捷方式）。"""
    return load_from_dirs([Path(folder)], costs_file, usd_to_local, mapping_file,
                          order_fields, cost_fields,
                          ads_fields, affiliate_fields, mapping_fields,
                          shop_id=shop_id, declared=declared)


def load_from_workbook(path: Path, usd_to_local: float = 1.0,
                       costs_file: Optional[Path] = None,
                       order_fields: dict | None = None,
                       cost_fields: dict | None = None,
                       ads_fields: dict | None = None,
                       affiliate_fields: dict | None = None,
                       mapping_fields: dict | None = None) -> RawData:
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

    titles: list[str] = []
    orders = parse_orders(order_rows, order_fields, titles)
    sku_index = parse_mapping(link_rows, mapping_fields)

    costs = parse_costs(cost_rows, cost_fields)
    # 如果 data/costs 里有更新的一份，用它覆盖工作簿里的成本表
    if costs_file and Path(costs_file).exists():
        cf = Path(costs_file)
        rows = read_table(cf, sheet="成本") if cf.suffix.lower().startswith(".xls") else read_table(cf)
        override = parse_costs(rows, cost_fields)
        if override:
            costs = override
    ads, ad_issues = parse_ads(ad_rows, usd_to_local, ads_fields)
    commissions, com_issues = parse_commissions(com_rows, affiliate_fields)
    issues += ad_issues + com_issues

    attach_product_ids(orders, sku_index, issues)
    missing_costs = attach_costs(orders, costs, issues)

    # 老工作簿没有导出区间（整本就是一个区间），闸门只按「有没有数据」判
    uses = {
        "order": prove.TableUse("order", "订单表", prove.ST_OK if orders else prove.ST_EMPTY,
                                [path.name], None, len(orders)),
        "ads": prove.TableUse("ads", "广告表", prove.ST_OK if ads else prove.ST_EMPTY,
                              [path.name], None, len(ads)),
        "affiliate": prove.TableUse("affiliate", "联盟佣金表",
                                    prove.ST_OK if commissions else prove.ST_EMPTY,
                                    [path.name], None, len(commissions)),
        "mapping": prove.TableUse("mapping", "商品SKU表",
                                  prove.ST_OK if len(sku_index.by_sku_id) else prove.ST_EMPTY,
                                  [path.name], None, len(sku_index.by_sku_id)),
    }
    return RawData(orders=orders, costs=costs, sku_index=sku_index, ads=ads,
                   commissions=commissions, issues=issues,
                   sources={"工作簿": path.name}, order_titles=titles, uses=uses,
                   missing_costs=dict(missing_costs))
