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
  if (!m) throw new Error('没找到函数 ' + name);
  let i = m.index + m[0].length;
  let depth = 1;
  for (; i < ui.length && depth > 0; i++) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
  }
  return ui.slice(m.index, i);
}

// 从 HTML 里抠出 <script> 块，取里面的全局常量（FIELD_VARS / FORMULA_CN）
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

const code = [
  grab('checkExpr'),
  grab('checkFml'),
].join('\n\n');

const fn = new Function(
  'document', 'esc', 'headers', 'varRates', 'rateCn', 'rateLabels',
  'rowsOf', 'cur', 'FIELD_VARS', 'FORMULA_CN', 'fmls', 'RATE_LABELS', 'rateCnServer',
  code + '\nreturn {checkExpr, checkFml};'
);

// FIELD_VARS / FORMULA_CN 从真实 HTML 里抠出来执行
const constSrc = html.match(/const FIELD_VARS = \[[\s\S]*?\];/)[0]
  + '\n' + html.match(/const FORMULA_CN = \{[\s\S]*?\};/)[0];
const constFn = new Function(constSrc + '\nreturn {FIELD_VARS, FORMULA_CN};');
const { FIELD_VARS, FORMULA_CN } = constFn();

const api = fn(document, esc, headers, varRates, rateCn, rateLabels,
               rowsOf, cur, FIELD_VARS, FORMULA_CN, fmls, RATE_LABELS, rateCnServer);

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

console.log(failed ? `\n✗ 有 ${failed} 项没过` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
