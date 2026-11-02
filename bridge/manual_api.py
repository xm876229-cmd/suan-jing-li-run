"""手动核算：按店铺放一批自己传的表，单独算，不掺自动取数的数据。

为什么按店铺
------------
用户的心智很简单：这批表是「LR Home 的」，那就算 LR Home 的。
所以手动数据也按店铺分，一家店一个抽屉：

    数据/店铺/<店名>/手动导入/     ← 手动数据（跟「收件箱」平级）
    数据/店铺/<店名>/收件箱/       ← 自动取数（重算只读这个）
    报表/<店名>/手动/              ← 手动算出来的报表

自动核算只读「收件箱」，所以手动数据天然不会掺进去；反过来，
手动核算也只读「手动导入」，不会把自动取的数据算进来。

另外给一个「临时」抽屉（`数据/手动核算/临时/`），放不属于任何店铺的表
（供应商对账、别人电脑拷来的一批）。它的报表落在 `报表/临时手动/`。

「批次」这个概念去掉了：同一家店的手动数据就是一个抽屉，文件累积、
按区间挑（跟自动取数一个逻辑），传错了单个删。
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

STORE_SUB = "手动导入"                # 店铺目录下手动数据的位置
TMP_KEY = "_tmp"                     # 临时抽屉的 key
TMP_DIR = "临时"
RECORD = "手动记录.json"              # 每次算完记一笔（净利/报表名）

KIND_CN = {
    "order": "订单表",
    "ads": "广告表",
    "affiliate": "联盟佣金表",
    "mapping": "商品SKU/映射",
    "": "未识别",
}


# ---------------- 空间（= 一家店的手动抽屉，或临时抽屉） ----------------

def tmp_dir() -> Path:
    return paths.DATA / "手动核算" / TMP_DIR


def space_dir(key: str) -> Path:
    k = str(key or "").strip()
    if k == TMP_KEY or not k:
        return tmp_dir()
    return paths.store_root(k) / STORE_SUB


def _meta(shop_id: str) -> dict:
    try:
        return json.loads((paths.store_root(shop_id) / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def known_keys() -> list[str]:
    """有档案的店铺 + 临时抽屉。别的 key 一律不认，免得建出一堆野目录。

    顺序跟驾驶舱一致（`paths.store_index()`，按店名排），
    这样两个页面的卡片一一对应，找同一家店不用重新找。
    """
    out: list[str] = [TMP_KEY]
    out.extend(it["shop_id"] for it in paths.store_index())
    return out


def _record_file(d: Path) -> Path:
    return d / RECORD


def load_record(d: Path) -> dict:
    doc = safeio.read_json(_record_file(d), default={})
    return doc if isinstance(doc, dict) else {}


def save_record(d: Path, doc: dict) -> None:
    safeio.write_json(_record_file(d), doc, indent=1)


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


def _file_records(d: Path, doc: dict) -> list[dict]:
    """抽屉里实际有哪些文件（以磁盘为准，防止有人在资源管理器里删/加）。"""
    kinds = {}
    for f in (doc.get("files") or []):
        if isinstance(f, dict) and f.get("name"):
            kinds[str(f["name"])] = str(f.get("kind") or "")
    out: list[dict] = []
    try:
        for p in d.iterdir():
            if not p.is_file() or p.name == RECORD or p.name.startswith("."):
                continue
            if p.suffix.lower() not in ALLOWED_SUFFIX:
                continue
            st = p.stat()
            k = kinds.get(p.name, "")
            out.append({
                "name": p.name,
                "kind": k,
                "label": KIND_CN.get(k, "未识别"),
                "size_kb": round(st.st_size / 1024, 1),
                "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime": st.st_mtime,
            })
    except OSError:
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def describe(key: str) -> dict:
    """一个抽屉的概况：卡片上那几行就靠它。"""
    k = str(key or "").strip()
    if k != TMP_KEY and k not in known_keys():
        return {}
    d = space_dir(k)
    doc = load_record(d)
    files = _file_records(d, doc)
    kinds: dict[str, int] = {}
    for f in files:
        kk = f["kind"] or "other"
        kinds[kk] = kinds.get(kk, 0) + 1
    if k == TMP_KEY:
        name, shop_name, region, country = "临时（不属于店铺）", "", "", doc.get("country") or ""
    else:
        meta = _meta(k)
        # 卡片标题用「店目录名」（用户起的别名，也是报表文件夹名），
        # 平台上的真实店名放副标题——跟驾驶舱卡片一致，两边能对上号
        name = paths.store_root(k).name or k
        shop_name = str(meta.get("shop_name") or "")
        region = str(meta.get("region") or "")
        country = str(meta.get("country") or doc.get("country") or "")
    try:
        created = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        created = ""
    return {
        "key": k,
        "kind": "tmp" if k == TMP_KEY else "store",
        "name": name,
        "shop_name": shop_name,
        "region": region,
        "country": country,
        "dir": str(d),
        "files": files,
        "count": len(files),
        "kinds": kinds,
        "has_orders": any(f["kind"] == "order" for f in files),
        "last_file": files[0]["at"] if files else "",
        "last": doc.get("last") or None,
        "created": created,
    }


def list_spaces() -> list[dict]:
    """所有店铺的抽屉，顺序固定按店名（跟驾驶舱一致）。

    以前是「有手动数据的排前面」——导完一批表，那张卡就跳到第一位，
    人刚记住的位置就没了。现在顺序只跟店名有关，导入、计算都不动位置。
    """
    out: list[dict] = []
    for k in known_keys():
        if k == TMP_KEY:
            continue
        item = describe(k)
        if item:
            out.append(item)
    # 只列店铺：手动核算在界面上就是「这家店的手动数据」。
    # 临时抽屉（不属于任何店）先不摆出来，接口还留着，以后要加再说。
    return out


# ---------------- 文件 ----------------

def _recycle(what: str) -> Path:
    d = paths.DATA / "回收站" / f"手动_{datetime.now():%Y%m%d_%H%M%S}_{what}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_file(key: str, filename: str, data: bytes, country: str = "") -> dict:
    """把一份表存进这家店（或临时）的手动抽屉。"""
    k = str(key or "").strip()
    if k != TMP_KEY and k not in known_keys():
        return {"ok": False, "error": "不认识的店铺，先在扩展里认一次店"}
    d = space_dir(k)
    if not d.is_dir():
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"建目录失败：{e}"}

    raw = Path(str(filename or "")).name
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        return {"ok": False, "error": f"不支持的文件类型：{suffix or '（没有扩展名）'}"}
    if len(data) > MAX_FILE:
        return {"ok": False, "error": f"文件超过 {MAX_FILE // 1024 // 1024} MB"}

    safe = re.sub(r'[\\/:*?"<>|]', "_", raw)[:110] or f"文件{suffix}"
    target = d / safe
    # 同名 = 同一份表重导了一次：覆盖，不堆 (2)(3)。
    # 旧的那份先挪进回收站（想找回还能找），再写新的；挪不动（Excel 开着）
    # 就退回另存一份，别让人传不进来。
    if target.exists():
        try:
            _tr = _recycle("旧文件")
            _dst = _tr / target.name
            if _dst.exists():
                _dst = _tr / f"{target.stem} ({datetime.now():%H%M%S}){target.suffix}"
            shutil.move(str(target), str(_dst))
        except OSError:
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
    doc = load_record(d)
    files = [f for f in (doc.get("files") or [])
             if isinstance(f, dict) and f.get("name") != target.name]
    files.append({"name": target.name, "kind": kind,
                  "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    doc["files"] = files
    if country and not doc.get("country"):
        doc["country"] = country
    try:
        save_record(d, doc)
    except OSError:
        pass
    return {"ok": True, "name": target.name, "kind": kind,
            "label": KIND_CN.get(kind, "未识别"), "count": len(_file_records(d, doc))}


def delete_file(key: str, filename: str) -> dict:
    d = space_dir(key)
    p = d / Path(str(filename or "")).name
    if not p.is_file():
        return {"ok": False, "error": "文件已经不在了"}
    try:
        dst = _recycle("文件") / p.name
        p.replace(dst)
    except OSError as e:
        return {"ok": False, "error": f"删不掉（可能正被 Excel 打开）：{e}"}
    doc = load_record(d)
    doc["files"] = [f for f in (doc.get("files") or [])
                    if isinstance(f, dict) and f.get("name") != p.name]
    try:
        save_record(d, doc)
    except OSError:
        pass
    return {"ok": True, "trash": str(dst)}


def clear_space(key: str) -> dict:
    """清空一家店的手动数据：所有文件挪进回收站，目录留着。

    「清空」和「删除文件」是一个逻辑——不真删，挪到 数据\回收站，
    想找回还能找。算过的记录也一并清掉（下次从「还没算过」开始）。
    """
    k = str(key or "").strip()
    if k != TMP_KEY and k not in known_keys():
        return {"ok": False, "error": "不认识的店铺"}
    d = space_dir(k)
    if not d.is_dir():
        return {"ok": True, "moved": 0}
    trash = _recycle("清空")
    moved = 0
    for f in list(d.iterdir()):
        if f.is_file() and f.name != RECORD:
            try:
                shutil.move(str(f), str(trash / f.name))
                moved += 1
            except OSError:
                continue
    try:
        save_record(d, {"version": 1, "files": [], "last": None})
    except OSError:
        pass
    return {"ok": True, "moved": moved, "trash": str(trash)}


# ---------------- 计算 ----------------

def _unique_report(out: Path) -> Path:
    """同名报表：旧的挪进回收站，用回同一个名字。

    手动报表按「店名 + 区间 + _手动」命名，重算同一段就该覆盖，
    不然点三次就 (2)(3)(4)——自动核算那边踩过这个坑。
    """
    if not out.exists():
        return out
    try:
        shutil.move(str(out), str(_recycle("报表") / out.name))
        return out
    except OSError:
        i = 2
        stem, suf = out.stem, out.suffix
        while out.exists():
            out = out.parent / f"{stem} ({i}){suf}"
            i += 1
        return out


def _report_target(key: str, label: str) -> tuple[Path, str]:
    """手动报表放哪：`报表\\手动\\<店名>\\`，临时数据放 `报表\\手动\\临时\\`。"""
    if key == TMP_KEY:
        out_dir = paths.report_room_dir("manual") / "临时"
        base = f"临时_{label}_手动"
    else:
        out_dir = paths.store_reports_dir(key, "manual")
        name = paths.store_display_name(key)
        base = f"{name}_{label}_手动"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return out_dir, base


def run(key: str, country: str = "", dfrom: str = "", dto: str = "",
        with_formula: bool = False) -> dict:
    """算这家店的手动数据。只读 `手动导入`，不碰收件箱。"""
    k = str(key or "").strip()
    if k != TMP_KEY and k not in known_keys():
        return {"ok": False, "error": "不认识的店铺"}
    d = space_dir(k)
    if not d.is_dir():
        return {"ok": False, "error": "这家店还没有手动数据，先点「导入」传表"}

    from src import loader as _ld

    try:
        has_orders = _ld.has_orders_table(d)
    except Exception:  # noqa: BLE001
        has_orders = True
    if not has_orders:
        return {"ok": False, "error": "这家店的手动数据里没有订单表。先传一份订单导出（表头要带 Order ID 和金额列）"}

    a, b = str(dfrom or ""), str(dto or "")
    if a and b and b < a:
        a, b = b, a
    label = f"{a}_{b}" if (a and b) else "全部"
    out_dir, base = _report_target(k, label)
    out = _unique_report(out_dir / f"{base}.xlsx")

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
            brief = "没有解析出商品：这家店的手动数据里缺商品SKU/映射表"
        elif "读不了「" in tail:
            brief = "有表格读不出来：多半是文件正在 Excel/WPS 里开着，关掉再算"
        else:
            brief = tail or "核算失败"
        return {"ok": False, "error": brief}

    summary["report_path"] = str(out)
    summary["report_name"] = out.name

    # 复制到「手动报表」的下载目录（设置里那个；没设就只留程序目录）
    copied = None
    try:
        from src import settings as cfg_settings
        dl = str((cfg_settings.load().get("report") or {}).get("manual_download_dir") or "")
        if dl:
            copied = cfg_settings.copy_to_download_dir(out, dl)
    except Exception:  # noqa: BLE001
        copied = None
    if copied:
        summary["copied_to"] = str(copied)

    doc = load_record(d)
    doc["from"], doc["to"] = a, b
    if country:
        doc["country"] = country
    doc["last"] = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "from": a, "to": b,
        "net": summary.get("net_local"),
        "net_cny": summary.get("net_cny"),
        "gmv": summary.get("gmv"),
        "margin": summary.get("net_margin"),
        "report": out.name,
    }
    try:
        save_record(d, doc)
    except OSError:
        pass
    summary["key"] = k
    return {"ok": True, **summary}
