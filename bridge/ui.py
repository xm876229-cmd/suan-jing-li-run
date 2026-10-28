"""程序主界面。

一个单页应用：左侧固定导航，右侧按功能分页显示。
打开 http://127.0.0.1:8765/ 就是这个界面，桌面窗口里装的也是它。

设计取向参考常见桌面工具（Linear / Vercel / GitHub 的后台）：
  · 左侧导航固定，右侧内容区限宽居中，不把东西堆满整屏
  · 每个功能独立一页，不挤在同一屏
  · 留白优先，卡片之间留够距离
  · 只用一种强调色（深绿），其余靠灰阶区分层级
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src import paths

STATE: dict = {"running": False, "last_run": None, "last_result": None}


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


def _inbox_files() -> list[dict]:
    out: list[dict] = []
    try:
        for p in paths.INBOX.iterdir():
            if p.is_file() and not p.name.startswith("_"):
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def _reports() -> list[dict]:
    out: list[dict] = []
    try:
        for p in paths.OUTPUT.glob("*.xlsx"):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    out.sort(key=lambda x: -x["mtime"])
    return out


def status() -> dict:
    from bridge.server import bridge_uptime, store_list

    files = _inbox_files()
    reports = _reports()
    beat = _ext_heartbeat()
    return {
        "ok": True,
        "uptime": bridge_uptime(),
        "ext_last": beat,
        "ext_ago": _ago(beat),
        "inbox_count": len(files),
        "inbox_latest": files[0]["name"] if files else "",
        "report_count": len(reports),
        "stores": store_list().get("stores", []),
        "running": STATE["running"],
    }


def reports() -> dict:
    return {"ok": True, "items": [
        {
            "name": r["name"],
            "size_mb": round(r["size"] / 1048576, 2),
            "at": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "stamp": int(r["mtime"]),
        }
        for r in _reports()
    ]}


def data_files() -> dict:
    """待核算的原始数据文件，让用户看得见程序到底拿到了什么。"""
    return {
        "ok": True,
        "items": [
            {
                "name": f["name"],
                "size_kb": round(f["size"] / 1024),
                "at": datetime.fromtimestamp(f["mtime"]).strftime("%m-%d %H:%M"),
            }
            for f in _inbox_files()
        ],
        "dir": str(paths.INBOX),
        "out_dir": str(paths.OUTPUT),
    }


def find_report(name: str) -> Path | None:
    """只认 output 目录下的 .xlsx，不接受任何路径分隔符。

    这个接口从浏览器访问，不能让它去读别的目录。
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.lower().endswith(".xlsx"):
        return None
    p = paths.OUTPUT / name
    return p if p.is_file() else None


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
    --bg:#f7f7f5;
    --panel:#ffffff;
    --line:#e7e6e0;
    --line-soft:#f0efe9;
    --text:#191917;
    --text-2:#5f5e58;
    --text-3:#8f8e87;
    --accent:#0f6e56;
    --accent-hover:#0c5b47;
    --accent-soft:#e8f4ef;
    --warn:#9c3d1e;
    --warn-soft:#fbeee8;
    --radius:14px;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--text);
    font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .app{display:flex;height:100vh;overflow:hidden}

  /* ---------- 左侧导航 ---------- */
  .side{
    width:216px;flex:0 0 216px;background:var(--panel);
    border-right:1px solid var(--line);
    display:flex;flex-direction:column;padding:22px 0 16px;
  }
  .brand{display:flex;align-items:center;gap:10px;padding:0 20px 24px}
  .brand .mark{
    width:32px;height:32px;border-radius:9px;background:var(--accent);
    display:flex;align-items:center;justify-content:center;flex:0 0 32px;
  }
  .brand .mark svg{display:block}
  .brand .name{font-size:15px;font-weight:500;letter-spacing:.2px}
  .brand .sub{font-size:11px;color:var(--text-3);margin-top:-2px}

  .nav{padding:0 12px;display:flex;flex-direction:column;gap:2px}
  .nav a{
    display:flex;align-items:center;gap:10px;
    padding:9px 12px;border-radius:9px;
    color:var(--text-2);text-decoration:none;font-size:13.5px;
    transition:background .12s,color .12s;
  }
  .nav a:hover{background:var(--line-soft);color:var(--text)}
  .nav a.on{background:var(--accent-soft);color:var(--accent);font-weight:500}
  .nav a svg{flex:0 0 16px;opacity:.85}
  .nav a.on svg{opacity:1}

  .side .foot{margin-top:auto;padding:0 20px;font-size:11.5px;color:var(--text-3);line-height:1.9}
  .side .foot .row{display:flex;justify-content:space-between}

  /* ---------- 右侧内容 ---------- */
  .main{flex:1;overflow-y:auto;padding:36px 44px 64px}
  .page{max-width:880px;margin:0 auto}
  .page[hidden]{display:none}

  .head{margin-bottom:28px}
  .head h1{font-size:21px;font-weight:500;margin:0 0 6px;letter-spacing:-.2px}
  .head p{margin:0;color:var(--text-2);font-size:13.5px}

  .card{
    background:var(--panel);border:1px solid var(--line);
    border-radius:var(--radius);margin-bottom:20px;overflow:hidden;
  }
  .card > h2{
    font-size:13.5px;font-weight:500;margin:0;
    padding:16px 22px;border-bottom:1px solid var(--line-soft);
    display:flex;align-items:center;gap:10px;
  }
  .card > h2 .hint{margin-left:auto;font-weight:400;font-size:12.5px;color:var(--text-3)}
  .card > .body{padding:22px}
  .card > .body.tight{padding:0}

  /* 状态四格 */
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px}
  .stat .k{font-size:12.5px;color:var(--text-2);margin-bottom:8px;display:flex;align-items:center;gap:6px}
  .stat .v{font-size:19px;font-weight:500;letter-spacing:-.2px}
  .stat .v.small{font-size:14px;font-weight:400}
  .stat .s{font-size:12px;color:var(--text-3);margin-top:4px;
           overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
  .dot.on{background:#1d9e75}
  .dot.off{background:#c4c3bb}

  /* 表单 */
  .field{display:flex;flex-direction:column;gap:7px}
  .field label{font-size:12.5px;color:var(--text-2)}
  .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
  input[type=date],select,input[type=text]{
    font:inherit;font-size:13.5px;padding:9px 12px;border:1px solid var(--line);
    border-radius:9px;background:var(--panel);color:var(--text);min-width:150px;
  }
  input:focus,select:focus{outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px var(--accent-soft)}

  button{
    font:inherit;font-size:13.5px;padding:9px 18px;border-radius:9px;
    border:1px solid var(--line);background:var(--panel);color:var(--text);
    cursor:pointer;transition:background .12s,border-color .12s;
  }
  button:hover{background:var(--line-soft)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:500}
  button.primary:hover{background:var(--accent-hover)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.mini{padding:5px 11px;font-size:12.5px;border-radius:8px}

  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
  .chips button{border-radius:20px;padding:6px 15px;font-size:12.5px;color:var(--text-2)}
  .chips button.on{background:var(--accent-soft);border-color:#b7ddd0;color:var(--accent);font-weight:500}

  /* 表格 */
  table{width:100%;border-collapse:collapse}
  th,td{padding:12px 22px;text-align:left;border-bottom:1px solid var(--line-soft);font-size:13px}
  th{font-size:12px;font-weight:500;color:var(--text-2);background:#fcfcfa}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#fcfcfa}
  .mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:12.5px}
  .muted{color:var(--text-3)}
  .empty{padding:44px 22px;text-align:center;color:var(--text-3);font-size:13.5px}
  .empty b{display:block;color:var(--text-2);font-weight:500;margin-bottom:4px}

  /* 提示条 */
  .note{
    display:none;margin-top:18px;padding:13px 16px;border-radius:10px;
    font-size:13px;line-height:1.6;
  }
  .note.show{display:block}
  .note.ok{background:var(--accent-soft);color:#0a5442}
  .note.err{background:var(--warn-soft);color:var(--warn)}
  .note b{font-weight:500}

  /* 参数表 */
  .tbl-wrap{overflow-x:auto}
  .param-tbl th{white-space:nowrap;padding:11px 14px}
  .param-tbl td{padding:10px 14px;vertical-align:middle}
  .param-tbl tr.off td{opacity:.42}
  .param-tbl input[type=text]{min-width:0;width:100%;padding:6px 9px;font-size:13px}
  .param-tbl select{padding:6px 9px;font-size:13px;min-width:0;width:100%}
  .param-tbl .pid{font-size:11px;color:var(--text-3);margin-top:3px;font-family:ui-monospace,Consolas,monospace}
  .kcell{text-align:center}
  .kcell label{display:inline-flex;cursor:pointer;padding:4px}
  .kcell input{width:15px;height:15px;accent-color:var(--accent);cursor:pointer;display:block}
  .tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
       background:var(--accent-soft);color:var(--accent);margin-left:6px;vertical-align:1px}
  .tag.warn{background:var(--warn-soft);color:var(--warn)}
  .tools{display:flex;gap:8px;flex-wrap:wrap}

  /* 试算结果 */
  .diff{display:none;margin-top:20px;border:1px solid #cfe8de;background:#fbfefc;
        border-radius:12px;overflow:hidden}
  .diff.show{display:block}
  .diff h3{margin:0;padding:13px 18px;font-size:13px;font-weight:500;
           color:var(--accent);background:var(--accent-soft);border-bottom:1px solid #d8ece4}
  .diff table th,.diff table td{padding:10px 18px}
  .up{color:#0f6e56;font-weight:500}
  .down{color:var(--warn);font-weight:500}

  .kv{display:grid;grid-template-columns:150px 1fr;gap:0}
  .kv > div{padding:13px 22px;border-bottom:1px solid var(--line-soft);font-size:13px}
  .kv > div:nth-child(odd){color:var(--text-2)}
  .kv > div:nth-last-child(-n+2){border-bottom:none}

  .toast{
    position:fixed;left:50%;bottom:32px;transform:translateX(-50%) translateY(8px);
    background:#191917;color:#fff;padding:11px 22px;border-radius:10px;font-size:13px;
    opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;z-index:50;
  }
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

  @media (max-width:820px){
    .side{width:64px;flex:0 0 64px}
    .brand .name,.brand .sub,.nav a span,.side .foot{display:none}
    .nav a{justify-content:center;padding:11px}
    .main{padding:24px 20px 48px}
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
      <div>
        <div class="name">净利助手</div>
        <div class="sub">TikTok 店铺</div>
      </div>
    </div>

    <nav class="nav" id="nav">
      <a href="#/overview" data-view="overview">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/>
          <rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/>
        </svg><span>总览</span>
      </a>
      <a href="#/run" data-view="run">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <circle cx="8" cy="8" r="6"/><path d="M6.6 5.6l4 2.4-4 2.4z" stroke-linejoin="round"/>
        </svg><span>开始核算</span>
      </a>
      <a href="#/reports" data-view="reports">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M4 2h5l3 3v9a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z" stroke-linejoin="round"/>
          <path d="M9 2v3.5h3.5M5.5 8.5h5M5.5 11h3.5" stroke-linecap="round"/>
        </svg><span>报表</span>
      </a>
      <a href="#/params" data-view="params">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <path d="M2 4.5h12M2 11.5h12" stroke-linecap="round"/>
          <circle cx="6" cy="4.5" r="1.8" fill="var(--panel)"/><circle cx="10.5" cy="11.5" r="1.8" fill="var(--panel)"/>
        </svg><span>参数配置</span>
      </a>
      <a href="#/data" data-view="data">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
          <ellipse cx="8" cy="4" rx="5.5" ry="2.2"/>
          <path d="M2.5 4v8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4" stroke-linecap="round"/>
          <path d="M2.5 8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2"/>
        </svg><span>数据文件</span>
      </a>
    </nav>

    <div class="foot">
      <div class="row"><span>版本</span><span>0.12.0</span></div>
      <div class="row"><span>端口</span><span>8765</span></div>
    </div>
  </aside>

  <main class="main">

    <!-- ============ 总览 ============ -->
    <section class="page" id="view-overview">
      <div class="head">
        <h1>总览</h1>
        <p>数据拿没拿到、上次算出来多少、接下来该做什么，都在这。</p>
      </div>

      <div class="stats" id="stats">
        <div class="stat"><div class="k"><span class="dot off"></span>取数服务</div><div class="v small">检查中…</div></div>
        <div class="stat"><div class="k">浏览器扩展</div><div class="v small">检查中…</div></div>
        <div class="stat"><div class="k">待核算数据</div><div class="v small">—</div></div>
        <div class="stat"><div class="k">已生成报表</div><div class="v small">—</div></div>
      </div>

      <div class="card">
        <h2>下一步做什么</h2>
        <div class="body">
          <div class="row">
            <button class="primary" onclick="go('run')">开始核算</button>
            <button onclick="go('params')">调整计算口径</button>
            <button onclick="go('reports')">查看报表</button>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>最近一份报表<span class="hint" id="ov-rep-hint"></span></h2>
        <div class="body tight" id="ov-rep"><div class="empty">还没有报表</div></div>
      </div>
    </section>

    <!-- ============ 核算 ============ -->
    <section class="page" id="view-run" hidden>
      <div class="head">
        <h1>开始核算</h1>
        <p>用已经下载到本机的外贸数据出报表。选好店铺和日期，点一下就行。</p>
      </div>

      <div class="card">
        <h2>核算范围</h2>
        <div class="body">
          <div class="chips" id="chips">
            <button data-k="month">本月</button>
            <button data-k="lastmonth">上月</button>
            <button data-k="d7">最近 7 天</button>
            <button data-k="d30">最近 30 天</button>
            <button data-k="all" class="on">全部数据</button>
          </div>
          <div class="row">
            <div class="field"><label>店铺</label><select id="store"><option value="">按店铺配置</option></select></div>
            <div class="field"><label>开始日期</label><input type="date" id="dfrom"></div>
            <div class="field"><label>结束日期</label><input type="date" id="dto"></div>
            <button class="primary" id="btn-run">开始核算</button>
          </div>
          <div class="note" id="run-note"></div>
        </div>
      </div>

      <div class="card">
        <h2>会用到哪些数据<span class="hint" id="run-files-hint"></span></h2>
        <div class="body tight" id="run-files"><div class="empty">读取中…</div></div>
      </div>
    </section>

    <!-- ============ 报表 ============ -->
    <section class="page" id="view-reports" hidden>
      <div class="head">
        <h1>报表</h1>
        <p>每次核算生成的 Excel。点「下载」存到本地，六页明细都在里面。</p>
      </div>
      <div class="card">
        <h2>全部报表<span class="hint" id="rep-hint"></span></h2>
        <div class="body tight" id="rep-list"><div class="empty">加载中…</div></div>
      </div>
    </section>

    <!-- ============ 参数配置 ============ -->
    <section class="page" id="view-params" hidden>
      <div class="head">
        <h1>参数配置</h1>
        <p>每个参数自己选算哪些订单类别。勾上=计入，取消=不计入。改完先试算，满意再保存。</p>
      </div>

      <div class="card">
        <h2>参数表<span class="hint">一行一个参数</span></h2>
        <div class="body tight">
          <div class="tbl-wrap">
            <table class="param-tbl" id="ptbl">
              <thead>
                <tr>
                  <th style="min-width:150px">参数</th>
                  <th style="width:120px">基数</th>
                  <th style="width:120px">费率</th>
                  <th class="kcell">正常</th>
                  <th class="kcell">已取消</th>
                  <th class="kcell">退货退款</th>
                  <th class="kcell">0元样品</th>
                  <th class="kcell">未付款</th>
                  <th class="kcell" style="width:52px">启用</th>
                  <th style="width:44px"></th>
                </tr>
              </thead>
              <tbody id="pbody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>操作<span class="hint">试算不会保存，保存才会生效</span></h2>
        <div class="body">
          <div class="tools">
            <button class="primary" id="btn-preview">试算看净利变化</button>
            <button id="btn-save">保存配置</button>
            <button class="mini" id="btn-all-on">全部类别都勾上</button>
            <button class="mini" id="btn-all-off">全部类别都取消</button>
            <button class="mini" id="btn-add">＋ 新增参数</button>
          </div>
          <p class="muted" style="font-size:12.5px;margin:14px 0 0">
            费率可以写数字（如 <b>2</b> 表示每单 2 元），也可以写 <b>params.xxx</b> 引用国家费率；
            留空表示原样取基数，GMV 和商品成本就是这么用的。
          </p>
          <div class="diff" id="diff">
            <h3>试算结果</h3>
            <div id="diff-body"></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============ 数据文件 ============ -->
    <section class="page" id="view-data" hidden>
      <div class="head">
        <h1>数据文件</h1>
        <p>浏览器扩展抓回来的原始表格。核算用的就是这些文件，缺哪一个都能在这里看出来。</p>
      </div>
      <div class="card">
        <h2>待核算的文件<span class="hint" id="data-hint"></span></h2>
        <div class="body tight" id="data-list"><div class="empty">加载中…</div></div>
      </div>
      <div class="card">
        <h2>说明</h2>
        <div class="kv">
          <div>数据目录</div><div class="mono" id="path-data">—</div>
          <div>报表目录</div><div class="mono" id="path-out">—</div>
          <div>缺文件怎么办</div><div>在战斧浏览器里打开对应页面，点扩展面板的「立即运行」重新取一次</div>
        </div>
      </div>
    </section>

  </main>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const KINDS = [
  {id:'normal', label:'正常'},
  {id:'cancelled', label:'已取消'},
  {id:'refunded', label:'退货退款'},
  {id:'zero_amount', label:'0元样品'},
  {id:'unpaid', label:'未付款'}
];
const BASES = [
  {id:'gmv', label:'GMV'},
  {id:'buyer_paid', label:'买家实付'},
  {id:'order_count', label:'订单数'},
  {id:'items', label:'件数'},
  {id:'cost', label:'商品成本'},
  {id:'refund', label:'退款金额'},
  {id:'commission', label:'达人佣金'},
  {id:'ads', label:'广告花费'}
];

let rows = [];
let formulas = {};

/* ---------- 导航 ---------- */
function go(view){
  document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'view-' + view);
  document.querySelectorAll('#nav a').forEach(a =>
    a.classList.toggle('on', a.dataset.view === view));
  if(view === 'reports') loadReports();
  if(view === 'params') loadParams();
  if(view === 'data') loadData();
  if(view === 'run') loadRunFiles();
  if(location.hash !== '#/' + view) location.hash = '#/' + view;
}
document.querySelectorAll('#nav a').forEach(a =>
  a.addEventListener('click', e => { e.preventDefault(); go(a.dataset.view); }));
window.addEventListener('hashchange', () => {
  const v = (location.hash || '').replace('#/', '') || 'overview';
  go(v);
});

/* ---------- 通用 ---------- */
function toast(msg){
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2600);
}
function note(el, text, kind){
  el.className = 'note show ' + (kind || 'ok');
  el.innerHTML = text;
}
const fmt2 = v => Number(v).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
const pad = n => String(n).padStart(2, '0');
const iso = d => d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());

/* ---------- 总览 ---------- */
async function loadStatus(){
  try{
    const j = await (await fetch('/api/status')).json();
    $('#stats').innerHTML = `
      <div class="stat">
        <div class="k"><span class="dot on"></span>取数服务</div>
        <div class="v small">运行中 · ${j.uptime}</div>
        <div class="s">本地端口 8765</div>
      </div>
      <div class="stat">
        <div class="k"><span class="dot ${j.ext_last ? 'on' : 'off'}"></span>浏览器扩展</div>
        <div class="v small">${j.ext_last ? j.ext_ago : '无记录'}</div>
        <div class="s">${j.ext_last ? '最近一次活动 ' + j.ext_last.slice(11) : '在战斧里打开扩展面板即可'}</div>
      </div>
      <div class="stat">
        <div class="k">待核算数据</div>
        <div class="v">${j.inbox_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">个文件</span></div>
        <div class="s">${j.inbox_latest || '还没有数据'}</div>
      </div>
      <div class="stat">
        <div class="k">已生成报表</div>
        <div class="v">${j.report_count} <span style="font-size:13px;font-weight:400;color:var(--text-2)">份</span></div>
        <div class="s">在「报表」页可以下载</div>
      </div>`;
    const sel = $('#store');
    if(sel && sel.options.length <= 1 && j.stores.length){
      j.stores.forEach(s => {
        const o = document.createElement('option');
        o.value = s.id;
        o.textContent = s.name + (s.country ? '（' + s.country + '）' : '');
        sel.append(o);
      });
    }
  }catch(e){}
}

async function loadOverviewReport(){
  try{
    const j = await (await fetch('/api/reports')).json();
    const box = $('#ov-rep');
    if(!j.items.length){
      $('#ov-rep-hint').textContent = '';
      box.innerHTML = '<div class="empty"><b>还没有报表</b>去「开始核算」跑一次就有了</div>';
      return;
    }
    const it = j.items[0];
    $('#ov-rep-hint').textContent = '共 ' + j.items.length + ' 份';
    box.innerHTML = `<div class="kv">
        <div>文件名</div><div>${it.name}</div>
        <div>生成时间</div><div>${it.at}</div>
        <div>大小</div><div>${it.size_mb} MB</div>
      </div>
      <div style="padding:16px 22px;border-top:1px solid var(--line-soft)">
        <a href="/report?name=${encodeURIComponent(it.name)}"
           style="text-decoration:none"><button class="primary mini">下载这份报表</button></a>
        <button class="mini" onclick="go('reports')">查看全部</button>
      </div>`;
  }catch(e){}
}

/* ---------- 核算 ---------- */
function setRange(key){
  const now = new Date();
  let from = '', to = '';
  if(key === 'month'){ from = iso(new Date(now.getFullYear(), now.getMonth(), 1)); to = iso(now); }
  else if(key === 'lastmonth'){
    from = iso(new Date(now.getFullYear(), now.getMonth()-1, 1));
    to = iso(new Date(now.getFullYear(), now.getMonth(), 0));
  }
  else if(key === 'd7'){ const d = new Date(now); d.setDate(d.getDate()-6); from = iso(d); to = iso(now); }
  else if(key === 'd30'){ const d = new Date(now); d.setDate(d.getDate()-29); from = iso(d); to = iso(now); }
  $('#dfrom').value = from; $('#dto').value = to;
  document.querySelectorAll('#chips button').forEach(b =>
    b.classList.toggle('on', b.dataset.k === key));
}
document.querySelectorAll('#chips button').forEach(b =>
  b.onclick = () => setRange(b.dataset.k));

async function loadRunFiles(){
  try{
    const j = await (await fetch('/api/data-files')).json();
    const box = $('#run-files');
    if(!j.items.length){
      $('#run-files-hint').textContent = '';
      box.innerHTML = '<div class="empty"><b>还没有数据文件</b>在战斧浏览器里打开店铺页面，点扩展面板的「立即运行」</div>';
      return;
    }
    $('#run-files-hint').textContent = j.items.length + ' 个';
    box.innerHTML = '<table><tbody>' + j.items.map(f => `<tr>
        <td style="width:60%">${f.name}</td>
        <td class="muted" style="width:15%">${f.size_kb} KB</td>
        <td class="muted" style="width:25%">${f.at}</td></tr>`).join('') + '</tbody></table>';
  }catch(e){}
}

$('#btn-run').onclick = async () => {
  const btn = $('#btn-run'), n = $('#run-note');
  btn.disabled = true;
  note(n, '核算中，数据多的时候要十几秒…', 'ok');
  try{
    const r = await fetch('/run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({store: $('#store').value,
                            from: $('#dfrom').value || null,
                            to: $('#dto').value || null})
    });
    const j = await r.json();
    if(j.ok){
      let t = '<b>核算完成</b>　净利 ' + fmt2(j.net_local);
      if(j.net_cny !== undefined) t += '（人民币 ' + fmt2(j.net_cny) + '）';
      t += '<br>GMV ' + fmt2(j.gmv);
      if(j.report_name) t += '　报表已生成：' + j.report_name;
      if(j.stale && j.stale.length) t += '<br>注意：' + j.stale.join('；');
      note(n, t, (j.stale && j.stale.length) ? 'err' : 'ok');
      loadOverviewReport();
    }else{
      note(n, '<b>核算失败</b><br>' + (j.error || '未知错误'), 'err');
    }
  }catch(e){ note(n, '<b>出错了</b><br>' + e.message, 'err'); }
  btn.disabled = false;
};

/* ---------- 报表 ---------- */
async function loadReports(){
  try{
    const j = await (await fetch('/api/reports')).json();
    const box = $('#rep-list');
    $('#rep-hint').textContent = j.items.length ? '共 ' + j.items.length + ' 份' : '';
    if(!j.items.length){
      box.innerHTML = '<div class="empty"><b>还没有报表</b>去「开始核算」跑一次</div>';
      return;
    }
    box.innerHTML = '<table><thead><tr><th>文件名</th><th style="width:100px">大小</th>'
      + '<th style="width:140px">生成时间</th><th style="width:90px"></th></tr></thead><tbody>'
      + j.items.map(it => `<tr>
          <td>${it.name}</td>
          <td class="muted">${it.size_mb} MB</td>
          <td class="muted">${it.at}</td>
          <td><a href="/report?name=${encodeURIComponent(it.name)}" style="text-decoration:none">
            <button class="mini">下载</button></a></td>
        </tr>`).join('') + '</tbody></table>';
  }catch(e){ $('#rep-list').innerHTML = '<div class="empty">读取失败</div>'; }
}

/* ---------- 参数配置 ---------- */
async function loadParams(){
  if(rows.length) return;
  try{
    const j = await (await fetch('/api/config')).json();
    rows = (j.params || []).map(p => ({
      ...p, kinds: p.kinds === undefined || p.kinds === null ? null : [...p.kinds]
    }));
    formulas = j.formulas || {};
    renderParams();
  }catch(e){ toast('读取配置失败'); }
}

function renderParams(){
  const body = $('#pbody');
  body.innerHTML = '';
  rows.forEach((r, i) => {
    const tr = document.createElement('tr');
    if(r.enabled === false) tr.classList.add('off');

    const tdName = document.createElement('td');
    const inp = document.createElement('input');
    inp.type = 'text'; inp.value = r.label || r.id || '';
    inp.oninput = () => { rows[i].label = inp.value; };
    const sub = document.createElement('div');
    sub.className = 'pid';
    sub.innerHTML = (r.id || '') +
      (r.kinds === null ? '<span class="tag">全部</span>'
       : (r.kinds.length === 0 ? '<span class="tag warn">不算</span>' : ''));
    tdName.append(inp, sub);
    tr.append(tdName);

    const tdBase = document.createElement('td');
    const sel = document.createElement('select');
    BASES.forEach(b => {
      const o = document.createElement('option');
      o.value = b.id; o.textContent = b.label;
      if(b.id === (r.base || 'gmv')) o.selected = true;
      sel.append(o);
    });
    sel.onchange = () => { rows[i].base = sel.value; renderParams(); };
    tdBase.append(sel);
    tr.append(tdBase);

    const tdRate = document.createElement('td');
    const rt = document.createElement('input');
    rt.type = 'text';
    rt.value = (r.rate === undefined || r.rate === null) ? '' : String(r.rate);
    rt.placeholder = '1';
    rt.oninput = () => {
      const v = rt.value.trim();
      if(v === ''){ delete rows[i].rate; return; }
      const n = Number(v);
      rows[i].rate = (v !== '' && Number.isFinite(n)) ? n : v;
    };
    tdRate.append(rt);
    tr.append(tdRate);

    KINDS.forEach(k => {
      const td = document.createElement('td');
      td.className = 'kcell';
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = r.kinds === null ? true : (r.kinds || []).includes(k.id);
      cb.onchange = () => {
        if(rows[i].kinds === null) rows[i].kinds = KINDS.map(x => x.id);
        const s = new Set(rows[i].kinds);
        cb.checked ? s.add(k.id) : s.delete(k.id);
        rows[i].kinds = KINDS.map(x => x.id).filter(x => s.has(x));
        renderParams();
      };
      lab.append(cb);
      td.append(lab);
      tr.append(td);
    });

    const tdOn = document.createElement('td');
    tdOn.className = 'kcell';
    const en = document.createElement('input');
    en.type = 'checkbox'; en.checked = r.enabled !== false;
    en.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer';
    en.onchange = () => { rows[i].enabled = en.checked; renderParams(); };
    tdOn.append(en);
    tr.append(tdOn);

    const tdDel = document.createElement('td');
    tdDel.className = 'kcell';
    const del = document.createElement('button');
    del.className = 'mini';
    del.textContent = '删';
    del.style.cssText = 'padding:3px 7px;font-size:12px;color:var(--warn)';
    del.onclick = () => {
      if(confirm('删掉「' + (r.label || r.id) + '」这一项？')){ rows.splice(i, 1); renderParams(); }
    };
    tdDel.append(del);
    tr.append(tdDel);

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
  box.classList.add('show');
  body.innerHTML = '<div class="empty">计算中…</div>';
  try{
    const r = await fetch('/api/config/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: paramPayload()})
    });
    const j = await r.json();
    if(!j.ok){ body.innerHTML = '<div class="empty">' + (j.error || '试算失败') + '</div>'; return; }
    const b = j.before, a = j.after, d = j.delta;
    const cls = v => v > 0 ? 'up' : (v < 0 ? 'down' : 'muted');
    const sg = v => (v > 0 ? '+' : '') + fmt2(v);
    body.innerHTML = `<table>
      <thead><tr><th style="width:34%"></th><th>当前配置</th><th>改后</th><th style="width:22%">变化</th></tr></thead>
      <tbody>
        <tr><td>GMV</td><td>${fmt2(b.gmv)}</td><td>${fmt2(a.gmv)}</td>
            <td class="${cls(d.gmv)}">${sg(d.gmv)}</td></tr>
        <tr><td>商品成本</td><td>${fmt2(b.cost)}</td><td>${fmt2(a.cost)}</td>
            <td class="${cls(d.cost)}">${sg(d.cost)}</td></tr>
        <tr><td>费用合计</td><td>${fmt2(b.fees)}</td><td>${fmt2(a.fees)}</td>
            <td class="${cls(-d.fees)}">${sg(d.fees)}</td></tr>
        <tr><td><b>净利</b></td><td><b>${fmt2(b.net)}</b></td><td><b>${fmt2(a.net)}</b></td>
            <td class="${cls(d.net)}"><b>${sg(d.net)}</b></td></tr>
        <tr><td>净利率</td><td>${b.net_rate}%</td><td>${a.net_rate}%</td><td></td></tr>
      </tbody></table>
      <div style="padding:12px 18px;font-size:12.5px;color:var(--text-3)">
        按 ${j.country} 的费率算的。试算不会保存，满意再点「保存配置」。
      </div>`;
  }catch(e){ body.innerHTML = '<div class="empty">试算出错：' + e.message + '</div>'; }
};

$('#btn-save').onclick = async () => {
  try{
    const r = await fetch('/api/config/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: paramPayload(), formulas})
    });
    const j = await r.json();
    toast(j.ok ? '已保存，下次核算立即生效' : ('保存失败：' + j.error));
  }catch(e){ toast('保存出错：' + e.message); }
};

/* ---------- 数据文件 ---------- */
async function loadData(){
  try{
    const j = await (await fetch('/api/data-files')).json();
    const box = $('#data-list');
    $('#data-hint').textContent = j.items.length ? j.items.length + ' 个' : '';
    if(!j.items.length){
      box.innerHTML = '<div class="empty"><b>还没有数据文件</b>在战斧浏览器里打开店铺页面，点扩展面板「立即运行」</div>';
    }else{
      box.innerHTML = '<table><thead><tr><th>文件名</th><th style="width:110px">大小</th>'
        + '<th style="width:130px">更新时间</th></tr></thead><tbody>'
        + j.items.map(f => `<tr><td>${f.name}</td>
            <td class="muted">${f.size_kb} KB</td>
            <td class="muted">${f.at}</td></tr>`).join('') + '</tbody></table>';
    }
    const s = await (await fetch('/api/status')).json();
    $('#path-data').textContent = j.dir || '—';
    $('#path-out').textContent = j.out_dir || '—';
  }catch(e){ $('#data-list').innerHTML = '<div class="empty">读取失败</div>'; }
}

/* ---------- 启动 ---------- */
setRange('all');
const startView = (location.hash || '#/overview').replace('#/', '') || 'overview';
go(startView);
loadStatus();
loadOverviewReport();
setInterval(loadStatus, 20000);
</script>
</body>
</html>
"""
