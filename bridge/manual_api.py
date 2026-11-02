"""手动核算：自己传一批表，单独算一份，不掺店铺的自动取数数据。

为什么单开一块
--------------
驾驶舱那条线是：扩展自动取数 → 店铺收件箱 → 重算。数据属于店铺，
不该有人往里面塞别的东西——一旦混进去，下次重算就会把它们算进来，
数字对不上，还查不出是哪份文件带的。

可有些时候就是想拿一批表单独算算：供应商给的表、从别人电脑上拷来的、
想试试某个口径。这些数据不该进店铺收件箱，也不该被下次自动核算带上。

所以给它们自己的地方：

    数据/手动核算/<批次>/      ← 这一批的文件（订单/广告/佣金/SKU 混放，按表头认）
    报表/手动核算/<批次>/      ← 这一批算出来的 Excel

「批次」= 一批数据的名字（默认「手动_日期时间」）。换一批就换一个目录，
互不干扰；删也只删这一批，不会动到店铺数据。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from src import paths, safeio

ALLOWED_SUFFIX = {".xlsx", ".xls", ".csv", ".txt", ".json"}
MAX_FILE = 60 * 1024 * 1024          # 单文件 60MB，和「导入数据」保持一致
NAME_MAX = 40                        # 批次名太长会把路径顶爆（Windows 260）
KEEP_BATCHES = 100                   # 列表最多列这么多批，免得越攒越慢

KIND_CN = {
    "order": "订单表",
    "ads": "广告表",
    "affiliate": "联盟佣金表",
    "mapping": "商品SKU/映射",
    "": "未识别",
}


def root() -> Path:
    return paths.DATA / "手动核算"


def report_root() -> Path:
    return paths.OUTPUT / "手动核算"


def safe_name(name: str, fallback: str = "手动批次") -> str:
    """批次名 / 文件名消毒：去掉路径分隔符和 Windows 不认的字符。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    if not s:
        s = fallback
    return s[:NAME_MAX]


def batch_dir(name: str) -> Path:
    return root() / safe_name(name)


def _meta_file(d: Path) -> Path:
    return d / "批次.json"


def load_meta(d: Path) -> dict:
    doc = safeio.read_json(_meta_file(d), default={})
    return doc if isinstance(doc, dict) else {}


def save_meta(d: Path, doc: dict) -> None:
    safeio.write_json(_meta_file(d), doc, indent=1)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _recycle(what: str) -> Path:
    """回收站目录：手动核算删掉的东西都挪这儿，不真删。"""
    d = paths.DATA / "回收站" / f"手动核算_{datetime.now():%Y%m%d_%H%M%S}_{what}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _classify(path: Path, country: str = "") -> str:
    """按表头认这是哪种表。认不出返回空串（文件仍然留着，不会丢）。"""
    try:
        from src import formula as _formula
        from src.loader import classify_rows, merged_aliases, read_table

        aliases = None
        if country:
            cfg = _formula.load_config(paths.CONFIG / "formulas.json")
            cc = _formula.country_config(cfg, country)
            aliases = merged_aliases(cc.get("order_fields"))
        return classify_rows(read_table(path), aliases) or ""
    except Exception:  # noqa: BLE001
        return ""


def _file_records(d: Path, meta: dict) -> list[dict]:
    """这批目录里实际有哪些文件（以磁盘为准，防止有人在资源管理器里删/加）。"""
    kinds = {}
    for f in (meta.get("files") or []):
        if isinstance(f, dict) and f.get("name"):
            kinds[str(f["name"])] = str(f.get("kind") or "")
    out: list[dict] = []
    try:
        for p in d.iterdir():
            if not p.is_file() or p.name == "批次.json" or p.name.startswith("."):
                continue
            if p.suffix.lower() not in ALLOWED_SUFFIX:
                continue
            st = p.stat()
            out.append({
                "name": p.name,
                "kind": kinds.get(p.name, ""),
                "label": KIND_CN.get(kinds.get(p.name, ""), "未识别"),
                "size_kb": round(st.st_size / 1024, 1),
                "bytes": st.st_size,
                "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime": st.st_mtime,
            })
    except OSError:
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def describe(d: Path) -> dict:
    """一批数据的概况：给界面列表用。"""
    meta = load_meta(d)
    files = _file_records(d, meta)
    kinds: dict[str, int] = {}
    for f in files:
        k = f["kind"] or "other"
        kinds[k] = kinds.get(k, 0) + 1
    try:
        created = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        created = ""
    return {
        "name": d.name,
        "created": meta.get("created") or created,
        "country": meta.get("country") or "",
        "from": meta.get("from") or "",
        "to": meta.get("to") or "",
        "files": files,
        "count": len(files),
        "kinds": kinds,
        "has_orders": any(f["kind"] == "order" for f in files),
        "last": meta.get("last") or None,
    }


