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


def _files(dir_: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for p in dir_.iterdir():
            if p.is_file() and not p.name.startswith("_"):
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
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
            "group": r["group"],
            "size_mb": round(r["size"] / 1048576, 2),
            "at": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "stamp": int(r["mtime"]),
        }
        for r in _all_reports()
    ]}


def data_files() -> dict:
    """按店铺分开列，哪家店有数据、哪家没有，一眼看得出来。"""
    from bridge.server import discovered_stores

    groups: list[dict] = []
    for d in discovered_stores():
        files = _files(paths.store_dir(d["shop_id"]))
        groups.append({
            "shop_id": d["shop_id"],
            "name": d["shop_name"] or d["shop_id"],
            "country": d["country"],
            "items": [
                {"name": f["name"], "size_kb": round(f["size"] / 1024),
                 "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M")}
                for f in files
            ],
        })
    legacy = _files(paths.INBOX)
    return {
        "ok": True,
        "groups": groups,
        "legacy": [
            {"name": f["name"], "size_kb": round(f["size"] / 1024),
             "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M")}
            for f in legacy
        ],
        "dir": str(paths.DATA),
        "out_dir": str(paths.OUTPUT),
    }


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
      <a href="#/debug" data-view="debug">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 6h6M5 9.5h4" stroke-linecap="round"/>
        </svg><span>调试</span></a>
    </nav>
    <div class="foot">
      <div class="row"><span>版本</span><span>0.17.4</span></div>
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
        <p>用已经下载到本机的帖子数据出报表。选好店铺和日期，点一下就行。</p></div>
      <div class="card"><h2>核算范围</h2><div class="body">
        <div class="chips" id="chips">
          <button data-k="month">本月</button><button data-k="lastmonth">上月</button>
          <button data-k="d7">最近 7 天</button><button data-k="d30">最近 30 天</button>
          <button data-k="all" class="on">全部数据</button></div>
        <div class="row">
          <div class="field"><label>店铺</label>
            <select id="store" style="min-width:200px"><option value="">全部店铺（逐个核算）</option></select></div>
          <div class="field"><label>开始日期</label><input type="date" id="dfrom"></div>
          <div class="field"><label>结束日期</label><input type="date" id="dto"></div>
          <button class="primary" id="btn-run">开始核算</button></div>
        <div class="note" id="run-note"></div>
      </div></div>
      <div class="card"><h2>会用到哪些数据<span class="hint" id="run-files-hint"></span></h2>
        <div class="body tight" id="run-files"><div class="empty">读取中…</div></div></div>
    </section>

    <section class="page" id="view-reports" hidden>
      <div class="head"><h1>报表</h1>
        <p>点「查看」直接在界面里看汇总和费用拆解，不用打开 Excel；点「下载」存到本地。</p></div>
      <div class="card"><h2>全部报表<span class="hint" id="rep-hint"></span></h2>
        <div class="body tight" id="rep-list"><div class="empty">加载中…</div></div></div>
    </section>

    <section class="page" id="view-stores" hidden>
      <div class="head"><h1>店铺</h1>
        <p>在战斧里登录过的店铺会自动出现在这里。名字可以自己改，只有你自己看得到——底下那串 ID 才是唯一标识。</p></div>
      <div class="card"><h2>所有店铺<span class="hint" id="store-hint"></span></h2>
        <div class="body tight" id="store-list"><div class="empty">加载中…</div></div></div>
      <div class="card"><h2>怎么识别店铺</h2><div class="body tight"><div class="kv">
        <div>识别方式</div><div>扩展在店铺页面里调平台的账号接口，取 shop_id、店铺名、国家</div>
        <div>为什么用 ID</div><div>店铺名可能重名、也可能改；shop_id 是平台给的唯一号，不会变</div>
        <div>数据怎么分开</div><div>每家店一个目录 <span class="mono">data\stores\&lt;shop_id&gt;\inbox</span>，核算时只读自己那份</div>
        <div>名字存哪</div><div><span class="mono">config\stores.json</span>，程序不会覆盖你改的名字</div>
      </div></div></div>
    </section>

    <section class="page" id="view-params" hidden>
      <div class="head"><h1>计算参数</h1>
        <p>每一项就是一条公式，公式里直接写订单表的列标题——写标题就是映射，不用另外配。
          标题可以写多个候选，第一个找不到就试下一个。每个国家各配一套。</p></div>

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
      </div>

      <div class="card"><h2>公式里能写什么<span class="hint">点一下就插到刚编辑的公式里</span></h2>
        <div class="body">
          <div class="vars" id="vars"></div>
        </div></div>

      <div class="card"><h2>订单表识别<span class="hint">程序靠这几列判断订单类型、找链接、对成本</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="oftbl"><thead><tr>
            <th style="width:210px">用途</th><th>订单表里的标题（多个候选用 | 分开，按顺序试）</th>
          </tr></thead><tbody id="ofbody"></tbody></table>
        </div></div>
        <div class="body" style="padding-top:0">
          <p class="muted" style="font-size:12.5px;margin:0">
            这一栏只用来判断「这行订单算不算取消/退货/未付款」「属于哪条链接」「按哪个编码对成本」。
            金额怎么算，全在参数表的公式里，不在这里。
          </p>
        </div>
      </div>

      <div class="card"><h2>净利怎么算<span class="hint">这一条也可以改</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="ftbl"><thead><tr>
            <th style="width:170px">结果</th><th>公式</th><th style="width:40px"></th>
          </tr></thead><tbody id="fbody"></tbody></table>
        </div></div>
        <div class="body" style="padding-top:0">
          <p class="muted" style="font-size:12.5px;margin:0">
            默认就是把上面每一项减掉：
            <span class="mono">net = gmv - cost - dispatch - platform_commission - tx_fee
            - ads - affiliate - order_service - other_fee</span>。
            你在参数表里新增了一项，记得也把它加进这一行。
          </p>
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
          <label class="mini-btn">导入 Excel / CSV
            <input type="file" id="cost-file" accept=".xlsx,.xls,.csv" hidden></label>
          <button class="mini" id="btn-cost-add">＋ 新增编码</button>
          <button class="primary" id="btn-cost-save">保存成本表</button>
        </div>
        <div class="note" id="cost-note"></div>
        <div id="cost-check"></div>
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
        <p class="muted" style="font-size:12.5px;margin:12px 0 0">
          每次核算完成后，报表会同时出现在程序的 <span class="mono">output\店铺名\</span> 和这里。同名不会覆盖，会自动加 (2)(3)。</p>
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

    <section class="page" id="view-debug" hidden>
      <div class="head"><h1>调试</h1>
        <p>出问题时先看这里：扩展有没有在干活、桥收到了什么、数据存在哪。</p></div>
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

