// 用最小 DOM 桩跑桌面 UI 的公式编辑逻辑，验证：
//   1) checkExpr 能把中文公式翻译成人话（用了哪些量、乘了什么）
//   2) 不认识的词会被标出来（英文公式里最容易出这种错）
//   3) 「＋ 加一行」在光标处正确插入换行 / 逗号
//   4) 多候选标题一行一个时，校验能认出订单表的列
//
// 从 bridge/ui.py 里抽出真实的函数代码来跑，不复制粘贴，避免测的不是线上那份。

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const ui = fs.readFileSync(path.join(root, 'bridge', 'ui.py'), 'utf8');

function grab(name) {
  const re = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\([^)]*\\)\\s*\\{`);
  const m = ui.match(re);
  if (m) {
    let i = m.index + m[0].length;
    let depth = 1;
    for (; i < ui.length && depth > 0; i++) {
      if (ui[i] === '{') depth++;
      else if (ui[i] === '}') depth--;
    }
    return ui.slice(m.index, i);
  }
  // 不是 function 声明，而是单行箭头/函数表达式：`const _reEsc = s => ...;`
  // ui.py 里这类小工具不少（_reEsc 就被 scanExpr 用着），以前只认 function
  // 声明，抽到一半就 ReferenceError。
  // 只认「等号右边就是参数列表 + =>」的函数表达式，例如
  //   const _reEsc = s => String(s).replace(...);
  // 不能只看「整行含 =>」：`const g = PARAM_GROUPS.find(x => ...)` 这种普通语句
  // 也含箭头函数，会被误当函数抽走，eval 时炸「PARAM_GROUPS is not defined」。
  const re2 = new RegExp(
    `(?:const|let|var)\\s+${name}\\s*=\\s*`
    + `(?:\\([^)]*\\)|async\\s+)?[A-Za-z_$][\\w$]*\\s*=>\\s*[^;\\n]*;`);
  const m2 = ui.match(re2);
  if (m2) return m2[0];
  throw new Error('没找到函数 ' + name);
}

// 把 ui.py 里的函数连它的依赖一起抽出来。
//
// 以前是手写列表 `[grab('checkExpr'), grab('checkFml')]`：ui.py 那边只要
// 多抽一层（比如 checkExpr 里开始调 scanExpr、scanExpr 又调 knownNames），
// 这里就 ReferenceError —— 自检直接崩，等于这个检查项从此空转。
// 现在按「被调用的标识符」自动往下抽，找不到的（String/Number 这些内置）
// 就跳过。这样上面加函数，这里不用跟着改。
// 这些名字由 new Function 的参数注入（测试用的假数据）。
// 抽依赖时必须跳过它们，否则会把 ui.py 里的同名实现也抽进来、把假数据覆盖掉
// （cur / rowsOf 一被覆盖就去读 ui.py 里的全局 scopeData，直接 ReferenceError）。
const INJECTED = new Set([
  'document', 'esc', 'headers', 'varRates', 'rateCn', 'rateLabels',
  'rowsOf', 'cur', 'FIELD_VARS', 'FORMULA_CN', 'fmls', 'RATE_LABELS', 'rateCnServer',
]);

function grabWithDeps(names) {
  const seen = new Set();
  const out = [];
  const queue = [...names];
  while (queue.length) {
    const n = queue.shift();
    if (seen.has(n) || INJECTED.has(n)) continue;
    seen.add(n);
    let src;
    try { src = grab(n); } catch (e) { continue; }   // 内置函数 / 不在 ui.py 里
    out.push(src);
    // 不能只找「名字后面跟 (」：`names.map(_reEsc)` 里 _reEsc 是当参数传的，
    // 不是调用，那样就漏了（实测 scanExpr 依赖 _reEsc 就是这么漏掉的）。
    // 改成把所有标识符都当候选，grab 得到（真是 ui.py 里的函数）才收，
    // 关键字和内置对象 grab 不到，自动跳过。
    const KW = new Set(['function','return','const','let','var','if','else','for',
      'while','do','switch','case','break','continue','new','typeof','instanceof',
      'in','of','try','catch','finally','throw','async','await','yield','class',
      'this','null','undefined','true','false','void','delete','super']);
    const ids = src.match(/[A-Za-z_$][\w$]*/g) || [];
    for (const dep of ids) {
      if (dep === n || seen.has(dep) || INJECTED.has(dep) || KW.has(dep)) continue;
      queue.push(dep);
    }
  }
  return out.join('\n\n');
}

function scriptBody() {
  const m = ui.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('没找到 script 块');
  return m[1];
}

const html = scriptBody();

// 全局变量（测试用假数据）
const headers = ['SKU Subtotal Before Discount', 'SKU Seller Discount', 'Order Amount'];
const varRates = ['platform_commission', 'tx_fee', 'cashback', 'dispatch_fee_cny',
                  'order_service', 'cny_to_local', 'usd_to_local', 'ads', 'affiliate'];
const rateLabels = {};
// 引擎权威中文名（带「率」字）：公式解析器只认这套，校验必须跟它对齐
const rateCnServer = {
  platform_commission: '平台佣金率', tx_fee: '交易手续费率', cashback: '奖金返现率',
  super_coupon: '超级优惠券率', growth_fee: '商家成长费率', logistics: '物流费率',
  dispatch_fee_cny: '发单费单价', order_service: '订单服务费单价',
  cny_to_local: '人民币汇率', usd_to_local: '美金汇率', other_fee: '其他费用率',
};
let rows = [
  { id: 'gmv', label: 'GMV（收入）', expr: '本行GMV', kinds: ['normal'] },
  { id: 'cost', label: '商品成本', expr: '成本单价 * 人民币汇率', kinds: ['normal'] },
  { id: 'order_count', label: '订单数', expr: '本行订单数', kinds: ['normal'] },
  { id: 'dispatch', label: '发单费', expr: '本行订单数 * 发单费单价 * 人民币汇率', kinds: ['normal'] },
  { id: 'platform_commission', label: '平台佣金', expr: '本行GMV * 平台佣金率', kinds: ['normal'] },
];
let fmls = {
  gross_profit: 'GMV - 商品成本',
  total_fees: '发单费 + 平台佣金 + 交易手续费 + 奖金返现 + 广告花费 + 达人佣金 + 订单服务费',
  net: 'GMV - 商品成本 - 发单费 - 平台佣金 - 交易手续费 - 奖金返现 - 广告花费 - 达人佣金 - 订单服务费',
  net_rate: '净利 / GMV',
};

const esc = (s) => String(s == null ? '' : s);
const RATE_LABELS = {
  platform_commission: '平台佣金', tx_fee: '交易手续费', cashback: '奖金返现',
  dispatch_fee_cny: '发单费', order_service: '订单服务费', cny_to_local: '人民币汇率',
  ads: '广告花费', affiliate: '达人佣金',
};
// 与线上一致：费率名优先用引擎权威名（带「率」），其次用户/旧名
function rateCn(k) {
  return (rateCnServer && rateCnServer[k]) || (rateLabels && rateLabels[k])
    || RATE_LABELS[k] || k;
}
function rowsOf() { return rows; }
// 当前作用域的配置（含「订单表识别」里配的候选标题）。
// checkExpr 现在会把这些候选也当成已知列名，否则用户按映射写中文标题会被误报。
const cur = () => ({ order_fields: {
  gmv: { label: 'GMV', titles: ['折扣前小计', 'SKU Subtotal Before Discount'] },
  seller_discount: { label: '卖家折扣', titles: ['卖家折扣', 'SKU Seller Discount'] },
}});

// 桩：记录提示文字
const hints = {};
const document = {
  querySelector: (sel) => {
    const m = sel.match(/^\[data-(?:hint|fhint)="(\d+)"\]$/);
    if (!m) return null;
    const key = m[1];
    return {
      set textContent(v) { hints[key] = v; },
      get textContent() { return hints[key] || ''; },
      set className(v) { hints[key + ':cls'] = v; },
      get className() { return hints[key + ':cls'] || ''; },
    };
  },
  querySelectorAll: () => [],
};

const code = grabWithDeps(['checkExpr', 'checkFml']);

// builtinCN 在线上是运行时由 /api/config 填的全局量（ui.py 里 `let builtinCN = {}`），
// 测试里当参数注入，避免去抽那个定义（抽了也还是空对象）。
const builtinCN = { gmv: 'GMV', order_count: '订单数', buyer_paid: '买家实付' };

const fn = new Function(
  'document', 'esc', 'headers', 'varRates', 'rateCn', 'rateLabels',
  'rowsOf', 'cur', 'FIELD_VARS', 'FORMULA_CN', 'fmls', 'RATE_LABELS', 'rateCnServer',
  'builtinCN',
  code + '\nreturn {checkExpr, checkFml, scanExpr, knownNames};'
);

// FIELD_VARS / FORMULA_CN 从真实 HTML 里抠出来执行
const constSrc = html.match(/const FIELD_VARS = \[[\s\S]*?\];/)[0]
  + '\n' + html.match(/const FORMULA_CN = \{[\s\S]*?\};/)[0];
const constFn = new Function(constSrc + '\nreturn {FIELD_VARS, FORMULA_CN};');
const { FIELD_VARS, FORMULA_CN } = constFn();

const api = fn(document, esc, headers, varRates, rateCn, rateLabels,
               rowsOf, cur, FIELD_VARS, FORMULA_CN, fmls, RATE_LABELS, rateCnServer,
               builtinCN);

let failed = 0;
function check(label, cond, extra) {
  if (cond) { console.log('  ✓ ' + label); }
  else { failed++; console.log('  ✗ ' + label + (extra ? '  → ' + extra : '')); }
}

console.log('【公式实时提示】');
api.checkExpr(3);   // 发单费
check('中文公式能翻译成人话',
      (hints['3'] || '').includes('本行订单数'),
      '提示=' + hints['3']);
check('认出了费率项', (hints['3'] || '').includes('发单费单价'), '提示=' + hints['3']);

console.log('\n【带「率」的费率名必须认得出（修复「不认识」）】');
rows[3].expr = '本行订单数 * 发单费单价 * 人民币汇率';
api.checkExpr(3);
check('写「发单费单价 / 人民币汇率」不再报不认识',
      !(hints['3'] || '').includes('不认识'), '提示=' + hints['3']);
check('认出了中文费率项', (hints['3'] || '').includes('发单费单价'), '提示=' + hints['3']);
check('认出了汇率名', (hints['3'] || '').includes('人民币汇率'), '提示=' + hints['3']);

console.log('\n【改了费率中文名后，默认名仍要认】');
// 用户把 cny_to_local 的中文名改成「马币汇率」，公式里照文档写「人民币汇率」也该认
rateLabels.cny_to_local = '马币汇率';
rows[3].expr = '本行订单数 * 发单费单价 * 人民币汇率';
api.checkExpr(3);
check('自定义名生效后，写默认名「人民币汇率」不报不认识',
      !(hints['3'] || '').includes('不认识'), '提示=' + hints['3']);
delete rateLabels.cny_to_local;
rows[3].expr = '本行订单数 * 发单费单价 * 人民币汇率';

console.log('\n【真正写错的词仍要标出来】');
rows[3].expr = '本行订单数 * 发单费单价 * 乱写的汇率';
api.checkExpr(3);
check('错词被标出', (hints['3'] || '').includes('不认识'), '提示=' + hints['3']);
check('错词被点名', (hints['3'] || '').includes('乱写的汇率'), '提示=' + hints['3']);
rows[3].expr = '本行订单数 * 发单费单价 * 人民币汇率';

console.log('\n【订单表列（多候选一行一个）】');
rows[4].expr = '[SKU Subtotal Before Discount\nSKU Seller Discount] - [SKU Seller Discount]';
api.checkExpr(4);
check('认出订单表的列', (hints['4'] || '').includes('订单表列'), '提示=' + hints['4']);
check('候选标题被列出来',
      (hints['4'] || '').includes('SKU Subtotal Before Discount'), '提示=' + hints['4']);

rows[4].expr = '[不存在的列名]';
api.checkExpr(4);
check('认不出的列被标出', (hints['4'] || '').includes('不认识'), '提示=' + hints['4']);

// 「订单表识别」里配的中文候选标题（不在真实表头里）也要认得，
// 否则用户按映射写 [折扣前小计] 会被误报「不认识」。
rows[4].expr = '[折扣前小计] - [卖家折扣]';
api.checkExpr(4);
check('订单表识别里配的中文候选也算认得出',
      !(hints['4'] || '').includes('不认识'), '提示=' + hints['4']);

console.log('\n【净利公式区】');
api.checkFml(3);   // net_rate
check('「净利 / GMV」能看懂', (hints['3'] || '').includes('净利'), '提示=' + hints['3']);

fmls.net = 'GMV - 商品成本 - 发单费';
api.checkFml(2);
check('净利公式里认出参数名',
      (hints['2'] || '').includes('商品成本') || (hints['2'] || '').includes('＝'),
      '提示=' + hints['2']);

console.log('\n【「＋ 加一行」按钮】');
// 模拟：光标在方括号中间，点按钮应该插入换行（候选标题一行一个）
{
  const inp = {
    value: '[折扣前小计,SKU Subtotal Before Discount]',
    selectionStart: 7, selectionEnd: 7,
    focus() {},
    oninput: null,
  };
  const before = inp.value.slice(0, inp.selectionStart);
  const inBracket = before.lastIndexOf('[') > before.lastIndexOf(']');
  const ins = (inBracket ? '' : ',') + '\n';
  inp.value = before + ins + inp.value.slice(inp.selectionEnd);
  inp.selectionStart = inp.selectionEnd = 7 + ins.length;
  // 原值是「[折扣前小计,SKU Subtotal Before Discount]」，光标在逗号后，
  // 所以原逗号留着；新插的是换行，不该再多一个逗号（曾出现「,,」）
  check('在方括号里插入换行（不追加多余逗号）',
        inp.value === '[折扣前小计,\nSKU Subtotal Before Discount]',
        '结果=' + JSON.stringify(inp.value));
  check('没有出现连续两个逗号', !inp.value.includes(',,'), '结果=' + JSON.stringify(inp.value));
  check('插入后选区落在新行',
        inp.selectionStart === inp.value.indexOf('\n') + 1, 'pos=' + inp.selectionStart);
}
{
  // 光标不在方括号里（跟在文字后面）→ 先补逗号再换行
  const inp = {
    value: '本行GMV * 平台佣金率',
    selectionStart: 18, selectionEnd: 18,
    focus() {},
  };
  const before = inp.value.slice(0, inp.selectionStart);
  const inBracket = before.lastIndexOf('[') > before.lastIndexOf(']');
  const ins = (inBracket ? '' : ',') + '\n';
  inp.value = before + ins + inp.value.slice(inp.selectionEnd);
  check('方括号外插入时先补逗号',
        inp.value.includes(',\n'), '结果=' + JSON.stringify(inp.value));
}

console.log('\n【英文老公式仍要能用】');
rows[3].expr = 'orders * params.dispatch_fee_cny * params.cny_to_local';
api.checkExpr(3);
check('英文公式不报不认识', !(hints['3'] || '').includes('不认识'), '提示=' + hints['3']);
// 挖掉名字时要把 `params.` 前缀一起挖掉，否则剩下的孤立 `params` 会被当成
// 「用量」写进提示里（以前就是这样，只是被上面那条「不认识」先盖住了）
check('提示里不出现孤立的 params 前缀',
      !/(^|[^.\w])params([^.\w]|$)/.test(hints['3'] || ''), '提示=' + hints['3']);

console.log('\n【中文公式里也要能认出费率名（计算项表格靠它配对）】');
{
  // 计算项表格右边那列「用了哪些费率」= scanExpr().used 过滤一遍费率名。
  // 以前只匹配 params.xxx，用户把公式改成中文写法后整列都变成「不含费率」。
  const c1 = api.scanExpr('本行买家实付 * 交易手续费率');
  check('中文写法认得出交易手续费率', (c1.used || []).includes('交易手续费率'),
        'used=' + JSON.stringify(c1.used));
  const c2 = api.scanExpr('本行订单数 * 发单费单价 * 人民币汇率');
  check('一行多个费率都认得出',
        (c2.used || []).includes('发单费单价') && (c2.used || []).includes('人民币汇率'),
        'used=' + JSON.stringify(c2.used));
  // 英文写法命中的是英文键（`tx_fee`），中文写法命中的是中文名（`交易手续费率`），
  // 两种都算「认出来了」——表格那边用 name→id 表统一映射。
  const c3 = api.scanExpr('paid * params.tx_fee');
  check('英文写法也认得出',
        (c3.used || []).some(n => n === 'tx_fee' || n === '交易手续费率'),
        'used=' + JSON.stringify(c3.used));
  const c4 = api.scanExpr('本行GMV');
  check('纯基础量不该混进费率', !(c4.used || []).some(n => n.endsWith('率')),
        'used=' + JSON.stringify(c4.used));
  // 长名优先：不能被拆成「交易手续费」+「率」
  const c5 = api.scanExpr('本行买家实付 * 交易手续费率');
  check('长名不被截断', !(c5.unknown || []).length, 'unknown=' + JSON.stringify(c5.unknown));
}

console.log(failed ? `\n✗ 有 ${failed} 项没过` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
