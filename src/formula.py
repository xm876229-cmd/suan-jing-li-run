"""公式与费用计算：让口径变成配置，而不是写死在代码里。

用户的原话：「核心开放开放还是开放」。所以这里做三件事：

1. 订单过滤开关（config/formulas.json 的 order_filter）
   已取消 / 退货退款 / 未付款 / 0元样品，各自独立选择计不计入。
   为什么要分开：发单费按订单数算，0 元样品加不加直接影响成本，
   所以不能"一刀切按金额判断"，要按状态 + 开关。

2. 费用项列表（fees）
   每一项只描述「基数 × 费率」，用户想加就加：
       {"id":"order_service","label":"订单服务费","base":"order_count","rate":2.0}
   基数可选 gmv / buyer_paid / order_count / items / cost / refund
   以及 commission（达人佣金）与 ads（广告花费）这种"直接取数"的。
   rate 可以是数字，也可以是 params.xxx（引用 per-国家 参数）。

3. 公式（formulas）
   引用变量名与费用 id，不引用列位置：
       "net": "gross_margin - sum(fees)"
   列位置变了只要改列映射（config/mapping.json），公式一个字不用动。

表达式求值是白名单实现（只认数字、四则运算、括号、变量、少数函数），
不是 eval，避免配置写错就把程序搞崩。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

FORMULAS_FILE_NAME = "formulas.json"


# ---------------- 表达式求值（白名单） ----------------

class FormulaError(Exception):
    """公式写错了。消息里要能看懂错在哪，不能只抛 SyntaxError。"""


_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b if b else 0.0,
    ast.Mod: lambda a, b: a % b if b else 0.0,
    ast.Pow: lambda a, b: a ** b,
}

_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

_MAX_POW = 8          # 防止 "9**9**9" 把内存吃光
_MAX_EXPR_LEN = 500   # 公式不该写这么长


def _to_num(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return 0.0


class _Evaluator(ast.NodeVisitor):
    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.used: set[str] = set()

    def eval(self, node: ast.AST) -> Any:
        return self.visit(node)

    # --- 基本节点 ---
    def visit_Expression(self, node: ast.Expression):
        return self.eval(node.body)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise FormulaError(f"公式里不能出现字符串/None：{node.value!r}")

    def visit_Name(self, node: ast.Name):
        name = node.id
        if name not in self.ctx:
            raise FormulaError(f"公式里用了未知变量「{name}」")
        self.used.add(name)
        return self.ctx[name]

    def visit_Attribute(self, node: ast.Attribute):
        base = self.eval(node.value)
        if isinstance(base, dict):
            if node.attr not in base:
                raise FormulaError(f"参数「{node.attr}」不存在")
            return base[node.attr]
        # 对象属性（Params dataclass）
        if hasattr(base, node.attr):
            return getattr(base, node.attr)
        raise FormulaError(f"参数「{node.attr}」不存在")

    def visit_BinOp(self, node: ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise FormulaError(f"不支持的运算符：{type(node.op).__name__}")
        a, b = self.eval(node.left), self.eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(_to_num(b)) > _MAX_POW:
            raise FormulaError(f"幂次太大（{b}），最多 {_MAX_POW}")
        return op(_to_num(a), _to_num(b))

    def visit_UnaryOp(self, node: ast.UnaryOp):
        v = _to_num(self.eval(node.operand))
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        raise FormulaError("不支持的负号运算")

    def visit_Compare(self, node: ast.Compare):
        left = self.eval(node.left)
        for op_node, comp in zip(node.ops, node.comparators):
            fn = _CMP_OPS.get(type(op_node))
            if fn is None:
                raise FormulaError("不支持的比较运算")
            right = self.eval(comp)
            if not fn(_to_num(left), _to_num(right)):
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp):
        return self.eval(node.body) if self.eval(node.test) else self.eval(node.orelse)

    def visit_Call(self, node: ast.Call):
        fn = node.func
        if not isinstance(fn, ast.Name):
            raise FormulaError("只能调用 sum/min/max/abs/round")
        name = fn.id
        args = [self.eval(a) for a in node.args]

        if name == "sum":
            if len(args) == 1 and isinstance(args[0], dict):
                return sum(_to_num(v) for v in args[0].values())
            total = 0.0
            for a in args:
                if isinstance(a, dict):
                    total += sum(_to_num(v) for v in a.values())
                elif isinstance(a, (list, tuple)):
                    total += sum(_to_num(v) for v in a)
                else:
                    total += _to_num(a)
            return total
        if name == "abs":
            return abs(_to_num(args[0])) if args else 0.0
        if name == "min":
            return min(_to_num(a) for a in args) if args else 0.0
        if name == "max":
            return max(_to_num(a) for a in args) if args else 0.0
        if name == "round":
            n = _to_num(args[0]) if args else 0.0
            d = int(_to_num(args[1])) if len(args) > 1 else 0
            return round(n, d)
        raise FormulaError(f"不支持的函数「{name}」")

    def visit_List(self, node: ast.List):
        return [self.eval(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple):
        return tuple(self.eval(e) for e in node.elts)

    def generic_visit(self, node):
        raise FormulaError(f"公式里有不支持的写法：{type(node).__name__}")


def safe_eval(expr: str, ctx: dict) -> tuple[float, set[str]]:
    """求值一个公式，返回 (结果, 用到的变量名)。"""
    text = str(expr or "").strip()
    if not text:
        return 0.0, set()
    if len(text) > _MAX_EXPR_LEN:
        raise FormulaError(f"公式太长（{len(text)} 字符，上限 {_MAX_EXPR_LEN}）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"公式语法错：{text}（{e.msg}）") from None
    ev = _Evaluator(ctx)
    val = ev.eval(tree)
    return _to_num(val), ev.used


# ---------------- 配置读取 ----------------

# 订单表里程序需要的几列。默认给中英双语候选，
# 某国表头特殊就在「计算参数 → 订单表识别」里补一条，不用改代码。
DEFAULT_ORDER_FIELDS: dict[str, dict] = {
    "order_id": {"label": "订单号", "titles": ["Order ID", "订单号", "平台订单号"]},
    "status": {"label": "订单状态", "titles": ["Order Status", "订单状态"]},
    "cancel": {"label": "取消/退货类型", "titles": [
        "Cancelation/Return Type", "Cancellation/Return Type", "取消/退货类型", "取消退货类型"]},
    "sku_id": {"label": "SKU ID", "titles": ["SKU ID"]},
    "seller_sku": {"label": "卖家编码（成本表按它对）", "titles": [
        "Seller SKU", "卖家SKU", "商家SKU"]},
    "product_id": {"label": "商品ID", "titles": ["Product ID", "商品ID"]},
    "product_name": {"label": "商品名称", "titles": ["Product Name", "商品名称"]},
    "qty": {"label": "购买数量", "titles": ["Quantity", "数量", "购买数量"]},
    "subtotal_before": {"label": "折扣前小计", "titles": [
        "SKU Subtotal Before Discount", "折扣前小计", "SKU折扣前小计"]},
    "seller_discount": {"label": "卖家折扣", "titles": [
        "SKU Seller Discount", "卖家折扣", "SKU卖家折扣"]},
    "order_amount": {"label": "买家实付", "titles": ["Order Amount", "订单金额", "买家实付"]},
    "refund_amount": {"label": "退款金额", "titles": ["Order Refund Amount", "退款金额"]},
    "created": {"label": "下单时间", "titles": ["Created Time", "创建时间", "下单时间"]},
    "paid": {"label": "付款时间", "titles": ["Paid Time", "付款时间", "支付时间"]},
}

# 成本表：哪一列是商品编码、哪一列是成本
DEFAULT_COST_FIELDS: dict[str, dict] = {
    "code": {"label": "商品编码", "titles": ["商品编码", "编码", "Seller SKU", "卖家SKU", "SKU"]},
    "cost": {"label": "成本（人民币）", "titles": ["成本", "成本价", "采购成本", "Cost"]},
}

DEFAULTS: dict = {
    "kinds": {
        "cancelled": "已取消订单",
        "refunded": "退货退款订单",
        "zero_amount": "0 元样品单",
        "unpaid": "未付款订单",
        "normal": "正常订单",
    },
    "order_fields": DEFAULT_ORDER_FIELDS,
    "cost_fields": DEFAULT_COST_FIELDS,
    # 计算参数表：每一项就是一条算式，公式里直接写订单表的列标题。
    "params": [
        {"id": "gmv", "label": "GMV（收入）", "role": "gmv",
         "expr": "[折扣前小计|SKU Subtotal Before Discount|SKU折扣前小计]"
                 " - [卖家折扣|SKU Seller Discount|SKU卖家折扣]",
         "kinds": ["normal", "zero_amount"]},
        {"id": "cost", "label": "商品成本", "role": "cost",
         "expr": "cost_cny * params.cny_to_local",
         "kinds": ["normal"]},
        {"id": "order_count", "label": "订单数", "role": "order_count",
         "expr": "orders",
         "kinds": ["normal"]},
        {"id": "items", "label": "件数", "role": "items",
         "expr": "qty",
         "kinds": ["normal"]},
        {"id": "buyer_paid", "label": "买家实付", "role": "buyer_paid",
         "expr": "paid",
         "kinds": ["normal", "zero_amount"]},
        {"id": "dispatch", "label": "发单费",
         "expr": "orders * params.dispatch_fee_cny * params.cny_to_local",
         "kinds": ["normal"]},
        {"id": "platform_commission", "label": "平台佣金",
         "expr": "gmv * params.platform_commission",
         "kinds": ["normal"]},
        {"id": "tx_fee", "label": "交易手续费",
         "expr": "paid * params.tx_fee"},
        {"id": "ads", "label": "广告花费", "expr": "ads"},
        {"id": "cashback", "label": "奖金返现",
         "expr": "gmv * params.cashback", "kinds": ["normal"]},
        {"id": "affiliate", "label": "达人佣金", "expr": "commission"},
        {"id": "order_service", "label": "订单服务费",
         "expr": "orders * params.order_service",
         "kinds": ["normal"]},
    ],
    "by_country": {},
    "formulas": {
        "gross_profit": "gmv - cost",
        "total_fees": ("dispatch + platform_commission + tx_fee + cashback"
                       " + ads + affiliate + order_service"),
        "net": ("gmv - cost - dispatch - platform_commission - tx_fee - cashback"
                " - ads - affiliate - order_service"),
        "net_rate": "net / gmv",
    },
}


def load_config(path: Path) -> dict:
    """读 formulas.json。缺失/损坏就用 DEFAULTS（保证还能算）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULTS))

    out = json.loads(json.dumps(DEFAULTS))
    if isinstance(doc.get("order_filter"), dict):
        # 这里刻意不做白名单过滤：order_filter 支持「按类别分组」的嵌套结构，
        # 之前只收 include_xxx 四个键，新结构会被整个丢掉，
        # 表现是「改配置毫无反应」——这个坑不好查，所以改成整体接受。
        for k, v in doc["order_filter"].items():
            out["order_filter"][k] = v
    # params = 参数表（GMV / 成本 / 各项费用都在这张表里，每项自己选订单类别）
    # fees 是早期的名字，保留兼容：有 params 就用 params
    if isinstance(doc.get("params"), list) and doc["params"]:
        out["params"] = doc["params"]
    elif isinstance(doc.get("fees"), list) and doc["fees"]:
        out["params"] = doc["fees"]
    if isinstance(doc.get("kinds"), dict) and doc["kinds"]:
        out["kinds"] = doc["kinds"]
    for key in ("order_fields", "cost_fields"):
        if isinstance(doc.get(key), dict) and doc[key]:
            merged = dict(out[key])
            for k, v in doc[key].items():
                merged[k] = v          # 单个字段可以只改一条，其余沿用默认
            out[key] = merged
    # 每个国家自己的一套：{国家: {params: [...], order_fields: {...}, cost_fields: {...}}}
    if isinstance(doc.get("by_country"), dict) and doc["by_country"]:
        out["by_country"] = doc["by_country"]
    if isinstance(doc.get("formulas"), dict) and doc["formulas"]:
        out["formulas"].update(doc["formulas"])
    return out