function go(view){
  document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'view-' + view);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  if(view === 'reports') loadReports();
  if(view === 'params') loadParams();
  if(view === 'rates') loadRates();
  if(view === 'costs') loadCosts();
  if(view === 'settings') loadSettings();
  if(view === 'debug') loadDebug();
  if(view === 'stores') loadStores();
  if(view === 'run') loadRunFiles();
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
    if(!groups.length && !legacy.length){
      $('#run-files-hint').textContent = '';
      $('#run-files').innerHTML = '<div class="empty"><b>还没有数据文件</b>在战斧浏览器里打开店铺页面，点扩展面板的「立即运行」</div>';
      return;
    }
    let n = legacy.length; groups.forEach(g => n += g.items.length);
    $('#run-files-hint').textContent = n + ' 个文件';
    let html = '';
    groups.forEach(g => {
      html += `<div style="padding:13px 20px 6px;font-size:12.5px;color:var(--text-2);background:#fcfcfa;
        border-bottom:1px solid var(--line-soft)">${esc(g.name)}
        <span class="tag gray">${esc(g.country || '')}</span></div>`;
      html += '<table><tbody>' + g.items.map(f => `<tr>
        <td style="width:58%">${esc(f.name)}</td><td class="muted" style="width:16%">${f.size_kb} KB</td>
        <td class="muted" style="width:26%">${f.at}</td></tr>`).join('') + '</tbody></table>';
    });
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

