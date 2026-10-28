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
    "order_filter": {
        "cancelled": {"income": False, "cost": True, "order_count": True, "fee_base": True},
        "refunded": {"income": False, "cost": True, "order_count": True, "fee_base": True},
        "zero_amount": {"income": True, "cost": True, "order_count": True, "fee_base": False},
        "unpaid": {"income": False, "cost": False, "order_count": False, "fee_base": False},
        "refund_keywords": ["return", "refund"],
    },
    "fees": [
        {"id": "dispatch", "label": "发单费", "base": "order_count",
         "rate": "params.dispatch_fee_local", "enabled": True},
        {"id": "platform_commission", "label": "平台佣金", "base": "fee_base",
         "rate": "params.platform_commission", "enabled": True},
        {"id": "tx_fee", "label": "交易手续费", "base": "fee_base_paid",
         "rate": "params.tx_fee", "enabled": True},
        {"id": "cashback", "label": "奖金返现", "base": "gmv",
         "rate": "params.cashback", "enabled": True},
        {"id": "super_coupon", "label": "超级优惠券", "base": "gmv",
         "rate": "params.super_coupon", "enabled": False},
        {"id": "growth_fee", "label": "商家成长", "base": "gmv",
         "rate": "params.growth_fee", "enabled": True},
        {"id": "logistics", "label": "物流", "base": "gmv",
         "rate": "params.logistics", "enabled": True},
        {"id": "affiliate", "label": "达人佣金", "base": "commission", "enabled": True},
        {"id": "ads", "label": "广告花费", "base": "ads", "enabled": True},
    ],
    "formulas": {
        "gmv": "sum(subtotal_before) - sum(seller_discount)",
        "cost": "sum(cost_local)",
        "gross_profit": "gmv - cost",
        "total_fees": "sum(fees)",
        "net": "gross_profit - total_fees",
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
    if isinstance(doc.get("fees"), list) and doc["fees"]:
        out["fees"] = doc["fees"]
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
    "cost": "商品成本",
    "refund": "退款金额",
    "order_amount": "订单金额",
    "commission": "达人佣金（直接取）",
    "ads": "广告花费（直接取）",
}


def compute_fees(fees_cfg: list[dict], ctx: dict) -> tuple[dict[str, float], list[str]]:
    """按费用项列表逐项算钱。返回 ({费用id: 金额}, [问题说明])。

    基数(rate) 的写法：
      · 数字                    → 固定费率/固定额
      · "params.xxx"            → 引用 per-国家 参数
      · 表达式，如 "0.02+0.01"  → 也能算（走同一个求值器）
    """
    out: dict[str, float] = {}
    issues: list[str] = []

    for item in fees_cfg or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "").strip()
        if not fid:
            continue
        if not item.get("enabled", True):
            out[fid] = 0.0
            continue

        base = str(item.get("base") or "gmv").strip()
        label = str(item.get("label") or fid)

        # 直接取数的两类：联盟佣金、广告花费（已经是金额，不再乘费率）
        if base in ("commission", "ads"):
            amt = _to_num(ctx.get(base))
            out[fid] = amt
            continue

        base_val = _to_num(ctx.get(base))
        rate_raw = item.get("rate")
        if rate_raw is None:
            # 没写 rate 就认为费率取国家参数里的同名项（方便老配置迁移）
            rate_raw = f"params.{fid}"
        try:
            rate, _ = safe_eval(str(rate_raw), ctx) if not isinstance(rate_raw, (int, float)) \
                else (float(rate_raw), set())
        except FormulaError as e:
            issues.append(f"费用「{label}」的费率写错了：{e}（该项按 0 计）")
            out[fid] = 0.0
            continue

        out[fid] = base_val * rate

    return out, issues


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
