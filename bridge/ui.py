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
from datetime import datetime
from pathlib import Path

from src import paths

STATE: dict = {"running": False, "last_run": None}


# ---------------- 数据 ----------------

def _ext_heartbeat() -> str:
    log = paths.DATA / "capture" / "task.log"
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return ""
    for ln in reversed(lines[-100:]):
        if len(ln) >= 19 and ln[4] == "-" and ln[13] == ":":
            return ln[:19]
    return ""


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
            st = p.stat()
            rel = p.relative_to(paths.OUTPUT)
            out.append({
                "name": p.name,
                # rel：相对 output 目录的完整路径。不同店的报表可能重名，
                # 勾选下载时靠它区分，只靠文件名会张冠李戴。
                "rel": str(rel).replace("\\", "/"),
                "group": str(rel.parent) if str(rel.parent) != "." else "",
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
            "size_mb": round(r["size"] / 1048576, 2),
            "at": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "stamp": int(r["mtime"]),
        }
        for r in _all_reports()
    ]}


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
                 "kind": f.get("kind") or "other"}
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
                                    "batch": d.name})
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


def render_html() -> str:
    return _TEMPLATE.replace("__STAMP__", datetime.now().strftime("%Y-%m-%d %H:%M"))


# ---------------- 页面 ----------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>净利助手</title>
<style>
  :root{
    --bg:#f7f7f5; --panel:#fff; --line:#e7e6e0; --line-soft:#f0efe9;
    --text:#191917; --text-2:#5f5e58; --text-3:#8f8e87;
    --accent:#0f6e56; --accent-hover:#0c5b47; --accent-soft:#e8f4ef;
    --warn:#9c3d1e; --warn-soft:#fbeee8; --radius:14px;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased}
  .app{display:flex;height:100vh;overflow:hidden}
  .side{width:212px;flex:0 0 212px;background:var(--panel);
    border-right:1px solid var(--line);display:flex;flex-direction:column;padding:22px 0 14px}
  .brand{display:flex;align-items:center;gap:10px;padding:0 18px 22px}
  .brand .mark{width:32px;height:32px;border-radius:9px;background:var(--accent);
    display:flex;align-items:center;justify-content:center;flex:0 0 32px}
  .brand .name{font-size:15px;font-weight:500}
  .brand .sub{font-size:11px;color:var(--text-3);margin-top:-2px}
  .nav{padding:0 10px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
  .nav a{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;
    color:var(--text-2);text-decoration:none;font-size:13.5px;transition:background .12s,color .12s}
  .nav a:hover{background:var(--line-soft);color:var(--text)}
  .nav a.on{background:var(--accent-soft);color:var(--accent);font-weight:500}
  .nav a svg{flex:0 0 16px;opacity:.85}
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
    margin-bottom:20px;overflow:hidden}
  .card > h2{font-size:13.5px;font-weight:500;margin:0;padding:15px 22px;
    border-bottom:1px solid var(--line-soft);display:flex;align-items:center;gap:10px}
  .card > h2 .hint{margin-left:auto;font-weight:400;font-size:12.5px;color:var(--text-3)}
  .card > .body{padding:22px}
  .card > .body.tight{padding:0}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px}
  .stat .k{font-size:12.5px;color:var(--text-2);margin-bottom:8px;display:flex;align-items:center;gap:6px}
  .stat .v{font-size:19px;font-weight:500;letter-spacing:-.2px}
  .stat .v.small{font-size:14px;font-weight:400}
  .stat .s{font-size:12px;color:var(--text-3);margin-top:4px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
  .dot.on{background:#1d9e75} .dot.off{background:#c4c3bb}
  .field{display:flex;flex-direction:column;gap:7px}
  .field label{font-size:12.5px;color:var(--text-2)}
  .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
  input[type=date],select,input[type=text],input[type=number]{
    font:inherit;font-size:13.5px;padding:9px 12px;border:1px solid var(--line);
    border-radius:9px;background:var(--panel);color:var(--text);min-width:140px}
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  button{font:inherit;font-size:13.5px;padding:9px 18px;border-radius:9px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);
    cursor:pointer;transition:background .12s,border-color .12s}
  button:hover{background:var(--line-soft)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:500}
  button.primary:hover{background:var(--accent-hover)}
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
  th{font-size:12px;font-weight:500;color:var(--text-2);background:#fcfcfa}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#fcfcfa}
  .muted{color:var(--text-3)}
  .empty{padding:44px 22px;text-align:center;color:var(--text-3);font-size:13.5px}
  .empty b{display:block;color:var(--text-2);font-weight:500;margin-bottom:4px}
  .mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:12.5px}
  .note{display:none;margin-top:18px;padding:13px 16px;border-radius:10px;font-size:13px;line-height:1.6}
  .note.show{display:block}
  .note.ok{background:var(--accent-soft);color:#0a5442}
  .note.err{background:var(--warn-soft);color:var(--warn)}
  .tbl-wrap{overflow-x:auto}
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
  .snap-item:hover{background:#fcfcfa}
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
  .pid{font-size:11px;color:var(--text-3);margin-top:3px;font-family:ui-monospace,Consolas,monospace}
  .tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
    background:var(--accent-soft);color:var(--accent);margin-left:6px;vertical-align:1px}
  .tag.warn{background:var(--warn-soft);color:var(--warn)}
  .tag.gray{background:var(--line-soft);color:var(--text-3)}
  .tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  /* 计算参数页：可点选的变量表 */
  .vars{display:flex;flex-wrap:wrap;gap:8px}
  .vars .v{display:flex;align-items:baseline;gap:7px;padding:7px 12px;border:1px solid var(--line);
    border-radius:9px;background:#fcfcfa;cursor:pointer;transition:.12s}
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
  .modal .box .content{overflow-y:auto;padding-bottom:10px}
  .bars{display:flex;align-items:flex-end;gap:4px;height:110px;padding:0 6px}
  .bars .b{flex:1 1 0;max-width:56px;background:#9fd3c1;border-radius:3px 3px 0 0;min-height:2px;transition:background .12s}
  .bars .b:hover{background:var(--accent)}
  .bars .b.neg{background:#f0c4b4}
  .legend{font-size:11.5px;color:var(--text-3);padding:8px 6px 0;display:flex;gap:14px}
  .toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%) translateY(8px);
    background:#191917;color:#fff;padding:11px 22px;border-radius:10px;font-size:13px;
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
    text-align:center;background:#fcfcfa;transition:.15s;cursor:pointer}
  .dropzone:hover{border-color:var(--accent);background:var(--accent-soft,#f0f7f5)}
  .dropzone.over{border-color:var(--accent);background:var(--accent-soft,#f0f7f5);
    box-shadow:0 0 0 3px rgba(15,110,86,.08)}
  .dropzone b{font-size:13.5px}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="mark">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <rect x="2.5" y="10" width="3" height="5.5" rx="1" fill="#fff"/>
          <rect x="7.5" y="6.5" width="3" height="9" rx="1" fill="#fff"/>
          <rect x="12.5" y="3" width="3" height="12.5" rx="1" fill="#fff"/>
        </svg>
      </div>
      <div><div class="name">净利助手</div><div class="sub">TikTok 店铺</div></div>
    </div>
    <nav class="nav" id="nav">
      <a href="#/overview" data-view="overview">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/>
          <rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/>
        </svg><span>总览</span></a>
      <a href="#/run" data-view="run">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="6"/><path d="M6.6 5.6l4 2.4-4 2.4z" stroke-linejoin="round"/>
        </svg><span>开始核算</span></a>
      <a href="#/import" data-view="import">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M8 2.6v7.2M5.2 7l2.8 2.8L10.8 7" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M2.8 11.2v1.4a1.6 1.6 0 001.6 1.6h7.2a1.6 1.6 0 001.6-1.6v-1.4" stroke-linecap="round"/>
        </svg><span>导入数据</span></a>
      <a href="#/reports" data-view="reports">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M4 2h5l3 3v9a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z" stroke-linejoin="round"/>
          <path d="M9 2v3.5h3.5M5.5 8.5h5M5.5 11h3.5" stroke-linecap="round"/>
        </svg><span>报表</span></a>
      <a href="#/stores" data-view="stores">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M2.5 6.5V13a1 1 0 001 1h9a1 1 0 001-1V6.5" stroke-linejoin="round"/>
          <path d="M2 6.5l1.4-3.2a1 1 0 01.9-.6h7.4a1 1 0 01.9.6L14 6.5" stroke-linejoin="round"/>
          <path d="M6 14v-3.5h4V14" stroke-linejoin="round"/>
        </svg><span>店铺</span></a>
      <div class="gap"></div>
      <a href="#/costs" data-view="costs">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M2.5 4.2L8 2l5.5 2.2v7.6L8 14l-5.5-2.2z" stroke-linejoin="round"/>
          <path d="M2.5 4.2L8 6.5l5.5-2.3M8 6.5V14" stroke-linejoin="round"/>
        </svg><span>成本表</span></a>
      <a href="#/rates" data-view="rates">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="6"/>
          <path d="M8 4.6v7M9.8 6.2c0-.8-.8-1.2-1.8-1.2s-1.7.5-1.7 1.2c0 1.8 3.5.9 3.5 2.7 0 .8-.8 1.2-1.8 1.2s-1.8-.4-1.8-1.2"
                stroke-linecap="round"/>
        </svg><span>费率</span></a>
      <a href="#/params" data-view="params">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M2 4.5h12M2 11.5h12" stroke-linecap="round"/>
          <circle cx="6" cy="4.5" r="1.8" fill="var(--panel)"/><circle cx="10.5" cy="11.5" r="1.8" fill="var(--panel)"/>
        </svg><span>计算参数</span></a>

      <a href="#/settings" data-view="settings">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="2.2"/>
          <path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7L3.6 3.6"
                stroke-linecap="round"/>
        </svg><span>设置</span></a>
      <a href="#/help" data-view="help">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="6.2"/>
          <path d="M6.3 6.2a1.8 1.8 0 113.1 1.3c-.6.5-1.4.9-1.4 1.9" stroke-linecap="round"/>
          <circle cx="8" cy="11.8" r=".7" fill="currentColor" stroke="none"/>
        </svg><span>使用帮助</span></a>
      <a href="#/debug" data-view="debug">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 6h6M5 9.5h4" stroke-linecap="round"/>
        </svg><span>调试</span></a>
    </nav>
    <div class="foot">
      <div class="row"><span>版本</span><span>0.21.36</span></div>
      <div class="row"><span>店铺</span><span id="foot-stores">—</span></div>
    </div>
  </aside>

  <main class="main">
    <section class="page" id="view-overview">
      <div class="head"><h1>总览</h1>
        <p>数据拿没拿到、上次算出多少、接下来该做什么，都在这。</p></div>
      <div class="stats" id="stats"></div>
      <div class="card"><h2>下一步做什么</h2><div class="body"><div class="row">
        <button class="primary" onclick="go('run')">开始核算</button>
        <button onclick="go('stores')">管理店铺</button>
        <button onclick="go('params')">调整计算口径</button>
        <button onclick="go('reports')">查看报表</button>
      </div></div></div>
      <div class="card"><h2>最近的净利走势<span class="hint" id="trend-hint"></span></h2>
        <div class="body" id="trend"><div class="empty">还没有核算记录</div></div></div>
    </section>

    <section class="page" id="view-run" hidden>
      <div class="head"><h1>开始核算</h1>
        <p>用已经下载到本机的订单数据出报表。选好店铺和日期，点一下就行。
           没有自动取到数据？去左栏<b>「导入数据」</b>传你自己导出的表。</p></div>
      <div class="card"><h2>核算范围</h2><div class="body">
        <div class="chips" id="chips">
          <button data-k="month">本月</button><button data-k="lastmonth">上月</button>
          <button data-k="d7">最近 7 天</button><button data-k="d30">最近 30 天</button>
          <button data-k="all" class="on">全部数据</button></div>
        <div class="row">
          <div class="field" style="flex:1;min-width:340px"><label>用哪次数据（可多选合并）</label>
            <div class="snap-list" id="snap-list"><div class="empty">读取中…</div></div>
            <div class="chips tight" id="snap-chips">
              <button data-sel="latest">仅最新</button>
              <button data-sel="all">全部</button>
              <button data-sel="last3">近 3 次</button>
              <button data-sel="none">不指定</button></div>
          </div>
          <div class="field"><label>店铺</label>
            <select id="store" style="min-width:200px"><option value="">全部店铺（逐个核算）</option></select></div>
          <div class="field"><label>开始日期</label><input type="date" id="dfrom"></div>
          <div class="field"><label>结束日期</label><input type="date" id="dto"></div>
          <button class="primary" id="btn-run">开始核算</button></div>
        <div style="margin-top:10px">
          <label class="switch">
            <input type="checkbox" id="with-formula">
            <span><b>导出带公式的报表</b>
              <em>明细页会多出「订单类别 / 是否样品 / 净利贡献(含样品) / 净利贡献(去样品)」四列；
                汇总页末尾的净利用 SUMIFS 引用明细，改「口径」页第一行的开关（含样品 / 去除样品）
                就能在 Excel 里直接切换口径，不必回程序重算。</em></span>
          </label>
        </div>
        <div class="note" id="run-note"></div>
      </div></div>
      <div class="card"><h2>会用到哪些数据<span class="hint" id="run-files-hint"></span></h2>
        <div class="body tight" id="run-files"><div class="empty">读取中…</div></div>
        <div class="body" style="border-top:1px solid var(--line-soft);padding-top:12px">
          <div class="row" style="gap:12px;align-items:center">
            <button class="ghost" style="width:auto;padding:7px 16px;margin:0" onclick="go('import')">＋ 导入我自己导出的表</button>
            <span class="muted" style="font-size:12.5px">自动取数没拿到？去「导入数据」页直接传订单 / 广告 / 联盟佣金 / 链接资料，算法完全一样</span>
          </div>
        </div></div>
    </section>

    <section class="page" id="view-import" hidden>
      <div class="head"><h1>导入数据</h1>
        <p>不经过插件也能算：你自己在店铺后台把表导出来，在这里传上来。
           订单 / 广告 / 联盟佣金 / 链接资料都收，之后跟自动取到的数据<b>走同一套算法</b>。</p></div>

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
            <button class="primary" onclick="go('run')">传完了，去核算</button>
            <button onclick="go('stores')">管理店铺</button>
          </div>
        </div></div>
    </section>

    <section class="page" id="view-reports" hidden>
      <div class="head"><h1>报表</h1>
        <p>点「查看」直接在界面里看汇总和费用拆解，不用打开 Excel；点「下载」存到本地。</p></div>
      <div class="card"><h2>全部报表<span class="hint" id="rep-hint"></span></h2>
        <div class="body" style="padding-bottom:0">
          <div class="chips" id="rep-filter"><button data-store="" class="on">所有报表</button></div>
          <div class="tools" style="margin:10px 0 0">
            <button class="mini" id="rep-all">全选</button>
            <button class="mini" id="rep-none">取消全选</button>
            <button class="primary" id="rep-dl">下载勾选的</button>
            <button id="rep-dl-all">下载这个店铺的全部</button>
          </div>
          <p class="muted" style="font-size:12.5px;margin:8px 0 12px">
            报表按店铺分目录存放（<span class="mono">output\店名\</span>）。
            勾几份就下载几份，打成一个 zip，解压后仍按店铺分着。
          </p>
        </div>
        <div class="body tight" id="rep-list"><div class="empty">加载中…</div></div>
      </div>
    </section>

    <section class="page" id="view-stores" hidden>
      <div class="head"><h1>店铺</h1>
        <p>在战斧里登录过的店铺会自动出现在这里。名字可以自己改，只有你自己看得到——底下那串 ID 才是唯一标识。</p></div>
      <div class="card"><h2>所有店铺<span class="hint" id="store-hint"></span></h2>
        <div class="body tight" id="store-list"><div class="empty">加载中…</div></div></div>
      <div class="card"><h2>链接资料（款式编码）<span class="hint">报表上那列「款式编码」靠它</span></h2>
        <div class="body">
          <p class="muted" style="font-size:12.5px;margin:0 0 12px">
            在店铺后台点「批量编辑」下载<b>链接资料</b>（商品信息表），在这里传上来。
            程序会读出每条链接下的所有款式编码，报表里那条链接就显示成
            <span class="mono">V1/V2/V61</span> 这样的样子。<br>
            店铺后台下载的表<b>没有款式编码那一列</b>，程序会从商家 SKU 推导
            （<span class="mono">V27-White-10Pcs</span> → <span class="mono">V27</span>），
            跟你手工维护的那列效果一致。
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
            <div style="padding:12px 16px;background:#fcfcfa;border-bottom:1px solid var(--line-soft)">
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
            <div style="font-size:13px;color:var(--text-2)">
              合计行位置：
              <label style="margin:0 4px;cursor:pointer"><input type="radio" name="rcol-total" value="bottom" checked> 表格底部</label>
              <label style="margin:0 4px;cursor:pointer"><input type="radio" name="rcol-total" value="top"> 表格顶部（首行）</label>
            </div>
          </div>
          <div class="tbl-wrap">
            <table class="cfg-tbl" id="rcoltbl"><thead><tr>
              <th style="width:54px">展示</th>
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
          <label>核算完自动复制到这个文件夹（留空则只在程序目录里）</label>
          <input type="text" id="set-dir" placeholder="D:\净利报表"></div>
        <div class="field" style="max-width:520px;margin-top:12px">
          <label>「下载导入模板」存到这个文件夹（留空则存到程序的「报表」目录）</label>
          <input type="text" id="set-tpl-dir" placeholder="D:\净利模板"></div>
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

      <div class="card"><h2>自动运行<span class="hint" id="auto-hint">到点自动抓数 → 核算 → 出报表</span></h2>
        <div class="body">
          <label class="switch">
            <input type="checkbox" id="auto-on">
            <span><b>开启自动运行</b>
              <em>到点后程序自动派任务给浏览器扩展：抓数据、算净利、出报表。
                浏览器关着也能跑——任务会排队，等扩展下次打开时补上。</em></span>
          </label>
          <div class="row" style="gap:14px;align-items:flex-end;flex-wrap:wrap;margin-top:14px">
            <div class="field" style="width:150px"><label>频率</label>
              <select id="auto-freq">
                <option value="daily">每天</option>
                <option value="weekly">每周某天</option></select></div>
            <div class="field" style="width:110px" id="auto-wd-field" hidden><label>星期几</label>
              <select id="auto-weekday">
                <option value="0">周一</option><option value="1">周二</option>
                <option value="2">周三</option><option value="3">周四</option>
                <option value="4">周五</option><option value="5">周六</option>
                <option value="6">周日</option></select></div>
            <div class="field" style="width:120px"><label>几点跑</label>
              <input type="time" id="auto-time" value="09:00"></div>
            <div class="field" style="width:180px"><label>算哪个区间</label>
              <select id="auto-range">
                <option value="last_month">上个月</option>
                <option value="month">本月到今天</option>
                <option value="last7">最近 7 天</option>
                <option value="yesterday">昨天</option>
                <option value="all">全部数据</option></select></div>
            <div class="field" style="flex:1;min-width:240px"><label>对哪些店跑（不选=全部）</label>
              <select id="auto-stores" multiple size="3"
                      style="width:100%;min-height:64px"></select></div>
          </div>
          <div class="row" style="gap:14px;align-items:flex-end;flex-wrap:wrap;margin-top:12px">
            <div class="field" style="flex:1;min-width:300px"><label>报表下载到哪儿（留空=只在程序目录）</label>
              <input type="text" id="auto-dir" placeholder="D:\净利报表"></div>
            <label class="switch" style="width:auto">
              <input type="checkbox" id="auto-formula">
              <span><b>报表带公式</b><em>同「报表保存位置」里的设置</em></span>
            </label>
            <button class="ghost" id="auto-now" style="width:auto;padding:7px 16px;margin:0">现在跑一次</button>
          </div>
          <div class="note" id="auto-note"></div>
          <p class="muted" style="font-size:12.5px;margin:12px 0 0" id="auto-next"></p>
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
          <div>第 1 步</div><div>打开<b>战斧浏览器</b></div>
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
          「重启用本程序的桥」会先关掉别的桥，再起当前程序目录这个；<br>
          「关闭取数桥」只停不启，之后双击程序文件夹里的「启动桥.bat」再开。
        </p>
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

function go(view){
  document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'view-' + view);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  if(view === 'reports') loadReports();
  if(view === 'params'){ loadParams(); presetScopes('ps'); loadPresets('ps'); }
  if(view === 'rates'){ loadRates(); presetScopes('pr'); loadPresets('pr'); }
  if(view === 'costs') loadCosts();
  if(view === 'settings') loadSettings();
  if(view === 'debug') loadDebug();
  if(view === 'stores') loadStores();
  if(view === 'run'){ loadRunFiles(); loadSnapshots(); }
  if(view === 'import'){ loadImportOptions(); loadImportFiles(); }
  if(view === 'overview'){ loadStatus(); loadTrend(); }
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
        <div class="v small">${j.ext_last ? j.ext_ago : '无记录'}</div>
        <div class="s">${j.ext_last ? j.ext_last.slice(11) : '在战斧里打开扩展面板'}</div></div>
      <div class="stat"><div class="k">店铺</div>
        <div class="v">${j.store_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">家</span></div>
        <div class="s">待核算 ${j.inbox_count} 个文件</div></div>
      <div class="stat"><div class="k">已生成报表</div>
        <div class="v">${j.report_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">份</span></div>
        <div class="s">在「报表」页可查看</div></div>`;
    $('#foot-stores').textContent = j.store_count + ' 家';
    storesCache = j.stores || [];
    const sel = $('#store');
    if(sel){
      sel.innerHTML = '<option value="">全部店铺（逐个核算）</option>';
      storesCache.forEach(s => {
        const o = document.createElement('option');
        o.value = s.id;
        o.textContent = (s.name || s.id) + (s.country ? '（' + s.country + '）' : '');
        sel.append(o);
      });
    }
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

function setRange(key){
  const now = new Date(); let from = '', to = '';
  if(key === 'month'){ from = iso(new Date(now.getFullYear(), now.getMonth(), 1)); to = iso(now); }
  else if(key === 'lastmonth'){
    from = iso(new Date(now.getFullYear(), now.getMonth()-1, 1));
    to = iso(new Date(now.getFullYear(), now.getMonth(), 0));
  } else if(key === 'd7'){ const d = new Date(now); d.setDate(d.getDate()-6); from = iso(d); to = iso(now); }
  else if(key === 'd30'){ const d = new Date(now); d.setDate(d.getDate()-29); from = iso(d); to = iso(now); }
  $('#dfrom').value = from; $('#dto').value = to;
  document.querySelectorAll('#chips button').forEach(b => b.classList.toggle('on', b.dataset.k === key));
}
document.querySelectorAll('#chips button').forEach(b => b.onclick = () => setRange(b.dataset.k));

async function loadRunFiles(){
  try{
    const j = await (await fetch('/api/data-files')).json();
    const groups = (j.groups || []).filter(g => g.items.length);
    const legacy = j.legacy || [];
    const unknown = j.unknown || [];
    if(!groups.length && !legacy.length && !unknown.length){
      $('#run-files-hint').textContent = '';
      $('#run-files').innerHTML = '<div class="empty"><b>还没有数据文件</b>在战斧浏览器里打开店铺页面，点扩展面板的「立即运行」</div>';
      return;
    }
    let n = legacy.length + unknown.length;
    groups.forEach(g => n += g.items.length);
    $('#run-files-hint').textContent = n + ' 个文件';
    let html = '';
    const KIND_CN = {orders:'订单', ads:'广告', affiliate:'联盟', mapping:'映射', other:'其他'};
    groups.forEach(g => {
      const k = g.kinds || {};
      const kbits = Object.keys(k).filter(x => x !== 'other')
        .map(x => KIND_CN[x] + ' ' + k[x]).join(' · ');
      html += `<div style="padding:13px 20px 6px;font-size:12.5px;color:var(--text-2);background:#fcfcfa;
        border-bottom:1px solid var(--line-soft)">${esc(g.name)}
        <span class="tag gray">${esc(g.country || '')}</span>
        ${kbits ? '<span class="tag">' + esc(kbits) + '</span>' : ''}</div>`;
      html += '<table><tbody>' + g.items.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    });
    if(unknown.length){
      html += `<div style="padding:13px 20px 6px;font-size:12.5px;color:var(--warn);background:#fdf6f5;
        border-bottom:1px solid var(--line-soft)">⚠ 未识别店铺的数据（${unknown.length} 个）——不会参与核算</div>`;
      html += '<table><tbody>' + unknown.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    }
    if(legacy.length){
      html += `<div style="padding:13px 20px 6px;font-size:12.5px;color:var(--text-2);background:#fcfcfa;
        border-bottom:1px solid var(--line-soft)">未归到店铺的旧数据</div>`;
      html += '<table><tbody>' + legacy.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    }
    $('#run-files').innerHTML = html;
  }catch(e){}
}

/** 换店铺时刷新快照列表与文件清单：快照是按店分开存的，
 *  A 店的快照在 B 店下拉里不该出现。 */
if($('#store')) $('#store').onchange = () => { loadSnapshots(); loadRunFiles(); };

/* ---------------- 手动导入 ---------------- */
/* 自动取数这条路依赖平台接口和风控参数，平台一改版就可能断。
   断了之后不能让人干等着，所以留一条人工通路：
   用户自己导出表 → 在这里上传 → 落进该店铺的 inbox → 和自动数据一样算。 */

/** 填充导入区的「店铺」和「国家」下拉。 */
async function loadImportOptions(){
  try{
    // 直接进「导入数据」页时缓存可能是空的，自己补一次
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
  }catch(e){}
}

/** 选店铺时把国家自动带上：各国费率、列名都不同，选错就全错。 */
if($('#imp2-store')) $('#imp2-store').onchange = () => {
  const st = $('#imp2-store'), cs = $('#imp2-country');
  const s = storesCache.find(x => x.id === st.value);
  if(s && cs && s.country) cs.value = s.country;
};

/** 读取文件为 base64，分块拼避免一次申请超大字符串。 */
async function fileToB64(f){
  const buf = new Uint8Array(await f.arrayBuffer());
  let bin = "";
  const CH = 0x8000;
  for(let i = 0; i < buf.length; i += CH){
    bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
  }
  return btoa(bin);
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
  loadImportFiles(); loadRunFiles(); loadSnapshots(); loadStatus();
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

/** 「已经传了哪些」：按店铺列文件，没选店铺的单独标出来 */
async function loadImportFiles(){
  const box = $('#imp2-files'), hint = $('#imp2-hint');
  if(!box) return;
  try{
    const j = await (await fetch('/api/data-files')).json();
    const groups = (j.groups || []).filter(g => (g.items || []).length);
    const unknown = j.unknown || [], legacy = j.legacy || [];
    let n = legacy.length + unknown.length;
    groups.forEach(g => n += g.items.length);
    if(hint) hint.textContent = n ? n + ' 个文件' : '';
    if(!n){
      box.innerHTML = '<div class="empty"><b>还没传过文件</b>上面选好店铺，把后台导出的表拖进来就行</div>';
      return;
    }
    const KIND_CN = {orders:'订单', ads:'广告', affiliate:'联盟', mapping:'映射', other:'其他'};
    let html = '';
    groups.forEach(g => {
      const k = g.kinds || {};
      const bits = Object.keys(k).filter(x => x !== 'other')
        .map(x => KIND_CN[x] + ' ' + k[x]).join(' · ');
      html += `<div style="padding:12px 20px 6px;font-size:12.5px;color:var(--text-2);background:#fcfcfa;
        border-bottom:1px solid var(--line-soft)">${esc(g.name)}
        <span class="tag gray">${esc(g.country || '')}</span>
        ${bits ? '<span class="tag">' + esc(bits) + '</span>' : ''}</div>`;
      html += '<table><tbody>' + g.items.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    });
    if(unknown.length){
      html += `<div style="padding:12px 20px 6px;font-size:12.5px;color:var(--warn);background:#fdf6f5;
        border-bottom:1px solid var(--line-soft)">⚠ 没选店铺的数据（${unknown.length} 个）——不会参与核算</div>`;
      html += '<table><tbody>' + unknown.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    }
    box.innerHTML = html;
  }catch(err){
    box.innerHTML = '<div class="empty">读取失败：' + esc(err.message) + '</div>';
  }
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

/** 列出这家店已有的取数快照，供「用哪次数据」多选使用。
 *  可勾选多份一起算——跨快照重复的订单只算一次（重叠以最新为准）。 */
let snapStore = '';
async function loadSnapshots(){
  try{
    const store = $('#store') ? $('#store').value : '';
    const j = await (await fetch('/api/snapshots?store=' + encodeURIComponent(store))).json();
    const box = $('#snap-list');
    if(!box) return;
    const items = j.items || [];
    // 换店铺就换一批快照，之前的勾选不再保留（保留也是对不上号的 id）
    const prev = (store === snapStore) ? selectedSnaps() : [];
    snapStore = store;
    if(!items.length){
      box.innerHTML = '<div class="empty">还没有快照，取一次数就会自动存一份</div>';
      const hint = $('#run-files-hint');
      if(hint) hint.textContent = '取一次数就会自动存一份快照';
      return;
    }
    box.innerHTML = items.map(it => {
      const kinds = it.kinds || {};
      const bits = [];
      if(kinds.orders) bits.push('订单 ' + kinds.orders + ' 行');
      if(kinds.ads) bits.push('广告 ' + kinds.ads + ' 行');
      if(kinds.affiliate) bits.push('联盟 ' + kinds.affiliate + ' 行');
      if(!bits.length) bits.push(it.file_count + ' 个文件');
      const on = prev.length ? prev.includes(it.id) : it.id === items[0].id;
      return `<label class="snap-item"><input type="checkbox" value="${esc(it.id)}" ${on?'checked':''}>
        <span><b>${esc(it.range)}</b><em>${esc(it.at)} · ${esc(bits.join(' / '))}</em></span></label>`;
    }).join('');
    const hint = $('#run-files-hint');
    if(hint) hint.textContent = items.length + ' 份快照，勾选多份可合并计算';
  }catch(e){}
}

/** 当前勾选的快照 id 列表 */
function selectedSnaps(){
  const box = $('#snap-list');
  if(!box) return [];
  return Array.from(box.querySelectorAll('input[type=checkbox]:checked')).map(x => x.value);
}

/** 快照快捷选择：仅最新 / 全部 / 近 3 次 / 不指定 */
function pickSnaps(mode){
  const box = $('#snap-list');
  if(!box) return;
  const boxes = Array.from(box.querySelectorAll('input[type=checkbox]'));
  boxes.forEach((b, i) => {
    if(mode === 'latest') b.checked = (i === 0);
    else if(mode === 'all') b.checked = true;
    else if(mode === 'last3') b.checked = (i < 3);
    else b.checked = false;
  });
}

document.querySelectorAll('#snap-chips button').forEach(b => {
  b.onclick = () => pickSnaps(b.dataset.sel);
});

$('#btn-run').onclick = async () => {
  const btn = $('#btn-run'), n = $('#run-note');
  const store = $('#store').value;
  const snaps = selectedSnaps();
  const ids = store ? [store] : storesCache.map(s => s.id).filter(Boolean);
  if(!ids.length){ note(n, '没有可核算的店铺，先去「店铺」页看看识别到没有', 'err'); return; }
  btn.disabled = true;
  const results = [];
  for(let i = 0; i < ids.length; i++){
    const how = snaps.length > 1 ? ('合并 ' + snaps.length + ' 份快照核算') : (snaps.length ? '按选定快照核算' : '按实时数据核算');
    note(n, how + '（' + (i+1) + '/' + ids.length + '）…', 'ok');
    try{
      const r = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({store: ids[i], from: $('#dfrom').value || null, to: $('#dto').value || null,
                             snapshots: snaps.length ? snaps : null,
                             with_formula: !!( $('#with-formula') && $('#with-formula').checked )})})
      results.push(await r.json());
    }catch(e){ results.push({ok:false, error:e.message, store: ids[i]}); }
  }
  const ok = results.filter(x => x.ok), bad = results.filter(x => !x.ok);
  let t = '';
  if(ok.length) t = '<b>核算完成 ' + ok.length + ' 家</b><br>' + ok.map(x =>
    esc(x.store_name || '') + '：净利 ' + fmt2(x.net_local) + '　' + esc(x.report_name || '')
    + (x.copied_to ? '　<span class="muted">已复制</span>' : '')).join('<br>');
  if(bad.length) t += (t ? '<br>' : '') + '<b>失败 ' + bad.length + ' 家</b><br>'
    + bad.map(x => esc(x.store || '') + '：' + esc(String(x.error || '').slice(0, 120))).join('<br>');
  note(n, t, bad.length ? 'err' : 'ok');
  btn.disabled = false;
  loadStatus(); loadTrend();
};

let repAll = [];      // 全部报表
let repStore = '';    // 当前按哪个店铺筛选（空 = 全部）

async function loadReports(){
  try{
    const j = await (await fetch('/api/reports')).json();
    repAll = j.items || [];
    $('#rep-hint').textContent = repAll.length ? '共 ' + repAll.length + ' 份' : '';
    // 店铺筛选条：所有报表 + 每家店
    const stores = [...new Set(repAll.map(x => x.group || ''))];
    const fc = $('#rep-filter');
    if(fc){
      fc.innerHTML = '<button data-store="" class="' + (repStore ? '' : 'on') + '">所有报表</button>'
        + stores.map(s => `<button data-store="${esc(s)}" class="${repStore === s ? 'on' : ''}">`
          + esc(s || '未分店') + '（' + repAll.filter(x => (x.group||'') === s).length + '）</button>').join('');
      fc.querySelectorAll('[data-store]').forEach(b => b.onclick = () => {
        repStore = b.dataset.store || '';
        loadReports();
      });
    }
    const items = repStore ? repAll.filter(x => (x.group||'') === repStore) : repAll;
    if(!items.length){
      $('#rep-list').innerHTML = '<div class="empty"><b>这里还没有报表</b>去「开始核算」跑一次</div>';
      return;
    }
    $('#rep-list').innerHTML = '<table><thead><tr><th style="width:34px"></th><th>文件名</th>'
      + '<th style="width:120px">店铺</th>'
      + '<th style="width:80px">大小</th><th style="width:130px">生成时间</th><th style="width:130px"></th>'
      + '</tr></thead><tbody>' + items.map(it => `<tr>
          <td><input type="checkbox" data-rep="${esc(it.rel || it.name)}"></td>
          <td>${esc(it.name)}</td><td class="muted">${esc(it.group || '—')}</td>
          <td class="muted">${it.size_mb} MB</td><td class="muted">${it.at}</td>
          <td><button class="mini" data-view-report="${esc(it.rel || it.name)}">查看</button>
            <a href="/report?name=${encodeURIComponent(it.name)}" style="text-decoration:none">
            <button class="mini">下载</button></a></td></tr>`).join('') + '</tbody></table>';
    document.querySelectorAll('[data-view-report]').forEach(b =>
      b.onclick = () => viewReport(b.dataset.viewReport));
  }catch(e){ $('#rep-list').innerHTML = '<div class="empty">读取失败</div>'; }
}

/** 勾选中的报表（相对路径） */
function repChecked(){
  return Array.from(document.querySelectorAll('#rep-list [data-rep]:checked')).map(x => x.dataset.rep);
}

$('#rep-all').onclick = () => {
  document.querySelectorAll('#rep-list [data-rep]').forEach(x => x.checked = true);
};
$('#rep-none').onclick = () => {
  document.querySelectorAll('#rep-list [data-rep]').forEach(x => x.checked = false);
};
$('#rep-dl').onclick = () => {
  const names = repChecked();
  if(!names.length){ toast('先勾几份报表'); return; }
  downloadReports(names);
};
$('#rep-dl-all').onclick = () => {
  const items = repStore ? repAll.filter(x => (x.group||'') === repStore) : repAll;
  if(!items.length){ toast('这里没有报表'); return; }
  downloadReports(items.map(x => x.rel || x.name));
};

function downloadReports(names){
  location.href = '/api/reports/zip?names=' + encodeURIComponent(names.join('||'));
}

async function viewReport(name){
  $('#modal').classList.add('show');
  $('#modal-title').textContent = name;
  $('#modal-sub').textContent = '';
  $('#modal-body').innerHTML = '<div class="empty">读取中…</div>';
  try{
    const j = await (await fetch('/api/report-view?name=' + encodeURIComponent(name))).json();
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
      $('#store-list').innerHTML = '<div class="empty"><b>还没识别到店铺</b>在战斧里打开店铺页面，点扩展面板的「立即运行」，它会自动认出来</div>';
      return;
    }
    let html = '<table><thead><tr><th style="width:210px">名字（可改）</th>'
      + '<th style="width:170px">店铺 ID</th><th style="width:110px">国家</th>'
      + '<th style="width:80px">数据</th><th style="width:110px"></th></tr></thead><tbody>';
    storesCache.forEach(s => {
      html += `<tr>
        <td><input type="text" value="${esc(s.name || '')}" data-sid="${esc(s.id)}"
          style="width:100%;padding:6px 9px;font-size:13px;border:1px solid var(--line);border-radius:8px"></td>
        <td class="mono muted" style="font-size:11.5px">${esc(s.id)}</td>
        <td class="muted">${esc(s.country || '—')}${s.auto ? ' <span class="tag">自动</span>' : ''}</td>
        <td class="muted">${s.inbox_count !== undefined ? s.inbox_count + ' 个' : '—'}</td>
        <td><button class="mini" data-rename="${esc(s.id)}">保存名字</button></td></tr>`;
    });
    $('#store-list').innerHTML = html + '</tbody></table>';
    document.querySelectorAll('[data-rename]').forEach(b =>
      b.onclick = () => renameStore(b.dataset.rename, b));
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
    toast(j.ok ? '已保存：' + name : ('保存失败：' + j.error));
    if(j.ok){ loadStores(); loadStatus(); }
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
function renderReportColumns(){
  const box = $('#rcols-body');
  if(!box) return;
  const cat = reportColumnCatalog();
  const c = cur();
  // 显式数组（含空数组）= 按它来；null/undefined = 默认全选。
  // 关键：空数组要当成「全不选」，不能又回退成全选（否则取消全选没反应）。
  const sel = Array.isArray(c.report_columns) ? new Set(c.report_columns) : null;
  const titles = c.report_titles || {};
  const total = c.report_total_position === 'top' ? 'top' : 'bottom';
  box.innerHTML = cat.map(col => {
    const on = !sel || sel.has(col.key);
    const title = titles[col.key] || '';
    return `<tr data-rk="${esc(col.key)}">
      <td class="kcell"><input type="checkbox" data-rcol="${esc(col.key)}" ${on ? 'checked' : ''}></td>
      <td>${esc(col.header)}<div class="pid">${esc(col.key)}</div></td>
      <td><input type="text" data-rtitle="${esc(col.key)}" value="${esc(title)}" placeholder="留空用默认"></td>
      <td class="muted" style="font-size:12px">${REPORT_KIND_CN[col.kind] || col.kind}</td>
    </tr>`;
  }).join('');

  // 合计行位置
  document.querySelectorAll('input[name="rcol-total"]').forEach(r => {
    r.checked = (r.value === total);
    r.onchange = () => { c.report_total_position = r.value; };
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
  };
  box.querySelectorAll('[data-rcol]').forEach(cb => cb.onchange = () => {
    const keys = [...box.querySelectorAll('[data-rcol]')]
      .filter(x => x.checked).map(x => x.dataset.rcol);
    c.report_columns = keys;
    allCb.checked = keys.length >= cat.length;
  });
  box.querySelectorAll('[data-rtitle]').forEach(inp => inp.oninput = () => {
    const k = inp.dataset.rtitle;
    const v = inp.value.trim();
    if(!v) delete titles[k]; else titles[k] = v;
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

$('#btn-save').onclick = async () => {
  try{
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
    const r = await fetch('/api/config/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: g.params, by_country: collectByCountry(),
                            formulas: fmls, order_fields: g.order_fields,
                            cost_fields: g.cost_fields,
                            ads_fields: g.ads_fields, affiliate_fields: g.affiliate_fields,
                            mapping_fields: g.mapping_fields,
                            exclude_zero_amount: !!g.exclude_zero_amount,
                            report_columns: g.report_columns || [],
                            report_titles: g.report_titles || {},
                            report_total_position: g.report_total_position || 'bottom'})});
    const j = await r.json();
    const n = Object.keys(collectByCountry()).length;
    if(j.ok) paramsLoaded = false;
    toast(j.ok ? ('已保存（通用 + ' + n + ' 个国家的单独设置），下次核算立即生效')
               : ('保存失败：' + j.error));
  }catch(e){ toast('保存出错：' + e.message); }
};

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
    box.innerHTML = '<div><span class="muted">状态</span>　<b style="color:var(--warn)">读不到（桥可能已停）</b></div>';
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
}

// 重启用本程序的桥：先关掉别的桥，再起当前程序目录这个
$('#btn-bridge-restart').onclick = async () => {
  const api = (window.pywebview && window.pywebview.api) || null;
  if(!api || !api.restart_bridge){ toast('请在程序窗口里操作'); return; }
  if(!confirm('会先关掉当前正在跑的桥，再用本程序目录的桥重启。确定？')) return;
  toast('正在重启取数桥…');
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
  if(!confirm('关闭取数桥后，本界面会断开。确定关闭？')) return;
  try{
    const ok = await api.stop_bridge();
    toast(ok ? '取数桥已关闭。要用时双击「启动桥.bat」或重开程序' : '没有正在跑的桥');
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
    if(j.ok) loadCosts();
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
    if(j.ok) loadRates();
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

    // 自动运行
    const a = all.auto || {};
    $('#auto-on').checked = !!a.enabled;
    $('#auto-freq').value = a.freq || 'daily';
    $('#auto-time').value = a.time || '09:00';
    $('#auto-range').value = a.range || 'last_month';
    $('#auto-weekday').value = String(a.weekday ?? 0);
    $('#auto-dir').value = a.download_dir || '';
    if($('#auto-formula')) $('#auto-formula').checked = !!a.with_formula;
    // 店铺多选
    const sel = $('#auto-stores');
    if(sel){
      const want = (a.stores || []).map(String);
      sel.innerHTML = storesCache.map(x =>
        `<option value="${esc(x.id)}" ${want.includes(String(x.id)) ? 'selected' : ''}>${esc(x.name || x.id)}</option>`).join('');
    }
    syncAutoUi();
  }catch(e){}
}

/** 自动运行：频率是「每周」时才显示星期几 */
function syncAutoUi(){
  const f = $('#auto-freq');
  if(f && $('#auto-wd-field')) $('#auto-wd-field').hidden = (f.value !== 'weekly');
}

/** 下次自动运行的时间 */
function autoNextText(){
  const on = $('#auto-on') && $('#auto-on').checked;
  const el = $('#auto-next');
  if(!el) return;
  if(!on){ el.textContent = '自动运行没开。'; return; }
  const freq = $('#auto-freq').value, t = $('#auto-time').value || '09:00';
  const wd = ['周一','周二','周三','周四','周五','周六','周日'][Number($('#auto-weekday').value) || 0];
  el.textContent = freq === 'weekly'
    ? `已开启：每${wd} ${t} 自动跑一次（区间：${$('#auto-range').selectedOptions[0].text}）。`
    : `已开启：每天 ${t} 自动跑一次（区间：${$('#auto-range').selectedOptions[0].text}）。`;
}

if($('#auto-freq')) $('#auto-freq').onchange = () => { syncAutoUi(); autoNextText(); };
if($('#auto-on')) $('#auto-on').onchange = () => autoNextText();
if($('#auto-range')) $('#auto-range').onchange = () => autoNextText();

$('#auto-now').onclick = async () => {
  const btn = $('#auto-now'), n = $('#auto-note');
  btn.disabled = true;
  try{
    const r = await fetch('/api/auto-run-now', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
    const j = await r.json();
    n.className = 'note show ' + (j.ok ? 'ok' : 'err');
    n.textContent = j.ok ? (j.note || '已派发') : ('失败：' + (j.error || ''));
  }catch(e){
    n.className = 'note show err';
    n.textContent = '出错：' + e.message;
  }
  btn.disabled = false;
};

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
        fx: {mode: $('#set-fx').value},
        auto: {
          enabled: !!($('#auto-on') && $('#auto-on').checked),
          freq: $('#auto-freq').value, time: $('#auto-time').value || '09:00',
          weekday: Number($('#auto-weekday').value || 0),
          range: $('#auto-range').value,
          stores: Array.from(($('#auto-stores') ? $('#auto-stores').selectedOptions : []))
                    .map(o => o.value),
          download_dir: ($('#auto-dir') ? $('#auto-dir').value : '').trim(),
          with_formula: !!($('#auto-formula') && $('#auto-formula').checked),
        }})});
    const j = await r.json();
    toast(j.ok ? '设置已保存' : ('保存失败：' + j.error));
    if(j.ok) autoNextText();
  }catch(e){ toast('出错：' + e.message); }
};

async function loadDebug(){
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

setRange('all');
go((location.hash || '#/overview').replace('#/', '') || 'overview');
loadTrend();
setInterval(() => { if(!$('#view-overview').hidden) loadStatus(); }, 20000);
</script>
</body>
</html>
"""
