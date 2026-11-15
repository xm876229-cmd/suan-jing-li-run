"""计算参数配置接口：读、存、试算。

新规矩：公式里直接写订单表的列标题，映射就在公式里——
所以这里不再维护一套「逻辑字段 → 列标题」的映射表，
只有一张「订单表识别」清单（订单号/状态/取消类型/数量…），
用来判断订单类型、找链接、对成本。每个国家可以各配一套。
"""

from __future__ import annotations

import json
import time
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


# 报表「汇总」页的列目录：基础列固定，费用/占比列跟着参数表走。
# 标题规则必须和 src/report.py 的 _summary_specs 一致，否则界面叫「广告费比」、
# 报表里叫别的，人对不上。
_REPORT_BASE_COLS = [
    ("rank", "排名", "text"), ("style", "款式编码", "text"),
    ("product", "链接（商品ID）", "text"), ("name", "商品名称", "text"),
    ("gmv", "GMV", "money"), ("orders", "订单数", "int"),
    ("pre_cancel_orders", "售前取消单数", "int"),
    ("post_cancel_orders", "售后取消单数", "int"),
    ("post_cancel_share", "售后取消占比", "pct"),
    ("all_cancel_share", "取消占总单比", "pct"),
    ("post_cancel_total_share", "售后占总单比", "pct"),
    ("items", "件数", "int"), ("aov", "客单价", "money"),
    ("buyer_paid", "买家实付", "money"), ("cost", "成本", "money"),
    ("gross_profit", "毛利", "money"), ("gross_margin", "链接毛利率", "pct"),
]
_REPORT_TAIL_COLS = [
    ("fee_total", "费用合计", "money"), ("net_rate", "净利率", "pct"),
    ("net", "净利", "money"), ("net_cny", "净利", "money"),
    ("alt_net", "净利（对比）", "money"), ("alt_diff", "样品差额", "money"),
]
_ROLE_KEYS = {"gmv", "cost", "order_count", "items", "buyer_paid"}
_RATIO_SPECIAL = {"dispatch": "发单费占比", "ads": "广告费比"}


def report_catalog(params: list) -> list[dict]:
    """按参数表推出「汇总页可选列」的完整清单。

    为什么要后端算一份（2026-11-15）：
        前端原本自己从 params 推费用列，但「报表 → 展示列设置」是独立视图，
        它渲染时前端那份 params 有可能还没到位，结果列表里一个费用列都没有，
        而报表本身是有这些列的 —— 用户看到的就是「费率列又没了」。
        现在后端直接把清单下发，前端优先用它，两边不会再算出两套。
    """
    out = [{"key": k, "header": h, "kind": t} for k, h, t in _REPORT_BASE_COLS]
    fees: list[tuple[str, str]] = []
    seen: set[str] = set()
    for it in params or []:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "").strip()
        if not fid or fid in seen:
            continue
        role = str(it.get("role") or "").strip()
        if role in _ROLE_KEYS or fid in _ROLE_KEYS:
            continue
        seen.add(fid)
        fees.append((fid, str(it.get("label") or fid)))
    for fid, label in fees:
        out.append({"key": "fee:" + fid, "header": label, "kind": "money"})
    for fid, label in fees:
        out.append({"key": "ratio:" + fid,
                    "header": _RATIO_SPECIAL.get(fid) or (label + "占比"),
                    "kind": "pct"})
    out += [{"key": k, "header": h, "kind": t} for k, h, t in _REPORT_TAIL_COLS]
    return out


