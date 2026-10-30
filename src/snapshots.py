"""取数快照：把「某一批取到的文件」原样存一份，方便事后回算与对账。

为什么需要它
------------
扩展每次取完数都往 `data/stores/<shop>/inbox/` 里丢一份带时间戳的文件，
比如 `全部 笔订单-2026-10-28-19_12.csv`。日子一长，同一个目录里会堆着
好几个不同区间、不同时间抓下来的订单表。

而核算是按目录扫的：同类文件**只取最新的一份**。于是会出现这种情况——

    10:00 取 10-01~10-15 → 算出来是这 15 天
    10:30 取 10-16~10-31 → 再算，只有后 16 天
                            但报表名还写着「全部数据」

数字看着正常，其实是错的，而且**没有任何提示**。对账时最难查的就是这种。

快照做的就是：每次取数结束，把这一批文件复制到一个带标签的目录里，
记下区间、取数时间、每个文件多少行。核算时你可以明确指定「用哪一批」，
报表上也会写明这批数据的来历。

目录布局
--------
    data/stores/<shop>/
      inbox/                        ← 扩展照常写这里（不动，扩展无感）
      snapshots/
        20261028-1912-a1b2c3d4/
          manifest.json
          全部 笔订单-2026-10-28-19_12.csv
          Transaction_Analysis_....csv

目录名 = 取数时间 + 内容签名前 8 位。内容签名由「文件名 + 修改时间 + 字节数」
算出，所以同一批文件反复核算不会堆出一堆一样的快照——签名相同就复用。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from . import paths

# 快照里保留的扩展名（和 loader 能读的范围保持一致）
KEEP_SUFFIX = {".xlsx", ".xls", ".csv", ".txt", ".json"}

# 单家店最多留多少份快照，超出的按时间从旧到新归档成 zip（不删）
KEEP_RECENT = 30


def root(shop_id: str) -> Path:
    """某家店的快照根目录。

    没识别到店铺时（旧数据 / 未登录直接导的），落到公共目录
    `data/exports/_snapshots/`，跟 `_inbox` 并列，免得混进某家店。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return paths.EXPORTS / "_snapshots"
    # 跟着店目录走（店目录用店名命名），快照子目录也用中文
    return paths.store_root(sid) / "快照"


