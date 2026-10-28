"""参数表配置界面。

在浏览器里打开 http://127.0.0.1:8765/config 就能改口径：
每个参数（GMV、商品成本、发单费、平台佣金……）
都有一排复选框，勾选它算哪些订单类别。

为什么放在桥里而不是另做一个程序：
桥本来就在跑、就监听在本机回环地址上，加一个页面零成本，
而且改完点「试算」能立刻看到净利变化——这是配口径时最需要的东西：
不是改完再跑一遍报表才知道对不对。

数据只在本机读写，不联网。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src import formula, paths

KINDS: list[tuple[str, str]] = [
    ("normal", "正常"),
    ("cancelled", "已取消"),
    ("refunded", "退货退款"),
    ("zero_amount", "0元样品"),
    ("unpaid", "未付款"),
]

BASES: list[tuple[str, str]] = [
    ("gmv", "GMV"),
    ("buyer_paid", "买家实付"),
    ("order_count", "订单数"),
    ("items", "件数"),
    ("cost", "商品成本"),
    ("refund", "退款金额"),
    ("commission", "达人佣金（直接取数）"),
    ("ads", "广告花费（直接取数）"),
]

# 试算用的数据缓存：同一份数据反复试算不必反复解析
_CACHE: dict[str, Any] = {"key": None, "data": None, "params": None}


def config_path() -> Path:
    return paths.CONFIG / "formulas.json"


def read_config() -> dict:
    """读当前配置（带默认值兜底）。"""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    cfg = formula.load_config(config_path())
    return {
        "params": cfg.get("params") or [],
        "formulas": cfg.get("formulas") or {},
        "kinds": cfg.get("kinds") or dict(KINDS),
        "raw_note": doc.get("_readme") or [],
    }


def save_config(params: list, formulas: dict, keep_readme: bool = True) -> dict:
    """保存参数表。保留原来的 _readme 说明文字，别把它冲掉。"""
    old: dict = {}
    try:
        old = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        old = {}

    out: dict = {}
    if keep_readme and old.get("_readme"):
        out["_readme"] = old["_readme"]
    out["kinds"] = old.get("kinds") or dict(KINDS)
    out["params"] = params
    out["formulas"] = formulas or {}

    config_path().write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"ok": True, "path": str(config_path()), "params": len(out["params"])}


def _load_data():
    """加载核算数据（按目录 + 文件数缓存，避免每次试算都重新解析）。"""
    inbox = paths.INBOX
    try:
        st = inbox.stat()
        files = sorted(p.name for p in inbox.iterdir() if p.is_file())
        key = (str(inbox), st.st_mtime_ns, len(files), tuple(files[-6:]))
    except OSError:
        return None, "找不到数据目录 data/exports/_inbox"

    if _CACHE["key"] == key and _CACHE["data"] is not None:
        return _CACHE["data"], None

    from src.calc import load_rates
    from src.loader import load_from_dir

    rates, _sw = load_rates(paths.RATES_FILE)
    country = next(iter(rates), None)
    if not country:
        return None, "config/rates.json 里没有国家参数"
    p = rates[country]

    costs = paths.DATA / "costs" / "master.csv"
    mapping = paths.DATA / "mapping" / "master.csv"
    try:
        data = load_from_dir(
            inbox,
            costs if costs.exists() else None,
            p.cny_to_local,
            mapping if mapping.exists() else None,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"读取数据失败：{e}"

    _CACHE.update({"key": key, "data": data, "params": (country, p, _sw)})
    return data, None


def preview(params: list, formulas: dict | None = None) -> dict:
    """用给定的参数表试算，并与当前配置对比。

    这是配口径时最重要的功能：改一个勾选，立刻看到净利差多少。
    """
    data, err = _load_data()
    if data is None:
        return {"ok": False, "error": err}

    from src.calc import compute

    country, p, sw = _CACHE["params"]
    cfg_now = formula.load_config(config_path())

    def run(cfg: dict) -> dict:
        r = compute(data, p, sw, fcfg=cfg)
        t = r.total
        return {
            "gmv": round(t.gmv, 2),
            "cost": round(t.cost_local, 2),
            "fees": round(t.fee_total, 2),
            "net": round(t.net_local, 2),
            "net_rate": round(t.net_margin * 100, 2),
            "orders": round(t.orders, 2),
            "products": len(r.products),
            "fee_detail": {k: round(v, 2) for k, v in t.fees.items() if abs(v) > 0.005},
        }

    before = run(cfg_now)
    trial_cfg = dict(cfg_now)
    trial_cfg["params"] = params
    if formulas:
        trial_cfg["formulas"] = {**cfg_now.get("formulas", {}), **formulas}
    after = run(trial_cfg)

    return {
        "ok": True,
        "country": country,
        "currency": getattr(p, "currency", ""),
        "before": before,
        "after": after,
        "delta": {
            "net": round(after["net"] - before["net"], 2),
            "cost": round(after["cost"] - before["cost"], 2),
            "fees": round(after["fees"] - before["fees"], 2),
            "gmv": round(after["gmv"] - before["gmv"], 2),
        },
    }


def render_html() -> str:
    cfg = read_config()
    boot = json.dumps(
        {
            "params": cfg["params"],
            "formulas": cfg["formulas"],
            "kinds": [{"id": k, "label": v} for k, v in KINDS],
            "bases": [{"id": k, "label": v} for k, v in BASES],
        },
        ensure_ascii=False,
    )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return _TEMPLATE.replace("__BOOT__", boot).replace("__STAMP__", stamp)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>净利计算 · 参数配置</title>
<style>
  :root{
    --bg:#f6f6f3; --card:#fff; --line:#e2e1da; --text:#22221f; --muted:#6f6e68;
    --accent:#0f6e56; --accent-soft:#e1f5ee; --warn:#993c1d; --warn-soft:#faece7;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:24px 28px 60px;background:var(--bg);color:var(--text);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  h1{font-size:18px;font-weight:500;margin:0 0 4px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:0;overflow:hidden;margin-bottom:18px}
  .bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
       padding:12px 16px;border-bottom:1px solid var(--line);background:#fbfbf9}
  .bar .ttl{font-weight:500;margin-right:auto}
  table{width:100%;border-collapse:collapse}
  th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);
        font-size:13px;vertical-align:middle}
  th{background:#fbfbf9;font-weight:500;color:#45443f;white-space:nowrap;
     position:sticky;top:0;z-index:1}
  tr:last-child td{border-bottom:none}
  tr.off td{opacity:.45}
  input[type=text],input[type=number],select{
    width:100%;padding:5px 7px;border:1px solid var(--line);border-radius:6px;
    font:inherit;font-size:13px;background:#fff;color:var(--text)}
  input[type=text]:focus,input[type=number]:focus,select:focus{
    outline:2px solid var(--accent-soft);border-color:var(--accent)}
  .id{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--muted)}
  .kbox{display:flex;gap:2px;justify-content:center}
  .kbox label{display:flex;flex-direction:column;align-items:center;gap:2px;
              cursor:pointer;padding:3px 5px;border-radius:6px;user-select:none}
  .kbox label:hover{background:#f2f1ec}
  .kbox input{width:15px;height:15px;accent-color:var(--accent);cursor:pointer}
  .kbox span{font-size:11px;color:var(--muted);white-space:nowrap}
  button{font:inherit;font-size:13px;padding:7px 14px;border-radius:8px;
         border:1px solid var(--line);background:#fff;color:var(--text);cursor:pointer}
  button:hover{background:#f2f1ec}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{background:#0c5b47}
  button.danger{color:var(--warn)}
  button.mini{padding:3px 8px;font-size:12px}
  .colhead{font-size:11px;color:var(--muted);font-weight:400}
  .wide{min-width:200px}
  .num{min-width:96px}
  .result{display:none;padding:16px;background:var(--accent-soft);
          border-top:1px solid var(--line)}
  .result.show{display:block}
  .result table{background:transparent}
  .result td{border-bottom:1px solid rgba(15,110,86,.15);font-size:14px}
  .delta.up{color:var(--accent);font-weight:500}
  .delta.down{color:var(--warn);font-weight:500}
  .hint{color:var(--muted);font-size:12px;padding:10px 16px;background:#fbfbf9;
        border-top:1px solid var(--line)}
  .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);
         background:#22221f;color:#fff;padding:10px 20px;border-radius:8px;
         font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
  .toast.show{opacity:1}
  .scroll{overflow-x:auto}
  .tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;
       background:var(--accent-soft);color:var(--accent);margin-left:6px}
  .tag.warn{background:var(--warn-soft);color:var(--warn)}
</style>
</head>
<body>
<h1>净利计算 · 参数配置</h1>
<div class="sub">每个参数自己选算哪些订单类别。勾选 = 计入，不勾 = 不计入。
改动后点「试算」立刻看净利变化，满意再保存。　<span id="stamp">__STAMP__</span></div>

<div class="card">
  <div class="bar">
    <span class="ttl">参数表</span>
    <button class="mini" onclick="toggleAll(true)">全选所有类别</button>
    <button class="mini" onclick="toggleAll(false)">全部取消</button>
    <button class="mini" onclick="addRow()">+ 新增参数</button>
  </div>
  <div class="scroll">
  <table id="tbl">
    <thead>
      <tr>
        <th class="wide">参数</th>
        <th class="colhead" style="width:130px">基数（乘什么）</th>
        <th class="colhead" style="width:130px">费率</th>
        <th colspan="5" style="text-align:center">算哪些订单类别</th>
        <th style="width:56px">启用</th>
        <th style="width:50px"></th>
      </tr>
    </thead>
    <tbody id="body"></tbody>
  </table>
  </div>
  <div class="hint">
    费率可以写数字（如 <b>2</b> 表示每单 2 元），也可以写 <b>params.xxx</b> 引用国家费率，
    留空表示原样取基数（GMV、成本这类就是这么用的）。
    一行订单只归一类：已取消 &gt; 退货退款 &gt; 0元样品 &gt; 未付款 &gt; 正常。
  </div>
</div>

<div class="card">
  <div class="bar">
    <span class="ttl">试算与保存</span>
    <button onclick="doPreview()">试算（看净利变化）</button>
    <button class="primary" onclick="doSave()">保存配置</button>
  </div>
  <div class="result" id="res"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const BOOT = __BOOT__;
let rows = BOOT.params.map(p => ({...p, kinds: p.kinds === undefined ? null : [...p.kinds]}));

const KIND_IDS = BOOT.kinds.map(k => k.id);
const KIND_LABEL = Object.fromEntries(BOOT.kinds.map(k => [k.id, k.label]));

function fmt(n){
  if(n === null || n === undefined) return '';
  return Number(n).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function el(tag, cls, html){ const d = document.createElement(tag); if(cls) d.className = cls; if(html !== undefined) d.innerHTML = html; return d; }

function render(){
  const body = document.getElementById('body');
  body.innerHTML = '';
  rows.forEach((r, i) => {
    const tr = el('tr');
    if(r.enabled === false) tr.classList.add('off');

    const td1 = el('td');
    const nm = el('input'); nm.type = 'text'; nm.value = r.label || r.id || '';
    nm.oninput = () => { rows[i].label = nm.value; };
    const idl = el('div', 'id', (r.id || '') + (r.kinds === null ? ' <span class="tag">全部</span>' : (r.kinds.length === 0 ? ' <span class="tag warn">不算</span>' : '')));
    td1.append(nm, idl);
    tr.append(td1);

    const td2 = el('td');
    const sel = el('select');
    BOOT.bases.forEach(b => {
      const o = el('option'); o.value = b.id; o.textContent = b.label;
      if(b.id === (r.base || 'gmv')) o.selected = true;
      sel.append(o);
    });
    sel.onchange = () => { rows[i].base = sel.value; };
    td2.append(sel);
    tr.append(td2);

    const td3 = el('td');
    const rt = el('input'); rt.type = 'text';
    rt.value = r.rate === undefined || r.rate === null ? '' : String(r.rate);
    rt.placeholder = '留空 = 原样取';
    rt.oninput = () => { andRate(i, rt.value); };
    td3.append(rt);
    tr.append(td3);

    KIND_IDS.forEach(k => {
      const td = el('td');
      const box = el('div', 'kbox');
      const lab = el('label');
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = r.kinds === null ? true : (r.kinds || []).includes(k);
      cb.onchange = () => {
        if(rows[i].kinds === null) rows[i].kinds = [...KIND_IDS];
        const s = new Set(rows[i].kinds);
        if(cb.checked) s.add(k); else s.delete(k);
        rows[i].kinds = KIND_IDS.filter(x => s.has(x));
        render();
      };
      const sp = el('span', null, KIND_LABEL[k]);
      lab.append(cb, sp);
      box.append(lab);
      td.append(box);
      tr.append(td);
    });

    const td5 = el('td');
    const en = el('input'); en.type = 'checkbox'; en.style.width='16px'; en.style.height='16px';
    en.style.accentColor = '#0f6e56';
    en.checked = r.enabled !== false;
    en.onchange = () => { rows[i].enabled = en.checked; render(); };
    td5.append(en);
    tr.append(td5);

    const td6 = el('td');
    const del = el('button', 'mini danger', '删');
    del.onclick = () => { if(confirm('删掉「' + (r.label || r.id) + '」这一项？')) { rows.splice(i,1); render(); } };
    td6.append(del);
    tr.append(td6);

    body.append(tr);
  });
}

function andRate(i, v){
  if(v.trim() === ''){ delete rows[i].rate; return; }
  const n = Number(v);
  rows[i].rate = Number.isFinite(n) && v.trim() !== '' ? n : v.trim();
}

function toggleAll(on){
  rows.forEach(r => { r.kinds = on ? [...KIND_IDS] : []; });
  render();
}

function addRow(){
  const id = prompt('参数的英文标识（公式里要用它，建议英文小写，如 order_service）：');
  if(!id) return;
  if(rows.some(r => r.id === id)){ toast('这个标识已经存在'); return; }
  const label = prompt('显示名称（如：订单服务费）', id) || id;
  rows.push({id, label, base:'order_count', rate:0, kinds:['normal'], enabled:true});
  render();
}

function payload(){
  return rows.map(r => {
    const o = {id:r.id, label:r.label, base:r.base || 'gmv'};
    if(r.rate !== undefined && r.rate !== null && r.rate !== '') o.rate = r.rate;
    if(r.kinds !== null) o.kinds = r.kinds;
    if(r.enabled === false) o.enabled = false;
    return o;
  });
}

async function doPreview(){
  const res = document.getElementById('res');
  res.className = 'result show';
  res.innerHTML = '计算中…';
  try{
    const r = await fetch('/api/config/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: payload()})
    });
    const j = await r.json();
    if(!j.ok){ res.innerHTML = '<b>试算失败：</b>' + (j.error || '未知错误'); return; }
    const b = j.before, a = j.after, d = j.delta;
    const cls = v => v > 0 ? 'up' : (v < 0 ? 'down' : '');
    const sign = v => (v > 0 ? '+' : '') + fmt(v);
    res.innerHTML = `
      <div style="font-size:13px;color:#0f6e56;margin-bottom:10px">
        国家：${j.country}　当前配置 → 你正在编辑的配置
      </div>
      <table>
        <tr><th style="width:150px"></th><th>当前</th><th>改后</th><th>变化</th></tr>
        <tr><td>GMV</td><td>${fmt(b.gmv)}</td><td>${fmt(a.gmv)}</td><td class="delta ${cls(d.gmv)}">${sign(d.gmv)}</td></tr>
        <tr><td>商品成本</td><td>${fmt(b.cost)}</td><td>${fmt(a.cost)}</td><td class="delta ${cls(d.cost)}">${sign(d.cost)}</td></tr>
        <tr><td>费用合计</td><td>${fmt(b.fees)}</td><td>${fmt(a.fees)}</td><td class="delta ${cls(-d.fees)}">${sign(d.fees)}</td></tr>
        <tr><td><b>净利</b></td><td><b>${fmt(b.net)}</b></td><td><b>${fmt(a.net)}</b></td>
            <td class="delta ${cls(d.net)}">${sign(d.net)}</td></tr>
        <tr><td>净利率</td><td>${b.net_rate}%</td><td>${a.net_rate}%</td><td></td></tr>
      </table>`;
  }catch(e){
    res.innerHTML = '<b>试算出错：</b>' + e.message;
  }
}

async function doSave(){
  try{
    const r = await fetch('/api/config/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({params: payload(), formulas: BOOT.formulas})
    });
    const j = await r.json();
    toast(j.ok ? '已保存（' + j.params + ' 个参数），下次核算立即生效' : ('保存失败：' + j.error));
  }catch(e){ toast('保存出错：' + e.message); }
}

let toastTimer = null;
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = 'toast', 2400);
}

render();
</script>
</body>
</html>
"""