def country_config(fcfg: dict, country: str) -> dict:
    """取某个国家实际生效的配置：该国有单独设置就用它的，没有就退回通用那套。

    返回结构的键与 fcfg 一致（params / order_fields / cost_fields / formulas），
    方便调用方直接当一份配置用。
    """
    base = {
        "params": list(fcfg.get("params") or []),
        "order_fields": dict(fcfg.get("order_fields") or {}),
        "cost_fields": dict(fcfg.get("cost_fields") or {}),
        "formulas": dict(fcfg.get("formulas") or {}),
    }
    one = (fcfg.get("by_country") or {}).get(country) or {}
    if isinstance(one.get("params"), list) and one["params"]:
        base["params"] = list(one["params"])
    for key in ("order_fields", "cost_fields"):
        if isinstance(one.get(key), dict):
            base[key].update(one[key])
    if isinstance(one.get("formulas"), dict):
        base["formulas"].update(one["formulas"])
    return base


# ---------------- 公式里的「标题」→ 数值 ----------------
#
# 规矩很简单：公式里直接写订单表的列标题，映射就写在公式里。
#     [折扣前小计|SKU Subtotal Before Discount] - [卖家折扣]
# 方括号里用 | 分隔多个候选标题：第一个在订单表里找不到，就试第二个，
# 一直试到找到为止。所以平台改列名时只改公式，不用改代码。