def list_batches() -> list[dict]:
    base = root()
    out: list[dict] = []
    if not base.is_dir():
        return out
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return out
    dirs.sort(key=lambda d: _mtime(d), reverse=True)
    for d in dirs[:KEEP_BATCHES]:
        out.append(describe(d))
    return out


def _mtime(d: Path) -> float:
    try:
        return d.stat().st_mtime
    except OSError:
        return 0.0


def create(name: str = "") -> dict:
    """新建一批。名字重复就加 (2)。"""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    nm = safe_name(name or f"手动_{datetime.now():%Y%m%d_%H%M}")
    d = base / nm
    i = 2
    while d.exists():
        d = base / f"{nm} ({i})"
        i += 1
        if i > 99:
            return {"ok": False, "error": "同名批次太多了，换个名字"}
    d.mkdir(parents=True)
    save_meta(d, {"version": 1, "name": d.name, "created": _now(),
                  "country": "", "from": "", "to": "", "files": [], "last": None})
    return {"ok": True, "batch": describe(d)}


def save_file(batch: str, filename: str, data: bytes, country: str = "") -> dict:
    """把一份表存进这批。返回识别出的类型，供界面显示。"""
    d = batch_dir(batch)
    if not d.is_dir():
        return {"ok": False, "error": f"找不到批次「{batch}」，先新建一批"}

    raw = Path(str(filename or "")).name
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        return {"ok": False, "error": f"不支持的文件类型：{suffix or '（没有扩展名）'}"}
    if len(data) > MAX_FILE:
        return {"ok": False, "error": f"文件超过 {MAX_FILE // 1024 // 1024} MB"}

    safe = re.sub(r'[\\/:*?"<>|]', "_", raw)[:110] or f"文件{suffix}"
    target = d / safe
    i = 2
    stem, suf = target.stem, target.suffix
    while target.exists():
        target = d / f"{stem} ({i}){suf}"
        i += 1

    try:
        safeio.write_bytes(target, data, backup=False)
    except OSError as e:
        return {"ok": False, "error": f"保存失败：{e}"}

    kind = _classify(target, country)
    meta = load_meta(d)
    files = [f for f in (meta.get("files") or [])
             if isinstance(f, dict) and f.get("name") != target.name]
    files.append({"name": target.name, "kind": kind, "at": _now()})
    meta["files"] = files
    if country and not meta.get("country"):
        meta["country"] = country
    try:
        save_meta(d, meta)
    except OSError:
        pass
    # 目录 mtime 变化 = 这批最近动过，列表按它排
    try:
        d.touch()
    except OSError:
        pass
    return {"ok": True, "name": target.name, "kind": kind,
            "label": KIND_CN.get(kind, "未识别"),
            "count": len(_file_records(d, meta))}


def delete_file(batch: str, filename: str) -> dict:
    d = batch_dir(batch)
    p = d / Path(str(filename or "")).name
    if not p.is_file():
        return {"ok": False, "error": "文件已经不在了"}
    try:
        dst = _recycle("文件") / p.name
        p.replace(dst)
    except OSError as e:
        return {"ok": False, "error": f"删不掉：{e}"}
    meta = load_meta(d)
    meta["files"] = [f for f in (meta.get("files") or [])
                     if isinstance(f, dict) and f.get("name") != p.name]
    try:
        save_meta(d, meta)
    except OSError:
        pass
    return {"ok": True, "trash": str(dst)}


