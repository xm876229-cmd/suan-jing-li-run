"""程序主界面。

单页应用：左侧固定导航，右侧按功能分页。桌面窗口里装的就是它。

设计取向参考常见后台工具（Linear / Vercel / GitHub）：
  · 左侧导航固定，右侧内容限宽居中，不把东西堆满整屏
  · 每个功能独立一页，不挤在同一屏
  · 留白优先，卡片之间留够距离
  · 只用一种强调色（深绿），其余靠灰阶区分层级
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from src import paths


def _app_version() -> str:
    """页脚显示的版本号，直接读扩展的 manifest。

    以前这里写死 "0.21.44"，一路升到 0.21.8x 页脚还停在旧版本，
    用户看着以为没更新——版本号只有一个来源，别再手抄一遍。
    """
    for name in ("manifest.json",):
        f = paths.EXTENSION / name
        try:
            v = json.loads(f.read_text(encoding="utf-8")).get("version")
            if v:
                return str(v)
        except Exception:  # noqa: BLE001
            pass
    return "—"

STATE: dict = {"running": False, "last_run": None}


# ---------------- 数据 ----------------

def _ext_heartbeat() -> str:
    """扩展最后一次跟桥说话的时间（「浏览器扩展」那张卡片）。

    两个来源取较新的那个：
      1. `数据/抓包记录/task.log` 的最后一行；
      2. 内存里的心跳表（`server._ONLINE`，桥一重启就空，所以不能只看它）。

    以前这里写的是 `数据/capture/task.log` —— 目录中文化之后这个路径根本不存在，
    于是卡片永远显示「无记录」，人以为扩展压根没连上。名字也一并改对：
    「浏览器扩展」这张卡说的是**最近一次收到消息**，不是「现在有没有连」。
    """
    newest = 0.0
    stamp = ""
    try:
        with (paths.CAPTURE / "task.log").open("r", encoding="utf-8",
                                               errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        lines = []
    for ln in reversed(lines[-100:]):
        if len(ln) >= 19 and ln[4] == "-" and ln[13] == ":":
            stamp = ln[:19]
            break
    if stamp:
        try:
            newest = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            newest = 0.0
    try:
        from bridge import server as _srv
        with _srv._ONLINE_LOCK:
            for rec in _srv._ONLINE.values():
                newest = max(newest, float(rec.get("at") or 0))
    except Exception:  # noqa: BLE001
        pass
    if newest <= 0:
        return ""
    return datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")


def _ago(stamp: str) -> str:
    if not stamp:
        return "无记录"
    try:
        t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp
    d = (datetime.now() - t).total_seconds()
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def _files(dir_: Path, recursive: bool = True) -> list[dict]:
    """列目录下的文件。默认递归——店里按类型分了子目录（orders/ads/…）。"""
    out: list[dict] = []
    try:
        it = dir_.rglob("*") if recursive else dir_.iterdir()
        for p in it:
            if p.is_file() and not p.name.startswith("_"):
                st = p.stat()
                # 用 parts 取子目录名：Windows 上分隔符是反斜杠，
                # 用 split("/") 会切不开，分类就全变成 other
                try:
                    rel = p.relative_to(dir_)
                    top = rel.parts[0] if len(rel.parts) > 1 else ""
                except ValueError:
                    top = ""
                out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                            "rel": str(rel) if recursive else p.name,
                            "path": str(p),
                            "kind": top or "other"})
    except (OSError, ValueError):
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def _all_reports() -> list[dict]:
    """报表可能分店铺放在子目录里，一并找出来。"""
    out: list[dict] = []
    try:
        for p in paths.OUTPUT.rglob("*.xlsx"):
            # 回收站里的不算「已有报表」，否则删完还在列表里，看着像没删掉。
            # 下划线开头的目录（_已删除 / _隔离_日期）都不是给人看的报表；
            # ~$ 开头的是 Excel 打开时自己生成的锁文件，点开会报「文件损坏」。
            if (any(part.startswith("_") for part in p.parts)
                    or p.name.startswith(("~$", "."))):   # ~$ 是 Excel 锁文件，. 开头是算到一半的临时文件
                continue
            st = p.stat()
            rel = p.relative_to(paths.OUTPUT)
            parts = rel.parts
            # 两个「房间」：报表\自动\<店名>\ 与 报表\手动\<店名>\。
            # 用目录区分而不是文件名后缀——名字可以改，目录结构不会骗人。
            # 同时兼容两种老布局：报表\<店名>\（自动）、报表\<店名>\手动\（手动）。
            p0 = parts[0] if parts else ""
            if p0 in ("自动", "手动"):
                room = "manual" if p0 == "手动" else "auto"
                store = parts[1] if len(parts) > 2 else ""
            elif p0 in ("临时手动", "手动核算"):
                room = "manual"
                store = parts[1] if len(parts) > 2 else "未指定"
            else:
                room = "manual" if "手动" in parts[:-1] else "auto"
                store = p0 if len(parts) > 1 else ""
            out.append({
                "name": p.name,
                # rel：相对 output 目录的完整路径。不同店的报表可能重名，
                # 勾选下载时靠它区分，只靠文件名会张冠李戴。
                "rel": str(rel).replace("\\", "/"),
                "group": store if store and store != p.name else "",
                "room": room,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    except OSError:
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def status() -> dict:
    from bridge.server import bridge_uptime, store_list

    beat = _ext_heartbeat()
    reports = _all_reports()
    stores = store_list().get("stores", [])
    total_items = 0
    for s in stores:
        total_items += int(s.get("inbox_count") or 0)
    leg = _files(paths.INBOX)
    return {
        "ok": True,
        "uptime": bridge_uptime(),
        "ext_last": beat,
        "ext_ago": _ago(beat),
        "inbox_count": total_items + len(leg),
        "report_count": len(reports),
        "store_count": len(stores),
        "stores": stores,
    }


def reports() -> dict:
    return {"ok": True, "items": [
        {
            "name": r["name"],
            "rel": r.get("rel") or r["name"],
            "group": r["group"],
            "room": r.get("room") or "auto",
            "size_mb": round(r["size"] / 1048576, 2),
            "at": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "stamp": int(r["mtime"]),
        }
        for r in _all_reports()
    ]}


def _rel_to_data(p) -> str:
    """文件相对「数据根」的路径。前端只拿到这个，删除接口也只用它。

    绝不把绝对路径交给前端：前端传回来什么就删什么的话，
    一个拼错的路径就能把程序目录外的文件挪走。
    """
    if not p:
        return ""
    try:
        return str(Path(p).relative_to(paths.DATA)).replace("\\", "/")
    except (ValueError, OSError):
        return ""


def delete_data_files(rels: list[str]) -> dict:
    """把选中的导入文件挪进「回收站」，不是真删。

    为什么不真删：这些都是从平台导出来的原始数据，有些报表只保留最近几个月，
    删了就得重新取一次（大店还得等平台生成）。挪走能恢复，代价只是占硬盘。
    """
    moved, missing, bad = [], [], []
    trash_root = paths.DATA / "回收站" / datetime.now().strftime("%Y%m%d_%H%M")
    data_root = paths.DATA.resolve()
    for rel in rels or []:
        r = str(rel or "").replace("\\", "/").strip().lstrip("/")
        if not r or ".." in r.split("/"):
            bad.append(str(rel))
            continue
        try:
            src = (paths.DATA / r).resolve()
            if not src.is_relative_to(data_root) or not src.is_file():
                missing.append(r)
                continue
            dst = trash_root / src.relative_to(data_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            i = 2
            base = dst
            while dst.exists():
                dst = base.with_name(f"{base.stem} ({i}){base.suffix}")
                i += 1
            os.replace(src, dst)
            moved.append(r)
        except OSError as e:
            bad.append(f"{r}（{e}）")
    if moved:
        try:
            sz = sum(f.stat().st_size for f in trash_root.rglob("*") if f.is_file())
        except OSError:
            sz = 0
        print(f"[导入] 挪走 {len(moved)} 个文件到 {trash_root}（{sz/1024/1024:.1f} MB）")
    return {"ok": True, "moved": len(moved), "missing": missing, "bad": bad,
            "trash": str(trash_root) if moved else ""}


def data_files() -> dict:
    """按店铺分开列，哪家店有数据、缺哪类，一眼看得出来。"""
    from bridge.server import discovered_stores

    groups: list[dict] = []
    for d in discovered_stores():
        root = paths.store_dir(d["shop_id"])
        files = _files(root)
        # 按类型统计：缺哪类一目了然，不用去翻文件名猜
        kinds: dict[str, int] = {}
        for f in files:
            k = f.get("kind") or "other"
            kinds[k] = kinds.get(k, 0) + 1
        groups.append({
            "shop_id": d["shop_id"],
            "name": d["shop_name"] or d["shop_id"],
            "country": d["country"],
            "kinds": kinds,
            "items": [
                {"name": f["name"], "size_kb": round(f["size"] / 1024),
                 "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M"),
                 "kind": f.get("kind") or "other",
                 "rel": _rel_to_data(f.get("path"))}
                for f in files
            ],
        })

    # 认不出店铺的数据：单独一个分组，标红提示，绝不混进任何一家店
    unknown: list[dict] = []
    try:
        if paths.UNKNOWN_ROOT.is_dir():
            for d in sorted(paths.UNKNOWN_ROOT.iterdir(), reverse=True):
                if not d.is_dir():
                    continue
                for f in _files(d):
                    unknown.append({"name": f["name"], "size_kb": round(f["size"] / 1024),
                                    "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M"),
                                    "batch": d.name, "rel": _rel_to_data(f.get("path"))})
    except OSError:
        pass

    legacy = _files(paths.INBOX)
    return {
        "ok": True,
        "groups": groups,
        "unknown": unknown,
        "legacy": [
            {"name": f["name"], "size_kb": round(f["size"] / 1024),
             "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M")}
            for f in legacy
        ],
        "dir": str(paths.DATA),
        "out_dir": str(paths.OUTPUT),
    }


def snapshots(store: str = "") -> dict:
    """某家店（或公共目录）已有的取数快照，新的在前。

    界面上用它做「用哪次数据」的下拉：默认最新，想回到某一批老数据直接选。
    """
    from src import snapshots as snaps

    sid = (store or "").strip()
    items = []
    for doc in snaps.list_all(sid):
        r = doc.get("range") or {}
        rng = "全量" if not (r.get("from") or r.get("to")) else f"{r.get('from')}~{r.get('to')}"
        items.append({
            "id": doc.get("id") or "",
            "at": doc.get("created_at") or "",
            "range": rng,
            "from": r.get("from") or "",
            "to": r.get("to") or "",
            "files": [f.get("name") for f in (doc.get("files") or [])],
            "file_count": len(doc.get("files") or []),
            "kinds": doc.get("kinds") or {},
        })
    return {"ok": True, "store": sid, "items": items,
            "root": str(snaps.root(sid))}


def trends() -> dict:
    """净利走势：从核算历史里取。"""
    from bridge import settings_api

    h = settings_api.history(80)
    return {"ok": True, "items": [
        {"at": x.get("at", ""), "store": x.get("store", ""),
         "net": x.get("net"), "gmv": x.get("gmv"), "report": x.get("report", "")}
        for x in (h.get("items") or []) if x.get("net") is not None
    ]}


def find_report(name: str) -> Path | None:
    """只认 output 目录下的 .xlsx，不接受任何路径分隔符。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.lower().endswith(".xlsx"):
        return None
    for p in paths.OUTPUT.rglob(name):
        if p.is_file():
            return p
    return None


def _resolve_report(rel: str) -> Path | None:
    """把「店铺/文件.xlsx」这种相对路径安全地解析成 OUTPUT 下的真实文件。

    列表里的勾选值带店铺子目录，而 find_report 只收纯文件名——直接拿它
    去删会一个都删不掉，所以这里单独解析，并且只允许落在 OUTPUT 里面。
    """
    raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw or raw.lower().endswith("/"):
        return None
    if any(part in ("..", "") for part in raw.split("/")[:-1]):
        return None
    base = paths.OUTPUT.resolve()
    try:
        p = (base / raw).resolve()
        p.relative_to(base)
    except Exception:  # noqa: BLE001
        return None
    if p.is_file() and p.suffix.lower() == ".xlsx":
        return p
    return None


def delete_reports(names: list[str]) -> dict:
    """删除报表——不是真删，是挪到 `报表/_已删除/` 里。

    为什么要留这一手：报表是算出来的，删了还能重跑；但万一删错的是
    「已经对完账、客户签过字」那份，重跑不出来（数据可能已经变了）。
    挪走可恢复，代价只是占点硬盘。
    """
    moved, missing = [], []
    trash = paths.OUTPUT / "_已删除"
    for name in names or []:
        raw = str(name).strip()
        f = _resolve_report(raw) or find_report(raw)
        if not f:
            missing.append(name)
            continue
        try:
            trash.mkdir(parents=True, exist_ok=True)
            # 同名报表可能来自不同店（命名模板只用区间时），同一秒删两份会撞名。
            # 撞名时 f.replace 会直接覆盖 → 回收站里少一份，等于真删了。
            dst = trash / f"{f.stem}_{datetime.now():%Y%m%d_%H%M%S}{f.suffix}"
            i = 2
            while dst.exists():
                dst = trash / f"{f.stem}_{datetime.now():%Y%m%d_%H%M%S} ({i}){f.suffix}"
                i += 1
            f.replace(dst)
            moved.append(f.name)
        except OSError as e:
            missing.append(f"{name}（{e}）")
    return {"ok": True, "moved": moved, "missing": missing,
            "trash": str(trash)}


