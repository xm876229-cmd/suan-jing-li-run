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
  .page{max-width:900px;margin:0 auto}
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
  .kcell{text-align:center}
  .kcell label{display:inline-flex;padding:3px;cursor:pointer}
  .kcell input{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;display:block}
  .pid{font-size:11px;color:var(--text-3);margin-top:3px;font-family:ui-monospace,Consolas,monospace}
  .tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
    background:var(--accent-soft);color:var(--accent);margin-left:6px;vertical-align:1px}
  .tag.warn{background:var(--warn-soft);color:var(--warn)}
  .tag.gray{background:var(--line-soft);color:var(--text-3)}
  .tools{display:flex;gap:8px;flex-wrap:wrap}
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
  .bars .b{flex:1;background:#9fd3c1;border-radius:3px 3px 0 0;min-height:2px;transition:background .12s}
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
      <a href="#/params" data-view="params">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M2 4.5h12M2 11.5h12" stroke-linecap="round"/>
          <circle cx="6" cy="4.5" r="1.8" fill="var(--panel)"/><circle cx="10.5" cy="11.5" r="1.8" fill="var(--panel)"/>
        </svg><span>参数配置</span></a>
      <a href="#/rates" data-view="rates">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="6"/>
          <path d="M8 4.6v7M9.8 6.2c0-.8-.8-1.2-1.8-1.2s-1.7.5-1.7 1.2c0 1.8 3.5.9 3.5 2.7 0 .8-.8 1.2-1.8 1.2s-1.8-.4-1.8-1.2"
                stroke-linecap="round"/>
        </svg><span>费率</span></a>
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
      <div class="row"><span>版本</span><span>0.13.0</span></div>
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
      <div class="head"><h1>参数配置</h1>
        <p>每个参数自己选算哪些订单类别。勾上=计入，取消=不计入。改完先试算，满意再保存。</p></div>
      <div class="card"><h2>参数表<span class="hint">一行一个参数</span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="ptbl"><thead><tr>
            <th style="min-width:140px">参数</th><th style="width:110px">基数</th><th style="width:110px">费率</th>
            <th class="kcell">正常</th><th class="kcell">已取消</th><th class="kcell">退货退款</th>
            <th class="kcell">0元样品</th><th class="kcell">未付款</th>
            <th class="kcell" style="width:48px">启用</th><th style="width:40px"></th>
          </tr></thead><tbody id="pbody"></tbody></table>
        </div></div></div>
      <div class="card"><h2>操作<span class="hint">试算不会保存</span></h2><div class="body">
        <div class="tools">
          <button class="primary" id="btn-preview">试算看净利变化</button>
          <button id="btn-save">保存配置</button>
          <button class="mini" id="btn-all-on">全部类别都勾上</button>
          <button class="mini" id="btn-all-off">全部类别都取消</button>
          <button class="mini" id="btn-add">＋ 新增参数</button></div>
        <p class="muted" style="font-size:12.5px;margin:14px 0 0">
          费率可以写数字（如 <b>2</b> 表示每单 2 元），也可以写 <b>params.xxx</b> 引用国家费率；留空表示原样取基数。</p>
        <div class="diff" id="diff"><h3>试算结果</h3><div id="diff-body"></div></div>
      </div></div>
    </section>

    <section class="page" id="view-rates" hidden>
      <div class="head"><h1>费率</h1>
        <p>每个国家的佣金、发单费、手续费都在这改。改完点保存，下次核算就用新费率。</p></div>
      <div class="card"><h2>国家费率<span class="hint" id="rate-hint"></span></h2>
        <div class="body tight"><div class="tbl-wrap">
          <table class="cfg-tbl" id="rtbl"><thead><tr>
            <th style="min-width:90px">国家</th><th style="width:56px">币种</th>
            <th style="width:84px">汇率CNY</th><th style="width:84px">汇率USD</th>
            <th style="width:84px">发单费</th><th style="width:80px">平台佣金</th>
            <th style="width:80px">手续费</th><th style="width:72px">返现</th>
            <th style="width:72px">成长</th><th style="width:64px">物流</th><th style="width:40px"></th>
          </tr></thead><tbody id="rbody"></tbody></table>
        </div></div></div>
      <div class="card"><h2>操作<span class="hint">汇率的自动抓取在「设置」里</span></h2><div class="body">
        <div class="tools"><button class="primary" id="btn-rate-save">保存费率</button>
          <button class="mini" id="btn-rate-add">＋ 新增国家</button></div>
        <p class="muted" style="font-size:12.5px;margin:14px 0 0">
          费率写小数，比如 <b>0.081</b> 表示 8.1%。发单费是按单收的金额（本地货币），不是比例。</p>
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
const RATE_FIELDS = ['cny_to_local','usd_to_local','dispatch_fee_local','platform_commission',
  'tx_fee','cashback','growth_fee','logistics'];
let rows = [], formulas = {}, rateRows = {}, storesCache = [];

function go(view){
  document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'view-' + view);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  if(view === 'reports') loadReports();
  if(view === 'params') loadParams();
  if(view === 'rates') loadRates();
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

async function loadParams(){
  if(rows.length) return;
  try{
    const j = await (await fetch('/api/config')).json();
    rows = (j.params || []).map(p => ({...p, kinds: (p.kinds === undefined || p.kinds === null) ? null : [...p.kinds]}));
    formulas = j.formulas || {};
    renderParams();
  }catch(e){ toast('读取配置失败'); }
}

function renderParams(){
  const body = $('#pbody'); body.innerHTML = '';
  rows.forEach((r, i) => {
    const tr = document.createElement('tr');
    if(r.enabled === false) tr.classList.add('off');
    const tdName = document.createElement('td');
    const inp = document.createElement('input'); inp.type = 'text'; inp.value = r.label || r.id || '';
    inp.oninput = () => { rows[i].label = inp.value; };
    const sub = document.createElement('div'); sub.className = 'pid';
    sub.innerHTML = (r.id || '') + (r.kinds === null ? '<span class="tag">全部</span>'
      : (r.kinds.length === 0 ? '<span class="tag warn">不算</span>' : ''));
    tdName.append(inp, sub); tr.append(tdName);

    const tdBase = document.createElement('td');
    const sel = document.createElement('select');
    BASES.forEach(b => {
      const o = document.createElement('option'); o.value = b.id; o.textContent = b.label;
      if(b.id === (r.base || 'gmv')) o.selected = true; sel.append(o);
    });
    sel.onchange = () => { rows[i].base = sel.value; renderParams(); };
    tdBase.append(sel); tr.append(tdBase);

    const tdRate = document.createElement('td');
    const rt = document.createElement('input'); rt.type = 'text'; rt.placeholder = '1';
    rt.value = (r.rate === undefined || r.rate === null) ? '' : String(r.rate);
    rt.oninput = () => {
      const v = rt.value.trim();
      if(v === ''){ delete rows[i].rate; return; }
      const n = Number(v); rows[i].rate = Number.isFinite(n) ? n : v;
    };
    tdRate.append(rt); tr.append(tdRate);

    KINDS.forEach(k => {
      const td = document.createElement('td'); td.className = 'kcell';
      const lab = document.createElement('label');
      const cb = document.createElement('input'); cb.type = 'checkbox';
      cb.checked = r.kinds === null ? true : (r.kinds || []).includes(k.id);
      cb.onchange = () => {
        if(rows[i].kinds === null) rows[i].kinds = KINDS.map(x => x.id);
        const st = new Set(rows[i].kinds);
        cb.checked ? st.add(k.id) : st.delete(k.id);
        rows[i].kinds = KINDS.map(x => x.id).filter(x => st.has(x));
        renderParams();
      };
      lab.append(cb); td.append(lab); tr.append(td);
    });

    const tdOn = document.createElement('td'); tdOn.className = 'kcell';
    const en = document.createElement('input'); en.type = 'checkbox'; en.checked = r.enabled !== false;
    en.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer';
    en.onchange = () => { rows[i].enabled = en.checked; renderParams(); };
    tdOn.append(en); tr.append(tdOn);

    const tdDel = document.createElement('td'); tdDel.className = 'kcell';
    const del = document.createElement('button'); del.className = 'mini'; del.textContent = '删';
    del.style.cssText = 'padding:3px 7px;font-size:12px;color:var(--warn)';
    del.onclick = () => { if(confirm('删掉「' + (r.label || r.id) + '」这一项？')){ rows.splice(i,1); renderParams(); } };
    tdDel.append(del); tr.append(tdDel);
    body.append(tr);
  });
}

function paramPayload(){
  return rows.map(r => {
    const o = {id:r.id, label:r.label, base:r.base || 'gmv'};
    if(r.rate !== undefined && r.rate !== null && r.rate !== '') o.rate = r.rate;
    if(r.kinds !== null) o.kinds = r.kinds;
    if(r.enabled === false) o.enabled = false;
    return o;
  });
}

$('#btn-all-on').onclick = () => { rows.forEach(r => r.kinds = KINDS.map(k => k.id)); renderParams(); };
$('#btn-all-off').onclick = () => { rows.forEach(r => r.kinds = []); renderParams(); };
$('#btn-add').onclick = () => {
  const id = prompt('参数的英文标识（公式里引用它，建议小写英文，如 order_service）');
  if(!id) return;
  if(rows.some(r => r.id === id)){ toast('这个标识已经存在'); return; }
  const label = prompt('显示名称', id) || id;
  rows.push({id, label, base:'order_count', rate:0, kinds:['normal'], enabled:true});
  renderParams();
};

$('#btn-preview').onclick = async () => {
  const box = $('#diff'), body = $('#diff-body');
  box.classList.add('show'); body.innerHTML = '<div class="empty">计算中…</div>';
  try{
    const r = await fetch('/api/config/preview', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({params: paramPayload()})});
    const j = await r.json();
    if(!j.ok){ body.innerHTML = '<div class="empty">' + esc(j.error || '试算失败') + '</div>'; return; }
    const b = j.before, a = j.after, d = j.delta;
    const cls = v => v > 0 ? 'up' : (v < 0 ? 'down' : 'muted');
    const sg = v => (v > 0 ? '+' : '') + fmt2(v);
    body.innerHTML = `<table>
      <thead><tr><th style="width:34%"></th><th>当前配置</th><th>改后</th><th style="width:22%">变化</th></tr></thead>
      <tbody>
        <tr><td>GMV</td><td>${fmt2(b.gmv)}</td><td>${fmt2(a.gmv)}</td><td class="${cls(d.gmv)}">${sg(d.gmv)}</td></tr>
        <tr><td>商品成本</td><td>${fmt2(b.cost)}</td><td>${fmt2(a.cost)}</td><td class="${cls(d.cost)}">${sg(d.cost)}</td></tr>
        <tr><td>费用合计</td><td>${fmt2(b.fees)}</td><td>${fmt2(a.fees)}</td><td class="${cls(-d.fees)}">${sg(d.fees)}</td></tr>
        <tr><td><b>净利</b></td><td><b>${fmt2(b.net)}</b></td><td><b>${fmt2(a.net)}</b></td>
            <td class="${cls(d.net)}"><b>${sg(d.net)}</b></td></tr>
        <tr><td>净利率</td><td>${b.net_rate}%</td><td>${a.net_rate}%</td><td></td></tr>
      </tbody></table>
      <div style="padding:12px 18px;font-size:12.5px;color:var(--text-3)">
        按 ${esc(j.country)} 的费率算。试算不会保存，满意再点「保存配置」。</div>`;
  }catch(e){ body.innerHTML = '<div class="empty">试算出错：' + esc(e.message) + '</div>'; }
};

