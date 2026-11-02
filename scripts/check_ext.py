"""检查扩展脚本里有没有「只有引用、没有定义」的函数。

为什么需要：`node --check` 只验证语法，不验证符号存在。
v0.3.6 一次替换漏插了 appendTaskLog 的定义，导致之后所有任务在写第一行
日志时就抛 ReferenceError，而且是静默的——排查了五轮才定位到。

用法：
    python -m scripts.check_ext
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT
FILES = ["浏览器扩展/background.js", "浏览器扩展/popup.js", "浏览器扩展/inject.js", "浏览器扩展/relay.js"]

# 语言内置与浏览器 API，不算缺失
BUILTIN = set("""
if for while switch catch return typeof instanceof function new await async delete void
Array Object String Number Boolean Date Math JSON Promise Set Map WeakMap WeakSet Proxy Reflect
RegExp Error TypeError RangeError SyntaxError fetch setTimeout clearTimeout setInterval
clearInterval requestAnimationFrame btoa atob encodeURIComponent decodeURIComponent
parseInt parseFloat isNaN isFinite structuredClone queueMicrotask
URL URLSearchParams TextEncoder TextDecoder Uint8Array Uint16Array Uint32Array Int8Array
ArrayBuffer DataView Blob File FileReader FormData Headers Request Response AbortController
console chrome browser self window document navigator localStorage sessionStorage location
history performance customElements HTMLElement Node NodeList MutationObserver
XMLHttpRequest WebSocket Image Audio Option Event CustomEvent
String.fromCharCode Array.from Object.entries Object.keys Object.assign Object.values
console.log console.warn console.error console.info JSON.stringify JSON.parse
resolve reject sendResponse next done cb callback fallback
confirm prompt alert open close focus blur print
""".split())

# 常见的数组/字符串方法，出现在链式调用里会误报
METHODS = set("""
map filter forEach find findIndex some every reduce reduceRight sort reverse push pop shift
unshift splice slice concat join indexOf lastIndexOf includes startsWith endsWith trim
trimStart trimEnd padStart padEnd repeat replace replaceAll split match matchAll search
toLowerCase toUpperCase substring substr charAt charCodeAt codePointAt at flat flatMap
fill keys values entries from isArray of asin sin cos tan
text json blob arrayBuffer clone catch then finally
""".split())


def strip_literals(src: str) -> str:
    """把字符串与模板字符串的内容抹掉，只留代码骨架。

    为什么必须做：文件里有 ``creative data for product campaigns (…)`` 这样的
    模板字符串，正则会把它当成 `campaigns(` 这个函数调用，
    于是报一个根本不存在的「缺失函数」。
    假阳性比漏报更坏——它会让每次检查都红，久了就没人看这个检查了。
    """
    src = re.sub(r"`(?:[^`\\]|\\.)*`", '""', src, flags=re.S)     # 模板字符串
    src = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)                # 双引号
    # 正则字面量：/post_(a|b)/i 里的 post_ 不是函数名
    src = re.sub(r"(?<![\w\)\]])\/(?![\/*])(?:[^\/\\\n]|\\.)+\/[gimsuy]*", '""', src)
    src = re.sub(r"'(?:[^'\\\n]|\\.)*'", '""', src)                # 单引号
    return src


def check_text(src: str) -> list[str]:
    """检查一段 JS 源码里有没有「只有调用、没有定义」的函数。"""
    code = strip_literals(src)

    defined = set(re.findall(r"(?:async\s+)?function\s+(\w+)", src))
    # 具名函数 / 箭头函数的形参也算「已定义」：
    # `const bind = (sel, key, cast) => {...}` 里的 cast 是参数，不是漏掉的函数
    for params in re.findall(r"(?:function[^(]*\(([^)]*)\)|\(([^)]*)\)\s*=>)", code):
        for part in (params if isinstance(params, str) else "").split(","):
            nm = part.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", nm or ""):
                defined.add(nm)
    for grp in re.findall(r"(?:function[^(]*\(([^)]*)\)|\(([^)]*)\)\s*=>)", code):
        for chunk in grp:
            for part in chunk.split(","):
                nm = part.split("=")[0].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", nm or ""):
                    defined.add(nm)
    defined |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=", src))
    defined |= set(re.findall(r"class\s+(\w+)", src))
    defined |= set(re.findall(r"(\w+)\s*[:=]\s*(?:async\s*)?(?:function)?\s*\(", src))
    # 解构赋值里的变量名
    for block in re.findall(r"(?:const|let)\s*[\[{]([^}\]]*)[\]}]\s*(?:of|in|\\=)", src):
        for part in block.split(","):
            name = part.split(":")[-1].strip().split("=")[0].strip()
            if re.fullmatch(r"\w+", name or ""):
                defined.add(name)

    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code))
    return sorted(
        c for c in called
        if c not in defined and c not in BUILTIN and c not in METHODS and len(c) > 3
    )


def check(path: Path) -> list[str]:
    if not path.exists():
        return []
    return check_text(path.read_text(encoding="utf-8"))


def check_ui() -> list[str]:
    """界面（bridge/ui.py）里那段内嵌 JS 也要查。

    为什么单列：界面是「一大段 HTML 字符串」，删页面时很容易连带删掉
    别的页面在用的函数（v0.24.10 就这么把 fileToB64 删了，
    导致「导入数据」和「手动核算」两处都点不动，还没人看得出来）。
    """
    try:
        from bridge import ui as _ui
        html = _ui.render_html()
    except Exception as e:  # noqa: BLE001
        return [f"（界面渲染失败：{e}）"]
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    missing = check_text("\n".join(blocks))
    # 导航里有这个页面，HTML 里却没有对应区块：点进去是空白，
    # 而且脚本里对它的引用全是空转（v0.24.10 把「导入数据」整页删掉就是这样）
    views = set(re.findall(r'data-view="([a-z0-9-]+)"', html))
    pages = set(re.findall(r'id="view-([a-z0-9-]+)"', html))
    for v in sorted(views - pages):
        missing.append(f"（导航「{v}」没有对应页面区块 #view-{v}）")
    return missing


def main() -> int:
    bad = 0
    targets = [(rel, check(ROOT / rel)) for rel in FILES]
    targets.append(("bridge/ui.py（内嵌界面脚本）", check_ui()))
    for rel, missing in targets:
        if missing:
            bad += 1
            print(f"[缺失] {rel}")
            for m in missing[:20]:
                print(f"    {m}(")
        else:
            print(f"[OK]   {rel}")
    if bad:
        print("\n有函数被调用但找不到定义，运行时会抛 ReferenceError 并静默失败。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