TITLE_RE = re.compile(r"\[([^\[\]]+)\]")


def _norm(s) -> str:
    """和 loader.norm_header 同一套归一规则：去空白/下划线/连字符、全角转半角、小写。"""
    t = str(s or "").strip()
    t = t.replace("\u3000", " ").translate(str.maketrans("　（）／", " ()/"))
    return re.sub(r"[\s_\-]+", "", t).lower()


def compile_expr(expr: str, titles: list[str] | None = None) -> tuple[str, dict[str, list[str]]]:
    """把公式里的标题换成安全的变量名。

    返回 (可求值的表达式, {变量名: [候选标题(归一化)]})。
    两种写法都认：
        [折扣前小计|SKU Subtotal]   显式，可写多个候选
        折扣前小计                   裸写，只要这个标题在订单表里存在
    """
    groups: dict[str, list[str]] = {}

    def var_for(cands: list[str]) -> str:
        var = f"__t{len(groups)}"
        groups[var] = cands
        return var

    def repl(m: re.Match) -> str:
        cands = [_norm(c) for c in m.group(1).split("|") if c.strip()]
        cands = [c for c in cands if c]
        return var_for(cands) if cands else "0"

    text = TITLE_RE.sub(repl, str(expr or ""))

    # 裸写标题也认：用户直接写 `折扣前小计 - 卖家折扣` 同样能算。
    # 按标题长度倒序替换，避免「数量」先命中把「购买数量」拆坏。
    pairs = sorted({(str(t).strip(), _norm(t)) for t in (titles or []) if str(t).strip()},
                   key=lambda p: -len(p[0]))
    if pairs:
        alt = "|".join(re.escape(raw) for raw, _ in pairs)
        pat = re.compile(r"(?<![A-Za-z0-9_.])(" + alt + r")(?![A-Za-z0-9_])", re.IGNORECASE)
        lookup = {raw.lower(): n for raw, n in pairs}

        def sub(m: re.Match) -> str:
            n = lookup.get(m.group(1).lower())
            if not n:
                return m.group(0)
            for var, cands in groups.items():   # 同一个标题只建一个变量
                if cands == [n]:
                    return var
            return var_for([n])

        text = pat.sub(sub, text)
    return text, groups


