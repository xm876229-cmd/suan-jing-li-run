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

DEFAULTS: dict = {
    "kinds": {
        "cancelled": "已取消订单",
        "refunded": "退货退款订单",
        "zero_amount": "0 元样品单",
        "unpaid": "未付款订单",
        "normal": "正常订单",
    },
    # 计算参数表：每一项就是一条算式，公式里引用映射字段 / 其它参数 / 国家费率。
    # 默认值按业务实情——取消/退货的收入不算，但成本、发单费、平台抽成照算。
    "params": [
        {"id": "gmv", "label": "GMV（收入）", "role": "gmv",
         "expr": "subtotal_before - seller_discount",
         "kinds": ["normal", "zero_amount"]},
        {"id": "cost", "label": "商品成本", "role": "cost",
         "expr": "qty * cost_unit",
         "kinds": ["normal", "cancelled", "refunded", "zero_amount"]},
        {"id": "order_count", "label": "订单数", "role": "order_count",
         "expr": "orders",
         "kinds": ["normal", "cancelled", "refunded", "zero_amount"]},
        {"id": "items", "label": "件数", "role": "items",
         "expr": "qty",
         "kinds": ["normal", "cancelled", "refunded", "zero_amount"]},
        {"id": "buyer_paid", "label": "买家实付", "role": "buyer_paid",
         "expr": "order_amount",
         "kinds": ["normal", "zero_amount"]},
        {"id": "dispatch", "label": "发单费",
         "expr": "orders * params.dispatch_fee_local",
         "kinds": ["normal", "cancelled", "refunded", "zero_amount"]},
        {"id": "platform_commission", "label": "平台佣金",
         "expr": "gmv * params.platform_commission",
         "kinds": ["normal", "cancelled", "refunded"]},
        {"id": "tx_fee", "label": "交易手续费",
         "expr": "order_amount * params.tx_fee",
         "kinds": ["normal", "cancelled", "refunded"]},
        {"id": "cashback", "label": "奖金返现",
         "expr": "gmv * params.cashback", "kinds": ["normal"]},
        {"id": "super_coupon", "label": "超级优惠券",
         "expr": "gmv * params.super_coupon", "kinds": ["normal"], "enabled": False},
        {"id": "growth_fee", "label": "商家成长",
         "expr": "gmv * params.growth_fee", "kinds": ["normal"]},
        {"id": "logistics", "label": "物流",
         "expr": "gmv * params.logistics", "kinds": ["normal"]},
        {"id": "affiliate", "label": "达人佣金", "expr": "commission"},
        {"id": "ads", "label": "广告花费", "expr": "ads"},
    ],
    "by_country": {},
    "formulas": {
        "gross_profit": "gmv - cost",
        "total_fees": "sum(fees)",
        "net": "gross_profit - sum(fees)",
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
        # 具体的键校验交给 calc.resolve_order_filter()，那边会按默认值补齐。
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
    # 每个国家自己的参数表：{国家: {params: [...]}}，有就优先用
    if isinstance(doc.get("by_country"), dict) and doc["by_country"]:
        out["by_country"] = doc["by_country"]
    if isinstance(doc.get("formulas"), dict) and doc["formulas"]:
        out["formulas"].update(doc["formulas"])
    return out


# ---------------- 费用计算 ----------------

# base 决定「乘什么」：gmv 按成交额、order_count 按订单数……
FEE_BASES = {
    "gmv": "GMV",
    "buyer_paid": "买家实付",
    "fee_base": "平台抽成计费基数（GMV，含取消/退货）",
    "fee_base_paid": "平台抽成计费基数（实付，含取消/退货）",
    "order_count": "订单数",
    "items": "商品件数",
    "qty": "商品件数",
    "cost": "商品成本",
    "cost_unit": "成本单价（成本÷件数）",
    "order_amount": "订单金额",
    "subtotal_before": "折扣前小计",
    "seller_discount": "卖家折扣",
    "refund": "退款金额",
    "refund_amount": "退款金额",
    "commission": "达人佣金（直接取）",
    "ads": "广告花费（直接取）",
}

# 计算参数里能直接引用的「映射字段」。
# 这张表决定了用户在公式里能写什么名字——名字就是 config/mapping.json 的逻辑字段名，
# 换平台、换表头只改映射，公式一个字都不用动。
FIELD_LABELS = {
    "subtotal_before": "折扣前小计（行合计）",
    "seller_discount": "卖家折扣（行合计）",
    "order_amount": "买家实付（行合计）",
    "refund_amount": "退款金额（行合计）",
    "qty": "件数（行合计）",
    "order_count": "订单数",
    "cost": "商品成本（件数×成本单价）",
    "cost_unit": "平均成本单价",
    "commission": "达人佣金（整期直接取数）",
    "ads": "广告花费（整期直接取数）",
    "params": "国家费率（params.字段名）",
}


# 费用基数在「按类别明细」里的字段名（对不上时要映射一下）
_BASE_TO_STAT = {
    "order_count": "orders",
    "orders": "orders",
    "gmv": "gmv",
    "buyer_paid": "buyer_paid",
    "buyerpaid": "order_amount",
    "items": "qty",
    "qty": "qty",
    "cost": "cost",
    "cost_unit": "cost_unit",
    "refund": "refund",
    "refund_amount": "refund_amount",
    "order_amount": "order_amount",
    "subtotal_before": "subtotal_before",
    "seller_discount": "seller_discount",
    "fee_base": "gmv",
    "fee_base_paid": "buyer_paid",
}


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


def _local_ctx(by_kind: dict, kinds, ctx: dict) -> dict:
    """给一条参数的公式准备「可用变量」。

    变量就是列映射里的逻辑字段（subtotal_before / seller_discount / order_amount
    / refund_amount / qty），再加上几个由它们派生的量（gmv / cost / cost_unit …）。
    全部按这条参数自己的 kinds 过滤——所以「平台佣金只算正常单」时，
    公式里的 gmv 也只剩正常单的 GMV。
    """
    agg = _pick_kinds(by_kind, kinds)
    local = dict(agg)

    subtotal = agg.get("subtotal_before", 0.0)
    discount = agg.get("seller_discount", 0.0)
    qty = agg.get("qty", agg.get("items", 0.0))
    cost = agg.get("cost", 0.0)

    local["qty"] = qty
    local["items"] = qty
    local["orders"] = agg.get("orders", 0.0)
    local["order_count"] = local["orders"]
    local["cost"] = cost
    local["cost_unit"] = (cost / qty) if qty else _to_num(ctx.get("cost_unit"))
    local["gmv"] = subtotal - discount
    local["buyer_paid"] = agg.get("order_amount", 0.0)
    local["order_amount"] = agg.get("order_amount", 0.0)
    local["refund"] = agg.get("refund", agg.get("refund_amount", 0.0))
    # 整期直接取数的两项，与订单类别无关
    local["commission"] = _to_num(ctx.get("commission"))
    local["ads"] = _to_num(ctx.get("ads"))
    local["params"] = ctx.get("params")
    local["param"] = {}            # 其它参数算出来的值：param.佣金id
    local["by_kind"] = by_kind
    return local


def _base_value(base: str, ctx: dict, kinds) -> float:
    """算一条费用的基数（base + rate 这种简写形式用）。

    没写 kinds → 全部类别；写了 kinds → 从「按订单类别分开的明细」里挑出来求和。
    """
    stat = _BASE_TO_STAT.get(base)
    if stat is None:
        return _to_num(ctx.get(base))
    agg = _pick_kinds(ctx.get("by_kind") or {}, kinds)
    if stat == "cost_unit":
        qty = agg.get("qty", 0.0)
        return (agg.get("cost", 0.0) / qty) if qty else 0.0
    return _to_num(agg.get(stat))


def compute_params(params_cfg: list[dict], ctx: dict) -> tuple[dict[str, float], list[str]]:
    """按「计算参数表」逐项算出金额。返回 ({参数id: 金额}, [问题说明])。

    这张表里什么都能放：GMV、商品成本、订单数、件数、各项费用。
    每一项有两种写法，随便挑：

      · 公式写法（推荐，最自由）
            {"id":"commission","label":"平台佣金","kinds":["normal"],
             "expr":"gmv * params.platform_commission"}
      · 简写写法（基数 × 费率）
            {"id":"dispatch","base":"order_count","rate":"params.dispatch_fee_local",
             "kinds":["normal"]}

    公式里能用的变量（都按这一项自己的 kinds 过滤后再汇总）：
        subtotal_before / seller_discount / order_amount / refund_amount / qty
        gmv（= 折扣前小计 − 卖家折扣）、cost（= 件数 × 成本单价）、cost_unit
        orders（订单数）、commission / ads（整期直接取数）、params.国家费率
        param.其它参数id（引用参数表里某一项算出来的值）
    参数之间可以互相引用，顺序无所谓，会自动排队算。

    每项可写字段：
      id       唯一标识，净利公式里用它引用金额
      label    中文名
      expr     计算公式；写了就以它为准
      base     简写用：基数；rate 简写用：乘数（不写 = 1）
      kinds    只在这些订单类别上算。不写 = 全部类别。写 [] = 这一项不算
      enabled  false = 暂时不算这一项
      role     报表固定列（gmv/cost/order_count/items/buyer_paid），其余都进费用
    """
    by_kind = ctx.get("by_kind") or {}
    out: dict[str, float] = {}
    issues: list[str] = []
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
            local = _local_ctx(by_kind, kinds, ctx)
            # 其它参数算出来的值放在 param.xxx 名下，显式引用才用：
            #   裸写 gmv   = 按这一项自己的 kinds 现算的 GMV（勾哪个类别就算哪个）
            #   param.gmv  = 参数表里「GMV」那一项算出来的值
            # 这样「勾了取消单却没反应」这种糊涂账不会出现。
            local["param"] = dict(resolved)
            local.pop(fid, None)      # 不允许自己引用自己

            expr = item.get("expr")
            try:
                if expr is not None and str(expr).strip() != "":
                    val, _ = safe_eval(str(expr), local)
                else:
                    base = str(item.get("base") or "gmv").strip()
                    if base in ("commission", "ads"):
                        resolved[fid] = _to_num(ctx.get(base))
                        progressed = True
                        continue
                    base_val = _base_value(base, ctx, kinds)
                    rate_raw = item.get("rate")
                    if rate_raw is None:
                        # 没写 rate = 1，即「原样取这个基数」（GMV、成本、订单数这类统计量）
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