$('#btn-save').onclick = async () => {
  try{
    const r = await fetch('/api/config/save', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({params: paramPayload(), formulas})});
    const j = await r.json();
    toast(j.ok ? '已保存，下次核算立即生效' : ('保存失败：' + j.error));
  }catch(e){ toast('保存出错：' + e.message); }
};

async function loadRates(){
  try{
    const j = await (await fetch('/api/rates')).json();
    rateRows = j.countries || {};
    const names = Object.keys(rateRows);
    $('#rate-hint').textContent = names.length + ' 个国家';
    $('#rbody').innerHTML = names.map((name, i) => {
      const c = rateRows[name];
      const cells = RATE_FIELDS.map(k =>
        `<td><input type="text" data-i="${i}" data-k="${k}"
           value="${c[k] === undefined || c[k] === null ? '' : c[k]}"></td>`).join('');
      return `<tr><td><input type="text" data-i="${i}" data-k="__name" value="${esc(name)}"></td>
        <td class="muted" style="text-align:center">${esc(c.currency || '')}</td>${cells}
        <td><button class="mini" style="padding:3px 7px;font-size:12px;color:var(--warn)"
             data-del="${i}">删</button></td></tr>`;
    }).join('');
    document.querySelectorAll('#rbody input').forEach(inp => {
      inp.oninput = () => {
        const i = Number(inp.dataset.i), k = inp.dataset.k;
        const key = Object.keys(rateRows)[i];
        if(!key) return;
        const raw = inp.value.trim();
        if(k === '__name'){ rateRows[key].__newName = raw; return; }
        const n = Number(raw);
        rateRows[key][k] = raw === '' ? 0 : (Number.isFinite(n) ? n : raw);
      };
    });
    document.querySelectorAll('[data-del]').forEach(b => b.onclick = () => {
      const key = Object.keys(rateRows)[Number(b.dataset.del)];
      if(!key) return;
      if(!confirm('删掉「' + key + '」这个国家的费率？')) return;
      delete rateRows[key]; loadRates();
    });
  }catch(e){}
}

$('#btn-rate-add').onclick = () => {
  const name = prompt('国家名（如：日本）');
  if(!name) return;
  rateRows[name] = {currency:'', cny_to_local:0, usd_to_local:0, dispatch_fee_local:0,
    platform_commission:0, tx_fee:0, cashback:0, growth_fee:0, logistics:0, rates_verified:false};
  loadRates();
};

$('#btn-rate-save').onclick = async () => {
  const out = {};
  Object.entries(rateRows).forEach(([k, v]) => {
    const nk = v.__newName || k; const copy = {...v}; delete copy.__newName; out[nk] = copy;
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
