"""真跑一遍界面脚本（用 Node + 极简 DOM 桩），抓「引用了不存在的变量」这类错。

为什么需要它：
    2026-11-11 那一版，`FEE_ROWS` 常量在重写时被删掉了，调用还在。
    `node --check` 只查语法，查不出这种错；`check_ext` 只查「函数有没有定义」，
    常量不算函数，也放过去了。结果「订单扣款」页整块渲染失败，
    界面上只显示一句「读取费率失败」，看着像连不上桥。

    这类错的后果都很重（整块 / 整页不显示），必须在打包前跑一遍，
    而不是等用户装完才看见白屏。

用法：
    python -m scripts.check_ui_runtime            # 有错退出码 1
    python -m scripts.check_ui_runtime --quiet    # 只输出结论

没装 node 时直接跳过（退出码 0），并说明跳过原因。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "bridge" / "ui.py"

STUB = r"""
const fs = require('fs');
const RATES = JSON.parse(fs.readFileSync(process.env.RATES_FILE, 'utf8'));
// 引擎认的中文名（src/formula.py 的 BUILTIN_CN），由 Python 侧导出，
// 不在这里再抄一份——抄一份就测不出「界面和引擎对不上」这个错。
const BUILTIN = process.env.BUILTIN_FILE
  ? JSON.parse(fs.readFileSync(process.env.BUILTIN_FILE, 'utf8')) : {};
function makeEl(tag){
  const el = {
    tagName: (tag||'div').toUpperCase(),
    style:{}, dataset:{}, checked:false, disabled:false, value:'',
    textContent:'', innerHTML:'', title:'', src:'',
    classList:{add(){},remove(){},toggle(){},contains(){return false}},
    append(){}, appendChild(){}, removeChild(){}, remove(){}, focus(){}, blur(){},
    setAttribute(){}, getAttribute(){return null}, removeAttribute(){},
    addEventListener(){}, removeEventListener(){}, click(){},
    insertAdjacentHTML(){}, scrollIntoView(){}, scrollTo(){},
    querySelector(){ return makeEl('div'); },
    querySelectorAll(){ return []; },
    closest(){ return makeEl('label'); },
    getBoundingClientRect(){ return {top:0,left:0,width:0,height:0}; },
  };
  return new Proxy(el, {
    get(t,k){ return (k in t) ? t[k] : undefined; },
    set(t,k,v){ t[k]=v; return true; }
  });
}
const _CACHE = new Map();
function _pick(sel){
  if(!_CACHE.has(sel)) _CACHE.set(sel, makeEl('div'));
  return _CACHE.get(sel);
}
const doc = {
  body: makeEl('body'), documentElement: makeEl('html'),
  querySelector: _pick,
  querySelectorAll(){ return []; },
  getElementById(id){ return _pick('#' + id); },
  createElement(t){ return makeEl(t); },
  addEventListener(){}, removeEventListener(){}, execCommand(){ return true; },
};
global.document = doc;
global.window = {
  addEventListener(){}, scrollTo(){}, matchMedia(){ return {matches:false, addEventListener(){}}; },
  location:{hash:''}, history:{replaceState(){}},
  localStorage:{getItem(){return null}, setItem(){}, removeItem(){}},
};
global.location = global.window.location;
global.history = global.window.history;
global.navigator = {userAgent:'node'};
global.localStorage = global.window.localStorage;
global.alert = () => {};
global.confirm = () => true;
global.prompt = () => 'x';
global.requestAnimationFrame = f => setTimeout(f, 0);
global.matchMedia = () => ({matches:false, addEventListener(){}});
global.getComputedStyle = () => ({getPropertyValue(){ return ''; }});

