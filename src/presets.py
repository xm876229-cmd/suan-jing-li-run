"""预设：一整套计算口径的命名快照，随时切回。

为什么需要它
------------
参数表、公式、订单表列标题、费率……这些东西调一次很费劲，
调完还想着留着、随时切回来，就得有「预设」。

两级作用域
----------
- **全局预设**：对所有店铺生效
- **店铺预设**：只对某一家店生效，优先级高于全局

一份预设能装什么
----------------
计算参数表（每项勾了哪些订单类型、公式）、净利公式、订单表识别清单、
成本表列标题、「去除 0 元样品订单」总开关、各国费率。

两种预设
--------
- **内置预设**：只改「勾了哪些订单类型」（kind 覆盖），公式和标题保持你现在的
  ——「套用模板口径」用这个，不会把你改过的公式冲掉
- **自定义预设**：完整快照，应用时整体替换

安全
----
应用前自动把当前配置备份到 ``config/_backup/``，随时能回滚。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import paths

PRESETS_FILE = "presets.json"
BACKUP_DIR = "_backup"

# 内置预设：只覆盖「勾了哪些订单类型」，其余（公式、标题）保持当前值。
# 这样套用内置预设不会把你手改过的公式冲掉。
BUILT_IN: list[dict] = [
    {
        "id": "builtin_default5",
        "name": "默认 5 条口径",
        "desc": "GMV/商品成本 不算取消退货；发单费 不算取消；交易手续费 只算正常订单；订单服务费 不算取消和样品",
        "builtin": True,
        "kind_overrides": {
            "gmv": ["normal", "zero_amount"],
            "cost": ["normal", "zero_amount"],
            "order_count": ["normal", "refunded", "zero_amount"],
            "items": ["normal", "refunded", "zero_amount"],
            "dispatch": ["normal", "refunded", "zero_amount"],
            "buyer_paid": ["normal", "zero_amount"],
            "tx_fee": ["normal"],
            "order_service": ["normal", "refunded"],
            "platform_commission": ["normal", "zero_amount"],
            "cashback": ["normal", "zero_amount"],
        },
    },
    {
        "id": "builtin_template_ae0",
        "name": "模板口径（AE2=0）",
        "desc": "对齐模板《V1-净利》的 AE2=0 那一档：订单数/成本/发单费 只算正常单；"
                "买家支付总额与交易手续费 含取消退货、剔 0 元样品",
        "builtin": True,
        "kind_overrides": {
            "gmv": ["normal", "zero_amount"],
            "cost": ["normal"],
            "order_count": ["normal"],
            "items": ["normal"],
            "dispatch": ["normal"],
            "buyer_paid": ["normal", "cancelled", "refunded"],
            "tx_fee": ["normal", "cancelled", "refunded"],
            "platform_commission": ["normal", "zero_amount"],
            "cashback": ["normal", "zero_amount"],
        },
    },
    {
        "id": "builtin_no_sample",
        "name": "去 0 元样品（等同打开总开关）",
        "desc": "所有勾了 0 元样品的项一律不算样品单，算纯正常订单的净利",
        "builtin": True,
        "kind_overrides": {},
        "exclude_zero_amount": True,
    },
]


def presets_path() -> Path:
    return paths.CONFIG / PRESETS_FILE


def _read() -> dict:
    try:
        doc = json.loads(presets_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    return doc if isinstance(doc, dict) else {}


def _write(doc: dict) -> None:
    from src import safeio
    safeio.write_json(presets_path(), doc)


def _safe_id(name: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", (name or "").strip())
    return s.strip("-") or "preset"


def list_all(scope: str = "") -> list[dict]:
    """列出预设。

    scope 传店铺 id 时，返回该店预设 + 全局预设（店铺的排前面）。
    """
    doc = _read()
    items: list[dict] = []
    for pid, p in (doc.get("presets") or {}).items():
        if not isinstance(p, dict):
            continue
        items.append({
            "id": pid,
            "name": p.get("name") or pid,
            "desc": p.get("desc") or "",
            "scope": p.get("scope") or "global",
            "builtin": False,
            "created_at": p.get("created_at") or "",
            "updated_at": p.get("updated_at") or "",
            "has_rates": bool((p.get("content") or {}).get("rates")),
        })
    shop = (scope or "").strip()
    if shop:
        items.sort(key=lambda x: (0 if x["scope"] == f"shop:{shop}" else
                                  1 if x["scope"] == "global" else 2))
    else:
        items.sort(key=lambda x: (0 if x["scope"] == "global" else 1, x["name"]))
    for b in BUILT_IN:
        items.insert(0, {
            "id": b["id"], "name": b["name"], "desc": b.get("desc", ""),
            "scope": "builtin", "builtin": True, "created_at": "", "updated_at": "",
            "has_rates": False,
        })
    return items


def get(preset_id: str) -> dict | None:
    """取一个预设（内置或自定义）。"""
    pid = (preset_id or "").strip()
    for b in BUILT_IN:
        if b["id"] == pid:
            return {**b, "builtin": True, "scope": "builtin"}
    p = (_read().get("presets") or {}).get(pid)
    return p if isinstance(p, dict) else None


def _config_doc() -> dict:
    p = paths.CONFIG / "formulas.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _rates_doc() -> dict:
    p = paths.RATES_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def capture(name: str, scope: str = "global", include_rates: bool = False,
            desc: str = "") -> dict:
    """把当前配置存成预设。

    scope = "global" 或店铺 id（内部存成 "shop:<id>"）。
    """
    nm = (name or "").strip()
    if not nm:
        return {"ok": False, "error": "预设名字不能为空"}
    sc = (scope or "global").strip()
    scope_key = "global" if sc in ("", "global") else f"shop:{sc}"

    doc = _config_doc()
    content: dict = {
        "params": doc.get("params") or [],
        "formulas": doc.get("formulas") or {},
        "order_fields": doc.get("order_fields") or {},
        "cost_fields": doc.get("cost_fields") or {},
        "by_country": doc.get("by_country") or {},
        "exclude_zero_amount": bool(doc.get("exclude_zero_amount")),
    }
    if include_rates:
        content["rates"] = _rates_doc().get("countries") or {}

    allp = _read()
    presets = allp.get("presets") or {}
    pid = _safe_id(nm)
    n = 2
    base = pid
    while pid in presets:
        pid = f"{base}-{n}"
        n += 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    presets[pid] = {
        "id": pid, "name": nm, "desc": desc or "",
        "scope": scope_key, "created_at": now, "updated_at": now,
        "content": content,
    }
    allp["presets"] = presets
    allp.setdefault("_readme", ["预设：一整套口径的命名快照。作用域 global=通用，shop:<店铺ID>=只对某家店生效。"])
    _write(allp)
    return {"ok": True, "id": pid, "name": nm, "scope": scope_key}


def update(preset_id: str, name: str = "", desc: str = "",
           include_rates: bool | None = None) -> dict:
    """更新已有预设（用当前配置覆盖它的内容）。"""
    pid = (preset_id or "").strip()
    allp = _read()
    presets = allp.get("presets") or {}
    p = presets.get(pid)
    if not isinstance(p, dict):
        return {"ok": False, "error": "找不到这个预设"}
    if name.strip():
        p["name"] = name.strip()
    if desc:
        p["desc"] = desc
    if include_rates:
        p.setdefault("content", {})["rates"] = _rates_doc().get("countries") or {}
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    presets[pid] = p
    allp["presets"] = presets
    _write(allp)
    return {"ok": True, "id": pid, "name": p.get("name")}


def delete(preset_id: str) -> dict:
    pid = (preset_id or "").strip()
    allp = _read()
    presets = allp.get("presets") or {}
    if pid not in presets:
        return {"ok": False, "error": "找不到这个预设"}
    presets.pop(pid, None)
    allp["presets"] = presets
    _write(allp)
    return {"ok": True, "id": pid}


def backup(tag: str = "") -> dict:
    """备份当前配置，返回备份文件路径。"""
    d = paths.CONFIG / BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = re.sub(r"[^\w-]+", "", tag or "")[:24]
    name = f"{stamp}{('-' + tag) if tag else ''}"
    out: list[str] = []
    for src, dst in ((paths.CONFIG / "formulas.json", d / f"{name}-formulas.json"),
                     (paths.RATES_FILE, d / f"{name}-rates.json")):
        if src.exists():
            try:
                shutil.copy2(src, dst)
                out.append(str(dst))
            except OSError:
                pass
    # 只留最近 20 份备份，多了删旧的（备份不是数据，丢了能重来）
    try:
        for f in sorted(d.glob("*-formulas.json"), reverse=True)[20:]:
            f.unlink(missing_ok=True)
            f.with_name(f.name.replace("-formulas.json", "-rates.json")).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True, "files": out}


def apply(preset_id: str, country: str = "") -> dict:
    """应用预设：把配置改成预设里的样子。

    内置预设只改「勾了哪些订单类型」，公式和标题保持当前值；
    自定义预设是完整快照，整体替换。
    """
    p = get(preset_id)
    if not p:
        return {"ok": False, "error": "找不到这个预设"}

    bk = backup(preset_id)

    if p.get("builtin"):
        doc = _config_doc()
        overrides = p.get("kind_overrides") or {}
        params = []
        for it in (doc.get("params") or []):
            it = dict(it)
            if it.get("id") in overrides:
                it["kinds"] = list(overrides[it["id"]])
            params.append(it)
        doc["params"] = params
        if "exclude_zero_amount" in p:
            if p.get("exclude_zero_amount"):
                doc["exclude_zero_amount"] = True
            else:
                doc.pop("exclude_zero_amount", None)
        from src import safeio
        safeio.write_json(paths.CONFIG / "formulas.json", doc)
        return {"ok": True, "applied": p.get("name"), "builtin": True,
                "backup": bk.get("files", []),
                "note": "只调整了订单类型的勾选，公式与列标题保持原样"}

    content = p.get("content") or {}
    doc = _config_doc()
    for key in ("params", "formulas", "order_fields", "cost_fields", "by_country"):
        if key in content:
            doc[key] = content[key]
    if content.get("exclude_zero_amount"):
        doc["exclude_zero_amount"] = True
    else:
        doc.pop("exclude_zero_amount", None)
    from src import safeio
    safeio.write_json(paths.CONFIG / "formulas.json", doc)

    # 费率：按国家合并，只覆盖预设里有的国家
    rates = content.get("rates") or {}
    if rates:
        rdoc = _rates_doc()
        countries = dict(rdoc.get("countries") or {})
        for cname, vals in rates.items():
            if isinstance(vals, dict):
                countries[cname] = {**countries.get(cname, {}), **vals}
        rdoc["countries"] = countries
        safeio.write_json(paths.RATES_FILE, rdoc)

    return {"ok": True, "applied": p.get("name"), "builtin": False,
            "backup": bk.get("files", []),
            "rates_applied": sorted(rates.keys()) if rates else []}


def backups() -> list[dict]:
    """列出可回滚的备份。"""
    d = paths.CONFIG / BACKUP_DIR
    out: list[dict] = []
    try:
        for f in sorted(d.glob("*-formulas.json"), reverse=True)[:20]:
            st = f.stat()
            out.append({
                "name": f.name.replace("-formulas.json", ""),
                "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "has_rates": f.with_name(f.name.replace("-formulas.json", "-rates.json")).exists(),
            })
    except OSError:
        pass
    return out


def rollback(name: str) -> dict:
    """回滚到某份备份。"""
    nm = re.sub(r"[^\w-]+", "", (name or "").strip())
    if not nm:
        return {"ok": False, "error": "备份名不对"}
    d = paths.CONFIG / BACKUP_DIR
    src = d / f"{nm}-formulas.json"
    if not src.exists():
        return {"ok": False, "error": "找不到这份备份"}
    backup("before-rollback")
    try:
        shutil.copy2(src, paths.CONFIG / "formulas.json")
        rsrc = d / f"{nm}-rates.json"
        if rsrc.exists():
            shutil.copy2(rsrc, paths.RATES_FILE)
    except OSError as e:
        return {"ok": False, "error": f"回滚失败：{e}"}
    return {"ok": True, "rolled_back": nm}