def _pick_kinds(by_kind: dict, kinds) -> dict[str, float]:
    """把指定订单类别的原始量加总。

    kinds=None 表示「全部类别」；[] 表示「一项都不算」（结果全是 0）。
    逐个类别相加而不是取汇总值，是为了让每一项参数各自挑类别——
    「发单费只算正常单和退货单」这类口径才能成立。
    """
    if kinds is None:
        chosen = list(by_kind.keys())
    else:
        chosen = list(kinds or [])
    agg: dict[str, float] = {}
    for k in chosen:
        d = by_kind.get(k)
        if not isinstance(d, dict):
            continue
        for kk, vv in d.items():
            agg[kk] = agg.get(kk, 0.0) + _to_num(vv)
    return agg


# 对外名字：calc 等模块要按类别取原始字段合计
pick_kinds = _pick_kinds


def _find_title(agg: dict[str, float], cands: list[str]) -> tuple[float, str | None]:
    """按候选顺序找列：先精确匹配，再退一步做包含匹配（只接受唯一命中）。"""
    for c in cands:
        if c in agg:
            return _to_num(agg[c]), c
    for c in cands:
        hits = [k for k in agg if c and (c in k or k in c)]
        if len(hits) == 1:
            return _to_num(agg[hits[0]]), hits[0]
    return 0.0, None


def _title_value(agg: dict[str, float], aliases: dict, key: str) -> float:
    """按「字段 → 标题候选」取一个基础字段的合计（gmv/实付这类派生量要用）。"""
    cands = [_norm(c) for c in (aliases.get(key) or []) if str(c).strip()]
    got, hit = _find_title(agg, cands)
    return got if hit else 0.0


