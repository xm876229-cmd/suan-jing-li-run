// 用最小 DOM 桩跑桌面 UI 的快照多选逻辑，验证：
//   1) 清单能渲染出每个快照一行，且默认只勾最新一份
//   2) 快捷键「全部 / 近 3 次 / 仅最新 / 不指定」勾选正确
//   3) selectedSnaps() 返回的正是要发给 /run 的快照 id
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

// ---- DOM 桩 ----
const SNAPS = [
  { id: 'B', at: '2026-10-29 10:00:00', range: '2026-10-16~2026-10-31', file_count: 3, kinds: { orders: 700 } },
  { id: 'A', at: '2026-10-29 09:00:00', range: '2026-10-01~2026-10-15', file_count: 2, kinds: { orders: 693 } },
];

let html = '';
const store = { value: '7496027585645218321' };
const els = {
  'snap-list': {
    set innerHTML(v) { html = v; },
    get innerHTML() { return html; },
    querySelectorAll(sel) {
      // 每次按当前 html 重新解析：checked 的读写都反映到 html 上，
      // 这样 pickSnaps 的勾选动作能真正留下来（桩也要讲道理）
      const list = [];
      const re = /<input type="checkbox" value="([^"]*)"(\s*checked)?\s*>/g;
      let m;
      const onlyChecked = String(sel).includes(':checked');
      while ((m = re.exec(html))) {
        const value = m[1];
        const box = {
          value,
          get checked() {
            return new RegExp('<input type="checkbox" value="' + value + '"\\s*checked').test(html);
          },
          set checked(v) {
            const re2 = new RegExp('(<input type="checkbox" value="' + value + '")(\\s*checked)?(\\s*>)');
            html = html.replace(re2, (_, a, c, b) => a + (v ? ' checked' : '') + b);
          },
        };
        if (onlyChecked && !box.checked) continue;
        list.push(box);
      }
      return list;
    },
  },
  'store': store,
  'run-files-hint': { textContent: '' },
};

const ctx = {
  document: {
    // 真实 UI 里 $ = s => document.querySelector(s)，选择器带 #
    querySelector: (sel) => els[String(sel).replace(/^#/, '')] || null,
    getElementById: (id) => els[id] || null,
    querySelectorAll: () => [],
  },
  fetch: async (url) => {
    if (String(url).includes('/api/snapshots')) {
      return { json: async () => ({ ok: true, items: SNAPS }) };
    }
    return { json: async () => ({}) };
  },
  esc: (s) => String(s),
  console,
};
ctx.window = ctx;

const code = [grab('loadSnapshots'), grab('selectedSnaps'), grab('pickSnaps')].join('\n\n')
  // 测试里把吞异常的 catch 改成往外抛，否则出错也看不见
  .replace(/catch\(e\)\{\}/g, 'catch(e){throw e;}');
// loadSnapshots 里用了 snapStore（模块级变量），这里一并声明
const $ = (sel) => ctx.document.querySelector(sel);
const fn = new Function('document', 'fetch', 'esc', 'console', '$', 'let snapStore = "";'
  + code + '\nreturn {loadSnapshots, selectedSnaps, pickSnaps};');
const api = fn(ctx.document, ctx.fetch, ctx.esc, ctx.console, $);

(async () => {
  await api.loadSnapshots();
  const boxes = els['snap-list'].querySelectorAll('input');
  console.log('渲染出的勾选框数量:', boxes.length, '（应为 2）');
  console.log('默认勾选:', boxes.filter(b => b.checked).map(b => b.value), '（应只有最新的一份 B）');
  console.log('清单含区间文字:', html.includes('2026-10-01~2026-10-15') && html.includes('2026-10-16~2026-10-31'));

  api.pickSnaps('all');
  console.log('「全部」后:', api.selectedSnaps());

  api.pickSnaps('last3');
  console.log('「近 3 次」后:', api.selectedSnaps());

  api.pickSnaps('latest');
  console.log('「仅最新」后:', api.selectedSnaps());

  api.pickSnaps('none');
  console.log('「不指定」后:', api.selectedSnaps(), '（应为空数组 → 走实时数据）');

  // 换店铺后应清掉旧勾选
  await api.loadSnapshots();
  console.log('重新加载后默认勾选:', api.selectedSnaps());
  store.value = 'other-shop';
  await api.loadSnapshots();
  console.log('换店铺后默认勾选:', api.selectedSnaps(), '（应为最新一份）');
})();