def render_html() -> str:
    html = _TEMPLATE.replace("__STAMP__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    # 版本号每次渲染时从扩展 manifest 读，避免页脚写着旧版本让人以为没更新
    return html.replace("{APP_VERSION}", _app_version())


# ---------------- 页面 ----------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>净利助手</title>
<style>
  :root{
    --bg:#f4f6f8; --panel:#fff; --line:#e4e8ee; --line-soft:#eff2f6;
    --text:#111827; --text-2:#5a6472; --text-3:#8c95a3;
    --accent:#0e8a6d; --accent-hover:#0a7159; --accent-soft:#e6f6f1;
    --accent-2:#13b48c;
    --warn:#c0462c; --warn-soft:#fdf1ed; --info:#3b6fd4;
    --radius:14px;
    --shadow-1:0 1px 2px rgba(17,24,39,.04);
    --shadow-2:0 10px 30px -14px rgba(17,24,39,.22);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased}
  .app{display:flex;height:100vh;overflow:hidden}
  .side{width:216px;flex:0 0 216px;
    background:linear-gradient(180deg,#fff 0%,#fbfcfd 100%);
    border-right:1px solid var(--line);display:flex;flex-direction:column;padding:22px 0 14px}
  .brand{display:flex;align-items:center;gap:11px;padding:0 18px 22px}
  .brand .mark{width:34px;height:34px;border-radius:11px;
    background:linear-gradient(145deg,var(--accent-2) 0%,#0a6d57 100%);
    box-shadow:0 4px 12px -4px rgba(14,138,109,.55);
    display:flex;align-items:center;justify-content:center;flex:0 0 34px}
  .brand .name{font-size:15px;font-weight:600;letter-spacing:-.1px}
  .brand .sub{font-size:11px;color:var(--text-3);margin-top:-1px;letter-spacing:.3px}
  .nav{padding:0 10px;display:flex;flex-direction:column;gap:1px;overflow-y:auto}
  .nav a{position:relative;display:flex;align-items:center;gap:10px;padding:8px 12px;
    border-radius:9px;color:var(--text-2);text-decoration:none;font-size:13.5px;
    transition:background .13s,color .13s}
  .nav a:hover{background:var(--line-soft);color:var(--text)}
  .nav a.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
  .nav a.on::before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
    width:3px;height:16px;border-radius:3px;background:var(--accent)}
  .nav a svg{flex:0 0 17px;width:17px;height:17px;opacity:.8}
  .nav a.on svg{opacity:1}
  .nav .gap{height:10px}
  .side .foot{margin-top:auto;padding:8px 18px 0;font-size:11.5px;color:var(--text-3);line-height:1.9}
  .side .foot .row{display:flex;justify-content:space-between}
  .main{flex:1;overflow-y:auto;padding:34px 42px 64px}
  .page{max-width:1120px;margin:0 auto}
  .page[hidden]{display:none}
  .head{margin-bottom:26px}
  .head h1{font-size:21px;font-weight:500;margin:0 0 6px;letter-spacing:-.2px}
  .head p{margin:0;color:var(--text-2);font-size:13.5px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    margin-bottom:20px;overflow:hidden;box-shadow:var(--shadow-1)}
  .card > h2{font-size:13.5px;font-weight:600;margin:0;padding:15px 22px;
    border-bottom:1px solid var(--line-soft);display:flex;align-items:center;gap:10px}
  .card > h2 .hint{margin-left:auto;font-weight:400;font-size:12.5px;color:var(--text-3)}
  .card > .body{padding:22px}
  .card > .body.tight{padding:0}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:18px 20px;box-shadow:var(--shadow-1)}
  .stat .k{font-size:12.5px;color:var(--text-2);margin-bottom:8px;display:flex;align-items:center;gap:6px}
  .stat .v{font-size:19px;font-weight:500;letter-spacing:-.2px}
  .stat .v.small{font-size:14px;font-weight:400}
  .stat .s{font-size:12px;color:var(--text-3);margin-top:4px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
  .dot.on{background:#12a382} .dot.off{background:#c6ccd4}
  .field{display:flex;flex-direction:column;gap:7px}
  .field label{font-size:12.5px;color:var(--text-2)}
  .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
  input[type=date],select,input[type=text],input[type=number]{
    font:inherit;font-size:13.5px;padding:9px 12px;border:1px solid var(--line);
    border-radius:9px;background:var(--panel);color:var(--text);min-width:140px}
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  button{font:inherit;font-size:13.5px;padding:9px 18px;border-radius:10px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);
    cursor:pointer;transition:background .13s,border-color .13s,box-shadow .13s}
  button:hover{background:var(--line-soft)}
  button.primary{background:linear-gradient(180deg,#12a382,#0b7a61);border-color:#0a7059;
    color:#fff;font-weight:600;box-shadow:0 1px 2px rgba(10,113,89,.28)}
  button.primary:hover{background:linear-gradient(180deg,#0f9578,#096a55)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.mini{padding:5px 11px;font-size:12.5px;border-radius:8px}
  .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--line)}
  .tabs button{border:none;background:none;border-bottom:2px solid transparent;border-radius:0;
    padding:9px 16px;font-size:13.5px;color:var(--text-2);margin-bottom:-1px}
  .tabs button:hover{background:none;color:var(--text)}
  .tabs button.on{border-bottom-color:var(--accent);color:var(--accent);font-weight:500}
  .tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .tags .t{display:inline-flex;align-items:center;gap:5px;padding:3px 4px 3px 10px;
    background:var(--accent-soft);color:var(--accent);border-radius:20px;font-size:12px}
  .tags .t b{font-weight:400}
  .tags .t span{cursor:pointer;padding:0 5px;color:var(--warn);font-weight:500}
  .tags .add{padding:3px 10px;border:1px dashed var(--line);border-radius:20px;
    font-size:12px;color:var(--text-3);background:none;cursor:pointer}
  .tags .add:hover{background:var(--line-soft)}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
  .chips button{border-radius:20px;padding:6px 15px;font-size:12.5px;color:var(--text-2)}
  .chips button.on{background:var(--accent-soft);border-color:#b7ddd0;color:var(--accent);font-weight:500}
  table{width:100%;border-collapse:collapse}
  th,td{padding:12px 20px;text-align:left;border-bottom:1px solid var(--line-soft);font-size:13px}
  th{font-size:12px;font-weight:600;color:var(--text-2);background:#fafbfc}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#fafbfc}
  .muted{color:var(--text-3)}
  .empty{padding:44px 22px;text-align:center;color:var(--text-3);font-size:13.5px}
  .empty b{display:block;color:var(--text-2);font-weight:500;margin-bottom:4px}
  .mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:12.5px}
  .note{display:none;margin-top:18px;padding:13px 16px;border-radius:10px;font-size:13px;line-height:1.6}
  .note.show{display:block}
  .note.ok{background:var(--accent-soft);color:#0a5442}
  .note.err{background:var(--warn-soft);color:var(--warn)}
  .tbl-wrap{overflow-x:auto}

  .imp-bar{display:flex;align-items:center;gap:12px;padding:10px 20px;
    border-bottom:1px solid var(--line-soft);background:#fafbfc}
  .imp-bar .chk{display:inline-flex;align-items:center;gap:7px;font-size:13px;
    color:var(--text-2);cursor:pointer;user-select:none}
  .imp-grp{padding:11px 20px 6px;font-size:12.5px;color:var(--text-2);background:#fafbfc;
    border-bottom:1px solid var(--line-soft);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .imp-grp.warn{color:var(--warn);background:#fdf6f5}

  .rep-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    padding:10px 0;border-top:1px solid var(--line-soft)}
  .rep-bar .chk{display:inline-flex;align-items:center;gap:7px;font-size:13px;
    color:var(--text-2);cursor:pointer;user-select:none}
  .rep-bar .chk input{width:15px;height:15px;accent-color:var(--accent)}
  .rep-k{font-size:13px;color:var(--text-2);white-space:nowrap}
  .pick-row{display:flex;gap:8px;align-items:center}
  .pick-row input{flex:1;min-width:0}
  .pick-row .mini{white-space:nowrap}
  /* ---------- 区间按钮 + 日历面板 ---------- */
  .rbtn{font:inherit;font-size:12.5px;padding:6px 10px;border-radius:8px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);
    cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
  .rbtn:hover{border-color:#ccd4de;background:#fff}
  .rbtn svg{width:13px;height:13px;color:var(--text-3);flex:none}
  .bar .rbtn{font-size:13px;padding:6px 11px}

  .rp{position:fixed;z-index:80;width:296px;background:var(--panel);border:1px solid var(--line);
    border-radius:14px;box-shadow:0 18px 44px -16px rgba(17,24,39,.32);padding:14px;
    display:none;user-select:none}
  .rp.show{display:block}
  .rp-top{display:flex;gap:8px;margin-bottom:10px}
  .rp-box{flex:1;border:1px solid var(--line);border-radius:9px;padding:6px 9px;min-width:0}
  .rp-box span{font-size:11px;color:var(--text-3);display:block}
  .rp-box b{font-size:12.5px;font-weight:600;display:block;margin-top:1px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .rp-box.on{border-color:var(--accent);background:var(--accent-soft)}
  .rp-box.on b{color:var(--accent)}
  .rp-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
  .rp-chips button{font-size:12px;padding:4px 10px;border-radius:20px;
    border:1px solid var(--line);background:var(--panel);color:var(--text-2);cursor:pointer}
  .rp-chips button:hover{border-color:#ccd4de;color:var(--text)}
  .rp-head{display:flex;align-items:center;gap:6px;margin-bottom:6px}
  .rp-head b{flex:1;text-align:center;font-size:13px}
  .rp-head button{width:28px;height:28px;padding:0;border-radius:8px;font-size:15px;
    color:var(--text-2);background:none;border:1px solid transparent}
  .rp-head button:hover{background:var(--line-soft)}
  .rp-wd{display:grid;grid-template-columns:repeat(7,1fr);margin-bottom:2px}
  .rp-wd span{text-align:center;font-size:11px;color:var(--text-3);padding:3px 0}
  .rp-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:1px}
  .rp-grid button{height:31px;padding:0;border:none;background:none;border-radius:7px;
    font-size:12.5px;color:var(--text);cursor:pointer}
  .rp-grid button:hover{background:var(--line-soft)}
  .rp-grid button.mute{color:#c8ced6}
  .rp-grid button.in{background:var(--accent-soft);color:#0a5442;border-radius:0}
  .rp-grid button.a,.rp-grid button.b{background:var(--accent);color:#fff;font-weight:600}
  .rp-grid button.a{border-radius:7px 0 0 7px}
  .rp-grid button.b{border-radius:0 7px 7px 0}
  .rp-grid button.a.b{border-radius:7px}
  .rp-grid button.today{box-shadow:inset 0 0 0 1px var(--accent)}
  .rp-foot{display:flex;align-items:center;gap:8px;margin-top:10px;
    padding-top:9px;border-top:1px solid var(--line-soft)}
  .rp-foot .rp-hint{flex:1;font-size:11.5px;color:var(--text-3);line-height:1.5}
  .rp-foot button{font-size:12px;padding:4px 10px;border-radius:7px}

  .ops{display:flex;gap:6px;align-items:center;white-space:nowrap}
  #rep-list td{padding:11px 16px}
  #rep-list th{padding:10px 16px}
  #rep-list td:nth-child(2){word-break:break-all}
  .cfg-tbl th{white-space:nowrap;padding:10px 12px;font-size:11.5px}
  .cfg-tbl td{padding:8px 12px}
  .cfg-tbl input[type=text],.cfg-tbl input[type=number]{min-width:0;width:100%;padding:6px 8px;font-size:12.5px}
  .cfg-tbl tr.off td{opacity:.42}
  /* 公式单元格：可多行（候选标题一行一个）+ 下方实时提示 */
  .cfg-tbl td.expr-cell{vertical-align:top}
  .cfg-tbl td.expr-cell textarea{
    font-size:12px;line-height:1.5;padding:6px 8px;border:1px solid var(--line);
    border-radius:6px;background:#fff;color:var(--text-1);min-height:38px}
  .cfg-tbl td.expr-cell textarea:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 2px rgba(31,59,77,.08)}
  .expr-tools{display:flex;align-items:center;gap:8px;margin-top:4px}
  .expr-tools .mini{padding:2px 8px;font-size:11.5px}
  .expr-hint{font-size:11.5px;color:var(--text-2);line-height:1.45;flex:1}
  .expr-hint.bad{color:var(--warn)}
  /* 快照多选清单 */
  .snap-list{border:1px solid var(--line);border-radius:10px;background:#fff;
    max-height:186px;overflow-y:auto;padding:4px 0}
  .snap-list .empty{padding:22px}
  .snap-item{display:flex;gap:9px;align-items:flex-start;padding:8px 14px;cursor:pointer}
  .snap-item:hover{background:#fafbfc}
  .snap-item input{width:15px;height:15px;margin-top:2px;flex:0 0 15px;accent-color:var(--accent);cursor:pointer}
  .snap-item b{display:block;font-size:12.5px;font-weight:600}
  .snap-item em{display:block;font-style:normal;font-size:11.5px;color:var(--text-2);margin-top:1px}
  .chips.tight{margin:8px 0 0}
  .chips.tight button{padding:4px 11px;font-size:11.5px}
  /* 总开关：去除 0 元样品订单 */
  .switch{display:flex;gap:10px;align-items:flex-start;padding:13px 15px;
    border:1px solid var(--line);border-radius:10px;background:var(--line-soft);cursor:pointer}
  .switch input{width:16px;height:16px;margin-top:1px;flex:0 0 16px;
    accent-color:var(--accent);cursor:pointer}
  .switch b{font-size:13px;font-weight:600}
  .switch em{display:block;font-style:normal;font-size:12px;color:var(--text-2);
    margin-top:3px;line-height:1.55}
  .switch.on{border-color:#f0c9c0;background:#fdf3f0}
  .cfg-tbl td.koff{opacity:.3}
  .cfg-tbl td.koff input{cursor:not-allowed}
  /* 表格比卡片宽时横向滚动，别把列挤窄（汇率 3560 这种四位数字会被截掉） */
  #ptbl{min-width:1010px}
  #ptbl td:nth-child(2) input{min-width:300px}
  #rtbl{min-width:1120px}
  #rtbl td:first-child input{min-width:86px}
  #rtbl td input{min-width:64px}
  /* 最后一列是「删」，别贴着卡片边缘 */
  .cfg-tbl td:last-child,.cfg-tbl th:last-child{padding-left:6px;padding-right:14px}
  .kcell{text-align:center}
  .kcell label{display:inline-flex;padding:3px;cursor:pointer}
  .kcell input{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;display:block}
  .kcell button.mini{padding:2px 6px;font-size:11.5px;line-height:1.2;margin:0 1px}
  .kcell button.mini:disabled{opacity:.28;cursor:default}
  .drag-handle{cursor:grab;user-select:none}
  .drag-handle:active{cursor:grabbing}
  .drag-handle .grip{color:var(--text-3);font-size:13px;margin-right:2px;letter-spacing:-1px}
  tr.dragging{opacity:.45;background:var(--accent-soft)}
  /* 危险操作（删除）单独一个视觉：红边框，不要跟「下载」长得一样 */
  button.danger{background:var(--warn-soft);border:1px solid #e6c2b4;color:var(--warn);
    padding:6px 13px;border-radius:9px;font-size:13px;cursor:pointer}
  button.danger:hover{background:#f7e0d6}
  button.danger:disabled{opacity:.5;cursor:default}
  .pid{font-size:11px;color:var(--text-3);margin-top:3px;font-family:ui-monospace,Consolas,monospace}
  .tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
    background:var(--accent-soft);color:var(--accent);margin-left:6px;vertical-align:1px}
  .tag.warn{background:var(--warn-soft);color:var(--warn)}
  .tag.gray{background:var(--line-soft);color:var(--text-3)}
  .tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  /* 计算参数页：可点选的变量表 */
  .vars{display:flex;flex-wrap:wrap;gap:8px}
  .vars .v{display:flex;align-items:baseline;gap:7px;padding:7px 12px;border:1px solid var(--line);
    border-radius:10px;background:#fafbfc;cursor:pointer;transition:.12s}
  .vars .v:hover{border-color:var(--accent);background:var(--accent-soft)}
  .vars .v code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--accent)}
  .vars .v span{font-size:11.5px;color:var(--text-3)}
  .mini-btn{display:inline-block;padding:5px 11px;font-size:12.5px;border-radius:8px;cursor:pointer;
    border:1px solid var(--line);background:var(--panel);color:var(--text)}
  .mini-btn:hover{border-color:var(--accent);color:var(--accent)}
  .stats-line{display:flex;gap:26px;flex-wrap:wrap}
  .stats-line .it{font-size:13px;color:var(--text-2)}
  .stats-line .it b{display:block;font-size:20px;font-weight:500;color:var(--text);margin-top:3px}
  .cost-miss{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .cost-miss span{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;padding:3px 8px;
    border-radius:6px;background:var(--warn-soft);color:var(--warn)}
  .diff{display:none;margin-top:20px;border:1px solid #cfe8de;background:#fbfefc;
    border-radius:12px;overflow:hidden}
  .diff.show{display:block}
  .diff h3{margin:0;padding:13px 18px;font-size:13px;font-weight:500;color:var(--accent);
    background:var(--accent-soft);border-bottom:1px solid #d8ece4}
  .up{color:#0f6e56;font-weight:500} .down{color:var(--warn);font-weight:500}
  .kv{display:grid;grid-template-columns:170px 1fr}
  .kv > div{padding:13px 22px;border-bottom:1px solid var(--line-soft);font-size:13px}
  .kv > div:nth-child(odd){color:var(--text-2)}
  .kv > div:nth-last-child(-n+2){border-bottom:none}
  .modal{position:fixed;inset:0;background:rgba(25,25,23,.45);display:none;
    align-items:center;justify-content:center;padding:32px;z-index:60}
  .modal.show{display:flex}
  .modal .box{background:var(--panel);border-radius:16px;width:min(920px,100%);
    max-height:88vh;display:flex;flex-direction:column;overflow:hidden}
  .modal .box header{display:flex;align-items:center;gap:12px;padding:18px 24px;
    border-bottom:1px solid var(--line)}
  .modal .box header h3{margin:0;font-size:15px;font-weight:500}
  .modal .box header .x{margin-left:auto}
  .modal .box .content{overflow-y:auto;padding-bottom:10px;flex:1}
  .modal .box footer{display:flex;align-items:center;gap:10px;padding:14px 24px;
    border-top:1px solid var(--line);background:#fafbfc}
  .bars{display:flex;align-items:flex-end;gap:4px;height:110px;padding:0 6px}
  .bars .b{flex:1 1 0;max-width:56px;background:#9fd3c1;border-radius:3px 3px 0 0;min-height:2px;transition:background .12s}
  .bars .b:hover{background:var(--accent)}
  .bars .b.neg{background:#f0c4b4}
  .legend{font-size:11.5px;color:var(--text-3);padding:8px 6px 0;display:flex;gap:14px}
  .toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%) translateY(8px);
    background:#191917;color:#fff;padding:11px 22px;border-radius:10px;font-size:13px;
    max-width:min(760px,calc(100vw - 80px));line-height:1.55;text-align:center;
    opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;z-index:70}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  pre.log{background:#fbfbf9;border:1px solid var(--line);border-radius:10px;
    padding:14px 16px;font-size:12px;line-height:1.7;max-height:340px;overflow:auto;
    font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;margin:0;
    white-space:pre-wrap;word-break:break-all}
  @media (max-width:860px){
    .side{width:60px;flex:0 0 60px}
    .brand .name,.brand .sub,.nav a span,.side .foot{display:none}
    .nav a{justify-content:center;padding:11px}
    .main{padding:22px 16px 44px}
    .stats{grid-template-columns:repeat(2,1fr)}
  }

  /* 计算参数页：顶部分段标签 + 分栏。每栏只放一类内容，
     不再把参数表/表头/报表列/公式全挤在一页。 */
  #ptabs{gap:8px;margin-bottom:16px;position:sticky;top:0;z-index:6;
    background:var(--bg,#f7f7f5);padding:6px 0 10px}
  #ptabs .mini{font-size:13px;padding:7px 15px;border-radius:9px}
  .pane[hidden]{display:none}

  /* 导入数据页的拖放区：整块都能接文件，不用瞄准某个小按钮 */
  .dropzone{border:1.5px dashed var(--line);border-radius:12px;padding:26px 18px;
    text-align:center;background:#fafbfc;transition:.15s;cursor:pointer}
  .dropzone:hover{border-color:var(--accent);background:var(--accent-soft,#f0f7f5)}
  .dropzone.over{border-color:var(--accent);background:var(--accent-soft,#f0f7f5);
    box-shadow:0 0 0 3px rgba(15,110,86,.08)}
  .dropzone b{font-size:13.5px}

  /* ---------- 驾驶舱（首页） ---------- */
  .nav .grp{margin:14px 18px 6px;font-size:11px;letter-spacing:.06em;
    color:var(--text-3);text-transform:uppercase}
  .nav .grp:first-of-type{margin-top:4px}

  .bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:12px 14px;margin-bottom:14px}
  .bar .sep{width:1px;height:22px;background:var(--line)}
  .bar .grow{flex:1}
  .bar #btn-run-picked{margin-left:auto}
  .bar button.danger{background:#fff4f3;border-color:#f0c2bc;color:#b42318}
  .bar button.danger:hover{background:#ffe9e6;border-color:#e6a79f}
  .bar label.chk{display:inline-flex;align-items:center;gap:7px;font-size:13px;
    color:var(--text-2);cursor:pointer;user-select:none}
  .bar label.chk input{width:15px;height:15px;accent-color:var(--accent);cursor:pointer}
  .bar input[type=date]{font:inherit;font-size:13px;padding:6px 9px;
    border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text)}
  .bar .inline{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text-2)}
  /* 工具条上的「重算」是次要动作：白底描边，别跟主按钮抢 */
  .bar button.ghost{background:var(--panel);border:1px solid var(--line);color:var(--text-2);
    padding:7px 16px}
  .bar button.ghost:hover{border-color:var(--accent);color:var(--accent);background:#fff}

  .seg{display:inline-flex;background:var(--line-soft);border-radius:9px;padding:3px;gap:2px}
  .seg button{border:0;background:transparent;color:var(--text-2);font:inherit;font-size:13px;
    padding:5px 13px;border-radius:7px;cursor:pointer;transition:background .12s,color .12s}
  .seg button.on{background:var(--panel);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.06)}

  .store-grid{display:grid;gap:14px;
    grid-template-columns:repeat(auto-fill,minmax(330px,1fr));margin-bottom:18px}
  .sc{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:0;overflow:hidden;display:flex;flex-direction:column;
    transition:border-color .12s,box-shadow .12s}
  .sc:hover{border-color:#ccd4de;box-shadow:var(--shadow-2)}
  .sc.picked{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset,var(--shadow-1)}
  .sc.fail{border-color:#e2b6a6;background:#fffdfc}
  .sc-head{display:flex;align-items:flex-start;gap:9px;padding:13px 14px 10px}
  .sc-head input[type=checkbox]{margin-top:3px;width:15px;height:15px;
    accent-color:var(--accent);cursor:pointer;flex:none}
  .sc-title{min-width:0;flex:1}
  .sc-title .n{font-size:14.5px;font-weight:500;line-height:1.3;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-title .m{font-size:12px;color:var(--text-3);margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block}
  .dot.on{background:#12a382}
  .dot.off{background:#c4c2b8}
  .dot.run{background:#3b82d6;animation:pulse 1.4s ease-in-out infinite}
  .dot.warn{background:#dfa13a}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .sc-state{font-size:12px;color:var(--text-3);display:inline-flex;align-items:center;
    gap:5px;white-space:nowrap;padding-top:2px}

  .sc-body{padding:0 14px 12px;flex:1}
  .step{display:flex;align-items:center;gap:8px;font-size:12.5px;line-height:2.05;
    color:var(--text-3)}
  .step .ic{width:14px;height:14px;flex:none;display:inline-flex;
    align-items:center;justify-content:center}
  .step .nm{flex:none;width:96px;color:var(--text-3)}
  .step .dt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .step .tm{flex:none;font-variant-numeric:tabular-nums;color:var(--text-3)}
  .step.done .nm,.step.done .tm{color:var(--text-2)}
  .step.done .ic{color:#12a382}
  .step.run .nm{color:var(--accent);font-weight:500}
  .step.run .dt{color:var(--accent)}
  .step.fail .nm,.step.fail .dt{color:#c0462c}
  .step.fail .ic{color:#c0462c}

  .sc-foot{display:flex;align-items:center;gap:8px;padding:10px 14px 12px;
    border-top:1px solid var(--line-soft);background:#fafbfc}
  .sc-foot .last{font-size:11.5px;color:var(--text-3);flex:1;min-width:0;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sc-foot button{font:inherit;font-size:12.5px;padding:6px 14px;border-radius:8px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer}
  .sc-foot button:hover{border-color:#ccd4de}
  .sc-foot button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .sc-foot button.primary:hover{background:var(--accent-hover)}
  .sc-foot button.ghost{background:transparent;border-color:var(--line);
    color:var(--text-2);padding:6px 11px}
  .sc-foot button.ghost:hover{border-color:var(--accent);color:var(--accent);background:#fff}
  .sc-foot button:disabled{opacity:.5;cursor:default}
  /* 「停止」跟「运行」并排出现，用红色系才分得清，别跟主按钮抢颜色 */
  .sc-foot button.stop{background:#fff4f3;border-color:#f0c2bc;color:#b42318}
  .sc-foot button.stop:hover{background:#ffe9e6;border-color:#e6a79f}
  .sc-foot button.stop:disabled{background:#f7f8fa;border-color:var(--line);
    color:var(--text-3)}
  .sc-date{display:flex;gap:4px;align-items:center}
  .sc-date input{font:inherit;font-size:11.5px;padding:4px 6px;width:104px;
    border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text)}

  .mn-rep{color:var(--accent);cursor:pointer;text-decoration:underline dotted;text-underline-offset:3px}
  .manual-grid .sc-foot button.stop{background:#fff4f3;border-color:#f0c2bc;color:#b42318}
  .manual-grid .sc-foot button.stop:hover{background:#ffe9e6;border-color:#e6a79f}
  .mn-link{margin-top:6px;font-size:12.5px;color:var(--accent);cursor:pointer;user-select:none}
  .mn-link:hover{text-decoration:underline}
  .sc-empty{padding:50px 20px;text-align:center;color:var(--text-3);font-size:13px;
    border:1px dashed var(--line);border-radius:var(--radius);grid-column:1/-1}

  /* 「参数改了，要不要重算」浮条：从底部出来，不挡卡片 */
  .recalc-bar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);
    display:none;align-items:center;gap:14px;background:#1b1b19;color:#fff;
    padding:12px 14px 12px 18px;border-radius:14px;max-width:calc(100vw - 60px);
    box-shadow:0 18px 44px -18px rgba(0,0,0,.55);z-index:65}
  .recalc-bar.show{display:flex}
  .recalc-bar .tx{min-width:0}
  .recalc-bar .tx b{display:block;font-size:13px;font-weight:600;line-height:1.4}
  .recalc-bar .tx em{display:block;font-style:normal;font-size:11.5px;color:#b6bcc4;margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
  .recalc-bar button{font-size:12.5px;padding:7px 15px;border-radius:9px;white-space:nowrap}
  .recalc-bar button.primary{background:var(--accent-2);border:1px solid var(--accent-2);
    color:#03231b;font-weight:600}
  .recalc-bar button.primary:hover{background:#17c79b}
  .recalc-bar button.plain{background:transparent;border:1px solid #3f3f3b;color:#cdd2d8}
  .recalc-bar button.plain:hover{background:#2a2a27}
  /* 重算条出现时把 toast 顶上去，两个都贴底会叠在一起 */
  body.has-recalc .toast{bottom:96px}

  .sc-date{display:none}
  body.single #home-range-wrap,body.single #home-presets{display:none}
  /* 单店模式：卡片上要放两个日期 + 按钮，一行塞不下，会挤成一团。
     让日期独占一行、按钮另起一行，跟「上次」那行不再抢空间。 */
  body.single .sc-foot{flex-wrap:wrap;gap:6px}
  body.single .sc-foot .last{display:none}
  body.single .sc-date{display:flex;width:100%}
  body.single .sc-date .rbtn{width:100%;justify-content:center}
  body.single .sc-foot button{width:100%}

  /* ---------- 计划任务 ---------- */
  .plan-grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(355px,1fr));
    margin-bottom:18px}
  .pc{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    display:flex;flex-direction:column;overflow:hidden;box-shadow:var(--shadow-1);
    transition:border-color .15s,box-shadow .15s}
  .pc:hover{border-color:#ccd4de;box-shadow:var(--shadow-2)}
  .pc.off{background:linear-gradient(180deg,#fff 0%,#fafbfc 100%)}
  .pc.off .pc-ic{background:#f1f3f6;color:var(--text-3)}
  .pc.off .pc-title .n{color:var(--text-2)}
  .pc-head{display:flex;align-items:flex-start;gap:11px;padding:16px 16px 11px}
  .pc-ic{width:36px;height:36px;border-radius:11px;background:var(--accent-soft);
    color:var(--accent);display:flex;align-items:center;justify-content:center;flex:none}
  .pc-ic svg{width:19px;height:19px}
  .pc-title{flex:1;min-width:0;padding-top:1px}
  .pc-title .n{font-size:14.5px;font-weight:600;line-height:1.35;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pc-title .m{font-size:12px;color:var(--text-3);margin-top:2px}
  .pc-body{padding:0 16px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
  .pc-row{display:flex;gap:9px;font-size:12.5px;min-width:0}
  .pc-row .k{color:var(--text-3);flex:0 0 38px}
  .pc-row .v{color:var(--text-2);min-width:0;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .pc-row .v b{color:var(--text);font-weight:600}
  .pc-row .v .soon{color:var(--accent);font-weight:600}
  .pc-foot{display:flex;align-items:center;gap:8px;padding:10px 16px;
    border-top:1px solid var(--line-soft);background:#fafbfc}
  .pc-foot button{font:inherit;font-size:12.5px;padding:6px 13px;border-radius:8px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer}
  .pc-foot button:hover{border-color:#ccd4de;background:#fff}
  .pc-foot button.primary{background:linear-gradient(180deg,#12a382,#0b7a61);
    border-color:#0a7059;color:#fff;font-weight:600}
  .pc-foot button.danger:hover{color:var(--warn);border-color:#e8c3b9;background:#fdf3f0}
  .pc-foot .grow{flex:1}

  .tgl{position:relative;display:inline-flex;flex:none;width:38px;height:22px;margin-top:2px}
  .tgl input{position:absolute;opacity:0;width:0;height:0}
  .tgl i{position:absolute;inset:0;border-radius:20px;background:#d7dce3;
    transition:background .16s;cursor:pointer}
  .tgl i::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;
    border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(17,24,39,.2);
    transition:transform .16s}
  .tgl input:checked + i{background:var(--accent)}
  .tgl input:checked + i::after{transform:translateX(16px)}
  .tgl input:disabled + i{opacity:.5;cursor:default}

  .alert{display:flex;align-items:center;gap:12px;padding:14px 16px;border-radius:12px;
    background:#fdf6e9;border:1px solid #f0dfbb;color:#8a5b12;font-size:13px;
    margin-bottom:16px}
  .alert .grow{flex:1}
  .alert button{padding:6px 14px;font-size:12.5px;border-radius:8px;
    border:1px solid #e4cf9f;background:#fff;color:#8a5b12;cursor:pointer}
  .alert button:hover{background:#fffbf2}

  .frm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 18px}
  .frm .full{grid-column:1/-1}
  .frm .field label{font-size:12.5px;color:var(--text-2);margin-bottom:1px}
  .frm .tip{grid-column:1/-1;font-size:12.5px;color:var(--text-3);line-height:1.7;
    background:var(--line-soft);border-radius:10px;padding:11px 14px}
  .frm .tip b{color:var(--accent);font-weight:600}
  .chk-row{display:flex;flex-wrap:wrap;gap:8px}
  .chk-row label{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;
    border:1px solid var(--line);border-radius:20px;font-size:12.5px;color:var(--text-2);
    cursor:pointer;user-select:none}
  .chk-row label:hover{border-color:#ccd4de}
  .chk-row input{width:14px;height:14px;accent-color:var(--accent);cursor:pointer}
  .chk-row label.on{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
  .prev{margin-bottom:16px;padding:12px 14px;border-radius:10px;background:var(--accent-soft);
    color:#0a5442;font-size:12.5px;line-height:1.8}
  .prev:empty{display:none}
  .prev b{font-weight:600;color:var(--accent)}
  .frm input,.frm select{height:38px;padding-top:0;padding-bottom:0}
  .frm select{padding-right:28px}

</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="mark">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none">
          <rect x="4" y="13" width="3.6" height="7" rx="1.3" fill="#fff" opacity=".82"/>
          <rect x="10.2" y="8.5" width="3.6" height="11.5" rx="1.3" fill="#fff"/>
          <rect x="16.4" y="4" width="3.6" height="16" rx="1.3" fill="#fff" opacity=".92"/>
        </svg>
      </div>
      <div><div class="name">净利助手</div><div class="sub">TikTok 店铺</div></div>
    </div>
    <nav class="nav" id="nav">
<div class="grp">运营</div>
      <a href="#/overview" data-view="overview">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="7.5" height="7.5" rx="2.2"/>
          <rect x="13.5" y="3" width="7.5" height="7.5" rx="2.2"/>
          <rect x="3" y="13.5" width="7.5" height="7.5" rx="2.2"/>
          <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2.2"/>
        </svg><span>驾驶舱</span></a>
      <a href="#/manual" data-view="manual">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3.2" y="4.2" width="17.6" height="15.6" rx="2.6"/>
          <path d="M3.2 9.4h17.6M9.2 9.4v10.4M15.4 9.4v10.4"/>
        </svg><span>手动核算</span></a>
      <a href="#/plan" data-view="plan">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="4.6" width="18" height="16.4" rx="3"/>
          <path d="M3 10h18M8 2.6v4M16 2.6v4"/>
          <path d="M12 13v3.2l2.1 1.3"/>
        </svg><span>计划任务</span></a>
      <a href="#/reports" data-view="reports">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 2.8h7.2L19 8.6v12.6H6z"/>
          <path d="M13.2 2.8v5.8H19M9 13.4h6M9 17.2h4"/>
        </svg><span>报表</span></a>
<div class="grp">数据</div>
      <a href="#/import" data-view="import">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3v11"/><path d="M7.6 10.4L12 14.8l4.4-4.4"/>
          <path d="M4 16v3.4A1.6 1.6 0 005.6 21h12.8a1.6 1.6 0 001.6-1.6V16"/>
        </svg><span>导入数据</span></a>
      <a href="#/stores" data-view="stores">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M4.2 8h15.6l-1.3 11.4a2 2 0 01-2 1.8H7.5a2 2 0 01-2-1.8z"/>
          <path d="M8.6 8V6.1a3.4 3.4 0 016.8 0V8"/>
        </svg><span>店铺</span></a>
      <a href="#/costs" data-view="costs">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3l8.6 4.5L12 12 3.4 7.5z"/>
          <path d="M3.4 12.4L12 16.9l8.6-4.5"/>
          <path d="M3.4 16.9L12 21.4l8.6-4.5"/>
        </svg><span>成本表</span></a>
<div class="grp">设置</div>
      <a href="#/params" data-view="params">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3.6 7.5h9.2M18.4 7.5h2M3.6 16.5h3.8M12.6 16.5h7.8"/>
          <circle cx="15.6" cy="7.5" r="2.4"/><circle cx="9.8" cy="16.5" r="2.4"/>
        </svg><span>计算参数</span></a>
      <a href="#/rates" data-view="rates">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M5.2 18.8L18.8 5.2"/>
          <circle cx="7.7" cy="7.7" r="2.6"/><circle cx="16.3" cy="16.3" r="2.6"/>
        </svg><span>费率</span></a>
      <a href="#/settings" data-view="settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3.1"/>
          <path d="M12 2.6v2.5M12 18.9v2.5M2.6 12h2.5M18.9 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8"/>
        </svg><span>设置</span></a>
      <div class="gap"></div>
      <a href="#/help" data-view="help">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="9"/>
          <path d="M9.3 9.4a2.8 2.8 0 015.3 1.2c0 1.9-2.6 2.1-2.6 3.9"/>
          <circle cx="12" cy="17.4" r="1" fill="currentColor" stroke="none"/>
        </svg><span>使用帮助</span></a>
      <a href="#/debug" data-view="debug">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="2.6" y="4.2" width="18.8" height="15.6" rx="2.8"/>
          <path d="M7 9.4l3 2.6-3 2.6M12.6 14.6h4.2"/>
        </svg><span>调试</span></a>
    </nav>
    <div class="foot">
      <div class="row"><span>版本</span><span id="app-ver">{APP_VERSION}</span></div>
      <div class="row"><span>店铺</span><span id="foot-stores">—</span></div>
    </div>
  </aside>

  <main class="main">
    <section class="page" id="view-overview">
      <div class="head"><h1>驾驶舱</h1>
        <p>每家店一张卡。浏览器开着的店几秒内开工；没开的先排队，等它打开自动跑。</p></div>

      <div class="bar">
        <div class="seg" id="home-mode">
          <button data-mode="total" class="on">总控</button>
          <button data-mode="single">单店</button>
        </div>
        <div class="sep"></div>
        <label class="chk"><input type="checkbox" id="pick-all"> 全选</label>
        <span class="muted" id="pick-count" style="font-size:12.5px">已选 0 家</span>
        <div class="sep"></div>
        <div class="inline" id="home-range-wrap">
          <span style="color:var(--text-3)">区间</span>
          <button class="rbtn" id="home-range" title="点一次，起止日期一起选">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                 stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4.6" width="18" height="16.4" rx="3"/><path d="M3 10h18M8 2.6v4M16 2.6v4"/>
            </svg><span id="home-range-txt">最近 30 天</span>
          </button>
        </div>
        <div class="chips tight" id="home-presets">
          <button data-k="last_month">上月</button>
          <button data-k="month">本月至今</button>
          <button data-k="last7">近 7 天</button>
          <button data-k="yesterday">昨天</button>
        </div>
        <div class="grow"></div>
        <label class="chk" title="不勾：今天同一区间已经取过的表直接复用，改完费率重算只需几十秒。
勾上：忽略记录，重新去平台导一遍（数据确实变了才勾）">
          <input type="checkbox" id="force-fetch"> 强制重新取数
        </label>
        <button id="btn-recalc-picked" class="ghost"
                title="用已经在本机的数据重算勾选的店，不重新取数。
改了佣金费率 / 成本表 / 汇率之后点这个">重算</button>
        <button class="danger" id="btn-stop-all" hidden
                title="把所有正在跑的店停下来（每一步之间都会退出）">停止全部</button>
        <button class="primary" id="btn-run-picked">运行勾选的</button>
      </div>

      <div class="store-grid" id="store-grid">
        <div class="sc-empty">正在读取店铺…</div>
      </div>

      <div class="stats" id="stats"></div>
      <div class="card"><h2>最近的净利走势<span class="hint" id="trend-hint"></span></h2>
        <div class="body" id="trend"><div class="empty">还没有核算记录</div></div></div>
    </section>

<section class="page" id="view-import" hidden>
      <div class="head"><h1>导入数据<span class="tag">补进店铺，参与自动核算</span></h1>
        <p>这里传的表会进<b>所选店铺的收件箱</b>，跟扩展自动取到的数据一起参与核算——
           用在「自动取数没拿到、手工补一张」的场景。<br>
           想拿一批表<b>单独算</b>、不掺店里数据，去左边的<b>「手动核算」</b>。</p></div>

      <div class="card"><h2>① 这份数据是哪家店、哪个国家<span class="hint">选错国家，列名认不出、费率也套错</span></h2>
        <div class="body">
          <div class="row" style="gap:10px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:240px"><label>店铺</label>
              <select id="imp2-store"><option value="">未识别店铺（公共目录）</option></select></div>
            <div class="field" style="min-width:190px"><label>国家（决定费率与列名）</label>
              <select id="imp2-country"><option value="">跟随店铺</option></select></div>
          </div>
          <p class="muted" style="font-size:12.5px;margin:10px 0 0">
            选店铺后国家会自动带上，一般不用改。订单表的列名按国家/语言分（马来站和菲律宾站不一样），
            费率也按国家分——<b>选错了不会报错，但数字会全错</b>。
          </p>
        </div></div>

      <div class="card"><h2>② 把表拖进来<span class="hint">可一次多选，程序自动认是哪种表</span></h2>
        <div class="body">
          <div class="dropzone" id="imp2-drop">
            <svg width="26" height="26" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                 stroke-width="1.3" style="color:var(--text-3)"><path d="M8 2.6v7.2M5.2 7l2.8 2.8L10.8 7"
                 stroke-linecap="round" stroke-linejoin="round"/><path d="M2.8 11.2v1.4a1.6 1.6 0 001.6 1.6h7.2a1.6 1.6 0 001.6-1.6v-1.4" stroke-linecap="round"/></svg>
            <div style="margin:6px 0 2px"><b>把导出的文件拖到这里</b></div>
            <div class="muted" style="font-size:12.5px">
              或者 <label class="mini-btn" style="margin:0">选择文件
                <input type="file" id="imp2-file" multiple hidden accept=".csv,.xlsx,.xls,.txt,.json"></label>
              （csv / xlsx / xls，可多选）
            </div>
            <div class="muted" style="font-size:12px;margin-top:10px">
              订单表 · 广告表 · 联盟佣金表 · 商品映射表 都能认
            </div>
          </div>
          <input type="file" id="imp2-link-file" accept=".xlsx,.xls,.csv" hidden>
          <input type="file" id="imp2-map-file" accept=".xlsx,.xls,.csv" hidden>
          <div class="tools" style="margin-top:12px;flex-wrap:wrap">
            <button class="ghost" id="btn-imp2-map">单独导入「商品映射表」（SKU → 商品ID）</button>
            <button class="ghost" id="btn-imp2-link">单独导入「链接资料」（款式编码）</button>
          </div>
          <p class="muted" style="font-size:12.5px;margin:10px 0 0">
            <b>商品映射表很关键</b>：订单导出表里只有 SKU ID / 商家 SKU，<b>没有商品 ID</b>，
            所以「哪笔订单属于哪条链接」全靠这张映射表。不传的话程序只能按 SKU 猜，
            猜不出来的全归到「无ID」——汇总页就只剩一行，看不出是哪个链接赚的。<br>
            从店铺后台「商品 → 批量编辑 / 导出」下载商品表（里面要有 <span class="mono">Product ID</span>
            和 <span class="mono">SKU ID</span> 两列），点上面这个按钮传上来即可。<br>
            链接资料是全局的、不分店铺；不传就按商家 SKU 自动推款式编码
            （<span class="mono">V27-White</span> → <span class="mono">V27</span>）。
          </p>
          <div class="note" id="imp2-note"></div>
          <div id="imp2-result"></div>
        </div></div>

      <div class="card"><h2>③ 已经传了哪些<span class="hint" id="imp2-hint"></span></h2>
        <div class="body tight" id="imp2-files"><div class="empty">读取中…</div></div>
        <div class="body" style="border-top:1px solid var(--line-soft);padding-top:12px">
          <div class="row">
            <button onclick="go('stores')">管理店铺</button>
          </div>
        </div></div>
    </section>

        <section class="page" id="view-manual" hidden>
      <div class="head"><h1>手动核算<span class="tag">独立数据区</span></h1>
        <p>每张卡就是一家店：<b>在哪张卡导入，数据就属于哪家店</b>，跟驾驶舱的自动取数完全隔开。
           文件放 <span class="mono">数据\店铺\&lt;店名&gt;\手动导入\</span>，
           报表放 <span class="mono">报表\手动\&lt;店名&gt;\</span>。<br>
           驾驶舱的「重算」永远只读收件箱，这里的表不会掺进去，反过来也一样。</p></div>

      <div class="bar">
        <div class="inline"><span style="color:var(--text-3)">区间</span>
          <button class="rbtn" id="mn-range" title="点一次，起止日期一起选">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                 stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="4.6" width="18" height="16.4" rx="3"/><path d="M3 10h18M8 2.6v4M16 2.6v4"/>
            </svg><span id="mn-range-txt">全部数据</span>
          </button>
        </div>
        <div class="chips tight" id="mn-chips" style="margin:0">
          <button data-k="all" class="on">全部</button>
          <button data-k="month">本月</button><button data-k="lastmonth">上月</button>
        </div>
        <label class="chk"><input type="checkbox" id="mn-formula"> 导出带公式</label>
        <div class="grow"></div>
        <span class="muted" id="mn-hint" style="font-size:12.5px"></span>
      </div>

      <div class="store-grid" id="mn-grid"><div class="sc-empty">正在读取店铺…</div></div>
      <div class="note" id="mn-note"></div>
      <input type="file" id="mn-file" multiple hidden accept=".csv,.xlsx,.xls,.txt,.json">
    </section>

    <section class="page" id="view-reports" hidden>
      <div class="head"><h1>报表</h1>
        <p>两个房间分开放：<b>自动报表</b>是驾驶舱取数、卡片「重算」跑出来的；<b>手动报表</b>是「手动核算」跑出来的。
           两边都能按店铺筛、都能下载到自己指定的位置。点「查看」直接在界面里看汇总和费用拆解。</p></div>

      <div class="card"><h2><span id="rep-room-title">自动报表</span><span class="hint" id="rep-hint"></span></h2>
        <div class="body" style="padding:14px 20px 0">
          <div class="chips" id="rep-rooms" style="margin-bottom:10px">
            <button data-room="auto" class="on">自动报表</button>
            <button data-room="manual">手动报表</button></div>
          <div class="chips" id="rep-filter" style="margin-bottom:12px">
            <button data-store="" class="on">所有店铺</button></div>

          <div class="rep-bar">
            <label class="chk"><input type="checkbox" id="rep-all-chk"> 全选</label>
            <span class="muted" id="rep-sel">已选 0 份</span>
            <span style="flex:1"></span>
            <button class="primary" id="rep-dl">下载勾选的</button>
            <button id="rep-dl-all">下载当前店铺全部</button>
            <button class="danger" id="rep-del">删除勾选的</button>
          </div>

          <div class="rep-bar" style="border-top:none">
            <span class="rep-k">下载到</span>
            <input type="text" id="rep-dir" placeholder="留空 = 桌面"
                   style="flex:1;min-width:220px;padding:7px 10px;font-size:13px;
                          border:1px solid var(--line);border-radius:8px">
            <button class="mini" id="rep-dir-pick">选择文件夹…</button>
            <button class="mini" id="rep-dir-open">打开</button>
            <label class="chk" style="margin-left:6px">
              <input type="checkbox" id="rep-formula"> 导出带公式
            </label>
          </div>

          <p class="muted" style="font-size:12.5px;margin:4px 0 14px;line-height:1.7" id="rep-note">
            报表按「房间」分目录存（<span class="mono">报表\自动\店名\</span>、
            <span class="mono">报表\手动\店名\</span>）。
            勾「导出带公式」会按原区间重算一份带 SUMIFS 的版本，能在 Excel 里自己切含/不含样品。<br>
            删除只是挪进 <span class="mono">报表\_已删除\</span>，删错了能找回来。</p>
        </div>
        <div class="body tight" id="rep-list"><div class="empty">加载中…</div></div>
      </div>
    </section>

    <section class="page" id="view-stores" hidden>
      <div class="head"><h1>店铺</h1>
        <p>在浏览器里登录过的店铺会自动出现在这里。名字可以自己改，只有你自己看得到——底下那串 ID 才是唯一标识。</p></div>
      <div class="card"><h2>所有店铺<span class="hint" id="store-hint"></span></h2>
        <div class="body tight" id="store-list"><div class="empty">加载中…</div></div></div>
      <div class="card"><h2>链接资料（款式编码）<span class="hint">现在不用导了，留作手动补漏</span></h2>
        <div class="body">
          <p class="muted" style="font-size:12.5px;margin:0 0 12px">
            <b>已不需要再导这张表。</b>订单表里每行都带「SKU ID」和「Seller SKU」，
            程序会自动从订单里推出款式编码
            （<span class="mono">MY-J12-5PCS</span> → <span class="mono">MY-J12</span>），
            报表里那条链接就显示成 <span class="mono">V1/V2/V61</span> 这样的样子。<br>
            这里保留上传入口，只用于两种情况：某个 SKU 一直没出过单（订单里抓不到），
            或你想手工指定某个款式编码。
          </p>
          <div class="tools">
            <label class="mini-btn">选择链接资料表
              <input type="file" id="link-file" accept=".xlsx,.xls,.csv" hidden></label>
            <button class="primary" id="btn-link-import">导入</button>
          </div>
          <div class="note" id="link-note"></div>
          <div id="link-stat" class="muted" style="font-size:12px;margin-top:10px"></div>
        </div></div>
      <div class="card"><h2>怎么识别店铺</h2><div class="body tight"><div class="kv">
        <div>识别方式</div><div>扩展在店铺页面里调平台的账号接口，取 shop_id、店铺名、国家</div>
        <div>为什么用 ID</div><div>店铺名可能重名、也可能改；shop_id 是平台给的唯一号，不会变</div>
        <div>数据怎么分开</div><div>每家店一个目录 <span class="mono">data\stores\&lt;shop_id&gt;\inbox</span>，核算时只读自己那份</div>
        <div>名字存哪</div><div><span class="mono">config\stores.json</span>，程序不会覆盖你改的名字</div>
      </div></div></div>
    </section>

    <section class="page" id="view-params" hidden>
      <div class="head"><h1>计算参数</h1>
        <p>每一项就是一条公式。公式里能写的东西，点下面的方块插进去就行——<b>不用记英文</b>。</p></div>

      <div class="tools" id="ptabs" style="margin-bottom:14px"></div>

      <div class="pane" data-pane="params">
      <div class="card"><h2>预设<span class="hint">一整套口径的命名快照，随时切回</span></h2>
        <div class="body">
          <div class="row" style="gap:8px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="width:210px"><label>选一个预设</label>
              <select id="ps-list" style="min-width:200px"></select></div>
            <div class="field" style="width:190px"><label>保存到</label>
              <select id="ps-scope"></select></div>
            <div class="field" style="width:190px"><label>预设名</label>
              <input type="text" id="ps-name" placeholder="如：马来标准口径"></div>
            <button class="primary" id="ps-apply">应用</button>
            <button class="ghost" id="ps-save" style="width:auto;padding:7px 15px;margin:0">保存为预设</button>
            <button class="ghost" id="ps-del" style="width:auto;padding:7px 15px;margin:0">删除</button>
          </div>
          <label class="switch" style="margin-top:11px">
            <input type="checkbox" id="ps-rates">
            <span><b>连各国费率一起存</b>
              <em>勾上：这份预设同时装下费率页的数值，应用时会一起覆盖。
                 不勾：只存参数口径与列标题。</em></span>
          </label>
          <p class="muted" id="ps-desc" style="font-size:12px;margin:9px 0 0"></p>
          <div class="note" id="ps-note"></div>
        </div>
      </div>

      <div class="card"><h2>参数表
        <select id="pscope" style="font-size:12.5px;padding:4px 9px;min-width:180px;margin-left:6px"></select>
        <span class="hint" id="pscope-hint">每个国家一套，互不影响</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="ptbl"><thead><tr>
            <th style="min-width:150px">参数</th><th style="min-width:280px">计算公式</th>
            <th class="kcell">正常</th><th class="kcell">已取消</th><th class="kcell">退货退款</th>
            <th class="kcell">0元样品</th><th class="kcell">未付款</th>
            <th class="kcell" style="width:48px">启用</th><th style="width:54px"></th>
          </tr></thead><tbody id="pbody"></tbody></table>
        </div></div>
        <div class="body" style="padding-top:0;padding-bottom:0">
          <label class="switch">
            <input type="checkbox" id="drop-zero">
            <span><b>去除 0 元样品订单</b>
              <em>打开后发单费、商品成本、订单数、件数都不算样品单，算出来的就是纯正常订单（含退货退款）的净利。
                  下面「0元样品」那一列会变灰、不生效，关掉开关各自勾选原样恢复。</em></span>
          </label>
        </div>
        <div class="body" style="padding-top:14px">
          <p class="muted" style="font-size:12.5px;margin:0">
            <b>订单数</b>、<b>件数</b> 这两行是报表上的显示列，也各自有勾选，单独控制——
            勾了「0元样品」订单数就含样品单，取消就只算正常 + 退货单。<br>
            发单费、订单服务费公式里的 <span class="mono">订单数</span> 同样按<b>各自那一行</b>的勾选算，
            所以「订单数列 973 单、服务费按 927 单」这种组合是可以的，互不干扰。
          </p>
        </div>
      </div>

      <div class="card"><h2>公式里能写什么<span class="hint">点一下就插到刚编辑的公式里</span></h2>
        <div class="body">
          <div style="border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:14px">
            <div style="padding:12px 16px;background:#fafbfc;border-bottom:1px solid var(--line-soft)">
              <b>公式长什么样</b>：用 <span class="mono">*</span>（乘）、
              <span class="mono">+</span>（加）、<span class="mono">-</span>（减）、
              <span class="mono">/</span>（除）把东西算出来。
              <div style="margin-top:8px;font-size:12.5px;color:var(--text-2)">
                照着抄这几个就够用了：
              </div>
              <div class="mono" style="margin-top:6px;font-size:12px;line-height:1.9">
                平台佣金　　＝ 本行GMV × 平台佣金率<br>
                交易手续费　＝ 本行买家实付 × 交易手续费率<br>
                发单费　　　＝ 本行订单数 × 发单费单价 × 人民币汇率<br>
                商品成本　　＝ 成本单价 × 人民币汇率
              </div>
            </div>
            <div class="kv">
            <div>本行 X</div>
            <div><span class="mono">本行订单数</span>、<span class="mono">本行件数</span>、
              <span class="mono">本行GMV</span>、<span class="mono">本行买家实付</span>、
              <span class="mono">本行成本单价</span>。<br>
              「本行」= 按<b>这一行自己勾选的订单类别</b>算。
              所以「发单费」只算正常单的话，它的「本行订单数」就只含正常单。</div>
            <div>费率项</div>
            <div>费率带「率」字：<span class="mono">平台佣金率</span>、
              <span class="mono">交易手续费率</span>、<span class="mono">奖金返现率</span>、
              <span class="mono">发单费单价</span>、<span class="mono">订单服务费单价</span>、
              <span class="mono">人民币汇率</span>。<br>
              中文名在「费率」页的「费率项的中文名」里改，改完两边都能用。</div>
            <div>参数表里别的行</div>
            <div>直接写那一行的名字，比如 <span class="mono">商品成本</span>、
              <span class="mono">订单服务费</span>，就是引用它们算出来的金额。</div>
            <div>订单表的列</div>
            <div>写成 <span class="mono">[列标题]</span>。列名会变、换国家就变，
              所以可以写<b>多个候选，一行一个</b>（点「＋ 加一行」就行）：
              <div class="mono" style="margin:5px 0;font-size:11.5px;line-height:1.6">
                [折扣前小计<br>SKU Subtotal Before Discount<br>SKU折扣前小计]
              </div>
              程序按顺序挨个找，找到第一个就用它——第一个填你常用的，后面填兜底的。
              找不到任何一个，这项按 0 计，并在报表「异常」页列出来。</div>
            </div>
          </div>
          <div class="vars" id="vars"></div>
        </div></div>
      </div>

      <div class="pane" data-pane="headers" hidden>
      <div class="card"><h2>表头识别<span class="hint">每个表一套：程序靠这些列找数；标题可加多个候选，按顺序试</span></h2>
        <div class="body tight">
          <div class="tools" id="oftabs" style="margin-bottom:10px"></div>
          <div class="tbl-wrap">
          <table class="cfg-tbl" id="oftbl"><thead><tr>
            <th style="width:210px">用途</th><th>表里的标题（多个候选一行一个，按顺序试）</th>
          </tr></thead><tbody id="ofbody"></tbody></table>
          </div>
        </div>
        <div class="body" style="padding-top:0">
          <p class="muted" style="font-size:12.5px;margin:0">
            这一栏只用来判断「这行订单算不算取消/退货/未付款」「属于哪条链接」「按哪个编码对成本」。
            金额怎么算，全在参数表的公式里，不在这里。
          </p>
        </div>
      </div>
      </div>

      <div class="pane" data-pane="reports" hidden>
      <div class="card"><h2>报表列（汇总页）
        <span class="hint">勾选要展示哪些列；想改名就在「自定义标题」里写</span></h2>
        <div class="body">
          <div class="row" style="gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <label class="switch" style="margin:0">
              <input type="checkbox" id="rcols-all">
              <span><b>全选展示</b><em>取消勾选 = 不展示这一列</em></span>
            </label>
            <label class="switch" style="margin:0">
              <input type="checkbox" id="rcol-total-on">
              <span><b>显示合计行</b><em>关掉后汇总页不出现「合计」那一行</em></span>
            </label>
            <div style="font-size:13px;color:var(--text-2)" id="rcol-total-pos">
              合计行位置：
              <label style="margin:0 4px;cursor:pointer"><input type="radio" name="rcol-total" value="bottom" checked> 表格底部</label>
              <label style="margin:0 4px;cursor:pointer"><input type="radio" name="rcol-total" value="top"> 表格顶部（首行）</label>
            </div>
          </div>
          <div class="tbl-wrap">
            <table class="cfg-tbl" id="rcoltbl"><thead><tr>
              <th style="width:54px">展示</th>
              <th style="width:104px">顺序（可拖）</th>
              <th style="width:160px">默认标题</th>
              <th>自定义标题（留空=用默认，如 毛利率 → 链接毛利率）</th>
              <th style="width:84px">类型</th>
            </tr></thead><tbody id="rcols-body"></tbody></table>
          </div>
          <p class="muted" style="font-size:12.5px;margin:8px 0 0">
            货币类列（GMV / 成本 / 毛利 / 各项费用 / 净利）会自动带上该国货币代码（如 MYR / PHP），改了名也会带。
            每个国家都能单独配：切到上面「参数表」的国家下拉即可。
          </p>
        </div>
      </div>
      </div>

      <div class="pane" data-pane="formula" hidden>
      <div class="card"><h2>净利怎么算<span class="hint">这一条也可以改</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="ftbl"><thead><tr>
            <th style="width:170px">结果</th><th>公式（可直接写中文）</th><th style="width:40px"></th>
          </tr></thead><tbody id="fbody"></tbody></table>
        </div></div>
        <div class="body" style="padding-top:0">
          <p class="muted" style="font-size:12.5px;margin:0">
            默认就是把上面每一项减掉：
            <span class="mono">净利 = GMV - 商品成本 - 发单费 - 平台佣金 - 交易手续费
            - 奖金返现 - 广告花费 - 达人佣金 - 订单服务费</span>。
            你在参数表里新增了一项，记得也把它加进这一行。<br>
            这里也能用中文：参数表里那一行的名字直接写（<span class="mono">商品成本</span>），
            费率写成「…率」（<span class="mono">平台佣金率</span>），
            「净利」这种结果名也能直接引用。
          </p>
        </div>
      </div>
      </div>

      <div class="card"><h2>操作<span class="hint">试算不会保存</span></h2><div class="body">
        <div class="tools">
          <button class="primary" id="btn-preview">试算看净利变化</button>
          <button id="btn-save">保存配置</button>
          <button class="mini" id="btn-all-on">全部类别都勾上</button>
          <button class="mini" id="btn-all-off">全部类别都取消</button>
          <button class="mini" id="btn-add">＋ 新增参数</button></div>
        <div class="diff" id="diff"><h3>试算结果</h3><div id="diff-body"></div></div>
      </div></div>
    </section>

    <section class="page" id="view-costs" hidden>
      <div class="head"><h1>成本表</h1>
        <p>商品成本 = 件数 × 该编码的成本。成本不常变，导一次就行，新增商品时补一条。</p></div>

      <div class="card"><h2>概况<span class="hint" id="cost-hint"></span></h2><div class="body">
        <div class="stats-line" id="cost-stats"></div>
        <div class="tools" style="margin-top:14px">
          <button id="btn-cost-check">拿订单核一遍</button>
          <a class="mini-btn" id="btn-cost-tpl" href="/api/costs/template"
             download="成本表导入模板.xlsx"
             title="窗口内会存到「设置 → 报表保存位置」里指定的模板目录（留空=程序里的「报表」目录）">下载导入模板</a>
          <label class="mini-btn">导入 Excel / CSV
            <input type="file" id="cost-file" accept=".xlsx,.xls,.csv" hidden></label>
          <button class="mini" id="btn-cost-add">＋ 新增编码</button>
          <button class="primary" id="btn-cost-save">保存成本表</button>
        </div>
        <div class="row" style="gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
          <span style="font-size:12.5px;color:var(--text-2)">模板导出到</span>
          <input type="text" id="cost-tpl-dir" placeholder="留空 = 程序「报表」目录；也可填 D:\净利模板"
                 style="width:320px;font-size:12.5px;padding:6px 9px">
          <span class="muted" style="font-size:12px">点「下载导入模板」时就存到这里（自动记住）</span>
        </div>
        <div class="note" id="cost-note"></div>
        <div id="cost-check"></div>
        <p class="muted" style="font-size:12.5px;margin:14px 0 0">
          <b>不知道表格该长什么样？点「下载导入模板」。</b><br>
          模板里第一列是<b>商品编码</b>（要和订单里的「卖家编码」一字不差），
          第二列是<b>单个成本</b>（人民币，买几件不用乘，程序自己乘）。<br>
          <b>导入是增量的：</b>表里没有的旧编码保留不动；新出现的编码新增；
          已有、这次又给了的编码，<b>以这次导入的为准</b>（覆盖旧成本）。
          导入完会告诉你新增几条、更新几条。
        </p>
      </div></div>

      <div class="card"><h2>成本表的列<span class="hint">表头叫别的名字也能对上</span></h2>
        <div class="body">
          <div class="row" style="gap:18px;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:260px"><label>哪一列是商品编码</label>
              <input type="text" id="cost-code-titles" placeholder="商品编码|编码|Seller SKU"></div>
            <div class="field" style="flex:1;min-width:260px"><label>哪一列是成本（人民币）</label>
              <input type="text" id="cost-cost-titles" placeholder="成本|成本价|采购成本"></div>
            <button id="btn-cost-fields">保存列设置</button>
          </div>
          <p class="muted" style="font-size:12.5px;margin:12px 0 0">
            成本表里的成本是<b>人民币</b>，核算时会按汇率换成本地货币。
            商品编码要和订单里的「卖家编码」一致，成本才记得上。
          </p>
        </div></div>

      <div class="card"><h2>所有编码<span class="hint" id="cost-list-hint"></span></h2>
        <div class="body tight">
          <div style="padding:0 0 10px"><input type="text" id="cost-search" placeholder="搜编码，也可直接翻页"
            style="width:280px"></div>
          <div class="tbl-wrap">
            <table class="cfg-tbl" id="ctbl"><thead><tr>
              <th style="width:44%">商品编码</th><th style="width:22%">成本（人民币）</th>
              <th style="width:26%">最后更新</th><th style="width:8%"></th>
            </tr></thead><tbody id="cbody"></tbody></table>
          </div>
          <div class="tools" style="padding-top:12px">
            <button class="mini" id="btn-cost-prev">上一页</button>
            <span class="muted" id="cost-page" style="font-size:12.5px"></span>
            <button class="mini" id="btn-cost-next">下一页</button>
          </div>
        </div>
      </div>
    </section>

    <section class="page" id="view-rates" hidden>
      <div class="head"><h1>费率</h1>
        <p>每个国家的佣金、发单费、手续费都在这改。改完点保存，下次核算就用新费率。</p></div>
      <div class="card"><h2>预设<span class="hint">和计算参数页共用同一套预设</span></h2>
        <div class="body">
          <div class="row" style="gap:8px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="width:210px"><label>选一个预设</label>
              <select id="pr-list" style="min-width:200px"></select></div>
            <div class="field" style="width:190px"><label>保存到</label>
              <select id="pr-scope"></select></div>
            <div class="field" style="width:190px"><label>预设名</label>
              <input type="text" id="pr-name" placeholder="如：马来费率"></div>
            <button class="primary" id="pr-apply">应用</button>
            <button class="ghost" id="pr-save" style="width:auto;padding:7px 15px;margin:0">保存为预设</button>
            <button class="ghost" id="pr-del" style="width:auto;padding:7px 15px;margin:0">删除</button>
          </div>
          <label class="switch" style="margin-top:11px">
            <input type="checkbox" id="pr-rates" checked>
            <span><b>连各国费率一起存</b>
              <em>费率页的这份预设默认连费率一起装；不勾就只存参数口径。</em></span>
          </label>
          <p class="muted" id="pr-desc" style="font-size:12px;margin:9px 0 0"></p>
          <div class="note" id="pr-note"></div>
        </div>
      </div>
      <div class="card"><h2>国家费率<span class="hint" id="rate-hint"></span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="rtbl"><thead id="rhead"></thead><tbody id="rbody"></tbody></table>
        </div></div></div>
      <div class="card"><h2>操作<span class="hint">汇率的自动抓取在「设置」里</span></h2><div class="body">
        <div class="tools"><button class="primary" id="btn-rate-save">保存费率</button>
          <button class="mini" id="btn-rate-add">＋ 新增国家</button>
          <button class="mini" id="btn-fee-add">＋ 新增费率项</button></div>
        <div class="kv" style="margin-top:18px;border:1px solid var(--line);border-radius:10px;overflow:hidden">
          <div>汇率方向</div><div><b>1 人民币 = 多少本地货币</b>。马来西亚填 0.61，意思是 1 元人民币换 0.61 马币</div>
          <div>发单费</div><div>按<b>人民币</b>定价（国内结算），核算时 × 汇率换成本地货币。
            所以改汇率，发单费会自动跟着变。「发单费(本地)」那列是算出来的，不用填</div>
          <div>订单服务费</div><div>每单固定收多少，直接填<b>本地货币</b>（马来西亚 0.54、菲律宾 5）。
            它和发单费分开算，报表里也是两列</div>
          <div>其余费率</div><div>写小数，比如 <b>0.081</b> 表示 8.1%，都是按成交额比例收的</div>
          <div>增删费率项</div><div>「＋ 新增费率项」给所有国家加一列；列标题右上角的 × 删掉整列。
            新增的项在「计算参数」里用 <span class="mono">params.字段名</span> 引用</div>
        </div>
      </div></div>
      <div class="card"><h2>费率项的中文名<span class="hint">起了名之后，公式里直接写中文</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="cnltbl"><thead><tr>
            <th style="width:230px">费率项</th>
            <th style="width:260px">中文名（公式里可用）</th>
            <th style="width:230px">公式里就这么写</th>
          </tr></thead><tbody id="cnlbody"></tbody></table>
        </div></div>
        <div class="body" style="padding-top:0">
          <p class="muted" style="font-size:12.5px;margin:0">
            记不住英文也没关系：在这里给每个费率项起个中文名，
            「计算参数」的公式里就能直接写 <span class="mono">平台佣金</span>、
            <span class="mono">交易手续费</span>，不用再写 <span class="mono">params.platform_commission</span>。
            改完点「保存费率」生效。
          </p>
        </div>
      </div>
    </section>

    <section class="page" id="view-settings" hidden>
      <div class="head"><h1>设置</h1>
        <p>报表叫什么名字、存到哪、汇率怎么取，都在这里定。</p></div>
      <div class="card"><h2>报表文件名</h2><div class="body">
        <div class="field" style="max-width:520px"><label>命名模板</label>
          <input type="text" id="set-template" placeholder="{店铺}_{区间}"></div>
        <p class="muted" style="font-size:12.5px;margin:12px 0 0">
          可用变量：<span class="mono">{店铺}</span> <span class="mono">{店铺ID}</span>
          <span class="mono">{国家}</span> <span class="mono">{开始}</span>
          <span class="mono">{结束}</span> <span class="mono">{区间}</span>
          <span class="mono">{生成时间}</span><br>
          例：<span class="mono">{店铺}_{开始}_{结束}</span> →
          <span class="mono">马来一号店_2026-10-01_2026-10-15.xlsx</span></p>
      </div></div>
      <div class="card"><h2>报表保存位置</h2><div class="body">
        <div class="field" style="max-width:520px">
        <div class="field" style="max-width:640px">
          <label>核算完自动复制到这个文件夹（留空则只在程序目录里）</label>
          <div class="pick-row">
            <input type="text" id="set-dir" placeholder="点右边选一个文件夹，或直接粘贴路径">
            <button class="mini" id="set-dir-pick">选择文件夹…</button>
            <button class="mini" id="set-dir-open">打开</button>
          </div>
        </div>
        <div class="field" style="max-width:640px;margin-top:14px">
          <label>「下载导入模板」存到这个文件夹（留空则存到程序的「报表」目录）</label>
          <div class="pick-row">
            <input type="text" id="set-tpl-dir" placeholder="点右边选一个文件夹，或直接粘贴路径">
            <button class="mini" id="set-tpl-pick">选择文件夹…</button>
            <button class="mini" id="set-tpl-open">打开</button>
          </div>
        </div>
        <p class="muted" style="font-size:12.5px;margin:10px 0 0">
          「打开」会在资源管理器里打开那个文件夹；路径不存在时程序会自动创建。</p>
        <label class="switch" style="margin-top:12px">
          <input type="checkbox" id="set-formula">
          <span><b>导出的报表带 Excel 公式</b>
            <em>带公式：明细页多几列辅助数据，汇总页用 SUMIFS 引用，
              在 Excel 里改「口径」页的开关就能自己切含/不含样品单，不用回程序重算。
              不带：文件小、打开快，数字是最终值。</em></span>
        </label>
        <p class="muted" style="font-size:12.5px;margin:12px 0 0">
          每次核算完成后，报表会同时出现在程序的 <span class="mono">报表\店铺名\</span> 和这里。同名不会覆盖，会自动加 (2)(3)。</p>
      </div></div>

      <div class="card"><h2>计划任务<span class="hint">定时自动跑</span></h2>
        <div class="body">
          <p style="margin:0 0 14px;color:var(--text-2);font-size:13px">
            定时自动抓数、核算、出报表，已经单独放到「计划任务」页——可以配好几条，
            每条各选时间、区间和店铺。</p>
          <button class="ghost" id="go-plan" style="width:auto;padding:7px 16px;margin:0">去计划任务页 →</button>
        </div></div>

      <div class="card"><h2>汇率</h2><div class="body">
        <div class="field"><label>取法</label><select id="set-fx">
          <option value="auto">联网自动抓（推荐）</option>
          <option value="manual">只用费率页里填的固定值</option></select></div>
        <p class="muted" style="font-size:12.5px;margin:12px 0 0">
          自动模式抓不到网络时会退回费率页里的固定值，不会让整次核算失败。</p>
      </div></div>
      <div class="card"><h2>保存</h2><div class="body">
        <button class="primary" id="btn-set-save">保存设置</button></div></div>
    </section>

    <section class="page" id="view-plan" hidden>
      <div class="head"><h1>计划任务</h1>
        <p>到点自动抓数 → 核算 → 出报表。可以配多条，各选时间、区间和店铺；
          浏览器没开就排队，等它下次打开自动补上。</p></div>

      <div id="plan-alert"></div>
      <div class="plan-grid" id="plan-list"></div>

      <div class="row" style="gap:12px;align-items:center">
        <button class="primary" id="plan-add">＋ 新建计划</button>
        <span class="muted" style="font-size:12.5px">
          只存「什么时候跑、算哪一段」的规则，不存死日期——明年、换台电脑都还是对的</span>
      </div>
    </section>

    <div class="modal" id="plan-modal"><div class="box" style="width:min(760px,100%)">
      <header><h3 id="pm-title">新建计划</h3><span class="muted" id="pm-sub"></span>
        <button class="ghost x" id="pm-x" style="width:auto;padding:5px 12px">关闭</button></header>
      <div class="content" style="padding:20px 24px">
        <div class="prev" id="pm-prev"></div>
        <div class="frm" id="pm-form"></div>
      </div>
      <footer>
        <button class="primary" id="pm-save">保存</button>
        <button id="pm-cancel">取消</button>
        <span style="flex:1"></span>
        <button id="pm-run">保存并立即跑一次</button>
      </footer>
    </div></div>

    <section class="page" id="view-help" hidden>
      <div class="head"><h1>使用帮助</h1>
        <p>第一次用先看这页，三分钟能跑起来。</p></div>

      <div class="card"><h2>程序在哪、怎么启动<span class="hint">最常见的问题</span></h2>
        <div class="body tight"><div class="kv">
          <div>装好的版本</div><div>桌面上有<b>「净利助手」快捷方式</b>，双击它即可。
            装到别的位置的：开始菜单搜「净利助手」，或直接双击安装目录里的
            <span class="mono">jingli.exe</span></div>
          <div>源码版</div><div>项目根目录里双击 <b class="mono">启动净利助手.bat</b>。
            它会自动找一个能用的 Python 并启动。</div>
          <div>程序界面在哪</div><div>程序启动后会自动打开浏览器；
            没打开的话手动访问 <span class="mono">http://127.0.0.1:8765</span></div>
          <div>关掉程序</div><div>关掉那个浏览器标签页不算关程序——
            桥还在后台跑。要彻底停：关掉「净利助手」窗口，或在任务管理器里结束它。</div>
        </div></div></div>

      <div class="card"><h2>第一次用（只做一次）</h2>
        <div class="body tight"><div class="kv">
          <div>第 1 步</div><div>打开<b>浏览器</b>（Chrome / Edge / QQ 浏览器都行）</div>
          <div>第 2 步</div><div>地址栏输入 <span class="mono">chrome://extensions</span></div>
          <div>第 3 步</div><div>打开右上角<b>「开发者模式」</b></div>
          <div>第 4 步</div><div>点<b>「加载已解压的扩展程序」</b></div>
          <div>第 5 步</div><div>选程序目录里的 <b class="mono">浏览器扩展</b> 文件夹
            （和 jingli.exe 平级的那个）。<b>选文件夹本身，不是里面的文件。</b></div>
        </div></div></div>

      <div class="card"><h2>各文件夹是干嘛的</h2>
        <div class="body tight"><div class="kv">
          <div>浏览器扩展/</div><div>刚才加载的那个。里面是抓数用的代码，不用改</div>
          <div>配置/</div><div>费率、店铺清单、计算参数、预设。<b>想改口径就在这儿</b>，
            也可以用记事本直接改里面的 json</div>
          <div>数据/</div><div>自动抓下来的原始数据、每家店的数据、历史快照。<b>别删</b></div>
          <div>报表/</div><div>算好的 Excel，按店铺分文件夹。也可以在「报表」页直接下载</div>
          <div>文档/</div><div>详细说明文档</div>
          <div>使用说明.txt</div><div>给新手的简短说明，就放在最外面</div>
          <div>_internal/</div><div>程序运行库，不用管</div>
        </div></div></div>

      <div class="card"><h2>三步用起来</h2>
        <div class="body tight"><div class="kv">
          <div>① 取数</div><div>点扩展图标 → 选日期 → 「立即运行：取数 + 算净利」。
            第一次会自动认出你的店铺</div>
          <div>② 核对</div><div>算完在「报表」页点「查看」，不用打开 Excel 就能看汇总和费用拆解</div>
          <div>③ 下载</div><div>「报表」页可以按店铺筛选，勾几份点「下载勾选的」，
            会打成一个 zip；也可以在「设置」里定好目录，以后自动放过去</div>
        </div></div></div>

      <div class="card"><h2>改了参数要重算，不用重新取数<span class="hint">最省时间的一条</span></h2>
        <div class="body tight"><div class="kv">
          <div>取数 vs 重算</div><div><b>取数</b>是去店铺后台把订单/广告/佣金导下来，慢（大店订单要等平台生成几分钟）；
            <b>重算</b>只用已经在本机的数据重新出报表，几十秒。</div>
          <div>什么时候重算</div><div>改了<b>佣金费率、成本表、计算参数、汇率</b>之后——保存完屏幕下方会冒出
            「重新计算」的按钮，点一下就行；也可以回「驾驶舱」在各店卡片上点<b>「重算」</b>。</div>
          <div>同一天重复跑</div><div>同一天、<b>同一个起止日期</b>再跑一次，会直接复用当天第一次导的数据（快）。
            <b>换区间就要重新取数</b>——数据是平台按区间导的，不能拿一段充另一段。</div>
          <div>数据确实变了</div><div>勾上驾驶舱工具条上的<b>「强制重新取数」</b>，就忽略当天的记录，重新去平台导一遍。</div>
          <div>数据分两条线</div><div><b>驾驶舱</b>（运行 / 重算）只认店铺收件箱里自动取到的数据；
            <b>「手动核算」</b>是独立的一块：自己建批次、自己传表，报表单独放在
            <span class="mono">报表\手动\&lt;店名&gt;</span>。两边不互相掺，
            手动传的表不会进店铺、也不会被下次自动核算带上。</div>
        </div></div></div>

      <div class="card"><h2>常见问题</h2>
        <div class="body tight"><div class="kv">
          <div>自动抓数失败</div><div>扩展面板里有<b>「手动导数据」</b>：
            自己去店铺后台点导出，文件一下载就自动入库；
            或者只开页面，导出后到左栏「导入数据」页传上来（要选国家和店铺）</div>
          <div>成本对不上</div><div>「成本表」页点<b>「拿订单核一遍」</b>，
            会列出哪些编码还没成本、哪些是按规则推导的</div>
          <div>净利算得不对</div><div>先看报表的<b>「口径」页</b>：每一项算了哪些订单类型、
            用的哪个汇率、哪个数据快照，都写在那里</div>
          <div>想对比含/不含样品单</div><div>报表里有<b>「样品对比」页</b>，
            两套净利并排列着；也可以在「计算参数」页勾「去除 0 元样品订单」</div>
          <div>口径调乱了</div><div>「计算参数」页有<b>预设</b>：随时切回之前那套，
            也能把现在这套存成预设</div>
        </div></div></div>
    </section>

    <section class="page" id="view-debug" hidden>
      <div class="head"><h1>调试</h1>
        <p>出问题时先看这里：扩展有没有在干活、桥收到了什么、数据存在哪。</p></div>
      <div class="card"><h2>取数桥<span class="hint">接收扩展数据、触发核算的后台服务</span></h2><div class="body">
        <div class="kv" id="br-status" style="margin-bottom:12px">读取中…</div>
        <div class="tools">
          <button class="primary" id="btn-bridge-restart">重启用本程序的桥</button>
          <button id="btn-bridge-stop">关闭取数桥</button>
        </div>
        <p class="muted" style="font-size:12.5px;margin:10px 0 0">
          「启动 / 重启」都会先关掉别的桥（比如另一个目录安装的那份），
          再起<b>当前程序目录</b>这个，保证数据是同一套；<br>
          「关闭」只停不启。关掉之后：定时任务不会跑，界面刷新也会打不开
          （界面本身就是桥提供的），但<b>已经开着的窗口还能点上面的按钮把它拉起来</b>。
        </p>
      </div></div>
      <div class="card"><h2>自检与修复<span class="hint">断电、蓝屏、强杀之后用它收拾残局</span></h2>
        <div class="body">
          <p style="margin:0 0 12px;font-size:13px;color:var(--text-2)">
            程序每次启动都会自动体检一遍：修好写坏的配置（用 .bak 恢复）、
            清掉断电留下的半截临时文件、清理早就死掉的合并锁、把打不开的报表挪进隔离目录、
            日志过大自动轮转。<b>坏文件只挪走、不删除</b>，都在
            <span class="mono">_损坏文件</span> / <span class="mono">_隔离_日期</span> 里。</p>
          <div class="tools">
            <button class="primary" id="btn-selfcheck">立即体检一次</button>
            <span class="muted" style="font-size:12.5px;align-self:center" id="sc-when"></span>
          </div>
          <div id="sc-actions" style="margin-top:12px"></div>
        </div></div>
      <div class="card"><h2>运行环境</h2><div class="body tight"><div class="kv" id="dbg-env"></div></div></div>
      <div class="card"><h2>扩展日志<span class="hint" id="dbg-log-hint"></span></h2>
        <div class="body"><pre class="log" id="dbg-log">读取中…</pre></div></div>
      <div class="card"><h2>抓包文件</h2><div class="body tight" id="dbg-cap"><div class="empty">无</div></div></div>
    </section>
  </main>
</div>

<div class="modal" id="modal"><div class="box">
  <header><h3 id="modal-title">报表</h3><span class="muted" id="modal-sub"></span>
    <span class="x"><button class="mini" onclick="closeModal()">关闭</button></span></header>
  <div class="content" id="modal-body"></div>
</div></div>
<div class="toast" id="toast"></div>

<!-- 参数改完后的重算条。改费率/成本是为了让数字变对，
     但改完不重算，报表还是旧数字——这一步以前全靠用户自己记得。 -->
<div class="recalc-bar" id="recalc-bar">
  <div class="tx">
    <b id="recalc-title">参数改了，现有报表还是旧数字</b>
    <em id="recalc-sub"></em>
  </div>
  <button class="primary" id="recalc-go">重新计算</button>
  <button class="plain" id="recalc-x">先不用</button>
</div>

<script>
const $ = s => document.querySelector(s);
const KINDS = [{id:'normal',label:'正常'},{id:'cancelled',label:'已取消'},{id:'refunded',label:'退货退款'},
  {id:'zero_amount',label:'0元样品'},{id:'unpaid',label:'未付款'}];
const BASES = [{id:'gmv',label:'GMV'},{id:'buyer_paid',label:'买家实付'},{id:'order_count',label:'订单数'},
  {id:'items',label:'件数'},{id:'cost',label:'商品成本'},{id:'refund',label:'退款金额'},
  {id:'commission',label:'达人佣金'},{id:'ads',label:'广告花费'}];
const RATE_META = ['currency', 'rates_verified', 'dispatch_fee_local', 'dispatch_fee_cny',
  '_dispatch_note', 'region', 'cny_to_local', 'usd_to_local'];
const RATE_LABELS = {
  platform_commission: '平台佣金', tx_fee: '交易手续费', cashback: '奖金返现',
  super_coupon: '超级优惠券', growth_fee: '商家成长', logistics: '物流'
};
let feeFields = [];
let rows = [], formulas = {}, rateRows = {}, storesCache = [];
// 费率项的中文名（费率页可改）：公式里可以直接写中文
let rateLabels = {};
// 引擎权威中文名（src/formula.DEFAULT_RATE_CN 合并用户自定义），
// 公式解析器只认这套名字，变量候选和「不认识」校验必须对齐它。
let rateCnServer = {};

/** 费率项的中文名：优先用费率页起的名，没有就用引擎默认名（带「率」字）。
 *
 *  之前这里只用 RATE_LABELS（不带率：平台佣金），而引擎认的是 DEFAULT_RATE_CN
 *  （带率：平台佣金率），两边对不上——用户在公式里写「平台佣金率」就被判
 *  「不认识」。现在改成跟引擎同一个权威来源，写带率或不带率都能认。 */
function rateCn(k){
  return (rateLabels && rateLabels[k]) || (rateCnServer && rateCnServer[k]) || RATE_LABELS[k] || k;
}


/* ---------------- 驾驶舱 ---------------- */
let LIVE = null, LIVE_T = null, PICKED = new Set(), HOME_MODE = 'total';
const STEP_KEYS = ['sku', 'orders', 'ads', 'affiliate', 'calc', 'report'];

function pad2(n){ return String(n).padStart(2, '0'); }
function dstr(d){ return d.getFullYear() + '-' + pad2(d.getMonth()+1) + '-' + pad2(d.getDate()); }

function sec2text(sec){
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + ' 分' + (s ? ' ' + pad2(s) + ' 秒' : '');
  return Math.floor(m/60) + ' 时' + (m%60) + ' 分';
}

/* ---------------- 区间日历 ----------------
 *
 * 为什么不用 <input type="date">：
 *   1. 原生日历只能点那个小图标才弹，命中区域小得离谱；
 *   2. 它是浏览器的浮层，卡片墙每 3 秒重绘一次 innerHTML，
 *      浮层当场被销毁 —— 表现就是「日历点开几秒自己收回去」。
 * 所以自己画一个，挂在 body 上（不受重绘影响），并且一次点完两个日期：
 * 第一次点 = 起点，第二次点 = 终点；第二下比第一下早，就自动把早的当起点。
 */
let HOME_RANGE = null;          // 总控用的区间
const CARD_RANGE = {};          // 单店模式下每张卡自己的区间
// 刚重算完的结果，按店铺记在内存里画在卡片上。
// 卡片墙每 3 秒重画一次，写进 DOM 会被冲掉，只能记在这儿。
const CARD_RECALC = {};

const RP = { el: null, anchor: null, onPick: null, a: null, b: null, y: 0, m: 0, phase: 'a' };

function fmtRange(r){
  if(!r || !r.startDate || !r.to) return '选择区间';
  return `${r.startDate.replace(/-/g, '/')} ~ ${r.to.replace(/-/g, '/')}`;
}

function rpBuild(){
  if(RP.el) return;
  const el = document.createElement('div');
  el.className = 'rp';
  el.id = 'rp';
  el.innerHTML = `
    <div class="rp-top">
      <div class="rp-box" id="rp-ba"><span>起点</span><b>—</b></div>
      <div class="rp-box" id="rp-bb"><span>终点</span><b>—</b></div>
    </div>
    <div class="rp-chips">
      <button data-k="last_month">上月</button>
      <button data-k="month">本月至今</button>
      <button data-k="last7">近 7 天</button>
      <button data-k="last30">近 30 天</button>
    </div>
    <div class="rp-head">
      <button id="rp-prev" title="上个月">‹</button>
      <b id="rp-title"></b>
      <button id="rp-next" title="下个月">›</button>
    </div>
    <div class="rp-wd">${['一','二','三','四','五','六','日'].map(x => `<span>${x}</span>`).join('')}</div>
    <div class="rp-grid" id="rp-grid"></div>
    <div class="rp-foot">
      <span class="rp-hint" id="rp-hint">点两下：先点起点，再点终点</span>
      <button id="rp-today">今天</button>
    </div>`;
  document.body.appendChild(el);
  RP.el = el;                 // 必须挂上，不然 rpOpen 里的 RP.el 还是 null

  el.querySelector('#rp-prev').onclick = e => { e.stopPropagation(); rpMonth(-1); };
  el.querySelector('#rp-next').onclick = e => { e.stopPropagation(); rpMonth(1); };
  el.querySelector('#rp-today').onclick = e => {
    e.stopPropagation();
    const t = new Date();
    rpPick(new Date(t.getFullYear(), t.getMonth(), t.getDate()));
  };
  el.querySelectorAll('.rp-chips button').forEach(b => {
    b.onclick = e => {
      e.stopPropagation();
      const [x, y] = rangeOf(b.dataset.k);
      rpApply(x, y);
    };
  });
  // 点面板内部不算「点了外面」
  el.addEventListener('mousedown', e => e.stopPropagation());
  document.addEventListener('mousedown', e => {
    if(!RP.el.classList.contains('show')) return;
    if(RP.el.contains(e.target) || (RP.anchor && RP.anchor.contains(e.target))) return;
    rpClose();
  });
  window.addEventListener('resize', () => rpClose());
  window.addEventListener('scroll', () => rpClose(), true);
}

function rpOpen(anchor, cur, onPick){
  rpBuild();
  RP.anchor = anchor;
  RP.onPick = onPick;
  RP.phase = 'a';
  RP.a = cur && cur.startDate ? new Date(cur.startDate + 'T00:00:00') : null;
  RP.b = cur && cur.to ? new Date(cur.to + 'T00:00:00') : null;
  if(RP.a && RP.b && RP.b < RP.a){ const t = RP.a; RP.a = RP.b; RP.b = t; }
  const base = RP.a || new Date();
  RP.y = base.getFullYear(); RP.m = base.getMonth();

  RP.el.classList.add('show');
  rpPaint();
  rpPlace();
}

function rpPlace(){
  const r = RP.anchor.getBoundingClientRect();
  const w = 296, h = RP.el.offsetHeight || 380;
  let left = Math.min(r.left, window.innerWidth - w - 12);
  left = Math.max(10, left);
  let top = r.bottom + 6;
  if(top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
  RP.el.style.left = left + 'px';
  RP.el.style.top = top + 'px';
}

function rpClose(){
  if(RP.el) RP.el.classList.remove('show');
  RP.anchor = null; RP.onPick = null;
}

function rpMonth(d){
  let m = RP.m + d, y = RP.y;
  if(m < 0){ m = 11; y--; }
  if(m > 11){ m = 0; y++; }
  RP.m = m; RP.y = y;
  rpPaint();
}

function rpPaint(){
  const t = new Date();
  const today = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const el = RP.el;

  el.querySelector('#rp-title').textContent = `${RP.y} 年 ${RP.m + 1} 月`;
  el.querySelector('#rp-ba b').textContent = RP.a ? dstr(RP.a).replace(/-/g, '/') : '—';
  el.querySelector('#rp-bb b').textContent = RP.b ? dstr(RP.b).replace(/-/g, '/') : '—';
  el.querySelector('#rp-ba').classList.toggle('on', RP.phase === 'a');
  el.querySelector('#rp-bb').classList.toggle('on', RP.phase !== 'a');
  el.querySelector('#rp-hint').textContent = RP.a && !RP.b
    ? '再点一下终点（点更早的日期也行，会自动换过来）'
    : '点两下：先点起点，再点终点';

  const first = new Date(RP.y, RP.m, 1);
  const offset = (first.getDay() + 6) % 7;          // 周一开头
  const days = new Date(RP.y, RP.m + 1, 0).getDate();
  const prevDays = new Date(RP.y, RP.m, 0).getDate();

  const A = RP.a ? RP.a.getTime() : null;
  const B = RP.b ? RP.b.getTime() : null;
  let html = '';
  for(let i = 0; i < offset; i++){
    const d = prevDays - offset + 1 + i;
    html += `<button class="mute" data-t="${new Date(RP.y, RP.m - 1, d).getTime()}">${d}</button>`;
  }
  for(let d = 1; d <= days; d++){
    const ts = new Date(RP.y, RP.m, d).getTime();
    let cls = '';
    if(A && ts === A) cls += ' a';
    if(B && ts === B) cls += ' b';
    if(A && B && ts > A && ts < B) cls += ' in';
    if(ts === today) cls += ' today';
    html += `<button class="${cls}" data-t="${ts}">${d}</button>`;
  }
  const tail = (7 - ((offset + days) % 7)) % 7;
  for(let i = 1; i <= tail; i++){
    html += `<button class="mute" data-t="${new Date(RP.y, RP.m + 1, i).getTime()}">${i}</button>`;
  }
  const grid = el.querySelector('#rp-grid');
  grid.innerHTML = html;
  grid.querySelectorAll('button').forEach(b => {
    b.onclick = e => { e.stopPropagation(); rpPick(new Date(Number(b.dataset.t))); };
  });
}

function rpPick(d){
  if(RP.phase === 'a' || (RP.a && RP.b)){
    RP.a = d; RP.b = null; RP.phase = 'b';
    rpPaint();
    return;
  }
  // 第二次点：早的当起点、晚的当终点（用户可能先点终点）
  let a = RP.a, b = d;
  if(b < a){ const t = a; a = b; b = t; }
  RP.a = a; RP.b = b; RP.phase = 'a';
  rpApply(dstr(a), dstr(b));
}

function rpApply(a, b){
  if(RP.onPick) RP.onPick({ startDate: a, to: b });
  rpClose();
}

function openRangePicker(anchor, cur, onPick){
  rpOpen(anchor, cur, onPick);
}

/** 区间预设：算出具体日期填进输入框，派任务时传具体日期，扩展不用懂规则 */
function rangeOf(kind){
  const now = new Date(), t = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const y = new Date(t); y.setDate(t.getDate() - 1);
  if (kind === 'yesterday') return [dstr(y), dstr(y)];
  if (kind === 'last7'){
    const s0 = new Date(y); s0.setDate(y.getDate() - 6);
    return [dstr(s0), dstr(y)];
  }
  if (kind === 'last30'){
    const s0 = new Date(y); s0.setDate(y.getDate() - 29);
    return [dstr(s0), dstr(y)];
  }
  if (kind === 'month') return [dstr(new Date(t.getFullYear(), t.getMonth(), 1)), dstr(t)];
  const first = new Date(t.getFullYear(), t.getMonth() - 1, 1);
  const last = new Date(t.getFullYear(), t.getMonth(), 0);
  return [dstr(first), dstr(last)];
}

function homeRange(){
  if(HOME_RANGE) return HOME_RANGE;
  const [a, b] = rangeOf('last30');
  return { startDate: a, to: b };
}

function paintHomeRange(){
  const el = $('#home-range-txt');
  if(el) el.textContent = fmtRange(homeRange());
}

function setHomeRange(kind){
  const [a, b] = rangeOf(kind);
  HOME_RANGE = { startDate: a, to: b };
  paintHomeRange();
}

let LIVE_DOWN = false;    // 桥连不上（被关了 / 正在重启）
let LIVE_BUSY = false;    // 上一次轮询还没回来

async function loadLive(){
  // 上一次没回来就跳过这一轮。
  // 为什么：算账时桥很忙，3 秒一次的轮询如果每次都发新请求，
  // 会越堆越多（浏览器同源并发有上限，堆满之后连静态资源都加载不了，
  // 界面就像卡死了）。跳过比排队更安全——反正下一轮还有。
  if(LIVE_BUSY) return;
  LIVE_BUSY = true;
  try{
    const r = await fetch('/api/live');
    if(!r.ok) throw new Error('HTTP ' + r.status);
    LIVE = await r.json();
    LIVE_DOWN = false;
  }catch(e){ LIVE = null; LIVE_DOWN = true; }
  finally{ LIVE_BUSY = false; }
  renderCards();
}

function startLive(){
  stopLive();
  loadLive();
  LIVE_T = setInterval(() => { if(!document.hidden) loadLive(); }, 3000);
}
function stopLive(){ if(LIVE_T){ clearInterval(LIVE_T); LIVE_T = null; } }

function updatePickCount(){
  const el = $('#pick-count');
  if(el) el.textContent = `已选 ${PICKED.size} 家`;
  const all = $('#pick-all');
  if(all){
    const total = LIVE && LIVE.stores ? Object.keys(LIVE.stores).length : 0;
    all.checked = total > 0 && PICKED.size === total;
    all.indeterminate = PICKED.size > 0 && PICKED.size < total;
  }
}

function stepIcon(state, isRun){
  if(isRun) return '<span style="color:#3b82d6">◐</span>';
  if(state === 'done') return '<span style="color:#12a382">✓</span>';
  if(state === 'fail') return '<span style="color:#b4462a">✕</span>';
  return '<span style="color:#c9c7bd">○</span>';
}

function stepsHtml(prog, labels){
  const steps = (prog && prog.steps) || {};
  const cur = (prog && prog.current) || '';
  return STEP_KEYS.map(k => {
    const one = steps[k] || {};
    const isRun = cur === k;
    const cls = isRun ? 'run' : (one.state || '');
    let detail = one.detail || one.error || '';
    if(isRun && !detail) detail = '正在跑…';
    let tm = '';
    if(one.elapsed) tm = sec2text(one.elapsed);
    else if(isRun && one.at) tm = '已等 ' + sec2text(Date.now()/1000 - one.at);
    return `<div class="step ${cls}">
      <span class="ic">${stepIcon(one.state, isRun)}</span>
      <span class="nm">${esc(labels[k] || k)}</span>
      <span class="dt">${esc(detail)}</span>
      <span class="tm">${esc(tm)}</span></div>`;
  }).join('');
}

function lastLine(s){
  const p = s.progress || {};
  if(p.finished) return (p.ok === false ? '上次失败' : '上次成功') + ' · ' +
    new Date(p.finished * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
  if(p.current) return '正在跑…';
  // 「还没收到过心跳」和「收到过、现在断了」要分开说：
  // 前者多半是程序刚启动/扩展刚刷新，等一分钟自己就好；后者才要人去开浏览器。
  if(!s.online) return s.heard ? '浏览器没开' : '等扩展上报（约 1 分钟）';
  return '还没跑过';
}

function cardHtml(id, s, labels){
  const prog = s.progress || {};
  const cur = prog.current || '';
  const anyFail = Object.values(prog.steps || {}).some(x => x.state === 'fail');
  // 扩展版本和磁盘上的对不上：多半是程序升级了、扩展页没点「刷新」。
  // 这时候界面是新的、跑的代码是旧的，最容易出现「功能看着有、就是不生效」。
  const appVer = ($('#app-ver') || {}).textContent || '';
  const stale = s.ver && appVer && s.ver !== appVer;
  const dot = cur ? 'run' : (s.online ? (anyFail ? 'warn' : (stale ? 'warn' : 'on')) : 'off');
  const stateTxt = s.stopping ? '正在停止…' : (cur ? '运行中'
    : (s.online ? (anyFail ? '有失败' : (stale ? ('扩展 ' + s.ver + ' 待刷新') : '待命'))
                : (s.heard ? '浏览器没开' : '等扩展上报')));
  const realName = s.shop_name && s.shop_name !== s.store_name ? s.shop_name : '';
  // 今天已经取到过数据就别让人白等：卡片上直接写「今日已取 09:12」，
  // 四个都齐 = 这次点运行会秒过（直接复用），缺的才会上平台导。
  const cap = s.captured || {};
  const capTxt = cap.count ? ('今日已取 ' + (cap.last || '') + (cap.count >= 4 ? ' · 全齐' : '')) : '';
  const meta = [realName || ('ID ' + String(id).slice(-8)), s.region, capTxt].filter(Boolean).join(' · ');
  // 刚点过「重算」的店，卡片底部直接显示新数字——不用再翻「报表」页确认改费率有没有生效
  const rc = CARD_RECALC[id];
  const lastTxt = rc ? ('重算于 ' + rc.hhmm + ' · 净利 ' + fmt2(rc.net)) : lastLine(s);
  const picked = PICKED.has(id);
  return `<div class="sc ${picked ? 'picked' : ''}" data-card="${esc(id)}">
    <div class="sc-head">
      <input type="checkbox" data-pick="${esc(id)}" ${picked ? 'checked' : ''}>
      <div class="sc-title">
        <div class="n">${esc(s.store_name || id)}</div>
        <div class="m">${esc(meta)}</div>
      </div>
      <span class="sc-state"><i class="dot ${dot}"></i>${esc(stateTxt)}</span>
    </div>
    <div class="sc-body">${stepsHtml(prog, labels)}</div>
    <div class="sc-foot">
      <div class="last">${esc(lastTxt)}</div>
      <div class="sc-date">
        <button class="rbtn" data-range="${esc(id)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="4.6" width="18" height="16.4" rx="3"/><path d="M3 10h18M8 2.6v4M16 2.6v4"/>
          </svg><span>${esc(fmtRange(CARD_RANGE[id]))}</span>
        </button>
      </div>
      <button class="ghost" data-recalc="${esc(id)}" ${cur ? 'disabled' : ''}
              title="用已经在本机的数据重算，不重新取数。改了佣金费率、成本表之后点这个">重算</button>
      ${cur || s.stopping
        ? `<button class="stop" data-stop="${esc(id)}" ${s.stopping ? 'disabled' : ''}>
             ${s.stopping ? '正在停…' : '停止'}</button>`
        : `<button data-run="${esc(id)}" class="primary">运行</button>`}
    </div>
  </div>`;
}

let LIVE_SIG = '';      // 上一次画卡片时的数据指纹
function renderCards(){
  const grid = $('#store-grid');
  if(!grid) return;
  const j = LIVE;
  if(!j || !j.stores || !Object.keys(j.stores).length){
    LIVE_SIG = '';
    // 桥没连上时别说「还没识别到店铺」——那会让人以为要重新登录店铺，
    // 其实只是取数服务被关了（调试页那个按钮），点一下就能起来。
    grid.innerHTML = LIVE_DOWN
      ? '<div class="sc-empty"><b style="display:block;margin-bottom:6px;font-size:14px">取数服务没连上</b>'
        + '它可能被关掉了，或者正在重启。<br>取数、核算、计划任务都靠它。<br><br>'
        + '<button class="ghost" style="width:auto;padding:7px 16px;margin:0" '
        + 'onclick="go(\'debug\')">去调试页启动 →</button></div>'
      : '<div class="sc-empty">还没识别到店铺。<br>在浏览器里打开店铺后台，扩展会自动认出是哪家店。</div>';
    updatePickCount();
    return;
  }
  const labels = j.labels || {};
  const ids = (j.order && j.order.length) ? j.order : Object.keys(j.stores);
  // 店铺被移除后，勾选集合里可能还留着它的 id：「已选 3 家」但只看得见 2 家
  for(const pid of [...PICKED]) if(!j.stores[pid]) PICKED.delete(pid);
  // 每 3 秒把整面卡片墙 innerHTML 重建一遍，数据和上一轮一样时纯属浪费：
  // 会丢鼠标悬停状态、白耗 CPU（大店多时更明显）。数据没变就不重画。
  const sig = JSON.stringify(j.stores) + '||' + JSON.stringify(j.queue || []);
  if(sig === LIVE_SIG && grid.dataset.ready === '1'){
    updatePickCount();
    return;
  }
  LIVE_SIG = sig;
  grid.dataset.ready = '1';
  grid.innerHTML = ids.map(id => cardHtml(id, j.stores[id] || {}, labels)).join('');

  // 「停止全部」只在有店在跑时出现，顺便报数量
  const stopAllBtn = $('#btn-stop-all');
  if(stopAllBtn){
    const busy = Object.values(j.stores || {}).filter(s => (s.progress || {}).current).length;
    stopAllBtn.hidden = !busy;
    stopAllBtn.textContent = busy ? ('停止全部（' + busy + '）') : '停止全部';
  }

  grid.querySelectorAll('[data-pick]').forEach(cb => {
    cb.onchange = () => {
      const id = cb.dataset.pick;
      if(cb.checked) PICKED.add(id); else PICKED.delete(id);
      const card = cb.closest('.sc');
      if(card) card.classList.toggle('picked', cb.checked);
      updatePickCount();
    };
  });
  grid.querySelectorAll('[data-range]').forEach(b => {
    b.onclick = () => {
      const id = b.dataset.range;
      openRangePicker(b, CARD_RANGE[id] || homeRange(), r => {
        CARD_RANGE[id] = r;
        const sp = b.querySelector('span');
        if(sp) sp.textContent = fmtRange(r);
      });
    };
  });
  grid.querySelectorAll('[data-stop]').forEach(b => {
    b.onclick = async () => {
      const id = b.dataset.stop;
      b.disabled = true;
      b.textContent = '正在停…';
      const r = await fetch('/api/run/stop', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({shop_id: id, store_name: ((LIVE.stores || {})[id] || {}).store_name || ''}),
      }).then(x => x.json()).catch(() => null);
      toast(r && r.ok ? '已发送停止，最多 5 秒收手' : '停止请求没发出去');
      setTimeout(loadLive, 800);
    };
  });
  grid.querySelectorAll('[data-run]').forEach(b => {
    b.onclick = () => {
      const id = b.dataset.run;
      // 派了新任务，之前那次「重算于 …」的结果就过期了，别再挂在卡片上
      delete CARD_RECALC[id];
      if(HOME_MODE === 'single'){
        runStores([id], b, cardRange(id) || homeRange());
      }else{
        runStores([id], b, homeRange());
      }
    };
  });
  grid.querySelectorAll('[data-recalc]').forEach(b => {
    b.onclick = async () => {
      const id = b.dataset.recalc;
      const rg = (HOME_MODE === 'single' ? (cardRange(id) || homeRange()) : homeRange());
      const old = b.textContent;
      b.disabled = true;
      b.textContent = '重算中…';
      const res = await recalcMany([id], rg, null);
      b.disabled = false;
      b.textContent = old;
      const one = res.find(x => x.ok);
      if(one){
        CARD_RECALC[id] = {net: one.net_local,
                           hhmm: new Date().toTimeString().slice(0, 5)};
        renderCards();
        toast('已重算：净利 ' + fmt2(one.net_local)
              + '（' + (Number(one.net_margin) * 100).toFixed(2) + '%）');
      }else{
        toast(String((res[0] || {}).error || '重算失败').slice(0, 70));
      }
      loadStatus(); loadTrend();
    };
  });
  if(HOME_MODE === 'single'){
    const [a, b] = rangeOf('last_month');
    grid.querySelectorAll('[data-range]').forEach(el => {
      const id = el.dataset.range;
      if(!CARD_RANGE[id]) CARD_RANGE[id] = { startDate: a, to: b };
    });
    // 刚补上的默认值要重画一次，否则按钮上还是「选择区间」
    grid.querySelectorAll('[data-range]').forEach(el => {
      const sp = el.querySelector('span');
      if(sp) sp.textContent = fmtRange(CARD_RANGE[el.dataset.range]);
    });
  }
  updatePickCount();
}

let HOME_INIT = false;

function initHome(){
  if(!HOME_INIT){
    HOME_INIT = true;
    setHomeRange('last30');
    const hr = $('#home-range');
    if(hr) hr.onclick = () => openRangePicker(hr, HOME_RANGE, r => {
      HOME_RANGE = r;
      paintHomeRange();
      document.querySelectorAll('#home-presets button').forEach(x => x.classList.remove('on'));
    });
    document.querySelectorAll('#home-presets button').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('#home-presets button').forEach(x => x.classList.remove('on'));
        btn.classList.add('on');
        setHomeRange(btn.dataset.k);
      };
    });
    document.querySelectorAll('#home-mode button').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('#home-mode button').forEach(x => x.classList.remove('on'));
        btn.classList.add('on');
        HOME_MODE = btn.dataset.mode;
        document.body.classList.toggle('single', HOME_MODE === 'single');
        renderCards();
      };
    });
    const all = $('#pick-all');
    if(all) all.onchange = (e) => {
      const ids = (LIVE && LIVE.stores) ? Object.keys(LIVE.stores) : [];
      PICKED = e.target.checked ? new Set(ids) : new Set();
      renderCards();
    };
  const runBtn = $('#btn-run-picked');
    if(runBtn) runBtn.onclick = (e) => runStores([...PICKED], e.currentTarget, homeRange());
    const stopAllBtn = $('#btn-stop-all');
    if(stopAllBtn) stopAllBtn.onclick = async () => {
      stopAllBtn.disabled = true;
      const r = await fetch('/api/run/stop', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({all: true}),
      }).then(x => x.json()).catch(() => null);
      stopAllBtn.disabled = false;
      toast(r && r.ok ? '已发送停止，正在收手的店会在几步内退出' : '停止请求没发出去');
      setTimeout(loadLive, 800);
    };
    // 工具条上的「重算」：勾了哪几家就重算哪几家，用的是已经在本机的数据
    const rcBtn = $('#btn-recalc-picked');
    if(rcBtn) rcBtn.onclick = (e) => recalcPicked(e.currentTarget);
  }
  loadStatus();
  loadTrend();
  startLive();
}

/** 某张卡片上自己选的日期（单店模式用）。
 *  存在内存里而不是从 DOM 读：卡片墙每 3 秒重绘，
 *  从 DOM 读会在重绘的那一瞬间读到空值，派任务就用了别的区间。 */
function cardRange(id){
  return CARD_RANGE[id] || null;
}

async function runStores(ids, btn, range){
  if(!ids || !ids.length){ toast('先勾选要跑的店铺'); return; }
  // 勾了「强制重新取数」就不复用今天已取的表，老老实实重导一遍。
  const force = !!($('#force-fetch') && $('#force-fetch').checked);
  // 单店模式：各区间的店可以不一样，只能一家一家派
  if(HOME_MODE === 'single' && ids.length >= 1 && !range){
    if(btn) btn.disabled = true;
    let n = 0;
    for(const id of ids){
      const rg = cardRange(id) || homeRange();
      try{
        const r = await (await fetch('/api/run-stores', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ stores: [id], range: rg, force })
        })).json();
        if(r && r.ok){ n++; rememberRun(rg); }
      }catch(e){ /* 单家失败不影响其它 */ }
    }
    if(btn) btn.disabled = false;
    toast(n ? `已按各店自己的区间派发 ${n} 家` : '派发失败');
    setTimeout(loadLive, 800);
    return;
  }
  if(btn) btn.disabled = true;
  try{
    const rg = range || homeRange();
    const r = await (await fetch('/api/run-stores', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ stores: ids, range: rg, force })
    })).json();
    if(r.ok){ toast(r.note || ('已派给 ' + (r.count || ids.length) + ' 家店')); rememberRun(rg); }
    else toast(r.error || '派发失败');
  }catch(e){
    toast(LIVE_DOWN ? '取数服务没连上（可能被关了），去「调试」页点「启动取数服务」'
                    : ('派发失败：' + e.message));
  }
  if(btn) btn.disabled = false;
  setTimeout(loadLive, 800);
}

/* ---------------- 重新计算（不重新取数） ----------------
   取数贵：订单要等平台生成（大店五六分钟），还容易撞风控；
   核算便宜：数据都在本机，几秒到几十秒。
   所以「改了佣金费率 / 成本表 / 汇率」这类事，只要重算，不用重取。
   和「运行」的区别就这一句：运行 = 取数 + 算，重算 = 只算。 */
let LAST_RUN = null;
try{ LAST_RUN = JSON.parse(localStorage.getItem('jl_last_run') || 'null'); }catch(e){ LAST_RUN = null; }

function rememberRun(rg){
  if(!rg || !rg.startDate) return;
  LAST_RUN = {from: rg.startDate, to: rg.to || '', at: Date.now()};
  try{ localStorage.setItem('jl_last_run', JSON.stringify(LAST_RUN)); }catch(e){}
}

/** 重算用哪个区间：优先「上次跑的那个」，没有就本月至今。 */
function recalcRange(){
  if(LAST_RUN && LAST_RUN.from) return {startDate: LAST_RUN.from, to: LAST_RUN.to || ''};
  const [a, b] = rangeOf('month');
  return {startDate: a, to: b};
}

function showRecalcBar(why){
  const bar = $('#recalc-bar'); if(!bar) return;
  const rg = recalcRange();
  const n = PICKED.size || (storesCache || []).length;
  $('#recalc-title').textContent = why || '参数改了，现有报表还是旧数字';
  $('#recalc-sub').textContent = '按 ' + rg.startDate + ' ~' + (rg.to || ' 今天')
    + ' 重算' + (n ? (' ' + n + ' 家') : '') + '：用已经在本机的数据，不重新取数';
  bar.classList.add('show');
  document.body.classList.add('has-recalc');
}

function hideRecalcBar(){
  const bar = $('#recalc-bar'); if(bar) bar.classList.remove('show');
  document.body.classList.remove('has-recalc');
}

/** 逐家重算。返回每家 /run 的结果（含 net_local / report_name）。 */
async function recalcMany(ids, rg, onProgress){
  const out = [];
  for(let i = 0; i < ids.length; i++){
    if(onProgress) onProgress(i, ids.length);
    try{
      const r = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({store: ids[i],
                              from: (rg && rg.startDate) || null,
                              to: (rg && rg.to) || null,
                              // 重算覆盖同一份报表，别再堆 (2)(3)——一天改几次费率
                              // 就能堆出十几份，报表页直接没法看。
                              replace: true})});
      out.push({ ...(await r.json()), _id: ids[i] });
    }catch(e){ out.push({ok:false, store: ids[i], _id: ids[i], error: e.message}); }
  }
  return out;
}

/** 重算完成后统一收尾：新数字写回各店卡片、返回成功/失败分组。 */
function applyRecalc(results){
  const ok = results.filter(x => x.ok), bad = results.filter(x => !x.ok);
  const hhmm = new Date().toTimeString().slice(0, 5);
  ok.forEach(x => { if(x._id) CARD_RECALC[x._id] = {net: x.net_local, hhmm}; });
  if(ok.length) renderCards();
  return {ok, bad};
}

/** 驾驶舱工具条上的「重算」：勾了哪几家就重算哪几家，不重新取数。 */
async function recalcPicked(btn){
  const ids = [...PICKED];
  if(!ids.length){ toast('先勾选要重算的店铺'); return; }
  const old = btn.textContent;
  btn.disabled = true;
  const all = [];
  for(let i = 0; i < ids.length; i++){
    btn.textContent = '重算 ' + (i + 1) + '/' + ids.length;
    const rg = HOME_MODE === 'single' ? (cardRange(ids[i]) || homeRange()) : homeRange();
    all.push(...await recalcMany([ids[i]], rg, null));
  }
  btn.disabled = false;
  btn.textContent = old;
  const {ok, bad} = applyRecalc(all);
  if(ok.length && !bad.length){
    toast(ok.length === 1
      ? ('已重算：净利 ' + fmt2(ok[0].net_local)
         + '（' + (Number(ok[0].net_margin) * 100).toFixed(2) + '%）')
      : ('已重算 ' + ok.length + ' 家'));
  }else if(bad.length){
    toast('重算失败 ' + bad.length + ' 家：' + String(bad[0].error || '未知原因').slice(0, 60));
  }
  // 单店模式下各店区间不同，没法记成「上次的区间」，就别记
  if(ok.length && !bad.length && HOME_MODE !== 'single') rememberRun(homeRange());
  loadStatus(); loadTrend();
}

/** 一句话说清重算结果，成功失败都给人能看懂的。 */
function recalcNote(res){
  const ok = res.filter(x => x.ok), bad = res.filter(x => !x.ok);
  let t = '';
  if(ok.length === 1){
    const o = ok[0];
    t = '已重算：净利 <b>' + fmt2(o.net_local) + '</b>　净利率 '
      + (Number(o.net_margin) * 100).toFixed(2) + '%　' + esc(o.report_name || '');
  }else if(ok.length){
    t = '已重算 ' + ok.length + ' 家：' + ok.map(o =>
      esc(o.store_name || '') + ' ' + fmt2(o.net_local)).join('　');
  }
  if(bad.length){
    t += (t ? '<br>' : '') + '<b>失败 ' + bad.length + ' 家</b>　'
      + esc(String(bad[0].error || '未知原因').slice(0, 120));
  }
  return {html: t, ok: ok.length, bad: bad.length};
}

$('#recalc-x') && ($('#recalc-x').onclick = hideRecalcBar);
$('#recalc-go') && ($('#recalc-go').onclick = async () => {
  const btn = $('#recalc-go');
  const rg = recalcRange();
  // 驾驶舱上勾了哪几家就重算哪几家；一家没勾 = 全都算一遍
  let ids = [...PICKED].filter(Boolean);
  if(!ids.length) ids = (storesCache || []).map(s => s.id).filter(Boolean);
  if(!ids.length){ toast('还没识别到店铺，先让扩展认一次店'); return; }
  btn.disabled = true;
  const res = await recalcMany(ids, rg, (i, n) => {
    $('#recalc-sub').textContent = '正在重算（' + (i + 1) + '/' + n + '）…';
  });
  btn.disabled = false;
  const {ok, bad} = applyRecalc(res);
  const s = recalcNote(res);
  toast((s.ok ? ('重算完成 ' + s.ok + ' 家，报表已更新') : '重算失败')
        + (s.bad ? ('，' + s.bad + ' 家没算成') : ''));
  if(ok.length && !bad.length){ hideRecalcBar(); rememberRun(rg); }
  loadStatus(); loadTrend();
});

function go(view){
  // 老链接（#/run 之类已删掉的页面）不能变成一片空白：找不到就回驾驶舱
  if(!document.getElementById('view-' + view)) view = 'overview';
  document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'view-' + view);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  if(view === 'reports') loadReports();
  if(view === 'params'){ loadParams(); presetScopes('ps'); loadPresets('ps'); }
  if(view === 'rates'){ loadRates(); presetScopes('pr'); loadPresets('pr'); }
  if(view === 'costs') loadCosts();
  if(view === 'settings') loadSettings();
  if(view === 'plan') loadPlan();
  if(view === 'debug') loadDebug();
  if(view === 'stores') loadStores();
  if(view === 'import'){ loadImportOptions(); loadImportFiles(); }
  if(view === 'manual'){ loadManual(); }
  if(view === 'overview') initHome(); else stopLive();
  if(location.hash !== '#/' + view) location.hash = '#/' + view;
}
document.querySelectorAll('#nav a').forEach(a =>
  a.addEventListener('click', e => { e.preventDefault(); go(a.dataset.view); }));
window.addEventListener('hashchange', () =>
  go((location.hash || '').replace('#/', '') || 'overview'));

function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2600);
}
function note(el, text, kind){ el.className = 'note show ' + (kind || 'ok'); el.innerHTML = text; }
const fmt2 = v => (v === null || v === undefined || v === '') ? '—'
  : Number(v).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
const pad = n => String(n).padStart(2, '0');
const iso = d => d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

async function loadStatus(){
  try{
    const j = await (await fetch('/api/status')).json();
    $('#stats').innerHTML = `
      <div class="stat"><div class="k"><span class="dot on"></span>取数服务</div>
        <div class="v small">运行中 · ${j.uptime}</div><div class="s">本地端口 8765</div></div>
      <div class="stat"><div class="k"><span class="dot ${j.ext_last?'on':'off'}"></span>浏览器扩展</div>
        <div class="v small">${j.ext_last ? j.ext_ago : '还没收到'}</div>
        <div class="s">${j.ext_last ? ('最近一次 ' + j.ext_last.slice(11)) : '开一次扩展面板就会上报'}</div></div>
      <div class="stat"><div class="k">店铺</div>
        <div class="v">${j.store_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">家</span></div>
        <div class="s">待核算 ${j.inbox_count} 个文件</div></div>
      <div class="stat"><div class="k">已生成报表</div>
        <div class="v">${j.report_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">份</span></div>
        <div class="s">在「报表」页可查看</div></div>`;
    $('#foot-stores').textContent = j.store_count + ' 家';
    storesCache = j.stores || [];
  }catch(e){}
}

async function loadTrend(){
  try{
    const j = await (await fetch('/api/trends')).json();
    const items = (j.items || []).slice(-24);
    if(!items.length){
      $('#trend-hint').textContent = '';
      $('#trend').innerHTML = '<div class="empty"><b>还没有核算记录</b>跑一次核算，这里就会出现走势</div>';
      return;
    }
    const vals = items.map(x => Number(x.net) || 0);
    const max = Math.max(...vals, 0), min = Math.min(...vals, 0), span = (max - min) || 1;
    $('#trend-hint').textContent = '最近 ' + items.length + ' 次';
    $('#trend').innerHTML = '<div class="bars">' + items.map(x => {
      const v = Number(x.net) || 0;
      const h = Math.max(3, Math.round(Math.abs(v) / span * 96));
      const tip = esc(x.at) + '　' + esc(x.store) + '　净利 ' + fmt2(v);
      return `<div class="b${v < 0 ? ' neg' : ''}" style="height:${h}px" title="${tip}"></div>`;
    }).join('') + '</div><div class="legend"><span>最高 ' + fmt2(max) + '</span><span>最低 '
      + fmt2(min) + '</span><span>最新 ' + fmt2(vals[vals.length-1]) + '</span></div>';
  }catch(e){}
}

// 单个文件上限。base64 本身要占 1.34 倍，再加上中间那层字符串，
// 传一个 300MB 的订单表能让标签页直接崩掉（而且崩得莫名其妙）。
// 订单表正常几十 MB，200MB 足够宽裕。
const MAX_UPLOAD = 200 * 1024 * 1024;

async function fileToB64(f){
  if(f.size > MAX_UPLOAD){
    throw new Error(`文件有 ${(f.size/1024/1024).toFixed(0)} MB，超过 `
      + `${MAX_UPLOAD/1024/1024} MB 上限。请在平台上把时间区间改小一点，分几次导出`);
  }
  // 用 FileReader 让浏览器原生转 base64。
  // 以前是逐块 String.fromCharCode 拼一个 53MB 的字符串（40MB 文件），
  // 主线程会卡好几秒，界面看着像死了；原生读只要几百毫秒。
  return await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result || '');
      const i = s.indexOf(',');
      resolve(i >= 0 ? s.slice(i + 1) : s);   // 去掉 "data:...;base64," 前缀
    };
    r.onerror = () => reject(r.error || new Error('读不了这个文件'));
    r.readAsDataURL(f);
  });
}

async function importFiles(files, kindHint){
  const n = $('#imp2-note'), box = $('#imp2-result');
  if(!files || !files.length) return;
  note(n, '导入中…（' + files.length + ' 个文件）', 'ok');
  if(box) box.innerHTML = '';
  const lines = [];
  for(const f of files){
    try{
      const b64 = await fileToB64(f);
      const r = await fetch('/api/data-import', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          name: f.name, b64,
          shop_id: ($('#imp2-store') ? $('#imp2-store').value : '') || '',
          country: ($('#imp2-country') ? $('#imp2-country').value : '') || '',
          kind: kindHint || '',
        }),
      });
      const j = await r.json();
      const warn = j.warning ? ` <span style="color:#B45309">⚠ ${esc(j.warning)}</span>` : '';
      lines.push(j.ok
        ? `✓ ${esc(f.name)} → <b>${esc(j.label)}</b>`
          + `<span class="muted">（按${esc(j.country || '默认')}的列标题识别）</span>${warn}`
        : `✕ ${esc(f.name)} → <span style="color:#C0392B">失败：${esc(j.error || '')}</span>`);
    }catch(err){
      lines.push(`✕ ${esc(f.name)} → <span style="color:#C0392B">失败：${esc(err.message || String(err))}</span>`);
    }
  }
  note(n, lines.join('<br>'), 'ok');
  // 导入完立刻刷新：本页清单、核算页文件、快照、总览数字
  loadImportFiles(); loadStatus();
}

if($('#imp2-file')) $('#imp2-file').onchange = (e) => {
  importFiles(Array.from(e.target.files || []));
  e.target.value = "";            // 清空，同一个文件才能再选一次
};

/* 拖放：整块区域都能接文件，不用先点按钮再翻路径 */
(function bindImportDrop(){
  const dz = $('#imp2-drop');
  if(!dz) return;
  const stop = e => { e.preventDefault(); e.stopPropagation(); };
  ['dragenter', 'dragover'].forEach(ev =>
    dz.addEventListener(ev, e => { stop(e); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev =>
    dz.addEventListener(ev, e => { stop(e); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => {
    const fs = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if(fs.length) importFiles(fs);
  });
  dz.addEventListener('click', e => {
    if(e.target.closest('label')) return;   // 点「选择文件」让它自己弹
    const inp = $('#imp2-file');
    if(inp) inp.click();
  });
})();

/** 单独导入「商品映射表」（SKU → 商品ID）：强制按映射表存，不靠表头猜 */
if($('#btn-imp2-map')) $('#btn-imp2-map').onclick = () => {
  const inp = $('#imp2-map-file');
  if(inp) inp.click();
};
if($('#imp2-map-file')) $('#imp2-map-file').onchange = (e) => {
  const fs = Array.from(e.target.files || []);
  if(fs.length) importFiles(fs, 'mapping');
  e.target.value = '';
};

/** 单独导入「链接资料」（款式编码）：全局生效，不分店铺 */
if($('#btn-imp2-link')) $('#btn-imp2-link').onclick = () => {
  const inp = $('#imp2-link-file');
  if(inp) inp.click();
};
if($('#imp2-link-file')) $('#imp2-link-file').onchange = async (e) => {
  const f = (e.target.files || [])[0];
  if(!f) return;
  const n = $('#imp2-note');
  note(n, '正在读取链接资料…', 'ok');
  try{
    const b64 = await fileToB64(f);
    const j = await (await fetch('/api/link-info', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: f.name, b64}),
    })).json();
    note(n, j.ok
      ? `✓ 链接资料已导入：<b>${j.sku_count}</b> 个 SKU / <b>${j.link_count}</b> 条链接`
        + (j.multi_count ? `<span class="muted">（其中 ${j.multi_count} 条链接含多个款式编码）</span>` : '')
      : '导入失败：' + esc(j.error || '未知原因'), j.ok ? 'ok' : 'err');
  }catch(err){ note(n, '出错：' + esc(err.message), 'err'); }
  e.target.value = '';
};

/** 「已经传了哪些」：按店铺列文件，没选店铺的单独标出来。
 *  可以勾选删除——文件只会挪进「数据/回收站」，随时能找回来。 */
const IMP_SEL = new Set();

const KIND_CN = {orders:'订单', ads:'广告', affiliate:'联盟', mapping:'映射',
                 other:'其他', 订单表:'订单', 广告表:'广告', 联盟佣金表:'联盟',
                 商品映射表:'映射', 商品SKU表:'SKU', 链接资料:'链接'};
function kindLabel(x){ return KIND_CN[x] || x; }   // 认不出就直接显示目录名

/** 填导入页的两个下拉：店铺、国家。
 *  直接进「导入数据」页时缓存可能是空的，自己补一次。 */
async function loadImportOptions(){
  try{
    if(!storesCache.length){
      try{ storesCache = (await (await fetch('/api/stores')).json()).stores || []; }catch(e){}
    }
    const sel = $('#imp2-store');
    if(sel){
      const keep = sel.value;
      sel.innerHTML = '<option value="">未识别店铺（公共目录）</option>';
      storesCache.forEach(s => {
        const o = document.createElement('option');
        o.value = s.id;
        o.textContent = (s.name || s.id) + (s.country ? '（' + s.country + '）' : '');
        sel.append(o);
      });
      if(keep) sel.value = keep;
    }
    const r = await (await fetch('/api/rates')).json();
    const cs = $('#imp2-country');
    if(cs){
      const keep = cs.value;
      cs.innerHTML = '<option value="">跟随店铺</option>';
      Object.keys(r.countries || {}).forEach(c => {
        const o = document.createElement('option');
        o.value = c; o.textContent = c;
        cs.append(o);
      });
      if(keep) cs.value = keep;
    }
  }catch(e){ /* 拉不到就留空，用户仍可以传，只是不能选店 */ }
}

async function loadImportFiles(){
  const box = $('#imp2-files'), hint = $('#imp2-hint');
  if(!box) return;
  try{
    const j = await (await fetch('/api/data-files')).json();
    const groups = (j.groups || []).filter(g => (g.items || []).length);
    const unknown = j.unknown || [], legacy = j.legacy || [];
    let n = legacy.length + unknown.length;
    groups.forEach(g => n += g.items.length);
    let totalKb = 0;
    groups.forEach(g => g.items.forEach(f => totalKb += Number(f.size_kb) || 0));
    unknown.forEach(f => totalKb += Number(f.size_kb) || 0);
    legacy.forEach(f => totalKb += Number(f.size_kb) || 0);
    if(hint){
      hint.textContent = n
        ? `${n} 个文件 · 共 ${(totalKb / 1024).toFixed(totalKb > 10240 ? 0 : 1)} MB`
        : '';
    }
    if(!n){
      box.innerHTML = '<div class="empty"><b>还没传过文件</b>上面选好店铺，把后台导出的表拖进来就行</div>';
      return;
    }

    // 勾选状态按 rel 存；列表重画后还在（rel 是稳定的相对路径）
    const alive = new Set();
    const collect = arr => arr.forEach(f => { if(f.rel) alive.add(f.rel); });
    groups.forEach(g => collect(g.items));
    collect(unknown); collect(legacy);
    [...IMP_SEL].forEach(x => { if(!alive.has(x)) IMP_SEL.delete(x); });

    const bar = `<div class="imp-bar">
      <label class="chk"><input type="checkbox" id="imp-all"> 全选</label>
      <span class="muted" id="imp-sel">已选 ${IMP_SEL.size} 个</span>
      <span style="flex:1"></span>
      <button class="danger" id="imp-del" ${IMP_SEL.size ? '' : 'disabled'}>删除选中</button>
    </div>`;

    const row = f => `<tr>
      <td style="width:34px"><input type="checkbox" data-rel="${esc(f.rel || '')}"
           ${IMP_SEL.has(f.rel) ? 'checked' : ''} ${f.rel ? '' : 'disabled'}></td>
      <td>${esc(f.name)}</td>
      <td class="muted" style="width:90px;white-space:nowrap">${f.size_kb} KB</td>
      <td class="muted" style="width:110px;white-space:nowrap">${f.at}</td>
      <td style="width:64px"><button class="mini" data-rm="${esc(f.rel || '')}">删除</button></td>
    </tr>`;

    let html = bar;
    groups.forEach(g => {
      const k = g.kinds || {};
      const bits = Object.keys(k).filter(x => x !== 'other')
        .map(x => kindLabel(x) + ' ' + k[x]).join(' · ');
      html += `<div class="imp-grp">${esc(g.name)}
        <span class="tag gray">${esc(g.country || '')}</span>
        ${bits ? '<span class="tag">' + esc(bits) + '</span>' : ''}</div>`;
      html += '<table><tbody>' + g.items.map(row).join('') + '</tbody></table>';
    });
    if(unknown.length){
      html += `<div class="imp-grp warn">⚠ 没选店铺的数据（${unknown.length} 个）——不会参与核算</div>`;
      html += '<table><tbody>' + unknown.map(row).join('') + '</tbody></table>';
    }
    if(legacy.length){
      html += `<div class="imp-grp">公共目录（老的导入方式）</div>`;
      html += '<table><tbody>' + legacy.map(f => `<tr>
        <td style="width:34px"></td><td>${esc(f.name)}</td>
        <td class="muted" style="width:90px">${f.size_kb} KB</td>
        <td class="muted" style="width:110px">${f.at}</td><td></td></tr>`).join('') + '</tbody></table>';
    }
    box.innerHTML = html;

    const refreshBar = () => {
      const el = $('#imp-sel'); if(el) el.textContent = '已选 ' + IMP_SEL.size + ' 个';
      const btn = $('#imp-del'); if(btn) btn.disabled = !IMP_SEL.size;
      const all = $('#imp-all');
      const boxes = [...box.querySelectorAll('input[data-rel]:not([disabled])')];
      if(all){
        all.checked = boxes.length > 0 && boxes.every(b => b.checked);
        all.indeterminate = !all.checked && boxes.some(b => b.checked);
      }
    };

    box.querySelectorAll('input[data-rel]').forEach(cb => {
      cb.onchange = () => {
        const r = cb.dataset.rel;
        if(!r) return;
        if(cb.checked) IMP_SEL.add(r); else IMP_SEL.delete(r);
        refreshBar();
      };
    });
    const all = $('#imp-all');
    if(all) all.onchange = () => {
      box.querySelectorAll('input[data-rel]:not([disabled])').forEach(cb => {
        cb.checked = all.checked;
        if(all.checked) IMP_SEL.add(cb.dataset.rel); else IMP_SEL.delete(cb.dataset.rel);
      });
      refreshBar();
    };
    box.querySelectorAll('[data-rm]').forEach(b => {
      b.onclick = () => {
        const r = b.dataset.rm;
        if(!r) return;
        impDelete([r]);
      };
    });
    const del = $('#imp-del');
    if(del) del.onclick = () => impDelete([...IMP_SEL]);
    refreshBar();
  }catch(err){
    box.innerHTML = '<div class="empty">读取失败：' + esc(err.message) + '</div>';
  }
}

async function impDelete(rels){
  if(!rels || !rels.length) return;
  if(!confirm(`把选中的 ${rels.length} 个文件移到回收站？\n\n`
    + '· 不会真删，需要时能从「数据\\回收站」找回来\n'
    + '· 删掉的如果是唯一的订单/SKU 数据，这家店就算不出报表了')) return;
  try{
    const j = await (await fetch('/api/data-files/delete', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rels})})).json();
    if(!j.ok){ toast('删除失败：' + (j.error || '')); return; }
    rels.forEach(r => IMP_SEL.delete(r));
    toast(`已移到回收站 ${j.moved} 个` + (j.missing?.length ? `，跳过 ${j.missing.length} 个已不存在的` : ''));
    loadImportFiles();
    if(typeof loadStatus === 'function') loadStatus();
  }catch(e){ toast('删除失败：' + e.message); }
}

/** 导入店铺后台下载的「链接资料」表，读出每条链接的款式编码。 */
if($('#btn-link-import')) $('#btn-link-import').onclick = async () => {
  const inp = $('#link-file');
  const btn = $('#btn-link-import'), n = $('#link-note'), st = $('#link-stat');
  const f = inp && inp.files && inp.files[0];
  if(!f){ note(n, '先选一个文件', 'err'); return; }
  btn.disabled = true;
  note(n, '正在读取…', 'ok');
  try{
    const b64 = await fileToB64(f);
    const r = await fetch('/api/link-info', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: f.name, b64}),
    });
    const j = await r.json();
    if(!j.ok){
      note(n, '导入失败：' + (j.error || '未知原因'), 'err');
      st.textContent = '';
      return;
    }
    note(n, `导入成功：${j.sku_count} 个 SKU / ${j.link_count} 条链接`, 'ok');
    const rows = (j.samples || []).map(x =>
      `<tr><td class="mono" style="font-size:11.5px">${esc(x.link)}</td>
       <td><b>${esc(x.styles)}</b></td></tr>`).join('');
    st.innerHTML = j.multi_count
      ? `其中 <b>${j.multi_count}</b> 条链接含多个款式编码（已用 / 连接）。下面是前几条：`
      : '下面是前几条链接的款式编码：';
    st.innerHTML += rows
      ? `<table style="margin-top:8px;width:auto"><tbody>${rows}</tbody></table>`
      : '';
  }catch(e){
    note(n, '出错：' + e.message, 'err');
  }
  btn.disabled = false;
  if(inp) inp.value = "";
};

let repAll = [];      // 全部报表（两个房间都在里面，靠 room 区分）
let repStore = '';    // 当前按哪个店铺筛选（空 = 全部）
let repRoom = 'auto'; // auto = 自动报表 / manual = 手动报表

/** 跳到「报表」页并只看指定房间/店铺（卡片上的报表名点一下就走这里） */
function openReports(room, store){
  repRoom = (room === 'manual') ? 'manual' : 'auto';
  repStore = store || '';
  go('reports');
}

/** 当前房间里的报表 */
function repInRoom(){
  return repAll.filter(x => (x.room || 'auto') === repRoom);
}

async function loadReports(){
  try{
    const j = await (await fetch('/api/reports')).json();
    repAll = j.items || [];
    const autoN = repAll.filter(x => (x.room || 'auto') === 'auto').length;
    const manN = repAll.length - autoN;

    // 两个房间：份数都标出来，一眼知道手动算过几份
    const rooms = $('#rep-rooms');
    if(rooms){
      rooms.innerHTML =
        `<button data-room="auto" class="${repRoom === 'auto' ? 'on' : ''}">自动报表（${autoN}）</button>`
        + `<button data-room="manual" class="${repRoom === 'manual' ? 'on' : ''}">手动报表（${manN}）</button>`;
      rooms.querySelectorAll('[data-room]').forEach(b => b.onclick = () => {
        repRoom = b.dataset.room;
        repStore = '';
        loadReports();          // 切房间要重读：下载目录也跟着换
      });
    }
    const title = $('#rep-room-title');
    if(title) title.textContent = repRoom === 'auto' ? '自动报表' : '手动报表';
    const note = $('#rep-note');
    if(note){
      note.innerHTML = repRoom === 'auto'
        ? '自动报表按店铺放在 <span class="mono">报表\\自动\\店名\\</span>（驾驶舱取数、卡片「重算」跑出来的）。<br>'
          + '勾「导出带公式」会按原区间重算一份带 SUMIFS 的版本；删除只是挪进 <span class="mono">报表\\_已删除\\</span>，能找回。'
        : '手动报表按店铺放在 <span class="mono">报表\\手动\\店名\\</span>（「手动核算」跑出来的）。<br>'
          + '下面「下载到」只管手动报表——自动报表那边有自己的一份，互不影响。';
    }

    const roomItems = repInRoom();
    const hint = $('#rep-hint');
    if(hint) hint.textContent = roomItems.length ? ('共 ' + roomItems.length + ' 份') : '';

    // 店铺筛选条：只列这个房间里真有的店
    const stores = [...new Set(roomItems.map(x => x.group || ''))];
    const fc = $('#rep-filter');
    if(fc){
      fc.innerHTML = '<button data-store="" class="' + (repStore ? '' : 'on') + '">所有店铺</button>'
        + stores.map(s => `<button data-store="${esc(s)}" class="${repStore === s ? 'on' : ''}">`
          + esc(s || '未分店') + '（' + roomItems.filter(x => (x.group || '') === s).length + '）</button>').join('');
      fc.querySelectorAll('[data-store]').forEach(b => b.onclick = () => {
        repStore = b.dataset.store || '';
        loadReports();
      });
    }

    const items = repStore ? roomItems.filter(x => (x.group || '') === repStore) : roomItems;
    if(!items.length){
      $('#rep-list').innerHTML = repRoom === 'auto'
        ? '<div class="empty"><b>自动报表还是空的</b>去「驾驶舱」点一次「运行」，或在卡片上点「重算」</div>'
        : '<div class="empty"><b>手动报表还是空的</b>去「手动核算」给某家店导表、点「计算」</div>';
      repSyncSel();
      return;
    }
    $('#rep-list').innerHTML = '<table><thead><tr><th style="width:34px"></th><th>文件名</th>'
      + '<th style="width:120px">店铺</th>'
      + '<th style="width:80px">大小</th><th style="width:130px">生成时间</th><th style="width:130px"></th>'
      + '</tr></thead><tbody>' + items.map(it => `<tr>
          <td><input type="checkbox" data-rep="${esc(it.rel || it.name)}"></td>
          <td>${esc(it.name)}</td><td class="muted">${esc(it.group || '—')}</td>
          <td class="muted" style="white-space:nowrap">${it.size_mb} MB</td>
          <td class="muted" style="white-space:nowrap">${it.at}</td>
          <td><div class="ops"><button class="mini" data-view-report="${esc(it.rel || it.name)}">查看</button><button class="mini" data-dl-report="${esc(it.rel || it.name)}">下载</button></div></td></tr>`).join('') + '</tbody></table>';
    document.querySelectorAll('[data-view-report]').forEach(b =>
      b.onclick = () => viewReport(b.dataset.viewReport));
    document.querySelectorAll('[data-dl-report]').forEach(b =>
      b.onclick = () => saveReports([b.dataset.dlReport], b));
    document.querySelectorAll('#rep-list [data-rep]').forEach(cb =>
      cb.onchange = () => repSyncSel());
    loadRepDir();
    repSyncSel();
  }catch(e){ $('#rep-list').innerHTML = '<div class="empty">读取失败</div>'; }
}

/** 「下载到」：两个房间各记一份（report.download_dir / report.manual_download_dir） */
async function loadRepDir(){
  try{
    const j = await (await fetch('/api/settings')).json();
    const rp = (j.settings || {}).report || {};
    const dir = repRoom === 'auto' ? (rp.download_dir || '') : (rp.manual_download_dir || '');
    const el = document.getElementById('rep-dir');
    if(el) el.value = dir;
  }catch(e){ /* 读不到就留空，留空=桌面 */ }
}

async function saveRepDir(dir){
  try{
    const body = repRoom === 'auto'
      ? {report: {download_dir: dir || ''}}
      : {report: {manual_download_dir: dir || ''}};
    await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
  }catch(e){ /* 存不上也不影响这次保存 */ }
}

const _repDirBtn = document.getElementById('rep-dir-pick');
if(_repDirBtn) _repDirBtn.onclick = async () => {
  const el = document.getElementById('rep-dir');
  const cur = (el.value || '').trim();
  const api = window.pywebview && window.pywebview.api;
  if(api && api.pick_folder){
    const picked = await api.pick_folder(cur);
    if(picked){ el.value = picked; await saveRepDir(picked); toast('以后默认存到：' + picked); }
  }else{
    toast('浏览器里没有文件夹选择器，直接在上面输入路径即可');
  }
};
/* 设置页的两个目录：点一下弹系统选择框，不用手打路径 */
async function pickDirInto(input){
  if(!input) return;
  const api = window.pywebview && window.pywebview.api;
  if(!api || !api.pick_folder){
    toast('浏览器里没有文件夹选择器，直接粘贴路径即可');
    return;
  }
  try{
    const picked = await api.pick_folder((input.value || '').trim());
    if(picked){ input.value = picked; toast('已选：' + picked); }
  }catch(e){ toast('选择失败：' + e.message); }
}

async function openDirOf(input){
  if(!input) return;
  const dir = (input.value || '').trim();
  if(!dir){ toast('还没填文件夹'); return; }
  const api = window.pywebview && window.pywebview.api;
  if(!api || !api.open_folder){ toast('在这里输入路径即可'); return; }
  const ok = await api.open_folder(dir);
  if(!ok) toast('打不开这个文件夹（可能还没创建）：' + dir);
}

[['#set-dir-pick', '#set-dir'], ['#set-tpl-pick', '#set-tpl-dir']].forEach(([b, i]) => {
  const btn = $(b), inp = $(i);
  if(btn) btn.onclick = () => pickDirInto(inp);
});
[['#set-dir-open', '#set-dir'], ['#set-tpl-open', '#set-tpl-dir']].forEach(([b, i]) => {
  const btn = $(b), inp = $(i);
  if(btn) btn.onclick = () => openDirOf(inp);
});

const _repDirOpen = document.getElementById('rep-dir-open');
if(_repDirOpen) _repDirOpen.onclick = async () => {
  const el = document.getElementById('rep-dir');
  const cur = (el.value || '').trim();
  if(!cur){ toast('还没设置保存位置（留空 = 桌面）'); return; }
  const api = window.pywebview && window.pywebview.api;
  if(api && api.open_folder){
    const ok = await api.open_folder(cur);
    if(!ok) toast('打不开这个文件夹：' + cur);
  }else{
    toast('在这里输入路径即可');
  }
};

/** 勾选中的报表（相对路径） */
function repChecked(){
  return Array.from(document.querySelectorAll('#rep-list [data-rep]:checked')).map(x => x.dataset.rep);
}

/** 勾选数 / 全选框 / 删除按钮的可用状态，三处一起更新 */
function repSyncSel(){
  const boxes = [...document.querySelectorAll('#rep-list [data-rep]')];
  const n = boxes.filter(x => x.checked).length;
  const el = $('#rep-sel'); if(el) el.textContent = `已选 ${n} 份`;
  const all = $('#rep-all-chk');
  if(all){
    all.checked = boxes.length > 0 && n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
  }
  const del = $('#rep-del'); if(del) del.disabled = n === 0;
  const dl = $('#rep-dl'); if(dl) dl.disabled = n === 0;
}

const _repAll = $('#rep-all-chk');
if(_repAll) _repAll.onchange = () => {
  document.querySelectorAll('#rep-list [data-rep]').forEach(x => x.checked = _repAll.checked);
  repSyncSel();
};

// 删除勾选的报表：先确认，再挪进 报表/_已删除（不真删）
$('#rep-del').onclick = async () => {
  const names = repChecked();
  if(!names.length){ toast('先勾选要删除的报表'); return; }
  if(!confirm('删除 ' + names.length + ' 份报表？\n\n不会真删，会挪到 报表\\_已删除\\ 里，随时能找回。')) return;
  const btn = $('#rep-del');
  btn.disabled = true;
  try{
    const r = await fetch('/api/reports/delete', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({names})});
    const j = await r.json();
    if(j.ok){
      toast('已删除 ' + (j.moved || []).length + ' 份（在 报表\\_已删除 里可找回）'
            + ((j.missing || []).length ? '；' + j.missing.length + ' 份没找到' : ''));
      loadReports();
    }else{
      toast('删除失败：' + (j.error || '未知原因'));
    }
  }catch(e){ toast('删除出错：' + e.message); }
  btn.disabled = false;
};
$('#rep-dl').onclick = (e) => {
  const names = repChecked();
  if(!names.length){ toast('先勾几份报表'); return; }
  saveReports(names, e.currentTarget);
};
$('#rep-dl-all').onclick = (e) => {
  // 只下当前房间（自动 / 手动）里、当前店铺筛选下的那批
  const items = repInRoom().filter(x => !repStore || (x.group || '') === repStore);
  if(!items.length){ toast('这里没有报表'); return; }
  saveReports(items.map(x => x.rel || x.name), e.currentTarget);
};

/**
 * 下载 = 由桥直接存盘，然后告诉你存哪了。
 *
 * 以前是 location.href = '/report?name=…' 让浏览器自己下载——在桌面窗口里
 * 根本不弹保存框，点了就跟没点一样。现在走 /api/reports/save。
 */
async function saveReports(names, btn){
  if(!names.length){ toast('先选报表'); return; }
  const wf = !!(document.getElementById('rep-formula') || {}).checked;
  const dir = (document.getElementById('rep-dir') || {}).value || '';
  if(btn) btn.disabled = true;
  toast(wf ? '正在按原区间重算带公式版，稍等…' : '正在存…');
  try{
    const r = await fetch('/api/reports/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({names, with_formula: wf, dir: dir.trim(), room: repRoom})});
    const j = await r.json();
    if(j.ok){
      toast('已存 ' + (j.saved||[]).length + ' 份到 ' + j.dir
            + ((j.failed||[]).length ? '；' + j.failed.length + ' 份没成功' : ''));
      // 记住这次用的目录（清空也要记，不然下次又冒出旧路径）
      saveRepDir(dir.trim());
    }else{
      toast('没存成：' + (j.error || (j.failed||[]).join('；') || '未知原因'));
    }
  }catch(e){ toast('存盘出错：' + e.message); }
  if(btn) btn.disabled = false;
}

async function viewReport(rel){
  const name = String(rel).split('/').pop();
  $('#modal').classList.add('show');
  $('#modal-title').textContent = name;
  $('#modal-sub').textContent = '';
  $('#modal-body').innerHTML = '<div class="empty">读取中…</div>';
  try{
    const j = await (await fetch('/api/report-view?rel=' + encodeURIComponent(rel))).json();
    if(!j.ok){ $('#modal-body').innerHTML = '<div class="empty">' + esc(j.error || '读取失败') + '</div>'; return; }
    $('#modal-sub').textContent = j.modified + '　' + j.size_kb + ' KB';
    const total = j.total || {};
    const pick = k => { const hit = Object.keys(total).find(x => x.includes(k)); return hit ? total[hit] : ''; };
    const cards = [['GMV', pick('GMV')], ['净利', pick('净利')], ['商品成本', pick('成本')], ['费用合计', pick('费用')]]
      .map(([k, v]) => `<div class="stat" style="padding:14px 16px"><div class="k">${k}</div>
        <div class="v" style="font-size:17px">${fmt2(v)}</div></div>`).join('');
    let html = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:18px 22px">${cards}</div>`;
    html += '<table><tbody>' + (j.summary || []).map(r =>
      '<tr>' + r.map((c, i) => `<td${i === 0 ? ' style="color:var(--text-2)"' : ''}>${esc(c)}</td>`).join('')
      + '</tr>').join('') + '</tbody></table>';
    if((j.fees || []).length){
      html += '<h4 style="padding:16px 22px 6px;font-size:12.5px;color:var(--text-2);font-weight:500;margin:0">费用拆解</h4>';
      html += '<table><tbody>' + j.fees.map(r => '<tr>' + r.map(c => `<td>${esc(c)}</td>`).join('') + '</tr>').join('')
        + '</tbody></table>';
    }
    $('#modal-body').innerHTML = html;
  }catch(e){ $('#modal-body').innerHTML = '<div class="empty">' + esc(e.message) + '</div>'; }
}
function closeModal(){ $('#modal').classList.remove('show'); }
$('#modal').addEventListener('click', e => { if(e.target.id === 'modal') closeModal(); });

async function loadStores(){
  try{
    const j = await (await fetch('/api/stores')).json();
    storesCache = j.stores || [];
    $('#store-hint').textContent = storesCache.length ? storesCache.length + ' 家' : '';
    if(!storesCache.length){
      $('#store-list').innerHTML = '<div class="empty"><b>还没识别到店铺</b>在浏览器里打开店铺页面，点扩展面板的「立即运行」，它会自动认出来</div>';
      return;
    }
    let html = '<table><thead><tr><th style="width:200px">名字（可改）</th>'
      + '<th style="width:180px">抓到的店名</th>'
      + '<th style="width:160px">店铺 ID</th><th style="width:100px">国家</th>'
      + '<th style="width:70px">数据</th><th style="width:180px"></th></tr></thead><tbody>';
    storesCache.forEach(s => {
      const real = s.shop_name || '';
      html += `<tr>
        <td><input type="text" value="${esc(s.name || '')}" data-sid="${esc(s.id)}"
          style="width:100%;padding:6px 9px;font-size:13px;border:1px solid var(--line);border-radius:8px"></td>
        <td class="muted" style="font-size:12.5px">${real
          ? esc(real)
          : '<span style="color:var(--text-3)">还没抓到</span>'}</td>
        <td class="mono muted" style="font-size:11.5px">${esc(s.id)}</td>
        <td class="muted">${esc(s.country || '—')}${s.auto ? ' <span class="tag">自动</span>' : ''}</td>
        <td class="muted">${s.inbox_count !== undefined ? s.inbox_count + ' 个' : '—'}</td>
        <td style="white-space:nowrap">
          <button class="mini" data-rename="${esc(s.id)}">保存名字</button>
          <button class="mini" data-del-store="${esc(s.id)}"
            data-store-name="${esc(s.name || s.id)}">移除</button>
        </td></tr>`;
    });
    $('#store-list').innerHTML = html + '</tbody></table>';
    document.querySelectorAll('[data-rename]').forEach(b =>
      b.onclick = () => renameStore(b.dataset.rename, b));
    document.querySelectorAll('[data-del-store]').forEach(b =>
      b.onclick = () => removeStore(b.dataset.delStore, b.dataset.storeName, b));
  }catch(e){ $('#store-list').innerHTML = '<div class="empty">读取失败</div>'; }
}

async function renameStore(shopId, btn){
  const input = btn.closest('tr').querySelector('input');
  const name = input.value.trim();
  if(!name){ toast('名字不能为空'); return; }
  btn.disabled = true;
  try{
    const r = await fetch('/api/stores/rename', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({shop_id: shopId, name})});
    const j = await r.json();
    if(j.ok){
      // 名字改了，目录也跟着改；改不动时要说清楚，不然界面还是旧名会让人以为没保存
      const moved = (j.moved || []).filter(x => !String(x).includes('没改'));
      const stuck = (j.moved || []).filter(x => String(x).includes('没改') || String(x).includes('改不了'));
      toast('已保存：' + name + (moved.length ? '（' + moved.join('；') + '）' : '')
            + (stuck.length ? '　⚠ ' + stuck.join('；') : ''));
    }else{
      toast('保存失败：' + (j.error || ''));
    }
    if(j.ok){ loadStores(); loadStatus(); }
  }catch(e){ toast('出错：' + e.message); }
  btn.disabled = false;
}

/* 移除店铺：只从列表拿掉，店目录挪进 数据/店铺/_已删除（数据不丢）。
   不真删是因为里面的历史订单/快照平台不一定还能补回来。 */
async function removeStore(shopId, name, btn){
  if(!confirm('从列表移除「' + name + '」？\n\n店里的数据不会删，会挪到 数据\\stores\\_已删除\\ 里；'
    + '以后在浏览器里重新登录这家店，它会自动回来。')) return;
  btn.disabled = true;
  try{
    const r = await fetch('/api/stores/remove', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: shopId})});
    const j = await r.json();
    toast(j.ok ? ('已移除「' + name + '」，数据在 _已删除 里') : ('移除失败：' + j.error));
    if(j.ok){ loadStores(); loadStatus(); loadReports(); }
  }catch(e){ toast('出错：' + e.message); }
  btn.disabled = false;
}

let fmls = {};
let scope = "";                       // "" = 通用；国家名 = 这个国家单独一套
let scopeData = { "": { params: [], order_fields: {}, cost_fields: {} } };
let paramsLoaded = false;
let lastFormulaInput = null;          // 点变量时插到这个输入框
let headers = [];                     // 订单表的真实表头（点一下插 [标题]）
let countrySet = new Set();           // 哪些国家单独配过

const clone = v => JSON.parse(JSON.stringify(v));
const COURSE_LABELS = {
  order_id: '订单号', status: '订单状态', cancel: '取消/退货类型', sku_id: 'SKU ID',
  seller_sku: '卖家编码（成本表按它对）', product_id: '商品ID', product_name: '商品名称',
  qty: '购买数量', subtotal_before: '折扣前小计', seller_discount: '卖家折扣',
  order_amount: '买家实付', refund_amount: '退款金额', created: '下单时间', paid: '付款时间'
};

// 除了 [标题] 之外，还能写在公式里的「算好的量」
/** 「净利怎么算」那几行的中文名（跟 src/formula.py 的 FORMULA_CN 对应）。 */
const FORMULA_CN = {
  gross_profit: '毛利',
  total_fees: '费用合计',
  net: '净利',
  net_rate: '净利率',
};

const FIELD_VARS = [
  {k: 'gmv', t: 'GMV = Σ折扣前小计 − Σ卖家折扣', cn: '本行GMV'},
  {k: 'paid', t: '买家实付合计', cn: '本行买家实付'},
  {k: 'qty', t: '件数', cn: '本行件数'},
  {k: 'orders', t: '订单数（按 SKU 行数分摊）', cn: '本行订单数'},
  {k: 'cost_cny', t: '商品成本（人民币）', cn: '本行成本单价'},
  {k: 'cost', t: '商品成本（当地货币）', cn: '本行商品成本'},
  {k: 'cost_unit', t: '平均成本单价（人民币）', cn: '本行平均成本单价'},
  {k: 'refund', t: '退款金额', cn: '本行退款金额'},
  {k: 'commission', t: '达人佣金（整期）', cn: '达人佣金'},
  {k: 'ads', t: '广告花费（整期）', cn: '广告花费'},
];

const normParams = list => (list || []).map(p => ({
  ...p,
  expr: p.expr !== undefined && p.expr !== null ? String(p.expr) : '',
  kinds: (p.kinds === undefined || p.kinds === null) ? null : [...p.kinds]
}));

function cur(){ return scopeData[scope] || scopeData[""]; }
function rowsOf(){ return cur().params; }

/* ---------------- 手动核算（按店铺的卡片墙） ----------------
   一家店一张卡：这家店自己传的表放 数据\店铺\<店名>\手动导入\，
   算出来的报表放 报表\<店名>\手动\。跟驾驶舱那条线（收件箱）完全不交叉。 */
const MN = {spaces: [], busy: {}};
let MN_RANGE = null;                 // null = 全部（不按日期过滤）

function mnSpace(key){ return MN.spaces.find(x => x.key === key) || null; }

function paintMnRange(){
  const t = $('#mn-range-txt');
  if(t) t.textContent = MN_RANGE ? (MN_RANGE.startDate + ' ~ ' + (MN_RANGE.to || '今天')) : '全部数据';
  document.querySelectorAll('#mn-chips button').forEach(b => {
    const k = b.dataset.k;
    let on = false;
    if(k === 'all') on = !MN_RANGE;
    else if(MN_RANGE){ const [a, c] = rangeOf(k); on = (a === MN_RANGE.startDate && c === MN_RANGE.to); }
    b.classList.toggle('on', on);
  });
}

const MN_KINDS = [['order', '订单表'], ['ads', '广告表'],
                  ['affiliate', '联盟佣金表'], ['mapping', 'SKU/映射']];

function mnCardHtml(s){
  const kinds = s.kinds || {};
  const rows = MN_KINDS.map(([k, label]) => {
    const n = kinds[k] || 0;
    return `<div class="step ${n ? 'done' : ''}">
      <span class="ic">${n ? '<span style="color:#12a382">✓</span>' : '<span style="color:#c9c7bd">○</span>'}</span>
      <span class="nm">${label}</span>
      <span class="dt">${n ? n + ' 个文件' : '缺'}</span></div>`;
  }).join('');
  const last = s.last;
  const span = last && last.from && last.to
    ? ('　' + last.from.slice(5) + '~' + last.to.slice(5)) : '';
  const busy = !!MN.busy[s.key];
  // 「计算」这一行说进度：正在算 / 上次算完的时间与净利 / 还没算过
  const calcTxt = busy ? '计算中…'
    : (last ? ('完成 ' + (last.at || '').slice(-5) + '　净利 ' + fmt2(last.net) + span)
            : (s.count ? '还没算过' : '还没导入'));
  // 「报表」这一行说结果：出没出、叫什么名字。点一下去报表的手动房间
  const repTxt = last && last.report ? String(last.report) : '还没有';
  // 跟驾驶舱一样的身份行：平台店名 + ID 后 8 位 + 站点
  const meta = [
    (s.shop_name && s.shop_name !== s.name) ? s.shop_name : '',
    'ID ' + String(s.key || '').slice(-8),
    s.region || s.country || ''
  ].filter(Boolean).join(' · ');
  return `<div class="sc" data-mn-card="${esc(s.key)}">
    <div class="sc-head">
      <div class="sc-title"><div class="n">${esc(s.name || s.key)}</div>
        <div class="m">${esc(meta || '—')}</div></div>
      <span class="sc-state"><i class="dot ${s.count ? 'on' : 'off'}"></i>${s.count ? '有数据' : '空'}</span>
    </div>
    <div class="sc-body">${rows}
      <div class="step ${busy ? 'run' : (last ? 'done' : '')}">
        <span class="ic">${busy ? '<span style="color:#3b82d6">◐</span>'
          : (last ? '<span style="color:#12a382">✓</span>' : '<span style="color:#c9c7bd">○</span>')}</span>
        <span class="nm">计算</span><span class="dt">${esc(calcTxt)}</span></div>
      <div class="step ${last && last.report ? 'done' : ''}">
        <span class="ic">${last && last.report ? '<span style="color:#12a382">✓</span>'
          : '<span style="color:#c9c7bd">○</span>'}</span>
        <span class="nm">报表</span>
        <span class="dt ${last && last.report ? 'mn-rep' : ''}"
              title="${last && last.report ? '点一下去「报表 → 手动报表」看这份报表' : esc(repTxt)}"
              data-mn-rep="${last && last.report ? esc(s.key) : ''}">${esc(repTxt)}</span></div>
      ${s.count ? `<div class="mn-link" data-mn-files="${esc(s.key)}">查看 / 清空文件（${s.count}）›</div>` : ''}
    </div>
    <div class="sc-foot">
      <div class="last">${esc(s.count ? ('最后导入 ' + (s.last_file || '')) : '也可以把表直接拖到卡片上')}</div>
      <button class="ghost" data-mn-imp="${esc(s.key)}" ${busy ? 'disabled' : ''}>导入</button>
      ${busy
        ? `<button class="stop" data-mn-stop="${esc(s.key)}"
             title="掐掉这次的核算。算到一半的临时文件会被清掉，报表不受影响">停止</button>`
        : `<button class="primary" data-mn-run="${esc(s.key)}"
             title="用这家店的手动数据算。改了佣金费率就再点一次">计算</button>`}
    </div>
  </div>`;
}

async function loadManual(){
  const grid = $('#mn-grid');
  if(!grid) return;
  let down = false;
  try{
    const j = await (await fetch('/api/manual')).json();
    MN.spaces = j.spaces || [];
    // 服务端说哪家在算就按哪家显示「计算中…」——刷新页面也能看到，
    // 不会因为前端内存清空就变成「还没算过」。
    (j.running || []).forEach(k => { MN.busy[k] = 1; });
  }catch(e){ MN.spaces = []; down = true; }
  if(!MN.spaces.length){
    // 「服务没连上」和「真没有店铺」要分开说：
    // 都写成「还没识别到店铺」会让人去重新登录店铺，白折腾一圈
    grid.innerHTML = down
      ? '<div class="sc-empty"><b style="display:block;margin-bottom:6px;font-size:14px">取数服务没连上</b>'
        + '手动核算也要靠它读写文件。<br><br>'
        + '<button class="ghost" style="width:auto;padding:7px 16px;margin:0" '
        + 'onclick="go(\'debug\')">去调试页启动 →</button></div>'
      : '<div class="sc-empty">还没识别到店铺。<br>在浏览器里打开店铺后台，扩展会自动认出是哪家店。</div>';
    return;
  }
  const done = MN.spaces.filter(x => x.count).length;
  const hint = $('#mn-hint');
  if(hint) hint.textContent = MN.spaces.length + ' 家' + (done ? ('，' + done + ' 家有手动数据') : '，都还没有手动数据');
  grid.innerHTML = MN.spaces.map(mnCardHtml).join('');
  bindMnCards(grid);
  paintMnRange();
}

function bindMnCards(grid){
  grid.querySelectorAll('[data-mn-imp]').forEach(b => {
    b.onclick = () => {
      const inp = $('#mn-file');
      if(!inp) return;
      inp.dataset.key = b.dataset.mnImp;
      inp.click();
    };
  });
  grid.querySelectorAll('[data-mn-run]').forEach(b => {
    b.onclick = () => mnRun(b.dataset.mnRun);
  });
  grid.querySelectorAll('[data-mn-stop]').forEach(b => {
    b.onclick = async () => {
      const k = b.dataset.mnStop;
      b.disabled = true;
      b.textContent = '停止中…';
      const r = await fetch('/api/run/stop', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({manual_key: k}),
      }).then(x => x.json()).catch(() => null);
      toast(r && r.cancelled ? '已停止' : '它已经跑完了，没掐到');
      delete MN.busy[k];
      loadManual();
    };
  });
  // 报表名点一下 → 报表页「手动报表」房间、只看这家店
  grid.querySelectorAll('[data-mn-rep]').forEach(el => {
    if(!el.dataset.mnRep) return;
    el.onclick = () => {
      const sp = mnSpace(el.dataset.mnRep);
      openReports('manual', sp ? sp.name : '');
    };
  });
  grid.querySelectorAll('[data-mn-files]').forEach(b => {
    b.onclick = () => mnShowFiles(b.dataset.mnFiles);
  });
  // 整张卡都能接拖进来的文件
  grid.querySelectorAll('[data-mn-card]').forEach(card => {
    const key = card.dataset.mnCard;
    const stop = e => { e.preventDefault(); e.stopPropagation(); };
    ['dragenter', 'dragover'].forEach(ev =>
      card.addEventListener(ev, e => { stop(e); card.classList.add('picked'); }));
    ['dragleave', 'drop'].forEach(ev =>
      card.addEventListener(ev, e => { stop(e); card.classList.remove('picked'); }));
    card.addEventListener('drop', e => {
      const fs = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      if(fs.length) mnUpload(key, fs);
    });
  });
}

async function mnUpload(key, files){
  const n = $('#mn-note');
  const s = mnSpace(key);
  if(!s){ note(n, '没找到这家店', 'err'); return; }
  if(!files || !files.length) return;
  const cs = document.querySelector('[data-mn-country="' + key + '"]');
  const country = (cs && cs.value) || s.country || '';
  note(n, `正在传 ${files.length} 个文件到「${s.name || key}」…`, 'ok');
  const lines = [];
  for(const f of files){
    try{
      const b64 = await fileToB64(f);
      const j = await (await fetch('/api/manual/upload', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({key, name: f.name, b64, country})})).json();
      if(j.ok){
        lines.push(`✓ ${esc(f.name)} → <b>${esc(j.label || '未识别')}</b>`
          + (j.kind ? '' : ' <span style="color:#B45309">（表头没认出来，确认下是不是这类表）</span>'));
      }else{
        lines.push(`✕ ${esc(f.name)} → <span style="color:#C0392B">${esc(j.error || '失败')}</span>`);
      }
    }catch(err){
      lines.push(`✕ ${esc(f.name)} → <span style="color:#C0392B">${esc(err.message || String(err))}</span>`);
    }
  }
  note(n, `<b>${esc(s.name || key)}</b><br>` + lines.join('<br>'), 'ok');
  loadManual();
}

async function mnRun(key){
  const n = $('#mn-note');
  const s = mnSpace(key);
  if(!s) return;
  // 只看「有没有文件」，不拿记录里的类型当门槛：
  // 记录丢了（或有人直接把表拷进目录）时类型可能显示「未识别」，
  // 但数据其实是好的，服务端会按真实文件再判一次。
  if(!s.count){
    note(n, `「${esc(s.name || key)}」还没有导入任何表——先点「导入」传一份订单导出`, 'err');
    return;
  }
  const cs = document.querySelector('[data-mn-country="' + key + '"]');
  const country = (cs && cs.value) || s.country || '';
  const rg = MN_RANGE;
  // 卡片上立刻显示「计算中…」，不然点了没反应，人会以为没点上
  MN.busy[key] = 1;
  loadManual();
  try{
    const j = await (await fetch('/api/manual/run', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({key, country,
                            from: rg ? rg.startDate : '',
                            to: rg ? rg.to : '',
                            with_formula: !!($('#mn-formula') && $('#mn-formula').checked)})})).json();
    if(j.ok){
      note(n, `<b>${esc(s.name || key)} 算完了</b>　GMV ${fmt2(j.gmv)}　`
        + `净利 <b>${fmt2(j.net_local)}</b>（人民币 ${fmt2(j.net_cny)}）　`
        + `净利率 ${(Number(j.net_margin) * 100).toFixed(2)}%<br>`
        + `报表：${esc(j.report_name)}<span class="muted">（在「报表 → 手动报表」里）</span>`
        + (j.copied_to ? `<br><span class="muted">已复制到 ${esc(j.copied_to)}</span>` : ''), 'ok');
    }else{
      note(n, '算不出来：' + esc(j.error || '未知原因'), 'err');
    }
  }catch(e){ note(n, '出错：' + esc(e.message), 'err'); }
  delete MN.busy[key];
  loadManual();
}

function mnShowFiles(key){
  const s = mnSpace(key);
  if(!s) return;
  $('#modal').classList.add('show');
  $('#modal-title').textContent = (s.name || key) + ' · 手动数据';
  $('#modal-sub').textContent = s.dir || '';
  const files = s.files || [];
  if(!files.length){
    $('#modal-body').innerHTML = '<div class="empty">这家店还没有手动数据</div>';
    return;
  }
  $('#modal-body').innerHTML = '<table><thead><tr><th>文件名</th><th style="width:120px">识别为</th>'
    + '<th style="width:90px">大小</th><th style="width:130px">时间</th><th style="width:60px"></th>'
    + '</tr></thead><tbody>' + files.map(f => `<tr>
        <td>${esc(f.name)}</td>
        <td>${f.kind ? '<span class="tag">' + esc(f.label) + '</span>'
                    : '<span class="tag warn">未识别</span>'}</td>
        <td class="muted">${f.size_kb} KB</td>
        <td class="muted">${esc(f.at)}</td>
        <td><button class="mini" data-mn-rm="${esc(f.name)}">删</button></td></tr>`).join('')
    + '</tbody></table>'
    + `<div style="padding:14px 22px;border-top:1px solid var(--line-soft);display:flex;align-items:center;gap:12px">
         <button class="ghost" id="mn-open" style="width:auto;margin:0">打开文件夹</button>
         <button class="danger" id="mn-clear">清空这家店的手动数据</button>
         <span class="muted" style="font-size:12px">全部文件挪进「数据/回收站」，不是真删，随时能找回</span>
       </div>`;

  $('#modal-body').querySelectorAll('[data-mn-rm]').forEach(b => {
    b.onclick = async () => {
      if(!confirm('把这个文件从「' + (s.name || key) + '」移走？\n\n会挪进「数据\\回收站」，不是真删。')) return;
      try{
        const j = await (await fetch('/api/manual/delete', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({key, file: b.dataset.mnRm})})).json();
        if(!j.ok) toast('删不掉：' + (j.error || ''));
      }catch(e){ toast('删不掉：' + e.message); }
      mnShowFiles(key);
      loadManual();
    };
  });

  const op = document.getElementById('mn-open');
  if(op) op.onclick = async () => {
    const api = window.pywebview && window.pywebview.api;
    if(api && api.open_folder){
      const ok = await api.open_folder(s.dir);
      if(!ok) toast('打不开这个文件夹：' + (s.dir || ''));
    }else{
      toast('路径：' + (s.dir || ''));
    }
  };

  const clr = document.getElementById('mn-clear');
  if(clr) clr.onclick = async () => {
    if(!confirm('清空「' + (s.name || key) + '」的全部手动数据？\n\n'
                + '· ' + (s.files || []).length + ' 个文件会挪进「数据\\回收站」，能找回\n'
                + '· 驾驶舱的自动数据不受影响')) return;
    clr.disabled = true;
    try{
      const j = await (await fetch('/api/manual/clear', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({key})})).json();
      if(j.ok) toast('已清空 ' + (j.moved || 0) + ' 个文件（在回收站里能找回）');
      else toast('清空失败：' + (j.error || ''));
    }catch(e){ toast('清空失败：' + e.message); }
    mnShowFiles(key);
    loadManual();
  };
}

(function bindManualUi(){
  if(!$('#mn-grid')) return;
  $('#mn-file').onchange = (e) => {
    const key = e.target.dataset.key || '';
    const fs = Array.from(e.target.files || []);
    e.target.value = '';
    if(key && fs.length) mnUpload(key, fs);
  };
  $('#mn-range').onclick = () => {
    openRangePicker($('#mn-range'), MN_RANGE, r => { MN_RANGE = r; paintMnRange(); });
  };
  document.querySelectorAll('#mn-chips button').forEach(b => {
    b.onclick = () => {
      const k = b.dataset.k;
      if(k === 'all') MN_RANGE = null;
      else { const [a, c] = rangeOf(k); MN_RANGE = {startDate: a, to: c}; }
      paintMnRange();
    };
  });
})();

/* ---------------- 预设 ---------------- */
/* 一整套口径的命名快照：参数表 + 公式 + 列标题 + （可选）各国费率。
   两个页面（计算参数 / 费率）共用这套逻辑，只是 id 前缀不同。
   应用前会自动备份当前配置到 config/_backup/，随时能回滚。 */

let PRESET_ITEMS = [];

function presetScopes(prefix){
  const sel = document.getElementById(prefix + '-scope');
  if(!sel) return;
  const keep = sel.value;
  sel.innerHTML = '<option value="global">通用（所有店铺）</option>' +
    storesCache.map(s => `<option value="${esc(s.id)}">只给「${esc(s.name || s.id)}」</option>`).join('');
  if(keep) sel.value = keep;
}

async function loadPresets(prefix){
  const P = (id) => document.getElementById(prefix + id);
  const scopeSel = P('-scope');
  const scope = scopeSel ? scopeSel.value : '';
  try{
    const j = await (await fetch('/api/presets?scope=' + encodeURIComponent(scope))).json();
    PRESET_ITEMS = j.items || [];
    const sel = P('-list');
    if(!sel) return;
    const keep = sel.value;
    sel.innerHTML = PRESET_ITEMS.map(it => {
      const tag = it.builtin ? '内置'
        : (it.scope === 'global' ? '通用' : String(it.scope || '').replace(/^shop:/, ''));
      return `<option value="${esc(it.id)}">${esc(it.name)}　[${esc(tag)}]</option>`;
    }).join('');
    if(keep && PRESET_ITEMS.some(x => x.id === keep)) sel.value = keep;
    presetDesc(prefix);
  }catch(e){}
}

function presetDesc(prefix){
  const P = (id) => document.getElementById(prefix + id);
  const sel = P('-list'), d = P('-desc');
  if(!sel || !d) return;
  const it = PRESET_ITEMS.find(x => x.id === sel.value);
  if(!it){ d.textContent = ''; return; }
  let extra = it.builtin
    ? '（内置预设：只调整订单类型的勾选，公式与列标题保持原样）'
    : '（完整快照：参数表、公式、列标题一起覆盖；应用前会自动备份当前配置）';
  if(it.scope && it.scope !== 'global' && it.scope !== 'builtin')
    extra += '　作用范围：只给这家店';
  d.textContent = (it.desc || '') + extra;
}

async function presetApply(prefix){
  const P = (id) => document.getElementById(prefix + id);
  const sel = P('-list');
  if(!sel || !sel.value) return;
  const id = sel.value;
  const r = await (await fetch('/api/presets/apply', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({preset_id:id})})).json();
  if(!r.ok){ note(P('-note'), '应用失败：' + (r.error || ''), 'err'); return; }
  note(P('-note'), '已应用：' + (r.applied || id) + (r.note ? '（' + r.note + '）' : ''), 'ok');
  // 配置变了，参数表和费率表都要重新读一遍
  paramsLoaded = false;
  try{ await loadParams(); }catch(e){}
  try{ await loadRates(); }catch(e){}
  await loadPresets(prefix);
}

async function presetSave(prefix){
  const P = (id) => document.getElementById(prefix + id);
  const nameEl = P('-name');
  const name = (nameEl && nameEl.value || '').trim();
  if(!name){ note(P('-note'), '先给预设起个名字', 'err'); return; }
  const scopeSel = P('-scope');
  const scope = scopeSel ? scopeSel.value : 'global';
  const includeRates = P('-rates') ? P('-rates').checked : false;
  const r = await (await fetch('/api/presets/save', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, scope, include_rates:includeRates})})).json();
  if(!r.ok){ note(P('-note'), '保存失败：' + (r.error || ''), 'err'); return; }
  note(P('-note'), '已保存预设：' + r.name, 'ok');
  if(nameEl) nameEl.value = '';
  await loadPresets(prefix);
}

async function presetDel(prefix){
  const P = (id) => document.getElementById(prefix + id);
  const sel = P('-list');
  if(!sel || !sel.value) return;
  const it = PRESET_ITEMS.find(x => x.id === sel.value);
  if(!it) return;
  if(it.builtin){ note(P('-note'), '内置预设不能删', 'err'); return; }
  if(!confirm(`确定删除预设「${it.name}」？`)) return;
  const r = await (await fetch('/api/presets/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({preset_id:it.id})})).json();
  if(!r.ok){ note(P('-note'), '删除失败：' + (r.error || ''), 'err'); return; }
  note(P('-note'), '已删除：' + it.name, 'ok');
  await loadPresets(prefix);
}

['ps','pr'].forEach(prefix => {
  const P = (id) => document.getElementById(prefix + id);
  const list = P('-list'), apply_ = P('-apply'), save = P('-save'), del = P('-del'), scope = P('-scope');
  if(list) list.onchange = () => presetDesc(prefix);
  if(apply_) apply_.onclick = () => presetApply(prefix);
  if(save) save.onclick = () => presetSave(prefix);
  if(del) del.onclick = () => presetDel(prefix);
  if(scope) scope.onchange = () => loadPresets(prefix);
});

async function loadParams(){
  if(paramsLoaded){
    await buildScopeSelect();
    renderAll();
    return;
  }
  try{
    const j = await (await fetch('/api/config')).json();
    fmls = j.formulas || {};
    headers = j.headers || [];
    scopeData = { "": {
      params: normParams(j.params),
      order_fields: clone(j.order_fields || {}),
      cost_fields: clone(j.cost_fields || {}),
      ads_fields: clone(j.ads_fields || {}),
      affiliate_fields: clone(j.affiliate_fields || {}),
      mapping_fields: clone(j.mapping_fields || {}),
      exclude_zero_amount: !!j.exclude_zero_amount,
      report_columns: (j.report_columns && j.report_columns.length) ? j.report_columns : null,
      // 列顺序也要读回来：只存不读的话，一刷新就退回默认顺序，
      // 表现就是「拖完切个页面再回来又变回去了」
      report_column_order: (j.report_column_order && j.report_column_order.length)
        ? j.report_column_order : null,
      report_titles: j.report_titles || {},
      report_total_position: j.report_total_position || 'bottom'
    }};
    countrySet = new Set(Object.keys(j.by_country || {}));
    // 每个国家：通用那套 + 它自己的覆盖
    Object.entries(j.by_country || {}).forEach(([k, v]) => {
      const d = {
        params: (v.params && v.params.length) ? normParams(v.params) : clone(scopeData[""].params),
        order_fields: Object.assign(clone(scopeData[""].order_fields), v.order_fields || {}),
        cost_fields: Object.assign(clone(scopeData[""].cost_fields), v.cost_fields || {}),
        ads_fields: Object.assign(clone(scopeData[""].ads_fields), v.ads_fields || {}),
        affiliate_fields: Object.assign(clone(scopeData[""].affiliate_fields), v.affiliate_fields || {}),
        mapping_fields: Object.assign(clone(scopeData[""].mapping_fields), v.mapping_fields || {}),
        exclude_zero_amount: ('exclude_zero_amount' in v)
          ? !!v.exclude_zero_amount : !!scopeData[""].exclude_zero_amount,
        report_columns: (v.report_columns && v.report_columns.length) ? v.report_columns : null,
        report_column_order: (v.report_column_order && v.report_column_order.length)
          ? v.report_column_order : (scopeData[""].report_column_order || null),
        report_titles: v.report_titles || {},
        report_total_position: v.report_total_position || 'bottom'
      };
      scopeData[k] = d;
    });
    if(!scopeData[scope]) scope = "";
    paramsLoaded = true;
    await buildScopeSelect();
    renderAll();
    loadFormulaVars();
  }catch(e){ toast('读取配置失败：' + e.message); }
}

async function buildScopeSelect(){
  try{
    const r = await (await fetch('/api/rates')).json();
    const sel = $('#pscope');
    sel.innerHTML = '<option value="">通用（没单独设的国家都用这套）</option>';
    Object.keys(r.countries || {}).forEach(c => {
      const o = document.createElement('option');
      o.value = c;
      o.textContent = c + (countrySet.has(c) ? ' · 已单独设置' : '');
      sel.append(o);
    });
    sel.value = scope;
    sel.onchange = () => switchScope(sel.value);
    $('#pscope-hint').textContent = scope
      ? ('正在编辑「' + scope + '」：参数、订单表标题都只对这个国家生效')
      : '每个国家一套，互不影响';
  }catch(e){}
}

function switchScope(next){
  scope = next || "";
  if(!scopeData[scope]){
    // 第一次给某个国家单独设：从通用那套复制一份，不用从零配
    scopeData[scope] = clone(scopeData[""] || {params:[], order_fields:{}, cost_fields:{}});
    countrySet.add(scope);
  }
  renderAll();
  buildScopeSelect();
  toast(scope ? ('已切到「' + scope + '」') : '已切回通用参数');
}

/* 计算参数页顶部分段：一屏只看一类内容，避免所有卡片挤在一页 */
const PTABS = [
  { key: 'params',  label: '参数表' },
  { key: 'headers', label: '表头识别' },
  { key: 'reports', label: '报表列' },
  { key: 'formula', label: '净利公式' },
];
let pTab = 'params';
function setPTab(k){
  pTab = k;
  document.querySelectorAll('#view-params .pane').forEach(p => {
    p.hidden = p.dataset.pane !== k;
  });
  renderPTabs();
  try{ window.scrollTo({top: 0, behavior: 'smooth'}); }catch(e){}
}
function renderPTabs(){
  const box = $('#ptabs');
  if(!box) return;
  box.innerHTML = PTABS.map(t =>
    `<button class="mini${t.key === pTab ? ' primary' : ''}" data-ptab="${t.key}">${esc(t.label)}</button>`).join('');
  box.querySelectorAll('[data-ptab]').forEach(b => b.onclick = () => setPTab(b.dataset.ptab));
}

function renderAll(){
  renderDropZero();
  renderPTabs();
  renderParams(); renderOrderFields(); renderFormulas(); renderVars(); renderReportColumns();
}

/* 总开关：去除 0 元样品订单 */
function renderDropZero(){
  const box = $('#drop-zero');
  if(!box) return;
  const on = !!cur().exclude_zero_amount;
  box.checked = on;
  box.closest('.switch').classList.toggle('on', on);
  box.onchange = () => {
    cur().exclude_zero_amount = box.checked;
    renderDropZero();
    renderParams();
    toast(box.checked
      ? '已打开：样品单不再计入发单费、商品成本、订单数。记得点「保存配置」'
      : '已关闭：各参数按自己的勾选算。记得点「保存配置」');
  };
}

/* 新国家：从通用那套复制，开关也跟着复制 */


/* 公式框下的「插入参数」下拉：把能用到的量分组列出来，选一下就插到光标处。
   新人不用记中文名、也不用去页面底部找变量芯片。 */
function paramOptionsHTML(){
  const opt = (val, label) => `<option value="${esc(val)}">${esc(label)}</option>`;
  const groups = [];
  if(FIELD_VARS && FIELD_VARS.length){
    groups.push('<optgroup label="本行 / 算好的量">' +
      FIELD_VARS.map(v => opt(v.cn || v.k, (v.cn || v.k) + (v.t ? '　· ' + v.t : ''))).join('') +
      '</optgroup>');
  }
  if(varRates && varRates.length){
    groups.push('<optgroup label="费率 / 汇率">' +
      varRates.map(k => opt(rateCn(k), rateCn(k))).join('') + '</optgroup>');
  }
  const prs = rowsOf().filter(r => r.id).map(r => (r.label || r.id).trim()).filter(Boolean);
  if(prs.length){
    groups.push('<optgroup label="参数表里别的行（结果）">' +
      prs.map(x => opt(x, x)).join('') + '</optgroup>');
  }
  // 订单表字段：插字段名（折扣前小计），程序按「表头识别」里配的候选自动匹配，
  // 表头改名只要加候选，公式不用动。
  const of = (cur() && cur().order_fields) || {};
  const ofEntries = Object.entries(of)
    .map(([k, v]) => [String((v && (v.label || v.titles && v.titles[0])) || k), k])
    .filter(([lb]) => lb);
  if(ofEntries.length){
    groups.push('<optgroup label="订单表字段（按表头识别自动匹配）">' +
      ofEntries.map(([lb]) => opt('[' + lb + ']', lb)).join('') + '</optgroup>');
  }
  if(headers && headers.length){
    groups.push('<optgroup label="订单表真实列名">' +
      headers.map(h => opt('[' + h + ']', h)).join('') + '</optgroup>');
  }
  return '<option value="">＋ 插入参数…</option>' + groups.join('');
}

function renderParams(){
  const rows = rowsOf();
  const body = $('#pbody');
  const dropZero = !!cur().exclude_zero_amount;
  body.innerHTML = rows.map((r, i) => {
    const kinds = r.kinds;
    const kc = KINDS.map(k => {
      const off = dropZero && k.id === 'zero_amount';
      const on = !off && (kinds === null ? true : (kinds || []).includes(k.id));
      return `<td class="kcell${off ? ' koff' : ''}"${off ? ' title="已打开「去除 0 元样品订单」，这一格暂时不生效"' : ''}>` +
             `<input type="checkbox" data-k="${i}:${k.id}" ${on ? 'checked' : ''} ${off ? 'disabled' : ''}></td>`;
    }).join('');
    return `<tr class="${r.enabled === false ? 'off' : ''}">
      <td>
        <input type="text" data-label="${i}" value="${esc(r.label || r.id)}">
        <div class="pid">${esc(r.id)}${r.role ? '<span class="tag gray">报表列</span>' : ''}${
          kinds === null ? '<span class="tag">全部</span>'
          : (kinds.length === 0 ? '<span class="tag warn">不算</span>' : '')}</div>
      </td>
      <td class="expr-cell">
        <textarea data-expr="${i}" rows="2"
          placeholder="如：本行订单数 * 发单费单价 * 人民币汇率"
          style="font-family:ui-monospace,Consolas,monospace;width:100%;resize:vertical"></textarea>
        <div class="expr-tools">
          <button class="mini" data-addline="${i}" title="在光标处加一行（多个候选标题一行一个）">＋ 加一行</button>
          <select class="ins-param" data-insfor="${i}" title="选一个参数，插到公式光标处"
                  style="max-width:190px;font-size:12px;padding:3px 6px">${paramOptionsHTML()}</select>
          <span class="expr-hint" data-hint="${i}"></span>
        </div>
        ${r.id === 'ads' ? '<div class="expr-hint" style="color:var(--text-2);margin-top:2px">广告表金额已按「货币」列自动换算成<b>本地货币</b>（USD/空 → × 美金汇率，MYR 直接用），公式直接引用即可，不用再乘汇率</div>' : ''}
        ${r.id === 'affiliate' ? '<div class="expr-hint" style="color:var(--text-2);margin-top:2px">联盟佣金表金额本身就是<b>本地货币</b>（如 RM2,678.75），公式直接引用即可</div>' : ''}
      </td>
      ${kc}
      <td class="kcell"><input type="checkbox" data-on="${i}" ${r.enabled !== false ? 'checked' : ''}
            style="width:15px;height:15px;accent-color:var(--accent);cursor:pointer"></td>
      <td class="kcell"><button class="mini" data-del="${i}"
            style="padding:3px 7px;font-size:12px;color:var(--warn)">删</button></td>
    </tr>`;
  }).join('');

  // textarea 的内容要放在 innerHTML 之后设，否则换行会被当成 HTML 空白吃掉
  body.querySelectorAll('[data-expr]').forEach(inp => {
    inp.value = rows[Number(inp.dataset.expr)].expr || '';
  });

  body.querySelectorAll('[data-label]').forEach(inp =>
    inp.oninput = () => { rows[Number(inp.dataset.label)].label = inp.value; });
  body.querySelectorAll('[data-expr]').forEach(inp => {
    const i = Number(inp.dataset.expr);
    inp.oninput = () => {
      rows[i].expr = inp.value;
      checkExpr(i);
    };
    inp.onfocus = () => { lastFormulaInput = inp; };
    inp.onblur = () => { checkExpr(i); };
    checkExpr(i);
  });
  // 「插入参数」下拉：选一个就插到该行公式的光标处
  body.querySelectorAll('[data-insfor]').forEach(sel => {
    sel.onchange = () => {
      const i = Number(sel.dataset.insfor);
      const txt = sel.value;
      sel.value = '';
      if(!txt) return;
      const inp = body.querySelector(`[data-expr="${i}"]`);
      if(!inp) return;
      const s = inp.selectionStart ?? inp.value.length, e = inp.selectionEnd ?? s;
      inp.value = inp.value.slice(0, s) + txt + inp.value.slice(e);
      rows[i].expr = inp.value;
      inp.focus();
      inp.selectionStart = inp.selectionEnd = s + txt.length;
      checkExpr(i);
    };
  });

  // 「＋ 加一行」：在光标处插一个换行，把候选标题拆成多行写
  body.querySelectorAll('[data-addline]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.addline);
    const inp = body.querySelector(`[data-expr="${i}"]`);
    if(!inp){ toast('先点一下要编辑的公式框'); return; }
    inp.focus();
    const s = inp.selectionStart ?? inp.value.length;
    // 前面是「[」就紧跟着换行，否则先补一个逗号再换行
    const before = inp.value.slice(0, s);
    // 在方括号里（还没闭合）→ 直接换行，一行一个候选；
    // 在方括号外面 → 先补个逗号再换行，免得拼成两个词。
    const inBracket = before.lastIndexOf('[') > before.lastIndexOf(']');
    const ins = (inBracket ? '' : ',') + '\n';
    inp.value = before + ins + inp.value.slice(inp.selectionEnd ?? s);
    rows[i].expr = inp.value;
    inp.selectionStart = inp.selectionEnd = s + ins.length;
    checkExpr(i);
  });
  body.querySelectorAll('[data-k]').forEach(cb => cb.onchange = () => {
    const [i, k] = cb.dataset.k.split(':');
    const r = rows[Number(i)];
    if(r.kinds === null) r.kinds = KINDS.map(x => x.id);
    const st = new Set(r.kinds);
    cb.checked ? st.add(k) : st.delete(k);
    r.kinds = KINDS.map(x => x.id).filter(x => st.has(x));
    renderParams();
  });
  body.querySelectorAll('[data-on]').forEach(cb => cb.onchange = () => {
    rows[Number(cb.dataset.on)].enabled = cb.checked;
    renderParams();
  });
  body.querySelectorAll('[data-del]').forEach(b => b.onclick = () => {
    const r = rows[Number(b.dataset.del)];
    if(confirm('删掉「' + (r.label || r.id) + '」这一项？净利公式里如果引用了它，会算不出来')){
      rows.splice(Number(b.dataset.del), 1); renderParams(); renderVars();
    }
  });
}

/* 表头识别：四个表各一套字段（订单 / 广告 / 联盟佣金 / 链接资料）。
   每套结构都是 {字段: {label, titles:[...]}}；标题一行一个、按顺序找。
   点 tab 切换表，互不影响。 */
const OF_TABLES = [
  { key: 'order_fields',     tab: '订单表',     hint: '判断订单类型、找链接、对成本' },
  { key: 'ads_fields',       tab: '广告表',     hint: '取商品ID、花费、货币（花费会自动换成本地货币）' },
  { key: 'affiliate_fields', tab: '联盟佣金表', hint: '取商品ID、预估佣金' },
  { key: 'mapping_fields',   tab: '链接资料表', hint: '取款式编码、商品ID、SKU、卖家编码' },
];
let ofTable = 'order_fields';

function curOfFields(){
  const c = cur();
  if(!c[ofTable] || typeof c[ofTable] !== 'object') c[ofTable] = {};
  return c[ofTable];
}

function renderOfTabs(){
  const box = $('#oftabs');
  if(!box) return;
  box.innerHTML = OF_TABLES.map(t =>
    `<button class="mini${t.key === ofTable ? ' primary' : ''}" data-oftab="${t.key}"
       title="${esc(t.hint)}">${esc(t.tab)}</button>`).join('');
  box.querySelectorAll('[data-oftab]').forEach(b => b.onclick = () => {
    ofTable = b.dataset.oftab;
    renderOfTabs();
    renderOrderFields();
  });
}

function renderOrderFields(){
  renderOfTabs();
  const of = curOfFields();
  const keys = Object.keys(of);
  if(!keys.length){
    $('#ofbody').innerHTML = '<tr><td colspan="2" class="muted" style="padding:10px">这个表还没有配置字段</td></tr>';
    return;
  }
  $('#ofbody').innerHTML = keys.map(k => {
    const v = of[k] || {};
    const titles = (Array.isArray(v) ? v : (v.titles || [])).map(x => String(x ?? ''));
    const list = titles.length ? titles : [''];
    const label = (v && v.label) || k;
    const rows = list.map((t, j) => `<div style="display:flex;gap:6px;align-items:center;margin-bottom:5px">
        <input type="text" data-of="${esc(k)}" data-idx="${j}" value="${esc(t)}"
               placeholder="这个表里的列标题" style="flex:1">
        <button class="mini" data-ofdel="${esc(k)}" data-idx="${j}"
                title="删掉这一行" style="padding:2px 7px;color:var(--warn)">×</button>
      </div>`).join('');
    return `<tr>
      <td>${esc(label)}<div class="pid">${esc(k)}</div></td>
      <td class="expr-cell">
        <div data-oflist="${esc(k)}">${rows}</div>
        <div class="expr-tools">
          <button class="mini" data-ofadd="${esc(k)}">＋ 加一行</button>
          <span class="expr-hint">一行一个标题，按顺序挨个找，找到第一个就用它</span>
        </div>
      </td>
    </tr>`;
  }).join('');

  // 取（必要时初始化）某个字段的 {label, titles}
  const ensure = (k) => {
    const v = of[k];
    if(!v || Array.isArray(v)){
      of[k] = {label: k, titles: Array.isArray(v) ? v.slice() : []};
    }
    if(!Array.isArray(of[k].titles)) of[k].titles = [];
    return of[k];
  };
  $('#ofbody').querySelectorAll('input[data-of]').forEach(inp => {
    inp.oninput = () => {
      const o = ensure(inp.dataset.of);
      o.titles[Number(inp.dataset.idx)] = inp.value;
    };
  });
  $('#ofbody').querySelectorAll('[data-ofadd]').forEach(b => b.onclick = () => {
    const k = b.dataset.ofadd;
    const o = ensure(k);
    o.titles.push('');
    renderOrderFields();
    const all = $('#ofbody').querySelectorAll(`input[data-of="${CSS.escape(k)}"]`);
    if(all.length) all[all.length - 1].focus();
  });
  $('#ofbody').querySelectorAll('[data-ofdel]').forEach(b => b.onclick = () => {
    const k = b.dataset.ofdel, idx = Number(b.dataset.idx);
    const o = ensure(k);
    if(o.titles.length <= 1) o.titles = [''];
    else o.titles.splice(idx, 1);
    renderOrderFields();
  });
}

/* 可用变量：订单表真实表头 + 算好的量 + 费率 + 其它参数 */
let varRates = [];
// 公式里能引用的费率字段：把「汇率类 / 发单费单价」也放进来，
// 它们虽然不在费率管理表的可编辑列里（那里有专门的固定列），
// 但公式里写 params.cny_to_local / 人民币汇率 是合法的。
const RATE_META_FORMULA = ['currency', 'rates_verified', 'dispatch_fee_local',
  '_dispatch_note', 'region'];
async function loadFormulaVars(){
  try{
    const j = await (await fetch('/api/rates')).json();
    rateLabels = j.labels || {};
    rateCnServer = j.rate_cn || {};
    const set = new Set();
    Object.values(j.countries || {}).forEach(c => {
      Object.keys(c).forEach(k => {
        if(RATE_META_FORMULA.includes(k) || k.startsWith('_')) return;
        if(c[k] !== null && typeof c[k] === 'object') return;
        set.add(k);
      });
    });
    varRates = [...set];
  }catch(e){ varRates = []; }
  renderVars();
  // 费率是异步加载的：加载完要重渲染参数表（让「插入参数」下拉带上费率），
  // 并重新校验一遍公式——否则初始渲染时 varRates 为空，会把「人民币汇率」误报不认识。
  if(typeof renderParams === 'function') renderParams();
}

function insertVar(txt){
  const inp = lastFormulaInput || document.querySelector('#pbody [data-expr]');
  if(!inp){ toast('先点一下要编辑的公式框'); return; }
  const s = inp.selectionStart ?? inp.value.length, e = inp.selectionEnd ?? s;
  inp.value = inp.value.slice(0, s) + txt + inp.value.slice(e);
  inp.focus();
  inp.selectionStart = inp.selectionEnd = s + txt.length;
  inp.oninput && inp.oninput();
}

function renderVars(){
  const box = $('#vars');
  if(!box) return;
  // 点一下插进公式：插中文名，不用记英文。
  // 括号里的英文是「这玩意儿的原名」，看不懂英文也能靠中文点。
  const colChips = headers.map(h =>
    `<div class="v" data-ins="[${esc(h)}]"><code>[${esc(h)}]</code><span>订单表列</span></div>`);
  const baseChips = FIELD_VARS.map(v =>
    `<div class="v" data-ins="${esc(v.cn || v.k)}"><code>${esc(v.cn || v.k)}</code>
      <span>${esc(v.t)}<i class="en">${esc(v.cn ? v.k : '')}</i></span></div>`);
  const rateChips = varRates.map(k => {
    const cn = rateCn(k);
    return `<div class="v" data-ins="${esc(cn)}"><code>${esc(cn)}</code>
      <span>${esc(cn)}${cn === k ? '' : ''}<i class="en">params.${esc(k)}</i></span></div>`;
  });
  const paramChips = rowsOf().filter(r => r.id).map(r => {
    const lb = (r.label || r.id).trim();
    return `<div class="v" data-ins="${esc(lb)}"><code>${esc(lb)}</code>
      <span>这一项算出来的结果<i class="en">param.${esc(r.id)}</i></span></div>`;
  });
  box.innerHTML = colChips.join('') + baseChips.join('') + rateChips.join('') + paramChips.join('');
  box.querySelectorAll('[data-ins]').forEach(el => el.onclick = () => insertVar(el.dataset.ins));
}

/** 公式实时校验：把公式翻译成人话，并指出不认识的词。
 *
 *  用户看英文公式最头疼的就是「这玩意儿到底算的啥」。
 *  这里直接在输入框下面写出来：用了哪些量、乘了什么、有没有写错的词。
 */
function checkExpr(i){
  const box = document.querySelector(`[data-hint="${i}"]`);
  if(!box) return;
  const rows = rowsOf();
  const r = rows[i];
  const raw = String((r && r.expr) || '').trim();
  if(!raw){ box.textContent = ''; box.className = 'expr-hint'; return; }

  // 已知的词：内置量、费率项、参数表那一行、订单表列标题
  const known = new Map();
  FIELD_VARS.forEach(v => {
    if(v.cn) known.set(v.cn, v.t);
    // 「本行GMV」的「GMV」也算认识（参数表里有一行就叫 GMV（收入））
    if(v.cn && v.cn.startsWith('本行')) known.set(v.cn.slice(2), v.t);
  });
  varRates.forEach(k => {
    const cn = rateCn(k);
    known.set(cn, '费率：' + cn);
    // 用户可能把费率中文名改成了别的；引擎默认名（人民币汇率…）和旧名也认，
    // 免得公式里照文档写默认名被误报「不认识」。下拉插入的是当前名，也不会错。
    [rateLabels && rateLabels[k], rateCnServer && rateCnServer[k], RATE_LABELS[k], k]
      .forEach(n => {
        const s = String(n || '').trim();
        if(s) known.set(s, '费率：' + s);
      });
  });
  rowsOf().filter(x => x.id).forEach(x => {
    const lb = (x.label || x.id).trim();
    known.set(lb, '参数表里「' + lb + '」那一项');
  });
  headers.forEach(h => known.set(h, '订单表的列'));
  // 「订单表识别」里配的候选标题也算认识：用户就是按映射写标题的。
  // 只认真实表头的话，没上传订单表 / 表头是本地化名字时，
  // 合法的 [折扣前小计] 之类会被误报「不认识」。
  Object.values(cur().order_fields || {}).forEach(v => {
    (Array.isArray(v) ? v : (v.titles || [])).forEach(t => {
      const s = String(t || '').trim();
      if(s) known.set(s, '订单表列（订单表识别里配的）');
    });
  });

  // 找出用到的词
  const used = [];
  const unknown = [];
  // 先把方括号里的候选标题摘出来（那些是订单表列）
  const cols = [];
  let s = raw.replace(/\[([^\[\]]+)\]/g, (m, inner) => {
    inner.split(/[,;|，；\n\r]+/).forEach(c => {
      const t = c.trim();
      if(t) cols.push(t);
    });
    return ' \u0001 ';
  });
  // 再按「中文词 / 英文标识」切出来
  const tokens = s.match(/[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*|[一-龥A-Za-z_][一-龥A-Za-z0-9_ ]*/g) || [];
  tokens.forEach(t => {
    const w = t.trim();
    if(!w || w === '\u0001') return;
    if(known.has(w)){ if(!used.includes(w)) used.push(w); return; }
    // 英文标识：内置量英文名 / params.字段 / param.字段
    const BUILTIN_EN = ['gmv','paid','qty','orders','order_count','items',
      'cost_cny','cost','cost_unit','refund','commission','ads'];
    if(BUILTIN_EN.includes(w)) return;
    if(/^params\.[A-Za-z0-9_]+$/.test(w)){
      const f = w.split('.')[1];
      if(varRates.includes(f)) return;
      unknown.push(w); return;
    }
    if(/^param\.[A-Za-z0-9_]+$/.test(w)) return;
    // 修饰过的中文词（如「本行订单数」）剥掉「本行」再找
    if(w.startsWith('本行') && known.has(w.slice(2))){
      if(!used.includes(w)) used.push(w);
      return;
    }
    unknown.push(w);
  });
  cols.forEach(c => {
    if(!known.has(c) && !unknown.includes(c)) unknown.push(c);
  });

  const parts = [];
  if(used.length) parts.push('＝ ' + used.join(' × '));
  if(cols.length) parts.push('订单表列：' + cols.join(' / '));
  if(unknown.length){
    box.className = 'expr-hint bad';
    box.textContent = '⚠ 不认识：' + unknown.join('、') + (parts.length ? '　' + parts.join('　') : '');
    return;
  }
  box.className = 'expr-hint';
  box.textContent = parts.join('　') || '（还没写内容）';
}

/* 汇总页可用列目录：基础列固定，费用列按参数表动态生成，费用占比只在该费用存在时出。
   与 src/report.py 的 _summary_specs / _default_report_columns 保持一致。 */
const REPORT_BASE = [
  {key:'rank', header:'排名', kind:'text'},
  {key:'style', header:'款式编码', kind:'text'},
  {key:'product', header:'链接（商品ID）', kind:'text'},
  {key:'name', header:'商品名称', kind:'text'},
  {key:'gmv', header:'GMV', kind:'money'},
  {key:'orders', header:'订单数', kind:'int'},
  {key:'items', header:'件数', kind:'int'},
  {key:'aov', header:'客单价', kind:'money'},
  {key:'buyer_paid', header:'买家实付', kind:'money'},
  {key:'cost', header:'成本', kind:'money'},
  {key:'gross_profit', header:'毛利', kind:'money'},
  {key:'gross_margin', header:'链接毛利率', kind:'pct'},
];
const REPORT_ROLE_KEYS = new Set(['gmv','cost','order_count','items','buyer_paid']);
const REPORT_RATIOS = [
  {key:'ratio:dispatch', header:'发单费占比', kind:'pct'},
  {key:'ratio:ads', header:'广告费比', kind:'pct'},
];
const REPORT_KIND_CN = {text:'文本', int:'整数', money:'金额', pct:'百分比'};

function reportColumnCatalog(){
  const ps = (cur() && cur().params) || [];
  const fees = [], seen = new Set();
  for(const it of ps){
    const id = String(it.id || '').trim();
    if(!id || seen.has(id)) continue;
    const role = String(it.role || '').trim();
    if(REPORT_ROLE_KEYS.has(id) || REPORT_ROLE_KEYS.has(role)) continue;
    seen.add(id);
    fees.push({key:'fee:' + id, header:String(it.label || id), kind:'money'});
  }
  const ratios = REPORT_RATIOS.filter(r => fees.some(f => f.key === 'fee:' + r.key.split(':')[1]));
  const tail = [
    {key:'fee_total', header:'费用合计', kind:'money'},
    {key:'net_rate', header:'净利率', kind:'pct'},
    {key:'net', header:'净利', kind:'money'},
    {key:'net_cny', header:'净利', kind:'money'},
    {key:'alt_net', header:'净利（对比）', kind:'money'},
    {key:'alt_diff', header:'样品差额', kind:'money'},
  ];
  return REPORT_BASE.concat(fees, ratios, tail);
}

/* 报表列配置卡片：勾选要展示的列 + 自定义标题 + 合计行位置 */
/* 当前列表顺序：优先按保存过的 report_column_order，
   没存过就按目录默认顺序。新加的列（升级后多出来的）接在最后。 */
function reportColumnOrder(cat, c){
  const saved = Array.isArray(c.report_column_order) ? c.report_column_order : [];
  const byKey = new Map(cat.map(x => [x.key, x]));
  const out = [];
  for(const k of saved){ if(byKey.has(k)){ out.push(byKey.get(k)); byKey.delete(k); } }
  for(const x of cat){ if(byKey.has(x.key)) out.push(x); }
  return out;
}

function renderReportColumns(){
  const box = $('#rcols-body');
  if(!box) return;
  const cat = reportColumnCatalog();
  const c = cur();
  const ordered = reportColumnOrder(cat, c);
  // 显式数组（含空数组）= 按它来；null/undefined = 默认全选。
  // 关键：空数组要当成「全不选」，不能又回退成全选（否则取消全选没反应）。
  const sel = Array.isArray(c.report_columns) ? new Set(c.report_columns) : null;
  const titles = c.report_titles || {};
  const total = c.report_total_position === 'top' ? 'top' : 'bottom';
  box.innerHTML = ordered.map((col, i) => {
    const on = !sel || sel.has(col.key);
    const title = titles[col.key] || '';
    const up = i === 0 ? 'disabled' : '';
    const dn = i === ordered.length - 1 ? 'disabled' : '';
    return `<tr data-rk="${esc(col.key)}">
      <td class="kcell"><input type="checkbox" data-rcol="${esc(col.key)}" ${on ? 'checked' : ''}></td>
      <td class="kcell drag-handle" draggable="true" data-handle="${esc(col.key)}"
          title="按住这里上下拖，就能调整顺序">
        <span class="grip">⠿</span>
        <button class="mini" data-move="${esc(col.key)}" data-dir="-1" ${up} title="上移一格">↑</button>
        <button class="mini" data-move="${esc(col.key)}" data-dir="1" ${dn} title="下移一格">↓</button>
      </td>
      <td>${esc(col.header)}<div class="pid">${esc(col.key)}</div></td>
      <td><input type="text" data-rtitle="${esc(col.key)}" value="${esc(title)}" placeholder="留空用默认"></td>
      <td class="muted" style="font-size:12px">${REPORT_KIND_CN[col.kind] || col.kind}</td>
    </tr>`;
  }).join('');

  // 合计行：先决定「显不显示」，再决定「显示在哪」
  const showTotal = total !== 'none';
  const totalOn = $('#rcol-total-on');
  const posBox = $('#rcol-total-pos');
  if(totalOn){
    totalOn.checked = showTotal;
    totalOn.onchange = () => {
      c.report_total_position = totalOn.checked ? 'bottom' : 'none';
      autoSaveSoon();
      renderReportColumns();       // 重画一次，让位置单选跟着启停
    };
  }
  if(posBox){
    posBox.style.opacity = showTotal ? '1' : '.45';
    posBox.style.pointerEvents = showTotal ? '' : 'none';
  }
  document.querySelectorAll('input[name="rcol-total"]').forEach(r => {
    r.checked = (r.value === (showTotal ? total : 'bottom'));
    r.disabled = !showTotal;
    r.onchange = () => { c.report_total_position = r.value; autoSaveSoon(); };
  });

  // 全选开关：点一下全选 / 再点一下全部取消。
  // 勾选要立刻写回 cur().report_columns，否则重渲染会按旧值恢复，看起来「点了没反应」。
  const allCb = $('#rcols-all');
  allCb.checked = !sel || sel.size >= cat.length;
  allCb.onchange = () => {
    const on = allCb.checked;
    c.report_columns = on ? cat.map(x => x.key) : [];
    box.querySelectorAll('[data-rcol]').forEach(cb => cb.checked = on);
    renderReportColumns();
    autoSaveSoon();
  };
  box.querySelectorAll('[data-rcol]').forEach(cb => cb.onchange = () => {
    const keys = [...box.querySelectorAll('[data-rcol]')]
      .filter(x => x.checked).map(x => x.dataset.rcol);
    c.report_columns = keys;
    allCb.checked = keys.length >= cat.length;
    autoSaveSoon();
  });
  // 拖拽排序：按住「顺序」格上下拖，松手即存。
  // 比一个个点箭头快得多，箭头保留做微调。
  let dragKey = '';
  const rowsNow = () => [...box.querySelectorAll('tr[data-rk]')];
  box.querySelectorAll('[data-handle]').forEach(h => {
    h.addEventListener('dragstart', (ev) => {
      dragKey = h.dataset.handle;
      h.closest('tr').classList.add('dragging');
      try { ev.dataTransfer.effectAllowed = 'move'; } catch(_) {}
      try { ev.dataTransfer.setData('text/plain', dragKey); } catch(_) {}
    });
    h.addEventListener('dragend', () => {
      const tr = h.closest('tr');
      if(tr) tr.classList.remove('dragging');
      dragKey = '';
      c.report_column_order = rowsNow().map(x => x.dataset.rk);
      autoSaveSoon();
    });
  });
  rowsNow().forEach(tr => {
    tr.addEventListener('dragover', (ev) => {
      if(!dragKey) return;
      ev.preventDefault();
      const from = box.querySelector('tr[data-rk="' + CSS.escape(dragKey) + '"]');
      if(!from || from === tr) return;
      const r = tr.getBoundingClientRect();
      const before = (ev.clientY - r.top) < r.height / 2;
      tr.parentNode.insertBefore(from, before ? tr : tr.nextSibling);
    });
  });

  // 上移 / 下移：改的是**整张表的顺序**（含没勾的列），
  // 这样勾选状态一变，行的位置也不会跳。
  box.querySelectorAll('[data-move]').forEach(btn => btn.onclick = (ev) => {
    ev.preventDefault();
    const key = btn.dataset.move;
    const dir = Number(btn.dataset.dir);
    const list = reportColumnOrder(reportColumnCatalog(), c).map(x => x.key);
    const i = list.indexOf(key), j = i + dir;
    if(i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    c.report_column_order = list;
    renderReportColumns();
    autoSaveSoon();
  });

  box.querySelectorAll('[data-rtitle]').forEach(inp => inp.oninput = () => {
    const k = inp.dataset.rtitle;
    const v = inp.value.trim();
    if(!v) delete titles[k]; else titles[k] = v;
    autoSaveSoon();
  });
}

function renderFormulas(){
  const keys = Object.keys(fmls);
  $('#fbody').innerHTML = keys.map((k, i) => `<tr>
    <td><span style="font-size:12.5px">${esc(FORMULA_CN[k] || k)}</span>
        <div class="pid">${esc(k)}</div></td>
    <td class="expr-cell">
      <textarea data-fi="${i}" rows="2"
        style="font-family:ui-monospace,Consolas,monospace;width:100%;resize:vertical"></textarea>
      <div class="expr-tools">
        <button class="mini" data-fadd="${i}" title="在光标处加一行">＋ 加一行</button>
        <span class="expr-hint" data-fhint="${i}"></span>
      </div>
    </td>
    <td><button class="mini" data-fdel="${i}"
         style="padding:3px 7px;font-size:12px;color:var(--warn)">删</button></td>
  </tr>`).join('');
  $('#fbody').querySelectorAll('[data-fi]').forEach(inp => {
    inp.value = fmls[Object.keys(fmls)[Number(inp.dataset.fi)]] || '';
    inp.oninput = () => {
      fmls[Object.keys(fmls)[Number(inp.dataset.fi)]] = inp.value;
      checkFml(Number(inp.dataset.fi));
    };
    inp.onfocus = () => { lastFormulaInput = inp; };
    checkFml(Number(inp.dataset.fi));
  });
  $('#fbody').querySelectorAll('[data-fadd]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.fadd);
    const inp = $('#fbody').querySelector(`[data-fi="${i}"]`);
    if(!inp){ toast('先点一下要编辑的公式框'); return; }
    inp.focus();
    const s = inp.selectionStart ?? inp.value.length;
    const ins = '\n';
    inp.value = inp.value.slice(0, s) + ins + inp.value.slice(inp.selectionEnd ?? s);
    fmls[Object.keys(fmls)[i]] = inp.value;
    inp.selectionStart = inp.selectionEnd = s + ins.length;
    checkFml(i);
  });
  $('#fbody').querySelectorAll('[data-fdel]').forEach(b => b.onclick = () => {
    const k = Object.keys(fmls)[Number(b.dataset.fdel)];
    if(!confirm('删掉公式「' + (FORMULA_CN[k] || k) + '」？')) return;
    delete fmls[k];
    renderFormulas();
  });
}

/** 净利公式区的实时提示。跟参数表那套一样，只是变量来源不同。 */
function checkFml(i){
  const box = document.querySelector(`[data-fhint="${i}"]`);
  if(!box) return;
  const k = Object.keys(fmls)[i];
  const raw = String(fmls[k] || '').trim();
  if(!raw){ box.textContent = ''; box.className = 'expr-hint'; return; }
  const known = new Set();
  FIELD_VARS.forEach(v => {
    if(v.cn) known.add(v.cn);
    if(v.cn && v.cn.startsWith('本行')) known.add(v.cn.slice(2));
  });
  varRates.forEach(x => {
    const cn = rateCn(x);
    known.add(cn);
    [rateLabels && rateLabels[x], rateCnServer && rateCnServer[x], RATE_LABELS[x], x]
      .forEach(n => { const s = String(n || '').trim(); if(s) known.add(s); });
  });
  rowsOf().filter(x => x.id).forEach(x => known.add((x.label || x.id).trim()));
  // 订单表识别里配的候选标题也算认识（净利公式里可能直接引用列）
  Object.values(cur().order_fields || {}).forEach(v => {
    (Array.isArray(v) ? v : (v.titles || [])).forEach(t => {
      const s = String(t || '').trim();
      if(s) known.add(s);
    });
  });
  Object.keys(FORMULA_CN).forEach(x => known.add(FORMULA_CN[x]));
  ['gmv','paid','qty','orders','order_count','items','cost_cny','cost',
   'cost_unit','refund','commission','ads','net','gross_profit',
   'total_fees','net_rate'].forEach(x => known.add(x));

  const unknown = [];
  (raw.match(/[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*|[一-龥]+/g) || []).forEach(t => {
    const w = t.trim();
    if(!w || known.has(w)) return;
    if(/^params\.[A-Za-z0-9_]+$/.test(w)){
      const f = w.split('.')[1];
      if(varRates.includes(f)) return;
    }
    if(/^param\.[A-Za-z0-9_]+$/.test(w)) return;
    if(w.startsWith('本行') && known.has(w.slice(2))) return;
    if(!unknown.includes(w)) unknown.push(w);
  });
  if(unknown.length){
    box.className = 'expr-hint bad';
    box.textContent = '⚠ 不认识：' + unknown.join('、');
    return;
  }
  box.className = 'expr-hint';
  box.textContent = '＝ ' + raw;
}

function paramPayload(){
  return rowsOf().map(r => {
    const o = { id: r.id, label: r.label };
    if(r.role) o.role = r.role;
    o.expr = String(r.expr || '').trim();
    if(r.kinds !== null) o.kinds = r.kinds;
    if(r.enabled === false) o.enabled = false;
    return o;
  });
}

function collectByCountry(){
  const out = {};
  const rootFlag = !!(scopeData[""] && scopeData[""].exclude_zero_amount);
  scopeData && Object.entries(scopeData).forEach(([k, v]) => {
    if(!k) return;
    const one = { params: v.params, order_fields: v.order_fields, cost_fields: v.cost_fields,
                  ads_fields: v.ads_fields, affiliate_fields: v.affiliate_fields,
                  mapping_fields: v.mapping_fields };
    // 开关跟通用一样就不写，省得每个国家都留一条冗余
    if(!!v.exclude_zero_amount !== rootFlag) one.exclude_zero_amount = !!v.exclude_zero_amount;
    // 报表列配置：列勾选 / 自定义标题 / 合计位置，各国独立（和参数表一样）
    if(v.report_columns && v.report_columns.length) one.report_columns = v.report_columns;
    if(v.report_column_order && v.report_column_order.length) one.report_column_order = v.report_column_order;
    if(v.report_titles && Object.keys(v.report_titles).length) one.report_titles = v.report_titles;
    if(v.report_total_position) one.report_total_position = v.report_total_position;
    out[k] = one;
  });
  return out;
}

$('#btn-all-on').onclick = () => { rowsOf().forEach(r => r.kinds = KINDS.map(k => k.id)); renderParams(); };
$('#btn-all-off').onclick = () => { rowsOf().forEach(r => r.kinds = []); renderParams(); };
$('#btn-add').onclick = () => {
  const id = prompt('参数的英文标识（公式里用它引用这个结果，建议小写英文，如 order_service）');
  if(!id) return;
  if(rowsOf().some(r => r.id === id)){ toast('这个标识已经存在'); return; }
  const label = prompt('显示名称', id) || id;
  rowsOf().push({id, label, expr: 'orders * params.order_service', kinds: ['normal'], enabled: true});
  renderParams(); renderVars();
};

$('#btn-preview').onclick = async () => {
  const box = $('#diff'), body = $('#diff-body');
  box.classList.add('show'); body.innerHTML = '<div class="empty">计算中…</div>';
  try{
    const cur1 = cur();
    const r = await fetch('/api/config/preview', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: paramPayload(), country: scope,
                            by_country: collectByCountry(), formulas: fmls,
                            order_fields: cur1.order_fields, cost_fields: cur1.cost_fields,
                            exclude_zero_amount: !!cur1.exclude_zero_amount})});
    const j = await r.json();
    if(!j.ok){ body.innerHTML = '<div class="empty">' + esc(j.error || '试算失败') + '</div>'; return; }
    const b = j.before, a = j.after, d = j.delta;
    const cls = v => v > 0 ? 'up' : (v < 0 ? 'down' : 'muted');
    const sg = v => (v > 0 ? '+' : '') + fmt2(v);
    const lbl = j.fee_labels || {};
    const fd = Object.keys(a.fee_detail || {}).map(k =>
      `<tr><td>${esc(lbl[k] || k)}</td><td>${fmt2((b.fee_detail || {})[k] || 0)}</td>
        <td>${fmt2(a.fee_detail[k])}</td><td></td></tr>`).join('');
    body.innerHTML = `<table>
      <thead><tr><th style="width:34%"></th><th>当前配置</th><th>改后</th><th style="width:22%">变化</th></tr></thead>
      <tbody>
        <tr><td>GMV</td><td>${fmt2(b.gmv)}</td><td>${fmt2(a.gmv)}</td><td class="${cls(d.gmv)}">${sg(d.gmv)}</td></tr>
        <tr><td>商品成本</td><td>${fmt2(b.cost)}</td><td>${fmt2(a.cost)}</td><td class="${cls(d.cost)}">${sg(d.cost)}</td></tr>
        <tr><td>费用合计</td><td>${fmt2(b.fees)}</td><td>${fmt2(a.fees)}</td><td class="${cls(-d.fees)}">${sg(d.fees)}</td></tr>
        <tr><td><b>净利</b></td><td><b>${fmt2(b.net)}</b></td><td><b>${fmt2(a.net)}</b></td>
            <td class="${cls(d.net)}"><b>${sg(d.net)}</b></td></tr>
        <tr><td>净利率</td><td>${b.net_rate}%</td><td>${a.net_rate}%</td><td></td></tr>
        ${fd}
      </tbody></table>
      <div style="padding:12px 18px;font-size:12.5px;color:var(--text-3)">
        按 ${esc(j.country)} 的费率算。试算不会保存，满意再点「保存配置」。</div>`;
  }catch(e){ body.innerHTML = '<div class="empty">试算出错：' + esc(e.message) + '</div>'; }
};

/* 保存配置：抽成一个函数，手动保存和「改了自动存」共用同一份 payload。
   注意 payload 必须是**完整**的一份（参数表 + 公式 + 各国…）：这个接口是整体覆盖，
   只传部分字段会把没传的当空，冲掉参数表和公式。 */
function buildSavePayload(){
  const g = scopeData[""] || {params: [], order_fields: {}, cost_fields: {}};
  // 把「报表列」卡片当前的勾选 / 自定义标题 / 合计位置写回当前作用域
  const rc = cur();
  rc.report_columns = [...document.querySelectorAll('#rcols-body [data-rcol]')]
    .filter(x => x.checked).map(x => x.dataset.rcol);
  rc.report_titles = {};
  document.querySelectorAll('#rcols-body [data-rtitle]').forEach(x => {
    const v = x.value.trim(); if(v) rc.report_titles[x.dataset.rtitle] = v;
  });
  rc.report_total_position = (document.querySelector('input[name="rcol-total"]:checked') || {}).value || 'bottom';
  // 整张表的行顺序（含没勾的列）
  rc.report_column_order = [...document.querySelectorAll('#rcols-body [data-rk]')]
    .map(x => x.dataset.rk);
  return {params: g.params, by_country: collectByCountry(),
          formulas: fmls, order_fields: g.order_fields,
          cost_fields: g.cost_fields,
          ads_fields: g.ads_fields, affiliate_fields: g.affiliate_fields,
          mapping_fields: g.mapping_fields,
          exclude_zero_amount: !!g.exclude_zero_amount,
          report_columns: g.report_columns || [],
          report_column_order: g.report_column_order || [],
          report_titles: g.report_titles || {},
          report_total_position: g.report_total_position || 'bottom'};
}

async function saveConfig(silent){
  try{
    const payload = buildSavePayload();
    const r = await fetch('/api/config/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)});
    const j = await r.json();
    if(j.ok) paramsLoaded = false;
    if(!silent){
      const n = Object.keys(payload.by_country || {}).length;
      toast(j.ok ? ('已保存（通用 + ' + n + ' 个国家的单独设置），下次核算立即生效')
                 : ('保存失败：' + j.error));
      // 参数改了但报表还是旧数字，是这里最容易踩的空档：
      // 直接给一条「重算」的出路，不用人自己想起来去核算页。
      if(j.ok) showRecalcBar('计算参数刚改过，现有报表还是旧数字');
    } else if(!j.ok){
      toast('自动保存失败：' + j.error);
    }
  }catch(e){ if(!silent) toast('保存出错：' + e.message); }
}

/* 改了「报表列」就自动存：勾选本来就是最后一锤子的事，
   让人再回头点一次「保存配置」纯属多余，忘了点就会以为没保存成功。 */
let _autoSaveTimer = 0;
function autoSaveSoon(){
  clearTimeout(_autoSaveTimer);
  _autoSaveTimer = setTimeout(() => saveConfig(true), 700);
}

$('#btn-save').onclick = () => saveConfig(false);

/* ---------- 成本表 ---------- */
let costItems = [], costFiltered = [], costPageNo = 0;
const COST_PAGE = 100;

async function loadCosts(){
  try{
    const j = await (await fetch('/api/costs')).json();
    costItems = (j.items || []).map(x => ({...x}));
    $('#cost-hint').textContent = j.count + ' 个编码';
    $('#cost-stats').innerHTML =
      `<div class="it">编码总数<b>${j.count}</b></div>
       <div class="it">存放位置<b class="mono" style="font-size:12px">${esc(j.path || '')}</b></div>`;
    $('#cost-note').className = 'note';
    $('#cost-note').textContent = '';
    $('#cost-check').innerHTML = '';
    costPageNo = 0;
    renderCosts();
    loadCostFields();
  }catch(e){ toast('读取成本表失败'); }
}

function renderCosts(){
  const q = ($('#cost-search').value || '').trim().toLowerCase();
  costFiltered = q ? costItems.filter(x => x.code.toLowerCase().includes(q)) : costItems;
  const pages = Math.max(1, Math.ceil(costFiltered.length / COST_PAGE));
  if(costPageNo >= pages) costPageNo = pages - 1;
  const slice = costFiltered.slice(costPageNo * COST_PAGE, (costPageNo + 1) * COST_PAGE);
  $('#cbody').innerHTML = slice.length ? slice.map(it => `<tr>
      <td><span class="mono" style="font-size:12px">${esc(it.code)}</span></td>
      <td><input type="text" data-cost="${esc(it.code)}" value="${it.cost}"
            style="width:110px;font-family:ui-monospace,Consolas,monospace;text-align:right"></td>
      <td class="muted" style="font-size:12px">${esc(it.updated_at || '—')}</td>
      <td><button class="mini" data-cdel="${esc(it.code)}"
            style="padding:3px 7px;font-size:12px;color:var(--warn)">删</button></td>
    </tr>`).join('') : '<tr><td colspan="4"><div class="empty">没有匹配的编码</div></td></tr>';

  $('#cost-list-hint').textContent = costFiltered.length + ' 条'
    + (q ? '（筛选自 ' + costItems.length + '）' : '');
  $('#cost-page').textContent = '第 ' + (costPageNo + 1) + ' / ' + pages + ' 页';

  $('#cbody').querySelectorAll('[data-cost]').forEach(inp => inp.onchange = () => {
    const it = costItems.find(x => x.code === inp.dataset.cost);
    const v = Number(inp.value);
    if(!it) return;
    if(!Number.isFinite(v)){ inp.value = it.cost; toast('成本要填数字'); return; }
    it.cost = v; it._dirty = true;
    $('#cost-note').className = 'note show warn';
    $('#cost-note').textContent = '有改动还没保存，点「保存成本表」才会生效';
  });
  $('#cbody').querySelectorAll('[data-cdel]').forEach(b => b.onclick = () => {
    const code = b.dataset.cdel;
    if(!confirm('删掉编码「' + code + '」的成本？没成本的编码会计成 0，净利偏高')) return;
    costItems = costItems.filter(x => x.code !== code);
    renderCosts();
    $('#cost-note').className = 'note show warn';
    $('#cost-note').textContent = '已删除，点「保存成本表」才会生效';
  });
}

$('#cost-search').oninput = () => { costPageNo = 0; renderCosts(); };
$('#btn-cost-prev').onclick = () => { if(costPageNo > 0){ costPageNo--; renderCosts(); } };
$('#btn-cost-next').onclick = () => {
  const pages = Math.max(1, Math.ceil(costFiltered.length / COST_PAGE));
  if(costPageNo < pages - 1){ costPageNo++; renderCosts(); }
};

// 「下载导入模板」：浏览器里正常下载；窗口（WebView2）里不弹下载框，
// 改成直接存到报表目录，并告诉用户文件在哪。
$('#btn-cost-tpl').onclick = async (e) => {
  const api = (window.pywebview && window.pywebview.api) || null;
  // 窗口里：弹系统「另存为」对话框，选哪存哪——比手填路径省事
  if(api && api.save_template){
    e.preventDefault();
    const dir = ($('#cost-tpl-dir') ? $('#cost-tpl-dir').value.trim() : '');
    try{
      const p = await api.save_template(dir);
      if(p){
        if($('#cost-tpl-dir')) $('#cost-tpl-dir').value = String(p).replace(/[\\/][^\\/]*$/, '');
        toast('模板已保存到：' + p);
      }
    }catch(err){ toast('保存失败：' + err.message); }
    return;
  }
  // 兜底（老窗口 / 浏览器）：浏览器走 <a download>；窗口里交给后端存盘
  if(!window.pywebview) return;
  e.preventDefault();
  const dir = ($('#cost-tpl-dir') ? $('#cost-tpl-dir').value.trim() : '');
  try{
    await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({report:{template_dir: dir}})});
    const url = '/api/costs/template/save' + (dir ? ('?dir=' + encodeURIComponent(dir)) : '');
    const r = await fetch(url);
    const j = await r.json();
    toast(j.ok ? ('模板已保存到：' + j.path) : ('保存失败：' + (j.error || '')));
  }catch(err){ toast('保存失败：' + err.message); }
};

// 取数桥状态：端口 / 数据根 / 桥程序，并标出「是不是本程序起的」
async function loadBridgeStatus(){
  const box = $('#br-status');
  if(!box) return;
  let s = {}, ok = false;
  try{ s = await (await fetch('/api/bridge/status')).json(); ok = !!s.ok; }catch(e){ ok = false; }
  if(!ok){
    box.innerHTML = '<div><span class="muted">状态</span>　'
      + '<b style="color:var(--warn)">已关闭</b></div>'
      + '<div class="muted" style="font-size:12.5px;margin-top:4px">'
      + '取数、核算、计划任务都会停。点下面的「启动取数服务」就能拉起来，不用重开程序。</div>';
    syncBridgeButtons(false);
    return;
  }
  let appDir = '';
  try{
    const api = window.pywebview && window.pywebview.api;
    if(api && api.app_dir) appDir = await api.app_dir();
  }catch(e){}
  const same = appDir && s.exe_dir &&
    String(appDir).toLowerCase() === String(s.exe_dir).toLowerCase();
  const tag = appDir
    ? (same ? '<span class="tag">就是本程序</span>' : '<span class="tag warn">不是本程序</span>')
    : '';
  box.innerHTML =
    `<div><span class="muted">状态</span>　<b style="color:var(--accent)">运行中</b>　端口 ${esc(s.port || 8765)}</div>` +
    `<div><span class="muted">数据根</span>　<span class="mono" style="font-size:12px">${esc(s.root || '')}</span></div>` +
    `<div><span class="muted">桥程序</span>　<span class="mono" style="font-size:12px">${esc(s.exe_dir || '')}</span> ${tag}</div>`;
  syncBridgeButtons(true);
}

/** 桥在跑时按钮是「重启」，停了就变成「启动」——不然用户不知道关掉后怎么再开。 */
function syncBridgeButtons(up){
  const restart = $('#btn-bridge-restart');
  const stop = $('#btn-bridge-stop');
  if(restart){
    restart.textContent = up ? '重启用本程序的桥' : '启动取数服务';
    restart.classList.toggle('primary', true);
  }
  if(stop){
    stop.disabled = !up;
    stop.textContent = up ? '关闭取数桥' : '取数桥已关闭';
  }
}

// 重启用本程序的桥：先关掉别的桥，再起当前程序目录这个
$('#btn-bridge-restart').onclick = async () => {
  const api = (window.pywebview && window.pywebview.api) || null;
  if(!api || !api.restart_bridge){ toast('请在程序窗口里操作'); return; }
  const running = !$('#btn-bridge-stop') || !$('#btn-bridge-stop').disabled;
  if(running && !confirm('会先关掉当前正在跑的桥，再用本程序目录的桥重启。确定？')) return;
  toast(running ? '正在重启取数服务…' : '正在启动取数服务…');
  try{
    const msg = await api.restart_bridge();
    toast(msg);
    setTimeout(() => { try{ location.reload(); }catch(e){} }, 1200);
  }catch(err){ toast('重启失败：' + err.message); }
};

// 关闭取数桥：只停不启
$('#btn-bridge-stop').onclick = async () => {
  const api = (window.pywebview && window.pywebview.api) || null;
  if(!api || !api.stop_bridge){ toast('请在程序窗口里操作（浏览器里关不了桥）'); return; }
  if(!confirm('关闭取数服务后：\n'
    + '· 界面里的运行、核算、报表都会用不了\n'
    + '· 定时计划任务也不会再跑\n'
    + '\n随时可以在这一页点「启动取数服务」再开起来。确定关闭？')) return;
  try{
    const ok = await api.stop_bridge();
    toast(ok ? '取数服务已关闭。要恢复就点上面的「启动取数服务」' : '没有正在跑的桥');
    setTimeout(loadBridgeStatus, 600);
  }catch(err){ toast('关闭失败：' + err.message); }
};

if($('#br-status')){ loadBridgeStatus(); setInterval(loadBridgeStatus, 10000); }

$('#btn-cost-add').onclick = () => {
  const code = (prompt('商品编码（和订单里的「卖家 SKU」一致）') || '').trim();
  if(!code) return;
  if(costItems.some(x => x.code === code)){ toast('这个编码已经有了，直接搜出来改'); return; }
  const v = Number(prompt('成本（人民币，单个）', '0'));
  if(!Number.isFinite(v)) { toast('成本要填数字'); return; }
  costItems.push({code, cost: v, updated_at: '待保存', _dirty: true});
  $('#cost-search').value = code;
  costPageNo = 0;
  renderCosts();
  $('#cost-note').className = 'note show warn';
  $('#cost-note').textContent = '已新增，点「保存成本表」才会生效';
};

$('#btn-cost-save').onclick = async () => {
  const btn = $('#btn-cost-save');
  btn.disabled = true;
  try{
    const r = await fetch('/api/costs/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({items: costItems.map(x => ({code: x.code, cost: x.cost}))})});
    const j = await r.json();
    toast(j.ok ? ('已保存 ' + j.count + ' 个编码' + (j.removed ? '，删掉 ' + j.removed + ' 个' : ''))
               : ('保存失败：' + j.error));
    if(j.ok){ loadCosts(); showRecalcBar('成本表刚改过，现有报表还是旧数字'); }
  }catch(e){ toast('保存出错：' + e.message); }
  btn.disabled = false;
};

$('#cost-file').onchange = async ev => {
  const f = ev.target.files && ev.target.files[0];
  if(!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    const b64 = String(rd.result).split(',')[1] || '';
    $('#cost-note').className = 'note show';
    $('#cost-note').textContent = '正在导入 ' + f.name + '…';
    try{
      const r = await fetch('/api/costs/import', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name: f.name, b64})});
      const j = await r.json();
      if(j.ok){
        $('#cost-note').className = 'note show ok';
        $('#cost-note').innerHTML = `<b>导入完成</b>新增 ${j.added} 个编码，更新 ${j.updated} 个，`
          + `没变 ${j.unchanged} 个，现在共 ${j.total} 个。`;
        loadCosts();
        showRecalcBar('成本表刚导入过，现有报表还是旧数字');
      }else{
        $('#cost-note').className = 'note show err';
        $('#cost-note').textContent = '导入失败：' + j.error;
      }
    }catch(e){
      $('#cost-note').className = 'note show err';
      $('#cost-note').textContent = '导入出错：' + e.message;
    }
  };
  rd.readAsDataURL(f);
  ev.target.value = '';
};

/* 成本表列标题：编码列 / 成本列 */
async function loadCostFields(){
  try{
    const j = await (await fetch('/api/config')).json();
    const cf = j.cost_fields || {};
    const titles = k => (cf[k] && (cf[k].titles || cf[k])) || [];
    $('#cost-code-titles').value = titles('code').join(' | ');
    $('#cost-cost-titles').value = titles('cost').join(' | ');
  }catch(e){}
}

$('#btn-cost-fields').onclick = async () => {
  const split = v => v.split('|').map(x => x.trim()).filter(Boolean);
  try{
    const r = await fetch('/api/cost-fields', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code: split($('#cost-code-titles').value),
                            cost: split($('#cost-cost-titles').value)})});
    const j = await r.json();
    toast(j.ok ? '成本表列设置已保存，下次核算按新标题找列' : ('保存失败：' + j.error));
  }catch(e){ toast('保存出错：' + e.message); }
};

$('#btn-cost-check').onclick = async () => {
  const box = $('#cost-check');
  box.innerHTML = '<div class="empty">对一遍订单里的编码…</div>';
  try{
    const j = await (await fetch('/api/costs?check=1')).json();
    if(!j.ok){ box.innerHTML = '<div class="empty">' + esc(j.error || '检查失败') + '</div>'; return; }
    const miss = j.missing || [], der = j.derived || [];
    box.innerHTML = `<div class="stats-line" style="margin-top:16px">
        <div class="it">订单行<b>${j.order_lines}</b></div>
        <div class="it">涉及编码<b>${j.codes_in_orders}</b></div>
        <div class="it">缺成本<b style="color:var(--warn)">${j.missing_total}</b></div>
        <div class="it">推导命中<b>${j.derived_total}</b></div>
      </div>`
      + (miss.length ? `<p class="muted" style="font-size:12.5px;margin:12px 0 0">这些编码在订单里有、成本表里没有（缺的行数）：</p>
          <div class="cost-miss">${miss.map(x => `<span>${esc(x.code)} · ${x.lines} 行</span>`).join('')}</div>` : '')
      + (der.length ? `<p class="muted" style="font-size:12.5px;margin:12px 0 0">这些是按多件装/组合装规则推出来的，建议补一条准确成本：</p>
          <div class="cost-miss">${der.map(x => `<span>${esc(x.code)} · ${x.lines} 行</span>`).join('')}</div>` : '')
      + ((!miss.length && !der.length) ? '<p class="muted" style="margin:12px 0 0">全部编码都有准确成本。</p>' : '');
    $('#cost-note').className = 'note show';
    $('#cost-note').textContent = '数据来源：' + (j.source || '');
  }catch(e){ box.innerHTML = '<div class="empty">检查出错：' + esc(e.message) + '</div>'; }
};

async function loadRates(){
  try{
    const j = await (await fetch('/api/rates')).json();
    rateRows = j.countries || {};
    // 费率项从数据里自动收集：谁有这一列就渲染这一列，
    // 这样新增的费率项（比如订单服务费）不用改代码就能出现在表里。
    const set = new Set();
    Object.values(rateRows).forEach(c => {
      Object.keys(c).forEach(k => {
        if(RATE_META.includes(k) || k.startsWith('_')) return;
        if(c[k] !== null && typeof c[k] === 'object') return;
        set.add(k);
      });
    });
    feeFields = [...set];
    renderRates();
  }catch(e){ toast('读取费率失败'); }
}

function renderRates(){
  const names = Object.keys(rateRows);
  $('#rate-hint').textContent = names.length + ' 个国家 / ' + feeFields.length + ' 项费率';

  $('#rhead').innerHTML = '<tr>'
    + '<th style="min-width:86px">国家</th>'
    + '<th style="width:50px">币种</th>'
    + '<th style="width:104px">1 人民币<br><span class="muted" style="font-weight:400">= ? 本地</span></th>'
    + '<th style="width:104px">1 美元<br><span class="muted" style="font-weight:400">= ? 本地</span></th>'
    + '<th style="width:104px">发单费<br><span class="muted" style="font-weight:400">人民币</span></th>'
    + '<th style="width:96px">发单费(本地)<br><span class="muted" style="font-weight:400">自动算</span></th>'
    + feeFields.map(f => `<th style="width:92px">${rateCn(f)}
        <span class="muted" style="font-weight:400;cursor:pointer;color:var(--warn);margin-left:3px"
              data-delfee="${f}" title="删掉这一列">×</span>
        <div class="pid">${f}</div></th>`).join('')
    + '<th style="width:38px"></th></tr>';

  $('#rbody').innerHTML = names.map((name, i) => {
    const c = rateRows[name] || {};
    const cny = Number(c.cny_to_local) || 0;
    const dcny = Number(c.dispatch_fee_cny) || 0;
    const local = (cny && dcny) ? (dcny * cny) : (Number(c.dispatch_fee_local) || 0);
    const feeCells = feeFields.map(f => {
      const v = c[f];
      return `<td><input type="text" data-i="${i}" data-k="${f}"
        value="${v === undefined || v === null ? '' : v}"></td>`;
    }).join('');
    return `<tr>
      <td><input type="text" data-i="${i}" data-k="__name" value="${esc(name)}"></td>
      <td class="muted" style="text-align:center;font-size:12px">${esc(c.currency || '')}</td>
      <td><input type="text" data-i="${i}" data-k="cny_to_local" value="${c.cny_to_local ?? ''}"></td>
      <td><input type="text" data-i="${i}" data-k="usd_to_local" value="${c.usd_to_local ?? ''}"></td>
      <td><input type="text" data-i="${i}" data-k="dispatch_fee_cny" value="${c.dispatch_fee_cny ?? ''}"></td>
      <td class="muted" style="text-align:center;font-size:12px" data-local="${i}">${local ? local.toFixed(2) : '—'}</td>
      ${feeCells}
      <td><button class="mini" style="padding:3px 7px;font-size:12px;color:var(--warn)"
           data-delcountry="${i}">删</button></td></tr>`;
  }).join('');

  $('#rbody').querySelectorAll('input').forEach(inp => {
    inp.oninput = () => {
      const i = Number(inp.dataset.i), k = inp.dataset.k;
      const key = Object.keys(rateRows)[i];
      if(!key) return;
      const raw = inp.value.trim();
      if(k === '__name'){ rateRows[key].__newName = raw; return; }
      const n = Number(raw);
      rateRows[key][k] = raw === '' ? 0 : (Number.isFinite(n) ? n : raw);
      // 汇率或人民币发单费一改，本地值跟着重算给人看
      if(k === 'cny_to_local' || k === 'dispatch_fee_cny'){
        const c = rateRows[key];
        const v = (Number(c.cny_to_local) || 0) * (Number(c.dispatch_fee_cny) || 0);
        const cell = $('#rbody').querySelector(`[data-local="${i}"]`);
        if(cell) cell.textContent = v ? v.toFixed(2) : '—';
      }
    };
  });

  $('#rbody').querySelectorAll('[data-delcountry]').forEach(b => b.onclick = () => {
    const key = Object.keys(rateRows)[Number(b.dataset.delcountry)];
    if(!key) return;
    if(!confirm('删掉「' + key + '」这个国家的费率？')) return;
    delete rateRows[key];
    renderRates();
  });

  renderCnLabels();

  $('#rhead').querySelectorAll('[data-delfee]').forEach(b => b.onclick = () => {
    const f = b.dataset.delfee;
    if(!confirm('从所有国家删掉「' + (RATE_LABELS[f] || f) + '」这一项？')) return;
    Object.values(rateRows).forEach(c => { delete c[f]; });
    feeFields = feeFields.filter(x => x !== f);
    renderRates();
  });
}

/** 费率项的中文名：起了名，公式里就能直接写中文。 */
function renderCnLabels(){
  const body = $('#cnlbody');
  if(!body) return;
  const fields = [...new Set([...feeFields, ...Object.keys(rateLabels || {}),
                              'cny_to_local', 'usd_to_local',
                              'dispatch_fee_cny', 'order_service'])];
  body.innerHTML = fields.map(f => `<tr>
    <td><span class="mono" style="font-size:12px">${esc(f)}</span>
      ${RATE_LABELS[f] && RATE_LABELS[f] !== f ? `<div class="muted" style="font-size:11px">${esc(RATE_LABELS[f])}</div>` : ''}</td>
    <td><input type="text" data-cn="${esc(f)}" value="${esc((rateLabels && rateLabels[f]) || '')}"
      placeholder="${esc(RATE_LABELS[f] || '起个中文名')}"></td>
    <td class="mono" style="font-size:12px;color:var(--accent)">${esc((rateLabels && rateLabels[f]) || RATE_LABELS[f] || f)}</td>
  </tr>`).join('');
  body.querySelectorAll('[data-cn]').forEach(inp => {
    inp.oninput = () => {
      const f = inp.dataset.cn;
      const v = inp.value.trim();
      if(v) rateLabels[f] = v; else delete rateLabels[f];
      const row = inp.closest('tr');
      if(row) row.querySelector('td.mono:last-child').textContent = v || rateCn(f);
      renderVars();
    };
  });
}

$('#btn-rate-add').onclick = () => {
  const name = prompt('国家名（如：日本）');
  if(!name || rateRows[name]) { if(name) toast('这个国家已经有了'); return; }
  const row = {currency:'', cny_to_local:0, usd_to_local:0,
               dispatch_fee_cny:0, dispatch_fee_local:0, rates_verified:false};
  feeFields.forEach(f => { if(f !== 'dispatch_fee_cny') row[f] = 0; });
  rateRows[name] = row;
  renderRates();
};

$('#btn-fee-add').onclick = () => {
  const field = prompt('费率项字段名（英文小写，计算参数里用 params.字段名 引用）\n例如 order_service');
  if(!field) return;
  const f = field.trim().replace(/\s+/g, '_');
  if(feeFields.includes(f) || RATE_META.includes(f)){ toast('已经有这一项了'); return; }
  const label = prompt('显示名称', f);
  RATE_LABELS[f] = (label || f).trim() || f;
  Object.values(rateRows).forEach(c => { c[f] = 0; });
  feeFields.push(f);
  renderRates();
  toast('已加「' + RATE_LABELS[f] + '」，填好数值后点保存');
};

$('#btn-rate-save').onclick = async () => {
  const out = {};
  Object.entries(rateRows).forEach(([k, v]) => {
    const nk = v.__newName || k;
    const copy = {...v};
    delete copy.__newName;
    // 本地发单费不入库靠算，但顺手同步一份进文件，
    // 这样用记事本打开 rates.json 也能看懂实际收多少
    const cny = Number(copy.cny_to_local) || 0;
    const dcny = Number(copy.dispatch_fee_cny) || 0;
    if(cny && dcny) copy.dispatch_fee_local = Math.round(dcny * cny * 10000) / 10000;
    out[nk] = copy;
  });
  // 费率项的中文名：公式里直接写中文，不用记英文
  const labels = {};
  document.querySelectorAll('#cnlbody [data-cn]').forEach(inp => {
    const v = (inp.value || '').trim();
    if(v) labels[inp.dataset.cn] = v;
  });
  try{
    const r = await fetch('/api/rates', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({countries: out, labels})});
    const j = await r.json();
    toast(j.ok ? '费率已保存（' + j.countries + ' 个国家）' : ('保存失败：' + j.error));
    if(j.ok){ loadRates(); showRecalcBar('佣金费率刚改过，现有报表还是旧数字'); }
  }catch(e){ toast('出错：' + e.message); }
};

async function loadSettings(){
  try{
    const j = await (await fetch('/api/settings')).json();
    const all = j.settings || {};
    const s = all.report || {};
    $('#set-template').value = s.name_template || '{店铺}_{区间}';
    $('#set-dir').value = s.download_dir || '';
    if($('#set-tpl-dir')) $('#set-tpl-dir').value = s.template_dir || '';
    if($('#cost-tpl-dir')) $('#cost-tpl-dir').value = s.template_dir || '';
    $('#set-fx').value = (all.fx || {}).mode || 'auto';
    if($('#set-formula')) $('#set-formula').checked = !!s.with_formula;
  }catch(e){}
}

$('#btn-set-save').onclick = async () => {
  try{
    const dir = $('#set-dir').value.trim();
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        report: {name_template: $('#set-template').value.trim() || '{店铺}_{区间}',
                 download_dir: dir, auto_copy: !!dir,
                 template_dir: ($('#set-tpl-dir') ? $('#set-tpl-dir').value : '').trim(),
                 with_formula: !!($('#set-formula') && $('#set-formula').checked)},
        fx: {mode: $('#set-fx').value}})});
    const j = await r.json();
    toast(j.ok ? '设置已保存' : ('保存失败：' + j.error));
  }catch(e){ toast('出错：' + e.message); }
};

/* ---------------- 计划任务 ---------------- */
let PLAN = {rules: [], stores: [], missed: []};
let PLAN_EDIT = null;
const WD = ['周一','周二','周三','周四','周五','周六','周日'];

async function loadPlan(){
  const list = $('#plan-list');
  try{
    const j = await (await fetch('/api/schedules')).json();
    if(!j.ok) throw new Error(j.error || '读取失败');
    PLAN = j;
    renderPlan();
  }catch(e){
    if(list) list.innerHTML = '<div class="sc-empty">读取失败：' + esc(e.message) + '</div>';
  }
}

function dueText(r){
  if(!r.enabled) return '已关闭';
  if(!r.next_full) return '—';
  const d = r.next_full.slice(0, 10), t = r.next_full.slice(11);
  const today = iso(new Date());
  const tmr = iso(new Date(Date.now() + 86400000));
  if(d === today) return '今天 ' + t;
  if(d === tmr) return '明天 ' + t;
  return d.slice(5).replace('-', '/') + ' ' + t;
}

function freqIcon(freq){
  if(freq === 'daily')
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.4"/><path d="M12 7.6V12l3 1.8"/></svg>';
  if(freq === 'weekly')
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.6" width="18" height="16.4" rx="3"/><path d="M3 10h18M8 2.6v4M16 2.6v4"/><path d="M9 14.4h6M9 17.4h3.4"/></svg>';
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.6" width="18" height="16.4" rx="3"/><path d="M3 10h18M8 2.6v4M16 2.6v4"/><circle cx="12" cy="15.2" r="2.2"/></svg>';
}

function ruleCard(r){
  const off = !r.enabled;
  const names = (r.store_names || []).filter(Boolean);
  const storeTxt = names.length
    ? (names.length <= 3 ? names.join('、') : names.slice(0, 3).join('、') + ` 等 ${names.length} 家`)
    : '全部店铺';
  const dirTxt = r.download_dir || '默认（程序里的「报表」目录）';
  const lastTxt = r.last
    ? esc(r.last) + (r.last_range ? ' · ' + esc(r.last_range) : '')
    : '还没跑过';
  return `<div class="pc ${off ? 'off' : ''}">
    <div class="pc-head">
      <div class="pc-ic">${freqIcon(r.freq)}</div>
      <div class="pc-title">
        <div class="n">${esc(r.name || r.when_label)}</div>
        <div class="m">${esc(r.when_label)} · ${esc(r.range_label)}</div>
      </div>
      <label class="tgl" title="${off ? '已关闭' : '运行中，点一下关闭'}">
        <input type="checkbox" data-toggle="${esc(r.id)}" ${off ? '' : 'checked'}><i></i></label>
    </div>
    <div class="pc-body">
      <div class="pc-row"><span class="k">下次</span><span class="v"><span class="soon">${esc(dueText(r))}</span></span></div>
      <div class="pc-row"><span class="k">区间</span><span class="v"><b>${esc(r.range_label)}</b>　${esc(r.range_preview)}</span></div>
      <div class="pc-row"><span class="k">店铺</span><span class="v">${esc(storeTxt)}</span></div>
      <div class="pc-row"><span class="k">上次</span><span class="v">${lastTxt}</span></div>
      <div class="pc-row"><span class="k">报表</span><span class="v">${esc(dirTxt)}${r.with_formula === true ? ' · 带公式' : (r.with_formula === false ? ' · 不带公式' : '')}</span></div>
    </div>
    <div class="pc-foot">
      <button class="primary" data-run="${esc(r.id)}">立即跑</button>
      <button data-edit="${esc(r.id)}">编辑</button>
      <span class="grow"></span>
      <button class="danger" data-del="${esc(r.id)}">删除</button>
    </div>
  </div>`;
}

function renderPlan(){
  const list = $('#plan-list'), alertBox = $('#plan-alert');
  if(!list) return;
  const missed = PLAN.missed || [];
  if(alertBox){
    alertBox.innerHTML = missed.length ? `<div class="alert">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>
        <path d="M12 7.4v5.4"/><circle cx="12" cy="16.4" r=".9" fill="currentColor" stroke="none"/></svg>
      <div class="grow">有 <b>${missed.length}</b> 条计划到点没跑成：${
        missed.map(x => esc((x.name || x.id) + '（' + x.at + '）')).join('、')}</div>
      <button id="plan-catchup">一键补跑</button></div>` : '';
    const b = $('#plan-catchup');
    if(b) b.onclick = catchupAll;
  }
  if(!PLAN.rules.length){
    list.innerHTML = '<div class="sc-empty"><b>还没有计划</b>点下面的「新建计划」，'
      + '比如「每月 1 号 09:00 导上个月的全部店铺」</div>';
    return;
  }
  list.innerHTML = PLAN.rules.map(ruleCard).join('');
}

function planById(id){ return (PLAN.rules || []).find(x => x.id === id); }

const RULE_KEYS = ['id','name','enabled','freq','time','day','weekday',
  'period','offset','cut','cut_day','stores','download_dir','with_formula'];
const stripRule = r => RULE_KEYS.reduce((o, k) => { if(k in r) o[k] = r[k]; return o; }, {});

async function saveRules(rules, quiet){
  try{
    const j = await (await fetch('/api/schedules/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rules: (rules || []).map(stripRule)})})).json();
    if(!j.ok){ toast('保存失败：' + (j.error || '')); return null; }
    PLAN = j;
    renderPlan();
    if(!quiet) toast('已保存');
    return j;
  }catch(e){ toast('保存失败：' + e.message); return null; }
}

async function toggleRule(id, on){
  const rules = (PLAN.rules || []).map(r => ({...r, enabled: r.id === id ? on : r.enabled}));
  await saveRules(rules, true);
  toast(on ? '已开启' : '已关闭');
}

async function runRule(id){
  const r = planById(id);
  try{
    const j = await (await fetch('/api/schedules/run', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})})).json();
    if(j.ok){
      const d = (j.ran || [])[0] || {};
      toast(`已派发：${r ? r.name : ''} ${d.range || ''}（${d.stores || 0} 家店）`);
      setTimeout(loadPlan, 1200);
    }else toast(j.error || '派发失败');
  }catch(e){ toast('派发失败：' + e.message); }
}

