"""计算参数配置接口：读、存、试算。

新规矩：公式里直接写订单表的列标题，映射就在公式里——
所以这里不再维护一套「逻辑字段 → 列标题」的映射表，
只有一张「订单表识别」清单（订单号/状态/取消类型/数量…），
用来判断订单类型、找链接、对成本。每个国家可以各配一套。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src import formula, paths

KINDS: list[tuple[str, str]] = [
    ("normal", "正常"),
    ("cancelled", "已取消"),
    ("refunded", "退货退款"),
    ("zero_amount", "0元样品"),
    ("unpaid", "未付款"),
]

# 试算用的数据缓存：同一份数据反复试算不必反复解析
_CACHE: dict[str, Any] = {"key": None, "data": None, "params": None}


def config_path() -> Path:
    return paths.CONFIG / "formulas.json"


def read_config() -> dict:
    """读当前配置（带默认值兜底）。"""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    cfg = formula.load_config(config_path())
    return {
        "params": cfg.get("params") or [],
        "by_country": cfg.get("by_country") or {},
        "formulas": cfg.get("formulas") or {},
        "order_fields": cfg.get("order_fields") or {},
        "cost_fields": cfg.get("cost_fields") or {},
        "kinds": cfg.get("kinds") or dict(KINDS),
        "raw_note": doc.get("_readme") or [],
        # 最近一次订单表的真实表头：界面上写标题时可以直接点选
        "headers": order_headers().get("titles") or [],
    }


def save_config(params: list, formulas: dict, keep_readme: bool = True,
                by_country: dict | None = None,
                order_fields: dict | None = None,
                cost_fields: dict | None = None) -> dict:
    """保存参数表。保留原来的 _readme 说明文字，别把它冲掉。"""
    old: dict = {}
    try:
        old = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}

    out: dict = {}
    if keep_readme and old.get("_readme"):
        out["_readme"] = old["_readme"]
    out["kinds"] = old.get("kinds") or dict(KINDS)
    out["order_fields"] = order_fields if order_fields is not None else (old.get("order_fields") or {})
    out["cost_fields"] = cost_fields if cost_fields is not None else (old.get("cost_fields") or {})
    out["params"] = params
    if by_country is not None:
        # 只留真有内容的：空 = 这个国家没单独设，删掉让通用那套生效
        clean = {}
        for k, v in (by_country or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get("params") or v.get("order_fields") or v.get("cost_fields"):
                clean[k] = v
        if clean:
            out["by_country"] = clean
    out["formulas"] = formulas or {}

    config_path().write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"ok": True, "path": str(config_path()), "params": len(out["params"])}


def save_cost_fields(code_titles: list, cost_titles: list) -> dict:
    """只改「成本表哪一列是编码/成本」，不动参数表。"""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    cf = dict(doc.get("cost_fields") or {})
    cf["code"] = {"label": "商品编码", "titles": [str(x).strip() for x in code_titles if str(x).strip()]}
    cf["cost"] = {"label": "成本（人民币）", "titles": [str(x).strip() for x in cost_titles if str(x).strip()]}
    doc["cost_fields"] = cf
    config_path().write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "cost_fields": cf}


def order_headers() -> dict:
    """读最近一份订单表的真实表头（原始标题，不改写）。"""
    from src.loader import find_header_row, merged_aliases, read_table

    folder = latest_inbox()
    if folder is None:
        return {"ok": False, "error": "还没有订单数据"}
    files = [f for f in folder.iterdir()
             if f.is_file() and f.suffix.lower() in (".csv", ".xlsx", ".xls")
             and not f.name.startswith("~$")]
    if not files:
        return {"ok": False, "error": "还没有订单文件"}
    f = max(files, key=lambda x: x.stat().st_mtime)
    try:
        rows = read_table(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读不了订单表：{e}"}
    hidx, _cm = find_header_row(rows, ("order_id", "sku_id", "seller_sku", "qty"),
                                aliases=merged_aliases(None))
    if hidx < 0:
        return {"ok": False, "error": "订单表里没找到表头行"}
    titles = [str(c).strip() for c in rows[hidx] if str(c).strip()]
    return {"ok": True, "file": f.name, "titles": titles}


def latest_inbox() -> Path | None:
    """最新有数据的目录：优先店铺目录，其次公共 inbox。"""
    cands: list[tuple[float, Path]] = []
    try:
        for d in paths.STORES_ROOT.iterdir():
            sub = d / "inbox"
            if sub.is_dir() and any(sub.iterdir()):
                cands.append((max(x.stat().st_mtime for x in sub.iterdir()), sub))
    except OSError:
        pass
    if paths.INBOX.is_dir() and any(paths.INBOX.iterdir()):
        cands.append((max(x.stat().st_mtime for x in paths.INBOX.iterdir()), paths.INBOX))
    if not cands:
        return None
    return max(cands)[1]


def _load_data(order_fields: dict | None = None, cost_fields: dict | None = None):
    """加载核算数据（按目录 + 文件 + 字段配置缓存，避免每次试算都重新解析）。"""
    inbox = latest_inbox()
    if inbox is None:
        return None, "还没有订单数据，先在店铺页跑一次取数"

    try:
        st = inbox.stat()
        files = sorted(p.name for p in inbox.iterdir() if p.is_file())
        stamp = json.dumps({"o": order_fields or {}, "c": cost_fields or {}}, sort_keys=True)
        key = (str(inbox), st.st_mtime_ns, len(files), tuple(files[-6:]), stamp)
    except OSError:
        return None, "找不到数据目录"

    if _CACHE["key"] == key and _CACHE["data"] is not None:
        return _CACHE["data"], None

    from src.calc import load_rates
    from src.loader import load_from_dir

    rates, _sw = load_rates(paths.RATES_FILE)
    country = next(iter(rates), None)
    if not country:
        return None, "config/rates.json 里没有国家参数"
    p = rates[country]

    costs = paths.COSTS / "master.csv"
    mapping = paths.MAPPING / "master.csv"
    try:
        data = load_from_dir(
            inbox,
            costs if costs.exists() else None,
            p.usd_to_local,
            mapping if mapping.exists() else None,
            order_fields=order_fields,
            cost_fields=cost_fields,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"读取数据失败：{e}"

    _CACHE.update({"key": key, "data": data, "params": (country, p, _sw)})
    return data, None


def preview(params: list, formulas: dict | None = None,
            country: str = "", by_country: dict | None = None,
            order_fields: dict | None = None,
            cost_fields: dict | None = None) -> dict:
    """用给定的参数表试算，并与当前配置对比。

    这是配口径时最重要的功能：改一个公式或勾选，立刻看到净利差多少。
    """
    cfg_now = formula.load_config(config_path())
    # 数据要按「这一版字段配置」重新解析，否则改了标题却看不出变化
    data, err = _load_data(order_fields or cfg_now.get("order_fields"),
                           cost_fields or cfg_now.get("cost_fields"))
    if data is None:
        return {"ok": False, "error": err}

    from src.calc import compute, load_rates

    asked_country = bool(country)   # 用户选了具体国家，还是「通用」
    _c, p, sw = _CACHE["params"]
    if country:
        rates, sw2 = load_rates(paths.RATES_FILE)
        if country in rates:
            p, sw = rates[country], sw2
            _c = country
    country = _c

    def run(cfg: dict) -> dict:
        r = compute(data, p, sw, fcfg=cfg)
        t = r.total
        return {
            "gmv": round(t.gmv, 2),
            "cost": round(t.cost_local, 2),
            "fees": round(t.fee_total, 2),
            "net": round(t.net_local, 2),
            "net_rate": round(t.net_margin * 100, 2),
            "orders": round(t.orders, 2),
            "products": len(r.products),
            "fee_detail": {k: round(v, 2) for k, v in t.fees.items() if abs(v) > 0.005},
        }

    # 编「通用」时不能被某个国家的覆盖项带跑：那会把该国的数当成通用的结果。
    cfg_now_eff = cfg_now if asked_country else {**cfg_now, "by_country": {}}
    before = run(cfg_now_eff)
    trial_cfg = dict(cfg_now)
    trial_cfg["params"] = params
    if by_country is not None:
        bc = dict(by_country)
        if asked_country and country in bc:
            bc[country] = {**bc[country], "params": params}
        trial_cfg["by_country"] = bc if asked_country else {}
    if order_fields:
        trial_cfg["order_fields"] = order_fields
    if cost_fields:
        trial_cfg["cost_fields"] = cost_fields
    if formulas:
        trial_cfg["formulas"] = {**cfg_now.get("formulas", {}), **formulas}
    after = run(trial_cfg)

    return {
        "ok": True,
        "country": country,
        "currency": getattr(p, "currency", ""),
        # 费用 id → 中文名：试算明细要给人看，不能甩一串英文 id
        "fee_labels": {str(it.get("id")): str(it.get("label") or it.get("id"))
                       for it in (params or []) if isinstance(it, dict) and it.get("id")},
        "before": before,
        "after": after,
        "delta": {
            "net": round(after["net"] - before["net"], 2),
            "cost": round(after["cost"] - before["cost"], 2),
            "fees": round(after["fees"] - before["fees"], 2),
            "gmv": round(after["gmv"] - before["gmv"], 2),
        },
    }