def restore_inbox(shop_id: str) -> int:
    """inbox 里的表被删了，从最新一份快照补回来。

    快照是程序自己维护的副本，文件夹里那些表只是「给人看的」。
    使用者随手删了不该丢数据——下次核算前自动补回。
    返回补回的文件数。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return 0
    base = paths.store_root(sid)
    snap_root = base / "快照"
    if not snap_root.is_dir():
        return 0
    snaps = sorted((p for p in snap_root.iterdir() if p.is_dir()), reverse=True)
    if not snaps:
        return 0
    snap = snaps[0]
    inbox = paths.store_dir(sid)

    # manifest 里有每个文件属于哪一类，决定补到哪个子目录
    kinds: dict[str, str] = {}
    mf = snap / "manifest.json"
    if mf.is_file():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            for f in (data.get("files") or []):
                kinds[str(f.get("name") or "")] = str(f.get("kind") or "orders")
        except Exception:  # noqa: BLE001
            pass

    n = 0
    for f in snap.iterdir():
        if not f.is_file() or f.name == "manifest.json":
            continue
        kind = kinds.get(f.name, "orders")
        sub = paths.KIND_DIR_CN.get(kind, "订单表")
        d = inbox / sub
        try:
            d.mkdir(parents=True, exist_ok=True)
            dst = d / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                n += 1
        except OSError:
            continue
    return n


def _stat_sig(f: Path) -> str:
    try:
        st = f.stat()
    except OSError:
        return f"{f.name}:0:0"
    return f"{f.name.lower()}:{st.st_mtime_ns}:{st.st_size}"


def signature(files: list[Path]) -> str:
    """一批文件的内容签名：名字/修改时间/大小都一样，就认为是同一批。

    不读文件内容算 hash，是因为订单表动辄几十 MB，每次核算都读一遍太慢；
    mtime + size 已经足够区分「重新取过一次」和「还是原来那批」。
    """
    sig = "|".join(sorted(_stat_sig(Path(f)) for f in files))
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]


def _quick_stats(f: Path) -> dict:
    """轻量统计一个文件：行数与类型。

    行数用 csv.reader 数「真正的记录条数」——直接数物理行会把
    字段里带换行的记录算成两行，界面上显示的行数就会虚高。
    """
    out: dict = {"bytes": 0, "rows": 0}
    try:
        out["bytes"] = f.stat().st_size
    except OSError:
        return out
    suffix = f.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            import csv as _csv
            n = 0
            with f.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                for _ in _csv.reader(fh):
                    n += 1
            out["rows"] = max(0, n - 1)      # 去掉表头
        elif suffix in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
            ws = wb[wb.sheetnames[0]]
            out["rows"] = max(0, (ws.max_row or 1) - 1)
            wb.close()
    except Exception:
        pass
    return out


def _kind_of_file(name: str) -> str:
    """按扩展名猜个类型（只是给人看的分类，认表还是靠表头）。"""
    n = name.lower()
    if "订单" in n or "order" in n:
        return "orders"
    if "ads" in n or "广告" in n:
        return "ads"
    if "affiliate" in n or "联盟" in n or "commission" in n:
        return "affiliate"
    if "sku" in n or "mapping" in n or "链接" in n:
        return "mapping"
    return "other"


def _manifest_path(snap_dir: Path) -> Path:
    return snap_dir / "manifest.json"


def create(shop_id: str, files: list[Path], dfrom: str = "", dto: str = "",
           store_name: str = "", note: str = "") -> dict:
    """把这一批文件存成快照。内容没变就复用已有的，不重复占空间。

    返回 {ok, id, dir, files, reused}
    """
    files = [Path(f) for f in files if Path(f).is_file()]
    if not files:
        return {"ok": False, "error": "没有可入库的文件"}

    sig = signature(files)
    base = root(shop_id)
    base.mkdir(parents=True, exist_ok=True)

    # 复用判定：只有「内容完全一样 + 时间很近」才复用。
    #
    # 为什么不是「同签名就复用」：那会把不同区间的取数吞掉——
    # 先取 10-01~10-07、再取 10-08~10-14，两次的文件签名可能相同
    # （平台导出内容一样），但这是两次独立的取数，应该各留一份。
    # 加上 5 分钟的时间窗，既能防手滑连点堆副本，又不会吞掉正常的再次取数。
    latest = list_all(shop_id)
    if latest:
        top = latest[0]
        if str(top.get("signature") or "") == sig:
            try:
                then = datetime.strptime(str(top.get("created_at") or ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                then = None
            if then and (datetime.now() - then).total_seconds() <= 300:
                return {"ok": True, "id": top["id"], "dir": top["dir"], "reused": True,
                        "files": [f.name for f in files]}

    now = datetime.now()
    snap_id = f"{now:%Y%m%d-%H%M}-{sig[:8]}"
    snap_dir = base / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    kept: list[dict] = []
    for f in files:
        if f.suffix.lower() not in KEEP_SUFFIX:
            continue
        dst = snap_dir / f.name
        try:
            shutil.copy2(f, dst)
            st = f.stat()
            info = _quick_stats(f)
            kept.append({"name": f.name, "bytes": st.st_size,
                         "rows": info.get("rows", 0),
                         "kind": _kind_of_file(f.name),
                         "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        except OSError:
            continue

    # 各类文件的行数汇总，界面上要显示「订单 1896 行」这类信息
    kinds: dict[str, int] = {}
    for k in kept:
        kinds[k["kind"]] = kinds.get(k["kind"], 0) + int(k.get("rows") or 0)

    manifest = {
        "id": snap_id,
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "shop_id": shop_id or "",
        "store_name": store_name or "",
        "range": {"from": dfrom or "", "to": dto or ""},
        "files": kept,
        "kinds": kinds,
        "signature": sig,
        "note": note or "",
    }
    _manifest_path(snap_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    _enforce_limit(shop_id)
    return {"ok": True, "id": snap_id, "dir": str(snap_dir), "reused": False,
            "files": [k["name"] for k in kept]}


def list_all(shop_id: str) -> list[dict]:
    """列出某家店的所有快照，新的在前。"""
    base = root(shop_id)
    if not base.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir(), key=lambda x: x.name, reverse=True):
        if not d.is_dir():
            continue
        mf = _manifest_path(d)
        if not mf.exists():
            continue
        try:
            doc = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        doc["id"] = doc.get("id") or d.name
        doc["dir"] = str(d)
        out.append(doc)
    return out


def resolve(shop_id: str, snap_id: str) -> Path | None:
    """取快照目录。snap_id 为空 → 用当前 inbox（实时数据）。"""
    sid = (snap_id or "").strip()
    if not sid:
        return paths.store_dir(shop_id)
    d = root(shop_id) / sid
    return d if d.is_dir() else None


def delete(shop_id: str, snap_id: str) -> bool:
    """删掉一份快照（连同目录）。"""
    d = root(shop_id) / str(snap_id or "").strip()
    if not d.is_dir() or not _manifest_path(d).exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def _enforce_limit(shop_id: str) -> None:
    """超过 KEEP_RECENT 份就把最旧的归档成 zip。

    不直接删：取数不容易（尤其广告要等平台生成），删了就真没了。
    归档后体积通常能压到几分之一，想翻旧账还能解开。
    """
    items = list_all(shop_id)
    if len(items) <= KEEP_RECENT:
        return
    base = root(shop_id)
    archive = base.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for doc in items[KEEP_RECENT:]:
        d = Path(doc["dir"])
        if not d.is_dir():
            continue
        dst = archive / f"{d.name}.zip"
        try:
            if dst.exists():
                shutil.rmtree(d, ignore_errors=True)
                continue
            tmp = dst.with_suffix(".tmp")
            made = shutil.make_archive(str(tmp.with_suffix("")), "zip", root_dir=d)
            Path(made).rename(dst)
            shutil.rmtree(d, ignore_errors=True)
        except (OSError, ValueError):
            continue


def pick_files(shop_id: str) -> list[Path]:
    """这家店当前所有「核算会用得上」的文件：inbox 里的（递归）。

    只取**这家店自己**的文件。映射表（data/mapping/）是全局累积主表，
    不进快照：一是它跟店铺无关，每份快照都存一份纯属浪费；
    二是核算时本来就会把它和快照里的映射合并，重复存没有意义。

    只读，不动任何东西。
    """
    out: list[Path] = []
    d = paths.store_dir(shop_id)
    if d.is_dir():
        out += [f for f in d.rglob("*")
                if f.is_file() and f.suffix.lower() in KEEP_SUFFIX
                and not f.name.startswith("~$") and not f.name.startswith(".")]
    return sorted(out, key=lambda f: f.name.lower())


def manifest(shop_id: str, snap_id: str) -> dict | None:
    """读某份快照的清单（区间、时间、各文件行数）。"""
    sid = (snap_id or "").strip()
    if not sid:
        return None
    mf = _manifest_path(root(shop_id) / sid)
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def describe(shop_id: str, snap_id: str) -> str:
    """一句人话说明这份快照：时间 + 区间 + 文件数。给人看的。"""
    sid = (snap_id or "").strip()
    if not sid:
        return "实时数据（当前 inbox，未入库快照）"
    d = root(shop_id) / sid
    mf = _manifest_path(d)
    if not mf.exists():
        return f"快照 {sid}（清单缺失）"
    try:
        doc = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"快照 {sid}（清单读不出来）"
    r = doc.get("range") or {}
    at = doc.get("created_at") or ""
    rng = "全量" if not (r.get("from") or r.get("to")) else f"{r.get('from')}~{r.get('to')}"
    return f"快照 {at}｜{rng}｜{len(doc.get('files') or [])} 个文件"