$('#btn-run').onclick = async () => {
  const btn = $('#btn-run'), n = $('#run-note');
  const store = $('#store').value;
  const ids = store ? [store] : storesCache.map(s => s.id).filter(Boolean);
  if(!ids.length){ note(n, '没有可核算的店铺，先去「店铺」页看看识别到没有', 'err'); return; }
  btn.disabled = true;
  const results = [];
  for(let i = 0; i < ids.length; i++){
    note(n, '核算中（' + (i+1) + '/' + ids.length + '）…', 'ok');
    try{
      const r = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({store: ids[i], from: $('#dfrom').value || null, to: $('#dto').value || null})});
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

async function loadReports(){
  try{
    const j = await (await fetch('/api/reports')).json();
    $('#rep-hint').textContent = j.items.length ? '共 ' + j.items.length + ' 份' : '';
    if(!j.items.length){
      $('#rep-list').innerHTML = '<div class="empty"><b>还没有报表</b>去「开始核算」跑一次</div>';
      return;
    }
    $('#rep-list').innerHTML = '<table><thead><tr><th>文件名</th><th style="width:120px">店铺</th>'
      + '<th style="width:80px">大小</th><th style="width:130px">生成时间</th><th style="width:130px"></th>'
      + '</tr></thead><tbody>' + j.items.map(it => `<tr>
          <td>${esc(it.name)}</td><td class="muted">${esc(it.group || '—')}</td>
          <td class="muted">${it.size_mb} MB</td><td class="muted">${it.at}</td>
          <td><button class="mini" data-view-report="${esc(it.name)}">查看</button>
            <a href="/report?name=${encodeURIComponent(it.name)}" style="text-decoration:none">
            <button class="mini">下载</button></a></td></tr>`).join('') + '</tbody></table>';
    document.querySelectorAll('[data-view-report]').forEach(b =>
      b.onclick = () => viewReport(b.dataset.viewReport));
  }catch(e){ $('#rep-list').innerHTML = '<div class="empty">读取失败</div>'; }
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
const FIELD_VARS = [
  {k: 'gmv', t: 'GMV = Σ折扣前小计 − Σ卖家折扣'},
  {k: 'paid', t: '买家实付合计'},
  {k: 'qty', t: '件数'},
  {k: 'orders', t: '订单数（按 SKU 行数分摊）'},
  {k: 'cost_cny', t: '商品成本（人民币）'},
  {k: 'cost', t: '商品成本（当地货币）'},
  {k: 'cost_unit', t: '平均成本单价（人民币）'},
  {k: 'refund', t: '退款金额'},
  {k: 'commission', t: '达人佣金（整期）'},
  {k: 'ads', t: '广告花费（整期）'},
];

const normParams = list => (list || []).map(p => ({
  ...p,
  expr: p.expr !== undefined && p.expr !== null ? String(p.expr) : '',
  kinds: (p.kinds === undefined || p.kinds === null) ? null : [...p.kinds]
}));

function cur(){ return scopeData[scope] || scopeData[""]; }
function rowsOf(){ return cur().params; }

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
      cost_fields: clone(j.cost_fields || {})
    }};
    countrySet = new Set(Object.keys(j.by_country || {}));
    // 每个国家：通用那套 + 它自己的覆盖
    Object.entries(j.by_country || {}).forEach(([k, v]) => {
      const d = {
        params: (v.params && v.params.length) ? normParams(v.params) : clone(scopeData[""].params),
        order_fields: Object.assign(clone(scopeData[""].order_fields), v.order_fields || {}),
        cost_fields: Object.assign(clone(scopeData[""].cost_fields), v.cost_fields || {})
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

function renderAll(){
  renderParams(); renderOrderFields(); renderFormulas(); renderVars();
}

function renderParams(){
  const rows = rowsOf();
  const body = $('#pbody');
  body.innerHTML = rows.map((r, i) => {
    const kinds = r.kinds;
    const kc = KINDS.map(k => {
      const on = kinds === null ? true : (kinds || []).includes(k.id);
      return `<td class="kcell"><input type="checkbox" data-k="${i}:${k.id}" ${on ? 'checked' : ''}></td>`;
    }).join('');
    return `<tr class="${r.enabled === false ? 'off' : ''}">
      <td>
        <input type="text" data-label="${i}" value="${esc(r.label || r.id)}">
        <div class="pid">${esc(r.id)}${r.role ? '<span class="tag gray">报表列</span>' : ''}${
          kinds === null ? '<span class="tag">全部</span>'
          : (kinds.length === 0 ? '<span class="tag warn">不算</span>' : '')}</div>
      </td>
      <td><input type="text" data-expr="${i}" value="${esc(r.expr || '')}"
            placeholder="如 orders * params.dispatch_fee_cny * params.cny_to_local"
            style="font-family:ui-monospace,Consolas,monospace"></td>
      ${kc}
      <td class="kcell"><input type="checkbox" data-on="${i}" ${r.enabled !== false ? 'checked' : ''}
            style="width:15px;height:15px;accent-color:var(--accent);cursor:pointer"></td>
      <td class="kcell"><button class="mini" data-del="${i}"
            style="padding:3px 7px;font-size:12px;color:var(--warn)">删</button></td>
    </tr>`;
  }).join('');

  body.querySelectorAll('[data-label]').forEach(inp =>
    inp.oninput = () => { rows[Number(inp.dataset.label)].label = inp.value; });
  body.querySelectorAll('[data-expr]').forEach(inp => {
    inp.oninput = () => { rows[Number(inp.dataset.expr)].expr = inp.value; };
    inp.onfocus = () => { lastFormulaInput = inp; };
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

/* 订单表识别：这几列是程序判断订单类型、找链接、对成本用的 */
function renderOrderFields(){
  const of = cur().order_fields || {};
  const keys = Object.keys(of).length ? Object.keys(of) : Object.keys(COURSE_LABELS);
  $('#ofbody').innerHTML = keys.map(k => {
    const v = of[k] || {};
    const titles = Array.isArray(v) ? v : (v.titles || []);
    const label = (v && v.label) || COURSE_LABELS[k] || k;
    return `<tr>
      <td>${esc(label)}<div class="pid">${esc(k)}</div></td>
      <td><input type="text" data-of="${esc(k)}" value="${esc(titles.join(' | '))}"
            placeholder="多个候选用 | 分开"></td>
    </tr>`;
  }).join('');
  $('#ofbody').querySelectorAll('[data-of]').forEach(inp => inp.oninput = () => {
    const k = inp.dataset.of;
    const of2 = cur().order_fields;
    const titles = inp.value.split('|').map(x => x.trim()).filter(Boolean);
    if(!of2[k] || Array.isArray(of2[k])) of2[k] = {label: COURSE_LABELS[k] || k, titles};
    else of2[k].titles = titles;
  });
}

/* 可用变量：订单表真实表头 + 算好的量 + 费率 + 其它参数 */
let varRates = [];
async function loadFormulaVars(){
  try{
    const j = await (await fetch('/api/rates')).json();
    const set = new Set();
    Object.values(j.countries || {}).forEach(c => {
      Object.keys(c).forEach(k => {
        if(RATE_META.includes(k) || k.startsWith('_')) return;
        if(c[k] !== null && typeof c[k] === 'object') return;
        set.add(k);
      });
    });
    varRates = [...set];
  }catch(e){ varRates = []; }
  renderVars();
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
  const colChips = headers.map(h =>
    `<div class="v" data-ins="[${esc(h)}]"><code>[${esc(h)}]</code><span>订单表列</span></div>`);
  const baseChips = FIELD_VARS.map(v =>
    `<div class="v" data-ins="${esc(v.k)}"><code>${esc(v.k)}</code><span>${esc(v.t)}</span></div>`);
  const rateChips = varRates.map(k =>
    `<div class="v" data-ins="params.${esc(k)}"><code>params.${esc(k)}</code><span>${esc(RATE_LABELS[k] || k)}</span></div>`);
  const paramChips = rowsOf().filter(r => r.id).map(r =>
    `<div class="v" data-ins="param.${esc(r.id)}"><code>param.${esc(r.id)}</code><span>${esc(r.label || r.id)} 的结果</span></div>`);
  box.innerHTML = colChips.join('') + baseChips.join('') + rateChips.join('') + paramChips.join('');
  box.querySelectorAll('[data-ins]').forEach(el => el.onclick = () => insertVar(el.dataset.ins));
}

function renderFormulas(){
  const keys = Object.keys(fmls);
  $('#fbody').innerHTML = keys.map((k, i) => `<tr>
    <td><span class="mono" style="font-size:12.5px">${esc(k)}</span></td>
    <td><input type="text" data-fi="${i}" value="${esc(fmls[k])}"
         style="font-family:ui-monospace,Consolas,monospace"></td>
    <td><button class="mini" data-fdel="${i}"
         style="padding:3px 7px;font-size:12px;color:var(--warn)">删</button></td>
  </tr>`).join('');
  $('#fbody').querySelectorAll('[data-fi]').forEach(inp => {
    inp.oninput = () => { fmls[Object.keys(fmls)[Number(inp.dataset.fi)]] = inp.value; };
    inp.onfocus = () => { lastFormulaInput = inp; };
  });
  $('#fbody').querySelectorAll('[data-fdel]').forEach(b => b.onclick = () => {
    const k = Object.keys(fmls)[Number(b.dataset.fdel)];
    if(!confirm('删掉公式「' + k + '」？')) return;
    delete fmls[k];
    renderFormulas();
  });
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
  scopeData && Object.entries(scopeData).forEach(([k, v]) => {
    if(!k) return;
    out[k] = { params: v.params, order_fields: v.order_fields, cost_fields: v.cost_fields };
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
                            order_fields: cur1.order_fields, cost_fields: cur1.cost_fields})});
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
    const r = await fetch('/api/config/save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: g.params, by_country: collectByCountry(),
                            formulas: fmls, order_fields: g.order_fields,
                            cost_fields: g.cost_fields})});
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
    + feeFields.map(f => `<th style="width:92px">${RATE_LABELS[f] || f}
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

  $('#rhead').querySelectorAll('[data-delfee]').forEach(b => b.onclick = () => {
    const f = b.dataset.delfee;
    if(!confirm('从所有国家删掉「' + (RATE_LABELS[f] || f) + '」这一项？')) return;
    Object.values(rateRows).forEach(c => { delete c[f]; });
    feeFields = feeFields.filter(x => x !== f);
    renderRates();
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
  try{
    const r = await fetch('/api/rates', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({countries: out})});
    const j = await r.json();
    toast(j.ok ? '费率已保存（' + j.countries + ' 个国家）' : ('保存失败：' + j.error));
    if(j.ok) loadRates();
  }catch(e){ toast('出错：' + e.message); }
};

async function loadSettings(){
  try{
    const j = await (await fetch('/api/settings')).json();
    const s = (j.settings || {}).report || {};
    $('#set-template').value = s.name_template || '{店铺}_{区间}';
    $('#set-dir').value = s.download_dir || '';
    $('#set-fx').value = ((j.settings || {}).fx || {}).mode || 'auto';
  }catch(e){}
}

$('#btn-set-save').onclick = async () => {
  try{
    const dir = $('#set-dir').value.trim();
    const r = await fetch('/api/settings', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        report: {name_template: $('#set-template').value.trim() || '{店铺}_{区间}',
                 download_dir: dir, auto_copy: !!dir},
        fx: {mode: $('#set-fx').value}})});
    const j = await r.json();
    toast(j.ok ? '设置已保存' : ('保存失败：' + j.error));
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