def _local_ctx(by_kind: dict, kinds, ctx: dict,
               groups: dict[str, list[str]]) -> tuple[dict, list[str]]:
    """准备一条参数公式的可用变量。

    两类变量：
      · 订单表的列：公式里写 [标题] 就取这一列在选定订单类别上的合计
      · 派生量：gmv / paid / qty / orders / cost_cny / cost / cost_unit / refund
                / commission / ads / params / param
    """
    agg = _pick_kinds(by_kind, kinds)
    local: dict[str, float] = {}
    missing: list[str] = []

    for var, cands in groups.items():
        val, hit = _find_title(agg, cands)
        local[var] = val
        if hit is None and cands:
            missing.append(cands[0])

    aliases = ctx.get("aliases") or {}
    params = ctx.get("params")
    cny = float(getattr(params, "cny_to_local", 1.0) or 1.0)

    qty = _to_num(agg.get("__qty"))
    cost_cny = _to_num(agg.get("__cost_cny"))

    local["qty"] = qty
    local["items"] = qty
    local["orders"] = _to_num(agg.get("__orders"))
    local["order_count"] = local["orders"]
    local["cost_cny"] = cost_cny
    local["cost_unit"] = (cost_cny / qty) if qty else _to_num(ctx.get("cost_unit"))
    local["cost"] = cost_cny * cny
    local["refund"] = _to_num(agg.get("__refund"))
    local["gmv"] = (_title_value(agg, aliases, "subtotal_before")
                    - _title_value(agg, aliases, "seller_discount"))
    local["paid"] = _title_value(agg, aliases, "order_amount")
    local["buyer_paid"] = local["paid"]
    local["commission"] = _to_num(ctx.get("commission"))
    local["ads"] = _to_num(ctx.get("ads"))
    local["params"] = params
    local["param"] = {}
    local["by_kind"] = by_kind
    return local, missing


# 费用基数里这些名字走内部量，不走标题
_BASE_INTERNAL = {
    "orders": "__orders", "order_count": "__orders",
    "items": "__qty", "qty": "__qty",
    "cost_cny": "__cost_cny", "refund": "__refund", "refund_amount": "__refund",
}


def _base_value(base: str, ctx: dict, kinds) -> float:
    """简写形式（base+rate）的基数。新配置一般用公式，这里保证老配置还能跑。"""
    b = str(base or "gmv").strip()
    agg = _pick_kinds(ctx.get("by_kind") or {}, kinds)
    aliases = ctx.get("aliases") or {}
    params = ctx.get("params")
    cny = float(getattr(params, "cny_to_local", 1.0) or 1.0)

    if b in ("commission", "ads"):
        return _to_num(ctx.get(b))
    if b in _BASE_INTERNAL:
        return _to_num(agg.get(_BASE_INTERNAL[b]))
    if b == "cost":
        return _to_num(agg.get("__cost_cny")) * cny
    if b == "cost_unit":
        q = _to_num(agg.get("__qty"))
        return (_to_num(agg.get("__cost_cny")) / q) if q else 0.0
    if b == "gmv":
        return (_title_value(agg, aliases, "subtotal_before")
                - _title_value(agg, aliases, "seller_discount"))
    if b in ("buyer_paid", "paid", "order_amount"):
        return _title_value(agg, aliases, "order_amount")
    if b == "subtotal_before":
        return _title_value(agg, aliases, "subtotal_before")
    if b == "seller_discount":
        return _title_value(agg, aliases, "seller_discount")
    return _to_num(agg.get(_norm(b)))