async function delRule(id){
  const r = planById(id);
  if(!confirm(`删除计划「${r ? r.name : id}」？\n（只是不再定时跑，已经出的报表和数据都还在）`)) return;
  await saveRules((PLAN.rules || []).filter(x => x.id !== id));
  toast('已删除');
}

async function catchupAll(){
  try{
    const j = await (await fetch('/api/schedules/catchup', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({})})).json();
    toast(j.note || '已补跑');
    setTimeout(loadPlan, 1200);
  }catch(e){ toast('补跑失败：' + e.message); }
}

/* ----- 新建 / 编辑 ----- */

const PLAN_BLANK = () => ({
  id: '', name: '', enabled: true, freq: 'monthly', time: '09:00', day: 1, weekday: 0,
  period: 'month', offset: 1, cut: 'end', cut_day: 1, stores: [],
  download_dir: '', with_formula: null,
});

function openPlan(id){
  const base = id ? planById(id) : null;
  PLAN_EDIT = base ? JSON.parse(JSON.stringify(base)) : PLAN_BLANK();
  $('#pm-title').textContent = id ? '编辑计划' : '新建计划';
  $('#pm-sub').textContent = id ? (base.name || '') : '';
  renderPlanForm();
  $('#plan-modal').classList.add('show');
}

function closePlan(){
  $('#plan-modal').classList.remove('show');
  PLAN_EDIT = null;
}