global.fetch = async (url) => {
  const u = String(url);
  const j = (o) => ({ok:true, status:200, json: async () => o, text: async () => JSON.stringify(o)});
  if (u.includes('/api/rates')) return j({ok:true, countries: RATES.countries || {}, labels: RATES.labels || {}});
  if (u.includes('/api/fx')) return j({ok:true, rate_date:'2026-11-10', provider:'stub'});
  if (u.includes('/api/config/preview')) return j({ok:false, error:'stub 不试算'});
  if (u.includes('/api/config')) return j({
    formulas: {gross_profit:'GMV - 商品成本', net:'GMV - 商品成本 - 物流费', net_rate:'净利 / GMV'},
    headers: [], params: [
      {id:'gmv', label:'GMV（收入）', expr:'本行GMV'},
      {id:'buyer_paid', label:'买家实付', expr:'本行买家实付'},
      {id:'order_count', label:'订单数', expr:'本行订单数'},
      {id:'items', label:'件数', expr:'本行件数'},
      {id:'cost', label:'商品成本', expr:'成本单价 * 人民币本地'},
      {id:'dispatch', label:'发单费', expr:'本行订单数 * 发单费单价', kinds:['normal']},
      {id:'platform_commission', label:'平台佣金', expr:'本行GMV * 平台佣金率'},
      {id:'logistics', label:'物流费', expr:'本行GMV * 物流费率', kinds:['normal']},
      {id:'ads', label:'广告花费', expr:'广告花费', kinds:null},
      {id:'affiliate', label:'联盟佣金', expr:'联盟佣金', kinds:null},
      {id:'storage_fee', label:'仓储费', expr:'本行GMV * 仓储费'},
    ],
    order_fields:{}, cost_fields:{}, ads_fields:{}, affiliate_fields:{},
    mapping_fields:{}, exclude_zero_amount:false, report_columns:null,
    // 后端下发的列目录：这里故意只给 3 项，用来验证前端**优先用它**、
    // 而不是自己从 params 推（推出来的会是一大串，长度对不上就说明没用上）
    report_column_catalog: [
      {key:'rank', header:'排名', kind:'text'},
      {key:'gmv', header:'GMV', kind:'money'},
      {key:'net', header:'净利', kind:'money'},
    ],
    builtin_cn: BUILTIN,
  });
  return j({ok:true, items:[], count:0, rules:[]});
};
"""

RUNNER = r"""
process.on('unhandledRejection', e => console.log('!! Promise 错误: ' + (e && e.message)));
setTimeout(() => { console.log('!! 超时未结束（可能有死循环）'); process.exit(2); }, 15000);
(async () => {
  const bad = [];
  const t = (n, f) => { try { f(); } catch (e) { bad.push(n + ' → ' + e.message); } };
  try { await loadParams(); } catch (e) { bad.push('loadParams → ' + e.message); }
  try { await loadRates(); } catch (e) { bad.push('loadRates → ' + e.message); }
  // stub 里给的是老写法「GMV - 商品成本」，读进来就该补成「GMV（收入）」
  t('老配置读进来就补口径', () => {
    if (String(fmls.gross_profit || '').indexOf('GMV（收入）') < 0)
      bad.push('loadParams 没给毛利补 GMV（收入）：' + fmls.gross_profit);
  });
  t('renderAll', () => renderAll());
  ['income','fees','spend','costs','formula'].forEach(k => t('页签 ' + k, () => setPTab(k)));
  ['overview','manual','schedule','reports','import','stores','calc','fields',
   'settings','help','debug'].forEach(v => t('页面 ' + v, () => go(v)));
  t('renderFormulas', () => renderFormulas());
  t('renderFeeOverview', () => renderFeeOverview());
  // 「订单扣款」页只管 收多少/开不开/算哪几类；公式统一去「计算公式」改。
  // 2026-11-12 用户反馈：两页都能改表达式 = 同一份数据三处能改，早晚不同步。
  t('扣款总览不再有公式输入框', () => {
    const h = document.querySelector('#fee-overview').innerHTML || '';
    if (h.indexOf('data-ov-expr') >= 0) bad.push('扣款总览又把公式输入框加回来了（应只留费率/开关/类别）');
    ['扣款项', '费率（各国）', '计入净利', '订单类别'].forEach(x => {
      if (h.indexOf(x) < 0) bad.push('扣款总览缺少列：' + x);
    });
    if (h.indexOf('data-ov-on') < 0) bad.push('扣款总览没有「计入净利」开关');
    if (h.indexOf('data-ov-k') < 0) bad.push('扣款总览没有「订单类别」勾选');
  });
  t('auditFees', () => auditFees());
  // 报表列目录必须**优先用后端下发的那份**（2026-11-15：前端自己推，
  // 「展示列设置」渲染时 params 还没到位 → 一个费用列都列不出来，
  // 用户两次反馈「费率列又没了」）。
  t('报表列目录用后端下发的', () => {
    const cat = reportColumnCatalog();
    if (cat.length !== 3) {
      bad.push('reportColumnCatalog 没用后端下发的目录（拿到 ' + cat.length + ' 项，应为 3）');
    }
    // 没有后端目录时，回退逻辑仍要能把费用列推出来
    const keep = REPORT_CAT; REPORT_CAT = null;
    try {
      const back = reportColumnCatalog();
      if (!back.some(x => String(x.key).indexOf('fee:') === 0)) {
        bad.push('后端目录缺失时，回退推导也没出费用列');
      }
    } finally { REPORT_CAT = keep; }
  });
  // 启动时先进「报表」页（还没去过计算中心）：列目录必须自己补上。
  // 2026-11-15 用户实测：刚开程序进报表，只有基础 23 列；
  // 去「计算中心 → 计算公式」点一下再回来，费用列才出现。
  REPORT_CAT = null; paramsLoaded = false;
  renderReportColumns();
  await new Promise(r => setTimeout(r, 600));
  {
    // 桩里的目录只有 3 项（故意），所以查的是「有没有自动去拉」，
    // 不是「有没有费用列」——真实后端下发的目录里才带 fee: 列。
    if (!Array.isArray(REPORT_CAT) || !REPORT_CAT.length) {
      bad.push('启动时直接进报表页，列目录没自动补上（会只剩基础列）');
    }
    const html = String((document.querySelector('#rcols-body') || {}).innerHTML || '');
    if (html.indexOf('net') < 0) {
      bad.push('列目录补上了，但报表列没重画（列表还是旧的）');
    }
  }
  t('renderParams', () => renderParams());
  t('renderNetCheck', () => renderNetCheck());
  t('计算项分组切换', () => {
    // 现在只渲染「当前选中的那一组」，不再一次性铺 4 段
    const expect = { income: 4, deduct: 4, spend: 2, cost: 1 };
    Object.keys(expect).forEach(k => {
      _grpActive = k;
      renderParams();
      const h = document.querySelector('#pbody').innerHTML || '';
      const n = (h.match(/data-expr=/g) || []).length;
      if (n !== expect[k]) bad.push('「' + k + '」组行数不对: ' + n + '（应为 ' + expect[k] + '）');
      const ks = (h.match(/<summary class="ksum">/g) || []).length;
      if (ks !== n) bad.push('「' + k + '」订单类别没收成一格: ' + ks + '/' + n);
    });
    _grpActive = 'income';
    renderParams();
    const pt = document.querySelector('#pg-tabs').innerHTML || '';
    if (!pt.includes('收入与数量') || !pt.includes('data-pg="cost"')) bad.push('组按钮没渲染出来');
    const fx = document.querySelector('#fx-tabs').innerHTML || '';
    if (!fx.includes('data-fx="formula"') || !fx.includes('data-fx="items"')) bad.push('公式/计算项按钮没渲染');
  });
  // 「插入参数」下拉必须能选到引擎认的名字，否则用户只能手打、打错就红字
  t('插入参数下拉', () => {
    _grpActive = 'cost';
    renderParams();
    const h = document.querySelector('#pbody').innerHTML || '';
    const m = h.match(/<select class="ins-param"[^>]*>([\s\S]*?)<\/select>/);
    if (!m) { bad.push('「插入参数」下拉没渲染出来'); return; }
    ['成本合计', '人民币本地', '商品成本', '本行GMV（收入）'].forEach(n => {
      if (!m[1].includes('value="' + n + '"')) bad.push('「插入参数」下拉里没有「' + n + '」');
    });
    if ((m[1].match(/<option/g) || []).length < 15) bad.push('「插入参数」下拉选项太少');
  });
  // 公式校验：认识的不能误报，写错的必须报
  t('公式校验不误报', () => {
    const rows = rowsOf();
    const ci = rows.findIndex(r => r.id === 'cost');
    rows[ci].expr = '成本合计 * 人民币本地';
    checkExpr(ci);
    const ok = document.querySelector('[data-hint="' + ci + '"]').textContent;
    if (/不认识/.test(ok)) bad.push('「成本合计 * 人民币本地」被误报：' + ok);
    if (!ok.includes('成本合计')) bad.push('认识的公式没翻译出来：' + ok);
    rows[ci].expr = '成本合汁 * 人民币本地';
    checkExpr(ci);
    const badTxt = document.querySelector('[data-hint="' + ci + '"]').textContent;
    if (!/不认识/.test(badTxt)) bad.push('写错的「成本合汁」没被报出来');
    rows[ci].expr = '成本单价 * 人民币本地';
  });
  // 净利公式里的 GMV 必须带口径，而且带了口径也不能被误报
  t('净利公式 GMV 口径', () => {
    fmls.net = 'GMV（收入） - 商品成本 - 物流费';
    renderFormulas();
    const i = Object.keys(fmls).indexOf('net');
    checkFml(i);
    const txt = document.querySelector('[data-fhint="' + i + '"]').textContent;
    if (/不认识/.test(txt)) bad.push('「GMV（收入）」在净利公式里被误报：' + txt);
    if (buildDefaultNet().indexOf('GMV（收入）') < 0) bad.push('buildDefaultNet 没写 GMV（收入）');
    const tray = document.querySelector('#ftray').innerHTML || '';
    if (!tray.includes('GMV（收入）')) bad.push('变量按钮里没有「GMV（收入）」');
    if (!tray.includes('买家实付')) bad.push('变量按钮里没有「买家实付」');
  });
  // 公式页那三行也要有「插入参数」下拉（以前只有计算项有，公式页只能手打）
  t('公式页插入参数下拉', () => {
    fmls = {
      gross_profit: 'GMV（收入） - 商品成本',
      net: 'GMV（收入） - 商品成本 - 物流费',
      net_rate: '净利 / GMV（收入）',
    };
    renderFormulas();
    const h = document.querySelector('#fbody').innerHTML || '';
    const n = (h.match(/data-fins=/g) || []).length;
    if (n !== 3) bad.push('公式页「插入参数」下拉数量不对: ' + n + '（应为 3）');
    const m = h.match(/<select class="ins-param"[^>]*data-fins="0"[^>]*>([\s\S]*?)<\/select>/);
    if (!m) { bad.push('毛利那一行没有「插入参数」下拉'); return; }
    ['成本合计', '人民币本地', '净利', '净利率'].forEach(nm => {
      if (!m[1].includes('value="' + nm + '"')) bad.push('公式页下拉里没有「' + nm + '」');
    });
    if (m[1].includes('value="毛利"')) bad.push('毛利那一行的下拉里出现了「毛利」（会自引用）');
  });
  // 往公式框插字：不能把两个词粘成一个（引擎就认不出了）
  t('插入参数不粘词', () => {
    const mk = (v, touched) => ({value: v, selectionStart: v.length, selectionEnd: v.length,
                                 __touched: touched, dataset: {}, focus(){}});
    const a = mk('成本合计 * 人民币本地', true);
    insertAtCursor(a, '本行GMV（收入）');
    if (a.value !== '成本合计 * 人民币本地 本行GMV（收入）')
      bad.push('插入空格不对：' + a.value);
    const b = mk('', false);
    insertAtCursor(b, '净利');
    if (b.value !== '净利') bad.push('空框插入不对：' + b.value);
    // 前面是运算符时不该多补空格
    const c = mk('GMV -', true);
    insertAtCursor(c, '商品成本');
    if (c.value !== 'GMV - 商品成本') bad.push('运算符后多补了空格：' + c.value);
  });
  t('GMV 补口径', () => {
    const cases = [
      ['GMV - 商品成本', 'GMV（收入） - 商品成本'],
      ['本行GMV * 物流费率', '本行GMV（收入） * 物流费率'],
      ['净利 / GMV', '净利 / GMV（收入）'],
      ['GMV（收入） - 商品成本', 'GMV（收入） - 商品成本'],
    ];
    cases.forEach(([a, b]) => {
      const got = normalizeGmv(a);
      if (got !== b) bad.push('normalizeGmv("' + a + '") = "' + got + '"，应为 "' + b + '"');
    });
  });
  if (bad.length) {
    console.log('!! 出错 ' + bad.length + ' 处');
    bad.forEach(x => console.log('   ' + x));
    process.exit(1);
  }
  console.log('OK 渲染 / 5 个页签 / 11 个页面切换 全部通过');
  process.exit(0);
})();
"""


def _inline_script(text: str) -> str:
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
    return "\n".join(blocks)


def main() -> int:
    quiet = "--quiet" in sys.argv
    node = shutil.which("node")
    if not node:
        if not quiet:
            print("[跳过] 没装 node，界面脚本无法真跑（建议装上 node 以便发现白屏类错误）")
        return 0

    if not UI.exists():
        print(f"[失败] 找不到 {UI}")
        return 1

    code = _inline_script(UI.read_text(encoding="utf-8"))
    if not code.strip():
        print("[失败] ui.py 里没找到内嵌脚本")
        return 1

    rates_path = ROOT / "release" / "净利助手" / "配置" / "rates.json"
    if not rates_path.exists():
        rates_path = ROOT / "配置" / "rates.json"
    if rates_path.exists():
        rates_json = rates_path.read_text(encoding="utf-8")
    else:
        rates_json = '{"countries": {}, "labels": {}}'

    tmp = Path(tempfile.mkdtemp(prefix="ui_runtime_"))
    try:
        rf = tmp / "rates.json"
        rf.write_text(rates_json, encoding="utf-8")
        # 引擎的中文名表：直接从 src/formula.py 导出，测的就是真名单
        bf = tmp / "builtin.json"
        try:
            sys.path.insert(0, str(ROOT))
            from src import formula as _formula
            bf.write_text(json.dumps(_formula.BUILTIN_CN, ensure_ascii=False), encoding="utf-8")
        except Exception:      # noqa: BLE001
            bf.write_text("{}", encoding="utf-8")
        js = tmp / "run.js"
        # 脚本本身是 SCRIPT；stub 必须先执行，所以拼在前面
        js.write_text(STUB + "\n" + code + "\n" + RUNNER, encoding="utf-8")
        r = subprocess.run([node, str(js)], capture_output=True, text=True,
                           timeout=90, env={**__import__("os").environ,
                                            "RATES_FILE": str(rf),
                                            "BUILTIN_FILE": str(bf)})
        out = (r.stdout or "") + (r.stderr or "")
        if quiet:
            ok = "OK 渲染" in out
            print("界面真跑：" + ("通过" if ok else "失败"))
        else:
            print(out.strip()[-1500:])
        return 0 if r.returncode == 0 else 1
    except subprocess.TimeoutExpired:
        print("[失败] 界面脚本跑超时（可能有死循环或死等）")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