def read_config() -> dict:
    """读当前配置（带默认值兜底）。"""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    cfg = formula.load_config(config_path())
    _params = cfg.get("params") or []
    return {
        "params": _params,
        # 汇总页可选列的完整清单（后端算好下发，前端别自己再推一遍）
        "report_column_catalog": report_catalog(_params),
        "by_country": cfg.get("by_country") or {},
        "formulas": cfg.get("formulas") or {},
        "order_fields": cfg.get("order_fields") or {},
        "cost_fields": cfg.get("cost_fields") or {},
        # 广告表 / 联盟佣金表 / 链接资料表的「表头识别」：结构同订单表
        "ads_fields": cfg.get("ads_fields") or dict(formula.DEFAULT_ADS_FIELDS),
        "affiliate_fields": cfg.get("affiliate_fields") or dict(formula.DEFAULT_AFFILIATE_FIELDS),
        "mapping_fields": cfg.get("mapping_fields") or dict(formula.DEFAULT_MAPPING_FIELDS),
        # 总开关：true = 整张参数表都不算 0 元样品单
        "exclude_zero_amount": bool(cfg.get("exclude_zero_amount")),
        "kinds": cfg.get("kinds") or dict(KINDS),
        # 报表「汇总」页要展示哪些列（列键列表）、合计行放哪（top/bottom）、列标题怎么命名
        "report_columns": cfg.get("report_columns") or [],
        # 历来出现过的全部列键。用来区分「后来新增的列」和「用户主动取消的列」：
        # 没有它，参数表加了新费用项，报表里也不会多出那一列（2026-11-15 踩到）。
        "report_columns_seen": (doc.get("report_columns_seen")
                                or cfg.get("report_columns_seen") or []),
        # 列的顺序：直接从文件读（load_config 只回它认识的键，容易漏掉新加字段）
        "report_column_order": (doc.get("report_column_order")
                                or cfg.get("report_column_order") or []),
        "report_total_position": str(cfg.get("report_total_position") or "bottom").strip()
        or "bottom",
        "report_titles": cfg.get("report_titles") or {},
        "raw_note": doc.get("_readme") or [],
        # 最近一次订单表的真实表头：界面上写标题时可以直接点选
        "headers": order_headers().get("titles") or [],
        # 引擎直接认的中文名（成本合计、费用项合计…）。
        # 界面拿它来：① 判断公式里有没有写错名字 ② 「插入参数」下拉里列出来。
        # 以前这两处各写一份硬编码，和 src/formula.py 一对不上就误报「不认识」。
        "builtin_cn": dict(formula.BUILTIN_CN),
    }


def _clean_order_fields(fields: dict | None) -> dict:
    """把订单表识别的候选标题洗干净：去空行、去重（不区分大小写），
    统一成 {字段: {label, titles: [...]}}。

    前端「一行一个标题」的输入框可能留下空串（点了加一行没填），
    存进去下次渲染会多出空框，所以保存时清掉。
    """
    clean: dict = {}
    for k, v in (fields or {}).items():
        key = str(k or "").strip()
        if not key:
            continue
        if isinstance(v, dict):
            label = str(v.get("label") or key).strip()
            raw = v.get("titles") or []
        elif isinstance(v, list):
            label = key
            raw = v
        else:
            continue
        seen: set = set()
        titles: list[str] = []
        for t in raw:
            s = str(t or "").strip()
            if not s:
                continue
            low = s.lower()
            if low in seen:
                continue
            seen.add(low)
            titles.append(s)
        if titles:
            clean[key] = {"label": label, "titles": titles}
    return clean


def _rate_labels() -> dict:
    """费率的中文名（rates.json 的 labels 段）。

    公式里写的费率名，用的就是用户在费率表里改过的名字
    （比如把 cny_to_local 叫成「人民币本地」）。保存前校验如果只认
    内置默认名（「人民币汇率」），用户自己写得对的公式会被拦下来，
    报一句「未知变量：人民币本地」——比不校验更让人摸不着头脑。
    """
    try:
        from src import paths as _paths
        doc = json.loads(_paths.RATES_FILE.read_text(encoding="utf-8"))
        lab = doc.get("labels")
        return lab if isinstance(lab, dict) else {}
    except Exception:      # noqa: BLE001 —— 读不到就退回内置默认名
        return {}