function offsetLabel(period, n){
  if(period === 'month') return n === 0 ? '本月' : (n === 1 ? '上月' : (n === 2 ? '上上个月' : n + ' 个月前'));
  if(period === 'week')  return n === 0 ? '本周' : (n === 1 ? '上周' : (n === 2 ? '上上周' : n + ' 周前'));
  return n === 0 ? '今天' : (n === 1 ? '昨天' : (n === 2 ? '前天' : n + ' 天前'));
}

function renderPlanForm(){
  const r = PLAN_EDIT, f = r.freq, p = r.period, cut = r.cut;

  const dayField = f === 'monthly'
    ? `<div class="field"><label>每月几号</label>
         <input type="number" id="pm-day" min="1" max="31" value="${r.day}"></div>`
    : (f === 'weekly'
      ? `<div class="field"><label>每周几</label><select id="pm-wd">${
          WD.map((w, i) => `<option value="${i}" ${i === r.weekday ? 'selected' : ''}>${w}</option>`).join('')
        }</select></div>`
      : `<div class="field"><label>重复</label>
           <div style="padding:9px 0;color:var(--text-2);font-size:13px">每天都跑</div></div>`);

  const offsetOpts = [];
  const maxN = p === 'month' ? 12 : (p === 'week' ? 12 : 30);
  if(r.offset > maxN) r.offset = maxN;   // 从「按月」切到「按周」时别让选项对不上
  for(let i = 0; i <= maxN; i++){
    offsetOpts.push(`<option value="${i}" ${i === r.offset ? 'selected' : ''}>${offsetLabel(p, i)}</option>`);
  }

  const cutField = cut === 'day'
    ? `<div class="field"><label>${p === 'week' ? '截到周几' : '截到几号'}</label>
         <input type="number" id="pm-cutday" min="1" max="${p === 'week' ? 7 : 31}" value="${r.cut_day}"></div>`
    : '';

  const chips = (PLAN.stores || []).map(st => {
    const on = (r.stores || []).includes(st.id);
    const tip = st.shop_name && st.shop_name !== st.name ? `${st.name} · ${st.shop_name}` : st.name;
    return `<label class="${on ? 'on' : ''}" title="${esc(tip)}">
      <input type="checkbox" data-store="${esc(st.id)}" ${on ? 'checked' : ''}>${esc(st.name || st.id)}</label>`;
  }).join('') || '<span class="muted" style="font-size:12.5px">还没识别到店铺</span>';

  $('#pm-form').innerHTML = `
    <div class="field full"><label>计划名称</label>
      <input type="text" id="pm-name" placeholder="比如：每月 1 号导上月" value="${esc(r.name || '')}"></div>

    <div class="field"><label>多久跑一次</label>
      <select id="pm-freq">
        <option value="monthly" ${f === 'monthly' ? 'selected' : ''}>每月某天</option>
        <option value="weekly"  ${f === 'weekly' ? 'selected' : ''}>每周某天</option>
        <option value="daily"   ${f === 'daily' ? 'selected' : ''}>每天</option>
      </select></div>
    ${dayField}
    <div class="field"><label>几点跑</label>
      <input type="time" id="pm-time" value="${r.time}"></div>

    <div class="field"><label>算哪一段（周期）</label>
      <select id="pm-period">
        <option value="month" ${p === 'month' ? 'selected' : ''}>按月</option>
        <option value="week"  ${p === 'week' ? 'selected' : ''}>按周</option>
        <option value="day"   ${p === 'day' ? 'selected' : ''}>按天</option>
        <option value="last7" ${p === 'last7' ? 'selected' : ''}>最近 7 天</option>
        <option value="all"   ${p === 'all' ? 'selected' : ''}>全部数据</option>
      </select></div>
    <div class="field"><label>哪一期</label>
      <select id="pm-offset" ${p === 'last7' || p === 'all' ? 'disabled' : ''}>${offsetOpts.join('')}</select></div>

    <div class="field"><label>截到哪天</label>
      <select id="pm-cut">
        <option value="end"   ${cut === 'end' ? 'selected' : ''}>整期（到期末）</option>
        <option value="today" ${cut === 'today' ? 'selected' : ''}>到今天</option>
        <option value="day"   ${cut === 'day' ? 'selected' : ''}>第 N 天</option>
      </select></div>
    ${cutField}

    <div class="field full"><label>跑哪些店（不选 = 全部）</label>
      <div class="chk-row" id="pm-stores">${chips}</div></div>

    <div class="field full"><label>报表另存到（留空 = 只在程序的「报表」目录）</label>
      <input type="text" id="pm-dir" placeholder="D:/净利报表" value="${esc(r.download_dir || '')}"></div>

    <div class="field"><label>报表格要不要带公式</label>
      <select id="pm-wf">
        <option value=""  ${r.with_formula === null ? 'selected' : ''}>跟随设置页的开关</option>
        <option value="1" ${r.with_formula === true ? 'selected' : ''}>带公式</option>
        <option value="0" ${r.with_formula === false ? 'selected' : ''}>不带（数字是最终值）</option>
      </select></div>

    <div class="tip full">
      区间不存死日期，只存规则。例：<b>每月 1 号 09:00</b> + <b>按月 / 上月 / 整期</b>
      = 每月 1 号自动算上个月整月；<b>每月 3 号</b> + <b>按月 / 本月 / 到今天</b>
      = 每月 3 号算本月 1 号到 2 号。浏览器没开时任务会排队，等它打开自动补。</div>`;

  // 事件：改任意字段都重画预览
  const bind = (sel, key, cast) => {
    const el = $(sel);
    if(!el) return;
    el.oninput = el.onchange = () => {
      PLAN_EDIT[key] = cast ? cast(el.value) : el.value;
      if(key === 'freq' || key === 'period' || key === 'cut') renderPlanForm();
      updatePlanPreview();
    };
  };
  bind('#pm-name', 'name');
  bind('#pm-time', 'time');
  bind('#pm-day', 'day', Number);
  bind('#pm-wd', 'weekday', Number);
  bind('#pm-freq', 'freq');
  bind('#pm-period', 'period');
  bind('#pm-offset', 'offset', Number);
  bind('#pm-cut', 'cut');
  bind('#pm-cutday', 'cut_day', Number);
  bind('#pm-dir', 'download_dir');
  bind('#pm-wf', 'with_formula', v => v === '' ? null : v === '1');

  document.querySelectorAll('#pm-stores input[data-store]').forEach(cb => {
    cb.onchange = () => {
      const id = cb.dataset.store;
      const set = new Set(PLAN_EDIT.stores || []);
      if(cb.checked) set.add(id); else set.delete(id);
      PLAN_EDIT.stores = Array.from(set);
      cb.closest('label').classList.toggle('on', cb.checked);
      updatePlanPreview();
    };
  });

  updatePlanPreview();
}

