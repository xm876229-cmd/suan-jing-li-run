"""给界面用的接口：费率、报表预览、历史、调试信息。

都只读本机文件，不联网。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from src import formula as _formula
from src import paths

# ---------------- 费率 ----------------


def read_rates() -> dict:
    try:
        from src import safeio
        doc = safeio.read_json(paths.RATES_FILE, default={}) or {}
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"读取 rates.json 失败：{e}", "countries": {}}

    fx = doc.get("fx") or {}
    # 费率项的中文名（引擎权威版，带「率」字：平台佣金率 / 发单费单价 / 人民币汇率…）。
    # 公式解析器只认这套名字，前端「不认识」的校验和变量候选必须跟它对齐，
    # 否则用户在公式里写「平台佣金率」会被判不认识。用户自定义名覆盖在后面。
    labels = doc.get("labels") or {}
    rate_cn = {**_formula.DEFAULT_RATE_CN, **{
        str(k): str(v).strip() for k, v in labels.items()
        if str(k).strip() and str(v).strip()}}
    return {
        "ok": True,
        "countries": doc.get("countries") or {},
        # 费率项的中文名：公式里可以直接写中文，不用记英文
        "labels": labels,
        # 引擎权威中文名（含用户自定义覆盖），前端校验/候选对齐用
        "rate_cn": rate_cn,
        "fx": {"mode": fx.get("mode", "auto"), "manual": fx.get("manual") or {},
               "ttl_hours": fx.get("ttl_hours", 12)},
    }


def save_rates(payload: dict) -> dict:
    """保存国家费率。

    只覆盖 countries 和 fx，其余字段（_comment 等说明文字）原样保留，
    免得用户辛苦写的注释被程序冲掉。
    """
    try:
        from src import safeio
        doc = safeio.read_json(paths.RATES_FILE, default={}) or {}
    except (OSError, json.JSONDecodeError):
        doc = {}

    if isinstance(payload.get("countries"), dict):
        doc["countries"] = payload["countries"]
    if isinstance(payload.get("fx"), dict):
        doc.setdefault("fx", {}).update(payload["fx"])
    if isinstance(payload.get("labels"), dict):
        # 费率项的中文名。空的名字不写，免得占位。
        labels = {str(k): str(v).strip() for k, v in payload["labels"].items()
                  if str(k).strip() and str(v).strip()}
        if labels:
            doc["labels"] = labels

    try:
        from src import safeio
        safeio.write_json(paths.RATES_FILE, doc)
    except OSError as e:
        return {"ok": False, "error": f"费率保存失败（磁盘满或被占用？）：{e}"}
    return {"ok": True, "countries": len(doc.get("countries") or {})}


# ---------------- 报表预览 ----------------

def _find_report(name: str, rel: str = "") -> Path | None:
    """找一份报表。

    rel 是相对 output 目录的路径（如「马来一号店/报表.xlsx」）。
    传了 rel 就按它找——不同店铺的报表可能重名，只按文件名找会张冠李戴。
    """
    if rel:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return None
        p = paths.OUTPUT / rel
        # 只认「报表目录里面」的文件。
        # 少了这一步，rel 传一个绝对路径（C:/Windows/...）就能把本机文件读出来；
        # 桥的 CORS 是全开的，随便一个网页都能来问一次 —— 这种口子不能留。
        try:
            if not p.resolve().is_relative_to(paths.OUTPUT.resolve()):
                return None
        except (OSError, ValueError):
            return None
        return p if p.is_file() else None
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.lower().endswith(".xlsx"):
        return None
    # 报表可能分店放，也可能还在根目录，两边都找
    base = paths.OUTPUT.resolve()
    # 三处都要找：老布局（报表/名字）、按店分（报表/店/名字）、
    # 新布局分房间（报表/自动/店/名字、报表/手动/店/名字）
    for p in [paths.OUTPUT / name, *paths.OUTPUT.glob(f"*/{name}"),
              *paths.OUTPUT.glob(f"*/*/{name}")]:
        try:
            if p.is_file() and p.resolve().is_relative_to(base):
                return p
        except (OSError, ValueError):
            continue
    return None


def reports_zip(names: list[str]) -> tuple[bytes, str]:
    """把选中的报表打成一个 zip。

    names 里是相对 output 目录的路径。zip 内保留店铺子目录，
    这样解压后还是分店放着的，不会一堆同名文件糊在一起。
    """
    import io
    import zipfile

    buf = io.BytesIO()
    picked: list[tuple[str, Path]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in names:
            f = _find_report("", rel)
            if not f:
                continue
            try:
                arc = str(f.relative_to(paths.OUTPUT)).replace("\\", "/")
            except ValueError:
                arc = f.name
            z.write(f, arc)
            picked.append((arc, f))
    if not picked:
        return b"", ""
    label = picked[0][0].split("/")[0] if len(picked) == 1 else "净利报表"
    return buf.getvalue(), f"{label}.zip"


def report_view(name: str, rel: str = "") -> dict:
    """把 xlsx 的汇总页与费用页读出来给界面显示。

    目的：想知道「这个月净利多少、钱花在哪」，不该逼用户打开 Excel。
    """
    f = _find_report(name, rel)
    if not f:
        return {"ok": False, "error": "找不到这个报表"}

    try:
        from openpyxl import load_workbook
        wb = load_workbook(f, data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取失败：{e}"}

    def sheet_rows(title: str, limit: int = 40, max_col: int = 0) -> list[list]:
        """读一页表格。

        汇总页的「净利」在十几列之后，截断到前若干列就取不到，
        所以默认不截（max_col=0）；费用拆解页结构简单，截 6 列就够。
        """
        if title not in wb.sheetnames:
            return []
        ws = wb[title]
        out: list[list] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= limit:
                break
            if row is None or all(c is None for c in row):
                continue
            cells = list(row)
            # 去掉尾部连续空列，免得表格拉出一堆空白
            while cells and cells[-1] is None:
                cells.pop()
            if max_col:
                cells = cells[:max_col]
            out.append(["" if c is None else c for c in cells])
        return out

    # 汇总页第一行是标题，第二行是表头，后面是数据
    summary = sheet_rows("汇总", 60)
    fees = sheet_rows("费用拆解", 40, max_col=6)
    daily = sheet_rows("日趋势", 80)

    # 从汇总页里找「合计」那一行，给界面做顶部大数字
    total: dict[str, Any] = {}
    header: list = []
    for row in summary:
        cells = [str(c) for c in row]
        if not header and any("GMV" in c or "商品名称" in c for c in cells):
            header = cells
            continue
        # 合计行不一定在第一列：这张表里「合计」写在第二列，
        # 所以整行找一个就算，别死盯 cells[0]。
        if header and any(("合计" in c or "总计" in c) for c in cells):
            for k, v in zip(header, cells):
                key = str(k).strip()
                if key:
                    total[key] = v
            break

    # 汇总页只到「交易手续费」为止，净利不在这页——
    # 它在「日趋势」页里，费用合计要自己把「费用拆解」的金额累加起来。
    # 不补这两步，界面顶部的净利卡片永远是空的。
    def _num(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    fee_sum = 0.0
    for row in fees[2:] if len(fees) > 2 else []:
        # 费用拆解页：商品ID / 款式编码 / 费用项 / 金额 ...
        if len(row) >= 4:
            fee_sum += _num(row[3])
    # 汇总页的合计行已经带「费用合计」时以它为准；
    # 费用拆解只读了前几十行，拿它覆盖会把总额算小（曾经少算过近 800）。
    if "费用合计" not in total and fee_sum:
        total["费用合计"] = round(fee_sum, 2)

    net_sum = 0.0
    net_col = None
    for row in daily[1:]:
        cells = [str(c) for c in row]
        if net_col is None:
            idx = next((i for i, c in enumerate(cells) if "净利" in c), None)
            if idx is not None:
                net_col = idx
            continue
        if net_col < len(row):
            net_sum += _num(row[net_col])
    if total.get("净利(本地货币)") not in (None, ""):
        total["净利"] = round(_num(total["净利(本地货币)"]), 2)
    elif net_col is not None:
        total["净利"] = round(net_sum, 2)

    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "name": f.name,
        "size_kb": round(f.stat().st_size / 1024),
        "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "summary": summary,
        "fees": fees,
        "daily": daily,
        "total": total,
    }


# ---------------- 核算历史 ----------------

HISTORY_FILE = paths.DATA / "history.jsonl"


def append_history(rec: dict) -> None:
    """每次核算完记一笔，界面上画走势用。"""
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def history(limit: int = 60) -> dict:
    items: list[dict] = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return {"ok": True, "items": items[-limit:]}


# ---------------- 调试 ----------------


def debug_info() -> dict:
    """界面上的「调试」页：出问题时有东西可看，不用去翻目录。"""
    log_tail: list[str] = []
    log_file = paths.CAPTURE / "task.log"
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as fh:
            log_tail = [ln.rstrip() for ln in fh.readlines()[-120:]]
    except OSError:
        pass

    bridge_log: list[str] = []
    bf = paths.CAPTURE / "bridge_run.log"
    try:
        with bf.open("r", encoding="utf-8", errors="replace") as fh:
            bridge_log = [ln.rstrip() for ln in fh.readlines()[-60:]]
    except OSError:
        pass

    captures: list[dict] = []
    try:
        for p in sorted(paths.CAPTURE.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime)[:8]:
            st = p.stat()
            captures.append({
                "name": p.name,
                "size_mb": round(st.st_size / 1048576, 2),
                "at": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
            })
    except OSError:
        pass

    return {
        "ok": True,
        "task_log": log_tail,
        "bridge_log": bridge_log,
        "captures": captures,
        "paths": paths.describe(),
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
    }

# ---------------- 列映射 ----------------

def read_mapping() -> dict:
    """读 config/mapping.json：列标题 → 逻辑字段。

    每行订单表的列名会变（平台改版、多语言），所以一个逻辑字段可以配
    任意多个候选标题，按顺序取第一个能对上的。
    """
    from src.loader import get_aliases

    doc: dict = {}
    try:
        doc = json.loads(paths.CONFIG.joinpath("mapping.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}

    aliases = get_aliases()
    fields = []
    for key, cands in aliases.items():
        meta = (doc.get("fields") or {}).get(key) or {}
        fields.append({
            "id": key,
            "label": str(meta.get("label") or key),
            "candidates": list(cands),
        })
    return {"ok": True, "fields": fields}


def save_mapping(fields: list) -> dict:
    """保存列映射。保留文件里的 _readme 说明，别冲掉。"""
    target = paths.CONFIG / "mapping.json"
    old: dict = {}
    try:
        old = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}

    out: dict = {}
    if old.get("_readme"):
        out["_readme"] = old["_readme"]
    clean: dict = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or "").strip()
        if not fid:
            continue
        cands = [str(c).strip() for c in (f.get("candidates") or []) if str(c).strip()]
        # 去重但保持顺序
        seen: set = set()
        uniq = []
        for c in cands:
            if c.lower() in seen:
                continue
            seen.add(c.lower())
            uniq.append(c)
        clean[fid] = {"label": str(f.get("label") or fid), "candidates": uniq}
    out["fields"] = clean

    try:
        from src import safeio
        safeio.write_json(target, out)
    except OSError as e:
        return {"ok": False, "error": f"保存失败：{e}"}
    return {"ok": True, "fields": len(clean)}