def delete_batch(batch: str) -> dict:
    d = batch_dir(batch)
    if not d.is_dir():
        return {"ok": False, "error": "这批已经不在了"}
    trash = _recycle("批次")
    try:
        shutil.move(str(d), str(trash / d.name))
    except OSError as e:
        return {"ok": False, "error": f"删不掉（文件可能被 Excel 开着）：{e}"}
    return {"ok": True, "trash": str(trash)}


def _unique_report(out: Path) -> Path:
    """同名报表：旧的挪进回收站，用回同一个名字。

    手动核算的报表是按「批次 + 区间」命名的，重算同一批同一段就该覆盖，
    不然点三次就 (2)(3)(4)——这个坑在自动核算那边已经踩过一次。
    """
    if not out.exists():
        return out
    try:
        shutil.move(str(out), str(_recycle("报表") / out.name))
        return out
    except OSError:
        # 正开着挪不动，退回另存一份，别让计算白跑
        i = 2
        stem, suf = out.stem, out.suffix
        while out.exists():
            out = out.parent / f"{stem} ({i}){suf}"
            i += 1
        return out


def run(batch: str, country: str = "", dfrom: str = "", dto: str = "",
        with_formula: bool = False) -> dict:
    """算这一批。用主程序自己的核算引擎，只是数据目录换成这批的目录。"""
    d = batch_dir(batch)
    if not d.is_dir():
        return {"ok": False, "error": f"找不到批次「{batch}」"}

    from src import loader as _ld

    try:
        has_orders = _ld.has_orders_table(d)
    except Exception:  # noqa: BLE001
        has_orders = True
    if not has_orders:
        return {"ok": False, "error": "这批里没有订单表。先传一份订单导出（表头要带 Order ID 和金额列）"}

    a, b = str(dfrom or ""), str(dto or "")
    if a and b and b < a:
        a, b = b, a
    label = f"{a}_{b}" if (a and b) else "全部"

    out_dir = report_root() / d.name
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"建报表目录失败：{e}"}
    out = _unique_report(out_dir / f"{d.name}_{label}.xlsx")

    if paths.is_frozen():
        cmd = [sys.executable, "run"]
    else:
        cmd = [sys.executable, "-m", "src.cli"]
    cmd += ["--dir", str(d), "--out", str(out), "--json"]
    if country:
        cmd += ["--country", country]
    if a:
        cmd += ["--from", a]
    if b:
        cmd += ["--to", b]
    if with_formula:
        cmd += ["--with-formula"]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            cmd, cwd=str(paths.ROOT), capture_output=True, env=env, timeout=300,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                           if sys.platform == "nt" else 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "算超过 5 分钟没跑完，已中断（多半是文件太大或格式有问题）"}
    except OSError as e:
        return {"ok": False, "error": f"启动核算失败：{e}"}

    def _dec(raw: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    stdout = _dec(proc.stdout or b"")
    stderr = _dec(proc.stderr or b"")
    summary = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                summary = json.loads(line)
            except ValueError:
                continue
            break

    if proc.returncode != 0 or not summary:
        tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-8:])
        if "没有解析出任何订单行" in tail:
            brief = "订单表里没有可用的行（表头不对，或者这批文件不是订单导出）"
        elif "没有解析出任何商品" in tail:
            brief = "没有解析出商品：这批里缺商品SKU/映射表"
        elif "读不了「" in tail:
            brief = "有表格读不出来：多半是文件正在 Excel/WPS 里开着，关掉再算"
        else:
            brief = tail or "核算失败"
        return {"ok": False, "error": brief}

    summary["report_path"] = str(out)
    summary["report_name"] = out.name
    summary["batch"] = d.name

    meta = load_meta(d)
    meta["from"], meta["to"] = a, b
    if country:
        meta["country"] = country
    meta["last"] = {
        "at": _now(),
        "net": summary.get("net_local"),
        "net_cny": summary.get("net_cny"),
        "gmv": summary.get("gmv"),
        "margin": summary.get("net_margin"),
        "report": out.name,
    }
    try:
        save_meta(d, meta)
    except OSError:
        pass
    return {"ok": True, **summary}