let PLAN_PREV_T = null;
async function updatePlanPreview(){
  const box = $('#pm-prev');
  if(!box || !PLAN_EDIT) return;
  clearTimeout(PLAN_PREV_T);
  PLAN_PREV_T = setTimeout(async () => {
    try{
      const j = await (await fetch('/api/schedules/preview', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({rule: PLAN_EDIT})})).json();
      if(!j.ok) return;
      box.innerHTML = `触发：<b>${esc(j.when_label)}</b>　·　区间：<b>${esc(j.range_label)}</b>`
        + `　·　这次会算 <b>${esc(j.range_preview)}</b>`
        + (j.next ? `　·　下次 <b>${esc(j.next)}</b>` : '');
    }catch(e){ /* 预览失败不影响填写 */ }
  }, 180);
}

async function submitPlan(runNow){
  if(!PLAN_EDIT) return;
  const r = PLAN_EDIT;
  const rules = (PLAN.rules || []).map(x => ({...x}));
  const isNew = !r.id;
  if(isNew) rules.push(r);
  else{
    const i = rules.findIndex(x => x.id === r.id);
    if(i >= 0) rules[i] = r; else rules.push(r);
  }
  const btn = $(runNow ? '#pm-run' : '#pm-save');
  if(btn) btn.disabled = true;
  const j = await saveRules(rules, true);
  if(btn) btn.disabled = false;
  if(!j) return;
  closePlan();
  toast(runNow ? '已保存，正在派发…' : '已保存');
  if(runNow){
    const target = isNew ? (j.rules || [])[j.rules.length - 1] : (j.rules || []).find(x => x.id === r.id);
    if(target) await runRule(target.id);
  }
}