def _check_formulas(params: list, formulas: dict) -> list[str]:
    """试算每一条公式，返回人话错误说明（空列表 = 都能算）。

    能做/不能做和核算时完全是同一套解析（formula.check_expr），
    所以这里放过去，核算时就不会再出现「名字认不出、静默按 0」。
    """
    from types import SimpleNamespace

    from src import formula as _f

    ctx: dict = {}
    for p in params or []:
        if isinstance(p, dict) and str(p.get("id") or "").strip():
            ctx[str(p["id"]).strip()] = 1.0
    for k in ("gmv", "cost", "paid", "orders", "qty", "order_count", "items",
              "buyer_paid", "cost_cny", "cost_unit", "refund", "commission",
              "ads", "fees_total"):
        ctx[k] = 1.0
    for k in (formulas or {}):
        ctx[str(k)] = 1.0
    # 费率写成 params.xxx，校验时给个「什么字段都有、都是 1」的对象
    ctx["params"] = SimpleNamespace(**{k: 1.0 for k in _f.DEFAULT_RATE_CN})

    aliases = _f.rate_aliases(_rate_labels())
    for fid, cn in _f.FORMULA_CN.items():      # FORMULA_CN 是 {id: 中文名}
        aliases.setdefault(cn, fid)
    for p in params or []:
        if not isinstance(p, dict):
            continue
        fid = str(p.get("id") or "").strip()
        lab = str(p.get("label") or "").strip()
        if fid and lab:
            aliases[lab] = fid

    problems: list[str] = []
    for name, expr in (formulas or {}).items():
        ok, err = _f.check_expr(expr, ctx, aliases)
        if not ok:
            label = _f.FORMULA_CN.get(str(name), str(name))
            problems.append(f"「{label}」这一行算不了：{err}")
    return problems


