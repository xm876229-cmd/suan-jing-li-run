# -*- coding: utf-8 -*-
"""自检：下载拦截器能不能拦住平台的所有下载触发方式。

为什么单独做这一项（2026-11-15）：
    跑 PHT-M2 时浏览器弹出了「另存为」对话框，整家店的取数被堵死 8 分钟。
    原因是拦截器当时只拦了 `<a>.click()` / `window.open` /
    `URL.createObjectURL` 三种，而平台实际用的是
    `a.dispatchEvent(new MouseEvent('click'))`、`form.submit()`、
    动态插 `<iframe src=文件地址>`——全在范围外。
    漏一次就真发起下载，浏览器「下载前询问保存位置」一弹框，那个标签页就停了。

这个脚本把 background.js 里 installDownloadGuard 的函数体原样抽出来，
在 node 里配一套最小 DOM 跑一遍，四种触发方式必须全部被记下、且都不真下载。
抽的是真代码，不是抄一份——抄的副本测了等于没测。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def extract_guard_body() -> str:
    src = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8", errors="replace")
    i = src.index("async function installDownloadGuard")
    k = src.index("func: () => {", i)
    start = k + len("func: () => {")
    depth = 1
    p = start
    while p < len(src) and depth:
        ch = src[p]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        p += 1
    if depth:
        raise ValueError("installDownloadGuard 的函数体括号不配平")
    return src[start:p - 1]


HARNESS = r"""
const FILE_URL = "https://seller-ph.tiktok.com/api/v2/insights/export/123_abc?aid=4068";

// 最小 DOM：EventTarget -> El -> {Anchor, Form}
function EventTarget() {}
EventTarget.prototype.dispatchEvent = function (ev) {
  if (this && this.__tag === "A") { this.__realDownload = true; }
  return true;
};

function El() { this.nodeType = 1; this.attrs = {}; this.__clicks = 0; }
El.prototype = Object.create(EventTarget.prototype);
El.prototype.getAttribute = function (n) { return (n in this.attrs) ? this.attrs[n] : null; };
El.prototype.hasAttribute = function (n) { return n in this.attrs; };
El.prototype.remove = function () { this.removed = true; };
El.prototype.click = function () { this.__clicks++; this.__realDownload = true; };

function Anchor() {}
Anchor.prototype = Object.create(El.prototype);

function Form() {}
Form.prototype = Object.create(El.prototype);
Form.prototype.submit = function () { global.__formSubmitted = true; };

global.EventTarget = EventTarget;
global.HTMLAnchorElement = Anchor;
global.HTMLFormElement = Form;
global.URL = { createObjectURL: () => "blob:fake" };
global.MutationObserver = function (cb) { this.cb = cb; this.observe = () => {}; };
global.document = { documentElement: { nodeType: 1 } };
global.window = {};

const mkA = () => {
  const a = Object.create(Anchor.prototype);
  a.__tag = "A"; a.tagName = "A"; a.href = FILE_URL; a.attrs = {};
  return a;
};

const run = () => {
__BODY__
};

run();
const W = window;
const results = [];

const a1 = mkA();
a1.click();
results.push(["a.click()", W.__jlDl.length > 0, !a1.__realDownload]);

const before2 = W.__jlDl.length;
const a2 = mkA();
a2.dispatchEvent({ type: "click" });
results.push(["a.dispatchEvent(click)", W.__jlDl.length > before2, !a2.__realDownload]);

const before3 = W.__jlDl.length;
const f = Object.create(Form.prototype);
f.tagName = "FORM"; f.action = FILE_URL; f.attrs = {};
f.submit();
results.push(["form.submit()", W.__jlDl.length > before3, global.__formSubmitted !== true]);

const before4 = W.__jlDl.length;
const mo = W.__jlGuardMO;
const iframe = { nodeType: 1, tagName: "IFRAME", src: FILE_URL,
                 remove: function () { this.removed = true; } };
if (mo) mo.cb([{ addedNodes: [iframe] }]);
results.push(["插入 <iframe src=文件>", W.__jlDl.length > before4, iframe.removed === true]);

let bad = 0;
for (const [name, recorded, notHappened] of results) {
  const ok = recorded && notHappened;
  if (!ok) bad++;
  console.log((ok ? "  OK  " : "  FAIL") + "  " + name +
    "  (已记录=" + recorded + " 未真下载=" + notHappened + ")");
}
console.log("记录条数：" + W.__jlDl.length);
process.exit(bad ? 1 : 0);
"""


def main() -> int:
    try:
        body = extract_guard_body()
    except Exception as e:  # noqa: BLE001
        print(f"[缺失] 下载拦截器：抽不出函数体（{e}）")
        return 1

    js = HARNESS.replace("__BODY__", body)
    tmp = Path(tempfile.gettempdir()) / "jl_guard_check.js"
    tmp.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True, timeout=90)
    out = (r.stdout or "").strip()
    if out:
        print(out)
    if r.returncode != 0:
        if r.stderr:
            print((r.stderr or "").strip()[:800])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