$('#plan-add') && ($('#plan-add').onclick = () => openPlan(null));
$('#pm-x') && ($('#pm-x').onclick = closePlan);
$('#pm-cancel') && ($('#pm-cancel').onclick = closePlan);
$('#pm-save') && ($('#pm-save').onclick = () => submitPlan(false));
$('#pm-run') && ($('#pm-run').onclick = () => submitPlan(true));
$('#plan-modal') && $('#plan-modal').addEventListener('click', e => {
  if(e.target.id === 'plan-modal') closePlan();
});
$('#go-plan') && ($('#go-plan').onclick = () => go('plan'));

document.addEventListener('click', e => {
  const t = e.target.closest('#plan-list [data-run],#plan-list [data-edit],#plan-list [data-del]');
  if(!t) return;
  if(t.dataset.run) runRule(t.dataset.run);
  else if(t.dataset.edit) openPlan(t.dataset.edit);
  else if(t.dataset.del) delRule(t.dataset.del);
});
document.addEventListener('change', e => {
  const cb = e.target.closest('#plan-list input[data-toggle]');
  if(cb) toggleRule(cb.dataset.toggle, cb.checked);
});

async function loadSelfcheck(){
  try{
    const j = await (await fetch('/api/selfcheck')).json();
    renderSelfcheck(j.last || {});
  }catch(e){ /* 读不到就先不显示 */ }
}