def compute_params(params_cfg: list[dict], ctx: dict) -> tuple[dict[str, float], list[str]]:
    """按「计算参数表」逐项算出金额。返回 ({参数id: 金额}, [问题说明])。

    每一项有两种写法：
      · 公式（推荐）  {"id":"dispatch","expr":"orders * params.dispatch_fee_cny * params.cny_to_local"}
      · 简写          {"id":"dispatch","base":"order_count","rate":"params.xxx"}

    公式里能写：
      [标题|备选标题]  订单表的列（按这一项自己的 kinds 合计）
      裸写标题         同上，只要该标题在订单表里存在
      gmv / paid / qty / orders / cost_cny / cost / cost_unit / refund
      commission / ads（整期直接取数）
      params.国家费率
      param.其它参数id（引用参数表里某一项算出来的值）
    """
    by_kind = ctx.get("by_kind") or {}
    titles = ctx.get("titles") or []
    out: dict[str, float] = {}
    issues: list[str] = []
    missing: list[str] = []

    pending: list[dict] = [it for it in (params_cfg or []) if isinstance(it, dict)
                           and str(it.get("id") or "").strip()]
    resolved: dict[str, float] = {}

    # 参数之间可以互相引用（佣金 = gmv × 费率），所以按轮次解：
    # 本轮算不出来的（引用还没算的项）留到下一轮，最多 len+2 轮，不会死循环。
    for _ in range(len(pending) + 2):
        if not pending:
            break
        progressed = False
        rest: list[dict] = []
        for item in pending:
            fid = str(item.get("id")).strip()
            if not item.get("enabled", True):
                resolved[fid] = 0.0
                progressed = True
                continue

            kinds = item.get("kinds")
            expr = item.get("expr")
            py_expr, groups = compile_expr(str(expr or ""), titles)
            local, miss = _local_ctx(by_kind, kinds, ctx, groups)
            missing.extend(miss)
            # 其它参数算出来的值放在 param.xxx 名下，显式引用才用：
            #   裸写 gmv   = 按这一项自己的 kinds 现算的 GMV
            #   param.gmv  = 参数表里「GMV」那一项算出来的值
            local["param"] = {k: v for k, v in resolved.items() if k != fid}
            # 刻意不把同名变量删掉：参数 id 叫 ads / gmv / cost 时，
            # 公式里写 ads 就是那个「算好的量」（广告表花费），不是自己引用自己。
            # 删掉会让「广告花费 = ads」直接变成 0，这个坑很难看出来。

            try:
                if py_expr.strip():
                    val, _ = safe_eval(py_expr, local)
                else:
                    base = str(item.get("base") or "gmv").strip()
                    if base in ("commission", "ads"):
                        resolved[fid] = _to_num(ctx.get(base))
                        progressed = True
                        continue
                    base_val = _base_value(base, ctx, kinds)
                    rate_raw = item.get("rate")
                    if rate_raw is None:
                        rate_raw = 1.0
                    if isinstance(rate_raw, (int, float)):
                        rate = float(rate_raw)
                    else:
                        rate, _ = safe_eval(str(rate_raw), local)
                    val = base_val * rate
            except FormulaError as e:
                if "未知变量" in str(e) or "不存在" in str(e):
                    rest.append(item)      # 可能只是依赖还没轮到自己，下一轮再试
                    continue
                label = str(item.get("label") or fid)
                issues.append(f"参数「{label}」的公式写错了：{e}（该项按 0 计）")
                resolved[fid] = 0.0
                progressed = True
                continue
            resolved[fid] = val
            progressed = True

        pending = rest
        if not progressed:
            for item in pending:
                fid = str(item.get("id")).strip()
                label = str(item.get("label") or fid)
                issues.append(f"参数「{label}」引用了算不出来的变量，该项按 0 计")
                resolved[fid] = 0.0
            pending = []
            break

    for t in dict.fromkeys(missing):
        issues.append(f"订单表里找不到标题「{t}」，用到它的参数按 0 计——"
                      f"在公式里补一个候选标题（用 | 分隔）就行")

    # 保持参数表里的顺序输出，界面与报表看起来才稳定
    for item in (params_cfg or []):
        if isinstance(item, dict):
            fid = str(item.get("id") or "").strip()
            if fid:
                out[fid] = resolved.get(fid, 0.0)
    return out, issues


# 旧名字保留：早期代码按「费用」称呼这张表
compute_fees = compute_params


def compute_formulas(formulas: dict, ctx: dict) -> tuple[dict[str, float], list[str]]:
    """按依赖顺序算公式。公式之间可以互相引用（如 net 引用 gross_profit）。"""
    values: dict[str, float] = {}
    issues: list[str] = []
    pending = dict(formulas or {})

    # 最多跑这么多轮：公式互相引用时靠轮次解开，不会死循环
    for _ in range(len(pending) + 2):
        if not pending:
            break
        progressed = False
        for name in list(pending):
            expr = pending[name]
            try:
                val, _ = safe_eval(expr, {**ctx, **values})
            except FormulaError as e:
                # 可能只是依赖还没算出来，下一轮再试
                if "未知变量" in str(e):
                    continue
                issues.append(f"公式「{name}」有错：{e}")
                values[name] = 0.0
                pending.pop(name)
                progressed = True
                continue
            values[name] = val
            pending.pop(name)
            progressed = True
        if not progressed:
            for name in list(pending):
                issues.append(f"公式「{name}」引用了算不出来的变量：{pending[name]}")
                values[name] = 0.0
                pending.pop(name)
            break

    return values, issues