def save_config(params: list, formulas: dict, keep_readme: bool = True,
                by_country: dict | None = None,
                order_fields: dict | None = None,
                cost_fields: dict | None = None,
                exclude_zero_amount: bool | None = None,
                report_columns: list | None = None,
                report_column_order: list | None = None,
                report_columns_seen: list | None = None,
                report_total_position: str | None = None,
                report_titles: dict | None = None,
                ads_fields: dict | None = None,
                affiliate_fields: dict | None = None,
                mapping_fields: dict | None = None) -> dict:
    """保存参数表。保留原来的 _readme 说明文字，别把它冲掉。"""
    # 先校验四条公式：名字写错、写成中文标点、括号不配，都在这里挡住。
    # 为什么必须挡：以前能存，核算时未知变量静默按 0，净利看着正常其实漏扣了。
    bad = _check_formulas(params, formulas or {})
    if bad:
        return {"ok": False, "error": "；".join(bad[:3])}

    old: dict = {}
    try:
        old = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}

    out: dict = {}
    if keep_readme and old.get("_readme"):
        out["_readme"] = old["_readme"]
    out["kinds"] = old.get("kinds") or dict(KINDS)
    out["order_fields"] = (_clean_order_fields(order_fields)
                           if order_fields is not None
                           else (old.get("order_fields") or {}))
    out["cost_fields"] = cost_fields if cost_fields is not None else (old.get("cost_fields") or {})
    # 广告表 / 联盟佣金表 / 链接资料表：结构同订单表，同样清洗空标题
    for _k, _v in (("ads_fields", ads_fields),
                   ("affiliate_fields", affiliate_fields),
                   ("mapping_fields", mapping_fields)):
        out[_k] = _clean_order_fields(_v) if _v is not None else (old.get(_k) or {})
    # 空参数表 / 空公式 = 配置报废（界面再也生成不出费用列、报表也没有明细列）。
    # 2026-11-15 实测：release 的 formulas.json 就是这么被写空的
    # （params/formulas/cost_fields 三块全成了 0），核算靠 formula.normalize()
    # 的代码默认值兜住所以数字没错，但界面看不到任何费用项、报表少了 6 列。
    # 以前这里是 `out["params"] = params`，空也照写，没有任何兜底。
    if not params:
        return {"ok": False, "error": "参数表是空的，拒绝保存（会清掉所有计算项，"
                                     "报表将失去费用明细）。请刷新页面重新读取配置后再改。"}
    if not (formulas or {}):
        return {"ok": False, "error": "净利公式是空的，拒绝保存（净利将算不出来）。"
                                     "请刷新页面重新读取配置后再改。"}
    out["params"] = params
    flag = old.get("exclude_zero_amount") if exclude_zero_amount is None else bool(exclude_zero_amount)
    if flag:
        out["exclude_zero_amount"] = True     # 只有开的时候才写，配置干净
    if by_country is not None:
        # 只留真有内容的：空 = 这个国家没单独设，删掉让通用那套生效
        clean = {}
        for k, v in (by_country or {}).items():
            if not isinstance(v, dict):
                continue
            if (v.get("params") or v.get("order_fields") or v.get("cost_fields")
                    or v.get("ads_fields") or v.get("affiliate_fields")
                    or v.get("mapping_fields")
                    or "exclude_zero_amount" in v
                    or (v.get("report_columns") and len(v["report_columns"]))
                    or (v.get("report_column_order") and len(v["report_column_order"]))
                    or str(v.get("report_total_position") or "").strip() in ("top", "bottom", "none")
                    or (v.get("report_titles") and len(v["report_titles"]))):
                clean[k] = v
        if clean:
            out["by_country"] = clean
    out["formulas"] = formulas or {}
    # 报表列配置：列键列表 + 合计位置（top/bottom）+ 列标题自定义命名
    if report_columns is not None:
        cols = [str(x).strip() for x in report_columns if str(x).strip()]
        if cols:
            out["report_columns"] = cols
        elif "report_columns" in old:
            pass  # 清空时不写键，配置更干净
    elif "report_columns" in old:
        out["report_columns"] = old["report_columns"]
    # 历来出现过的列键：保存时由界面把当前列目录整份传上来。
    # 有了它，以后参数表再加费用项，报表里才会自动多出那一列。
    if report_columns_seen is not None:
        seen = [str(x).strip() for x in report_columns_seen if str(x).strip()]
        if seen:
            out["report_columns_seen"] = sorted(set(seen))
        elif "report_columns_seen" in old:
            out["report_columns_seen"] = old["report_columns_seen"]
    elif "report_columns_seen" in old:
        out["report_columns_seen"] = old["report_columns_seen"]
    # 列的顺序（包含没勾的列），界面靠它稳定排列，拖动才不会被重渲染打回原样
    if report_column_order is not None:
        order = [str(x).strip() for x in report_column_order if str(x).strip()]
        if order:
            out["report_column_order"] = order
        elif "report_column_order" in old:
            pass
    elif "report_column_order" in old:
        out["report_column_order"] = old["report_column_order"]
    if report_total_position is not None:
        pos = str(report_total_position).strip()
        if pos in ("top", "bottom", "none"):
            out["report_total_position"] = pos
    elif "report_total_position" in old:
        out["report_total_position"] = old["report_total_position"]
    if report_titles is not None:
        # 只保留有内容的改名（空值 = 用默认标题，不写）
        clean_titles = {str(k).strip(): str(v).strip()
                        for k, v in report_titles.items()
                        if str(k).strip() and str(v).strip()}
        if clean_titles:
            out["report_titles"] = clean_titles
        elif "report_titles" in old:
            pass  # 清空时不写键，配置更干净
    elif "report_titles" in old:
        out["report_titles"] = old["report_titles"]

    from src import safeio
    safeio.write_json(config_path(), out)
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
    from src import safeio
    safeio.write_json(config_path(), doc)
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
            if not d.is_dir():
                continue
            sub = paths.store_inbox(d)
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
            cost_fields: dict | None = None,
            exclude_zero_amount: bool | None = None) -> dict:
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
    if exclude_zero_amount is not None:
        trial_cfg["exclude_zero_amount"] = bool(exclude_zero_amount)
    if order_fields:
        trial_cfg["order_fields"] = order_fields
    if cost_fields:
        trial_cfg["cost_fields"] = cost_fields
    if formulas:
        trial_cfg["formulas"] = {**cfg_now.get("formulas", {}), **formulas}
    after = run(trial_cfg)

    # 这次试算到底用了谁的数据——不写清楚，用户看到「净利 -45.33」根本不知道
    # 是哪家店、哪段时间、多少行算出来的，没法判断可不可信。
    src: dict = {}
    try:
        inbox = latest_inbox()
        if inbox is not None:
            # 表都在子目录里（订单表 / 广告表 / 联盟佣金表 / 商品映射），要递归找
            all_files = [f for f in inbox.rglob("*") if f.is_file()]
            all_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            # 报「最新的那张表」，不是它的 .srcmeta.json 元数据
            data_files = [f for f in all_files
                          if f.suffix.lower() in (".csv", ".xlsx", ".xls")]
            newest = (data_files or all_files)[0] if all_files else None
            src = {
                "store": inbox.parent.name,
                "files": len(all_files),
                "lines": len(getattr(data, "orders", []) or []),
                "newest_file": newest.name if newest else "",
                "newest_time": (time.strftime("%m-%d %H:%M",
                                              time.localtime(newest.stat().st_mtime))
                                if newest else ""),
            }
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "country": country,
        "currency": getattr(p, "currency", ""),
        "source": src,
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