function renderSelfcheck(rep){
  const box = $('#sc-actions');
  const when = $('#sc-when');
  if(!box) return;
  if(when) when.textContent = rep.at ? ('上次体检：' + rep.at) : '还没体检过';
  const acts = rep.actions || [];
  if(!acts.length){
    box.innerHTML = '<div class="note show ok" style="margin:0">一切正常，没发现需要处理的东西。</div>';
    return;
  }
  box.innerHTML = '<div class="note show ok" style="margin:0">'
    + (rep.crashed ? '<b>上次不是正常退出</b>（断电/强杀），已做完整检查。<br>' : '')
    + acts.map(a => '· ' + esc(a)).join('<br>') + '</div>';
}

const _scBtn = $('#btn-selfcheck');
if(_scBtn) _scBtn.onclick = async () => {
  _scBtn.disabled = true;
  const old = _scBtn.textContent;
  _scBtn.textContent = '体检中…';
  try{
    const r = await fetch('/api/selfcheck', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
    const j = await r.json();
    if(j.ok){ renderSelfcheck(j.last || {}); toast('体检完成'); }
    else toast(j.error || '体检失败');
  }catch(e){ toast('体检失败：' + e.message); }
  _scBtn.disabled = false;
  _scBtn.textContent = old;
};

async function loadDebug(){
  loadSelfcheck();
  try{
    const j = await (await fetch('/api/debug')).json();
    $('#dbg-env').innerHTML = Object.entries(j.paths || {}).map(([k, v]) =>
      `<div>${esc(k)}</div><div class="mono" style="word-break:break-all">${esc(v)}</div>`).join('');
    const lines = (j.task_log || []).slice(-80);
    $('#dbg-log-hint').textContent = lines.length ? '最近 ' + lines.length + ' 行' : '';
    $('#dbg-log').textContent = lines.length ? lines.join('\n') : '（还没有日志）';
    const caps = j.captures || [];
    $('#dbg-cap').innerHTML = caps.length ? '<table><tbody>' + caps.map(c => `<tr>
      <td>${esc(c.name)}</td><td class="muted" style="width:110px">${c.size_mb} MB</td>
      <td class="muted" style="width:130px">${c.at}</td></tr>`).join('') + '</tbody></table>'
      : '<div class="empty">暂无抓包文件</div>';
  }catch(e){}
}

go((location.hash || '#/overview').replace('#/', '') || 'overview');
loadTrend();
setInterval(() => { if(!$('#view-overview').hidden) loadStatus(); }, 20000);
</script>
</body>
</html>
"""
