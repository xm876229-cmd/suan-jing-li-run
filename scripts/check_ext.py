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
MouseEvent KeyboardEvent PointerEvent DragEvent SubmitEvent
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


def check_imports() -> list[str]:
    """检查 `from src import xxx` / `from bridge import xxx` 里的模块真的存在。

    为什么需要：这种导入通常写在函数体内（为了不拖慢启动），
    语法检查查不出来，只有点到那个功能才炸。
    v0.24.12 就有一处：`from src import manual_api`（实际在 bridge 里），
    结果「手动报表 → 导出带公式」一点就 500。
    """
    import ast

    bad: list[str] = []
    for f in [*sorted(ROOT.glob("src/*.py")), *sorted(ROOT.glob("bridge/*.py")),
              *sorted(ROOT.glob("scripts/*.py"))]:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{f.relative_to(ROOT)} 语法错误：{e}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in ("src", "bridge"):
                continue
            for a in node.names:
                mod = ROOT / node.module / f"{a.name}.py"
                init = ROOT / node.module / "__init__.py"
                if mod.is_file():
                    continue
                txt = init.read_text(encoding="utf-8") if init.is_file() else ""
                if f"def {a.name}" in txt or f"{a.name} =" in txt:
                    continue
                bad.append(f"{f.relative_to(ROOT)}: from {node.module} import {a.name}")
    return bad


def check(path: Path) -> list[str]:
    if not path.exists():
        return []
    return check_text(path.read_text(encoding="utf-8"))


def _js_syntax_error(code: str) -> str:
    """用 node --check 验一遍内嵌脚本的语法（没装 node 就跳过）。

    为什么非加不可：0.24.87 有两行正则少写了一个右括号
    （`(after && !/^[…]/.test(after) ? ' ' : ''`），整个前端脚本语法错，
    界面停在「正在读取店铺…」、所有按钮都点不动。
    而当时的 check_ext 只查「函数有没有定义」，语法错照样报 OK，
    于是这个版本被打包、发布，用户装完才发现用不了。
    """
    import os
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        return ""
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(code)
            path = f.name
        r = subprocess.run(["node", "--check", path], capture_output=True,
                           text=True, timeout=60)
        if r.returncode != 0:
            lines = [ln.strip() for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
            return " ".join(lines[:4])[:400]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    return ""


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
    code = "\n".join(blocks)
    syntax = _js_syntax_error(code)
    if syntax:
        return [f"语法错误（浏览器里会整页失灵）：{syntax}"]
    missing = check_text(code)
    # 导航里有这个页面，HTML 里却没有对应区块：点进去是空白，
    # 而且脚本里对它的引用全是空转（v0.24.10 把「导入数据」整页删掉就是这样）
    views = set(re.findall(r'data-view="([a-z0-9-]+)"', html))
    pages = set(re.findall(r'id="view-([a-z0-9-]+)"', html))
    for v in sorted(views - pages):
        missing.append(f"（导航「{v}」没有对应页面区块 #view-{v}）")
    return missing


# 「停止」必须真停：这三处守卫少任何一处，点了停止那一轮还会继续核算、
# 继续登记待补，订单落地后自己又算一遍（2026-11-11 15:36 实测：
# 停止 0.7 秒回执，20 秒后仍登记了待补，用户看到报表凭空冒出来）。
STOP_GUARDS = [
    ("async function runTaskThenCalc", "取数完了还要核算的那一段"),
    ("async function registerPendingCalc", "登记「等订单」待补"),
    ("async function calcNow", "真正发核算请求"),
]


def check_stop_guards(src: str | None = None) -> list[str]:
    """停止守卫必须在，否则「停止」等于按了个寂寞。

    src 可传，方便自测「删掉守卫会不会被这条拦住」。
    """
    if src is None:
        src = (ROOT / "浏览器扩展/background.js").read_text(encoding="utf-8", errors="replace")
    out = []
    for sig, what in STOP_GUARDS:
        at = src.find(sig)
        if at < 0:
            out.append(f"找不到函数（{sig}）")
            continue
        nxt = src.find("\nasync function ", at + 1)
        body = src[at: nxt if nxt > 0 else at + 4000]
        if "isStoppedRun()" not in body:
            out.append(f"{sig} 里没有 isStoppedRun() 守卫（{what}会停不下来）")
    return out


def check_ads_order(src: str | None = None) -> list[str]:
    """广告取数必须「11 列优先、4 列兜底」，顺序被改回去就报警。

    为什么锁这个顺序：11 列的「商品维度报表」货币列写真实币种（USD/PHP/MYR），
    4 列那份只写 `LOCAL`（账号本位币），得靠「费率 → 该国 → 广告表币种」猜。
    2026-11-11 马来店就因为猜成 USD 差点把广告费算成 1/4。
    两条路都要留（用户要求），但**11 列必须排在前面**。

    src 可传，方便自测「把顺序改回去会不会被这条拦住」。
    """
    if src is None:
        src = (ROOT / "浏览器扩展/background.js").read_text(encoding="utf-8", errors="replace")
    at = src.find("  ads: async (range, log) => {")
    if at < 0:
        return ["找不到 TASKS.ads（广告取数入口）"]
    # 截到 TASKS 对象结尾。源码是 CRLF（Windows 检出），
    # 直接找 "\n};" 匹配不到 —— 那样会退化成「截后面 4000 字」，
    # 检查范围就飘了，以后加几行就可能漏判。这里按行尾两种写法都认。
    m = re.search(r"\r?\n  \},\r?\n\};", src[at:])
    body = src[at: at + m.start()] if m else src[at: at + 4000]

    want = [
        ("exportAdsByApi", "11 列「商品维度报表」接口"),
        ("exportAdsByClickFresh", "11 列页面导出"),
        ("runSpecTask(\"ads\"", "4 列规格接口"),
        ("pullAdsDirect", "4 列页面直取"),
        ("runAds(range, log, { noDirect: true })", "最后一层：通用页面导出 / 查已完成任务"),
    ]
    out = []
    pos = -1
    for sig, what in want:
        i = body.find(sig)
        if i < 0:
            out.append(f"TASKS.ads 里没有 {what}（{sig}）")
            continue
        if i < pos:
            out.append(f"顺序不对：{what} 排到了后面（要求 11 列 → 4 列）")
        pos = i
    # 4 列那份不能再排在最前面
    i4 = body.find("runSpecTask(\"ads\"")
    i11 = body.find("exportAdsByApi")
    if i4 >= 0 and i11 >= 0 and i4 < i11:
        out.append("4 列接口排在了 11 列前面：币种会退回 LOCAL，广告费可能差几倍")
    return out


def check_ads_schedule() -> list[str]:
    """真跑一遍 TASKS.ads 的调度（node scripts/check_ads_order.js）。

    上面的 check_ads_order 只比字符串先后，管不住「顺序对但逻辑写错」
    （比如只试一次就不往下走）。这里把那段函数体抠出来注入假依赖真跑，
    8 种场景逐一断言调用顺序和兜底落点。没装 node 就跳过。
    """
    import shutil
    import subprocess
    js = ROOT / "scripts" / "check_ads_order.js"
    if not js.is_file():
        return ["缺少 scripts/check_ads_order.js（广告调度自检）"]
    if not shutil.which("node"):
        return []
    try:
        r = subprocess.run(["node", str(js)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return [f"调度自检跑不起来：{e}"]
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()]
    bad = [l for l in lines if l.startswith("✗")]
    return bad or [l for l in lines[-3:] if l] or ["广告调度自检没通过"]


def check_order_pick() -> list[str]:
    """真跑一遍「订单导出记录怎么挑」（node scripts/check_order_pick.js）。

    这段逻辑错了不会报错：要么一直等一个永远不好的任务（实测白等 9 分钟），
    要么更糟——拿错区间的订单去算净利。所以 9 种情形必须逐一断言。
    """
    import shutil
    import subprocess
    js = ROOT / "scripts" / "check_order_pick.js"
    if not js.is_file():
        return ["缺少 scripts/check_order_pick.js（订单记录挑选自检）"]
    if not shutil.which("node"):
        return []
    try:
        r = subprocess.run(["node", str(js)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return [f"订单挑选自检跑不起来：{e}"]
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()]
    return [l for l in lines if l.startswith("✗")] or lines[-3:]


def check_kinds_sync() -> list[str]:
    """「订单类别」的勾选必须**双向**联动。

    两处都能勾：计算中心 → 计算项（每行一个 details）、订单扣款 → 扣款总览。
    以前只有「计算项 → 总览」这一向：总览的 onChange 只重绘自己，不重绘计算项，
    于是在订单扣款里勾掉一类，切到计算项还是老状态（2026-11-12 用户反馈）。
    """
    src = (ROOT / "bridge" / "ui.py").read_text(encoding="utf-8", errors="replace")
    out = []
    for needle, need, what in (
        ("querySelectorAll('[data-ov-k]')", "renderParams()",
         "扣款总览改了订单类别却没重绘「计算项」（会变成单向联动）"),
        ("querySelectorAll('[data-k]')", "renderFeeOverview()",
         "计算项改了订单类别却没重绘「扣款总览」"),
    ):
        at = src.find(needle)
        if at < 0:
            out.append(f"找不到 {needle}")
            continue
        # 只截到这段 handler 结束（`\n  });`），不能给一个大窗口——
        # 窗口里混进下一段代码的 renderParams()，断言就永远为真、等于没检查。
        end = src.find("\n  });", at)
        seg = src[at:end] if end > at else src[at:at + 900]
        if need not in seg:
            out.append(what)
    return out


def check_card_seed() -> list[str]:
    """点「运行」后卡片不许 1 秒就打勾（node/py 真跑 scripts/check_card_seed.py）。

    2026-11-12 踩过：派单占位只清空步骤，但 `_fill_step_files` 会把磁盘上
    上一轮 20 分钟前的文件当成「这一轮已完成」补上去，卡片瞬间全绿，
    用户以为取了旧数据。这里真跑三种情形守住它。
    """
    import subprocess
    import sys as _sys
    py = ROOT / "scripts" / "check_card_seed.py"
    if not py.is_file():
        return ["缺少 scripts/check_card_seed.py（卡片占位自检）"]
    try:
        r = subprocess.run([_sys.executable, str(py)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return [f"卡片占位自检跑不起来：{e}"]
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()]
    return [l for l in lines if l.startswith("✗")] or lines[-3:]


def check_pending_race() -> list[str]:
    """真跑一遍「补订单写回会不会抹掉刚写进来的任务号」（node scripts/check_pending_race.js）。

    补订单线程处理几个任务要十几秒，期间用户可能刚点了「运行」提交了新任务。
    以前结尾是拿进循环时的快照整体覆盖，新任务号就被静默抹掉——之后一直等
    上一轮那个可能永远不生成的旧号，订单表停在上一份，报表是旧数据。
    这条对所有店铺都一样（是时序，不是店铺差异），所以断言也必须对所有店成立。
    """
    import shutil
    import subprocess
    js = ROOT / "scripts" / "check_pending_race.js"
    if not js.is_file():
        return ["缺少 scripts/check_pending_race.js（补订单写回自检）"]
    if not shutil.which("node"):
        return []
    try:
        r = subprocess.run(["node", str(js)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return [f"补订单写回自检跑不起来：{e}"]
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()]
    bad = [l for l in lines if l.startswith("✗")]
    return bad or [l for l in lines[-3:] if l] or ["补订单写回自检没通过"]


def check_formula_ui() -> list[str]:
    """真跑一遍界面上的「公式实时提示」（node scripts/check_formula_ui.js）。

    这段以前是孤儿脚本：没人调用，ui.py 改了依赖之后它一直 ReferenceError，
    等于「公式提示」这项从来没有被自动验过。现在修好并接进来，20 项断言：
    中文公式能翻译成人话、费率名改中文名后仍认默认名、写错的词要点名、
    「＋ 加一行」在方括号内外都要插对。
    """
    import shutil
    import subprocess
    js = ROOT / "scripts" / "check_formula_ui.js"
    if not js.is_file():
        return ["缺少 scripts/check_formula_ui.js（公式提示自检）"]
    if not shutil.which("node"):
        return []
    try:
        r = subprocess.run(["node", str(js)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return [f"公式提示自检跑不起来：{e}"]
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()]
    bad = [l for l in lines if l.startswith("✗")]
    return bad or [l for l in lines[-3:] if l] or ["公式提示自检没通过"]


def check_param_chips() -> list[str]:
    """计算项表格必须把「扣款值」和「费率」分开，并标出各自的公式写法。

    背景（2026-11-12 两次反馈）：
      ① 早先「费率」和「它的结果」被拆成上下两排、中间隔十几项，
         看「交易手续费率」和「交易手续费」要在两排之间来回找；
      ② 两者的英文写法只差一个 s（params.tx_fee / param.tx_fee），
         并排放在标签里分不清哪个是率、哪个是钱。
    现在的规矩：计算项用表格，一行一项，左边写明这项算什么，
    中间是钱（蓝 .val + param.id），右边是率（紫 .rate + params.id）。
    """
    import re
    ui = (ROOT / "bridge" / "ui.py").read_text(encoding="utf-8")
    m = re.search(r"function renderVars\(\)\{[\s\S]*?\n\}", ui)
    if not m:
        return ["找不到 renderVars（计算参数页的变量表）"]
    code = m.group(0)
    bad = []
    need = [
        ('class="vals"', "计算项表格（表格没了，又回到一排标签）"),
        ("cell val", "「扣款值」格子"),
        ("cell rate", "「费率」格子"),
        ("param.${esc(r.id)}", "扣款值的公式写法（param.<id>）"),
        ("params.${esc(k)}", "费率的公式写法（params.<id>）"),
        ("scanExpr(", "从本行公式里解析它用了哪些费率（不能写死）"),
        ("rateName2Id", "费率中文名 ↔ id 的反查表（只认 params.xxx 的话，"
                        "公式写成「交易手续费率」就会全判成没费率）"),
        ("grp(", "分组行（本行基础量 / 计算项 / 其它参数 并在同一张表里）"),
        ("直接取数，不含费率", "没有费率的项（广告/佣金）要有说明，不能留空"),
    ]
    for frag, why in need:
        if frag not in code:
            bad.append(f"缺少{why}（找不到 {frag}）")
    if "cell.val b{color:" not in ui or "cell.rate b{color:" not in ui:
        bad.append("值和费率的颜色没分开（.vals .cell.val / .cell.rate 缺颜色）")
    # 改了某一行的公式/名字，表格必须跟着重画：右边那列「用了哪些费率」
    # 是从本行 expr 解析出来的，不重画就还是旧的（2026-11-12 用户问出来的缺口）
    if "inp.onblur = () => { checkExpr(i); renderVars(); };" not in ui:
        bad.append("改完公式没有重画计算项表格（onblur 里少了 renderVars）")
    if "inp.onblur = () => renderVars();" not in ui:
        bad.append("改完名称没有重画计算项表格（onblur 里少了 renderVars）")
    # 「怎么算的」这一列：要真接上 exprHuman，而且不能退回「一律用 × 连接」
    if "怎么算的" not in code:
        bad.append("表格缺「怎么算的」列（看不到这一项是怎么算出来的）")
    if "exprHuman(r.expr)" not in code:
        bad.append("「怎么算的」列没接上 exprHuman")
    # 汇率/币种本身是配置值，不是「算出来的钱」：扣款值那列必须写明是「固定值」，
    # 不能留「—」（用户会以为少了东西）
    if 'class="fixed"' not in code:
        bad.append("汇率/币种那几行没标明「固定值」（扣款值列留空看着像缺一块）")
    if "function exprHuman(" not in ui:
        bad.append("缺少 exprHuman（把公式翻成人话）")
    else:
        body = ui[ui.index("function exprHuman("):]
        body = body[:body.index("\n\nfunction ")]
        if "join(' × ')" in body:
            bad.append("exprHuman 用「× 」连接全部名字会把减法也说成乘法，必须保留原运算符")
    return bad


def check_trial_manual() -> list[str]:
    """试算必须是**手动**触发的。

    2026-11-12 用户要求：「改成手动启动，不要一启动程序或改了参数后就自动
    在那里算」。preview 要重读订单表，大店五万多行一次几十秒，自动算等于
    一进页面就卡着（截图里那条「试算了 30 秒还没回来」就是这么来的）。
    """
    import re
    ui = (ROOT / "bridge" / "ui.py").read_text(encoding="utf-8")
    bad = []
    # 任何 setTimeout(runTrial / 直接调 runTrial 的自动触发都不许有
    for m in re.finditer(r"setTimeout\(\s*runTrial", ui):
        line = ui[:m.start()].count("\n") + 1
        bad.append(f"ui.py:{line} 还在自动试算（setTimeout(runTrial …)）")
    # 「改完自动跑」的写法也堵掉：setTimeout(() => runTrial(), …)
    for m in re.finditer(r"setTimeout\(\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>\s*|[\w$]+\s*=>\s*)\{?\s*runTrial", ui):
        line = ui[:m.start()].count("\n") + 1
        bad.append(f"ui.py:{line} 还在自动试算（setTimeout 里调 runTrial）")
    if "renderTrialIdle" not in ui:
        bad.append("缺少「还没试算」的空态（进页面要能看到试算按钮）")
    if 'data-trial-run' not in ui:
        bad.append("试算条上没有「试算」按钮（data-trial-run）")
    return bad


def check_pending_gate() -> list[str]:
    """并发闸门不能把「派单占位」算成正在跑。

    2026-11-12 15:37 实测：一次给 8 家点运行，桥先给每家点亮一格
    「已排上，等浏览器领任务」，然后每家来领活时看到其余 7 家都"在跑"
    （上限 `_MAX_ACTIVE_FETCH = 7`），7 >= 7 全部被挡 —— 队列一直停在 8，
    8 家互相锁死，界面上全显示"在线"却纹丝不动。
    2 家的时候 1 >= 7 不成立，所以这个坑一直没暴露。
    """
    src = (ROOT / "bridge" / "server.py").read_text(encoding="utf-8")
    start = src.find("_active = []")
    if start < 0:
        return ["找不到 /pending 的并发闸门（_active 计算）"]
    seg = src[start:start + 1400]
    bad = []
    if "seeded" not in seg:
        bad.append("闸门没跳过「派单占位」（seeded）——8 家一起点运行会互相锁死")
    if "_MAX_ACTIVE_FETCH" not in src:
        bad.append("并发上限常量不见了")
    # 上限必须是可配的：闸门得用 fetch_cap()，不能直接用常量
    if "len(_active) >= fetch_cap()" not in src:
        bad.append("闸门没走 fetch_cap()，上限又写死了（用户没法在设置里改）")
    if "def fetch_cap(" not in src:
        bad.append("缺少 fetch_cap()（读「设置 → 取数」里的并发上限）")
    # 设置页要有对应的输入框和读写接线
    ui = (ROOT / "bridge" / "ui.py").read_text(encoding="utf-8")
    if "set-max-parallel" not in ui:
        bad.append("设置页没有「同时取数的店铺数上限」输入框")
    if "all.fetch || {}" not in ui or "fetch: {max_parallel" not in ui:
        bad.append("设置页没接上 fetch.max_parallel 的读写")
    return bad


def check_affiliate_filename() -> list[str]:
    """联盟佣金表两条路必须起**同一个**文件名（末日、去横线）。

    三次踩坑，一次比一次隐蔽：

    ① 2026-11-12 15:55 PHT-B1：规格接口回 `code=10000`，退回兜底，
       那份表叫 `..._2026-11-05_2026-11-12.csv` —— B 路当时用
       `endDateExclusive`（末日 + 1），A 路用 `{{endDateCompact}}`（末日）。
       同一个区间两个名字，核算侧要求一字不差 → 判成「区间不对」→ 整份不用
       → 报「都不覆盖这次报表区间」，整家店不出表。
       当时把 B 路改成末日，但只对齐了一半。

    ② 2026-11-16 14:35 PHT-M4：走了兜底，名字是
       `Transaction_Analysis_Product_List_2026-11-01_2026-11-15.csv`，
       而 A 路是 `20261101_20261115`（不带横线）。
       目录里留两份内容相同的文件，按文件名匹配的脚本会选错 ——
       核对时就读到了十几分钟前的旧文件，差点误判成「这轮没重新导」。

    ③ 现在：**两条路都去掉横线**，跟 `{{startDateCompact}}_{{endDateCompact}}`
       一字不差。这个检查盯住「别再改回带横线」。
    """
    import json
    import tempfile
    from pathlib import Path as _P

    bad: list[str] = []

    # ① 扩展侧：B 路必须「末日 + 去横线」
    js = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8")
    need_compact = ("_d8(startDate)" in js
                    and "_d8(shiftDate(endDateExclusive, -1))" in js)
    if not need_compact:
        bad.append("B 路联盟表文件名没同时做到「区间末日 + 去横线」"
                   "（应 _d8(startDate) / _d8(shiftDate(endDateExclusive, -1))，"
                   "否则跟 A 路的 20261101_20261115 对不上）")
    if "Transaction_Analysis_Product_List_${startDate}_" in js:
        bad.append("B 路联盟表文件名又用回了带横线的 ${startDate}"
                   "（会跟 A 路起出两个名字）")
    if "Transaction_Analysis_Product_List_${startDate}_${endDateExclusive}" in js:
        bad.append("B 路联盟表文件名还在直接用 endDateExclusive（末日 + 1）")

    # ② 规格侧：A 路必须仍然用「末日 + Compact」
    try:
        specs = json.loads((ROOT / "配置" / "api_specs.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return bad + [f"读不了 配置/api_specs.json：{e}"]
    blob = json.dumps(specs, ensure_ascii=False)
    if "Transaction_Analysis_Product_List_{{startDateCompact}}_{{endDateCompact}}" not in blob:
        bad.append("A 路联盟表文件名模板变了（不是 {{startDateCompact}}_{{endDateCompact}}）")

    # ③ 核算侧：不许再有 +1 天容忍
    src = (ROOT / "src" / "loader.py").read_text(encoding="utf-8")
    if "_same_window" in src:
        bad.append("loader 里还留着「容忍终点差一天」（_same_window），"
                   "会把拿错邻月的表静默认下来")

    # ④ 真跑 _pick_latest：等值要选中，差一天不能选中
    sys.path.insert(0, str(ROOT))
    try:
        from src import loader, provenance as prove
    except Exception as e:  # noqa: BLE001
        return bad + [f"导入 loader 失败：{e}"]

    rows = [("Product ID", "Est. commission"), ("1", "10")]
    tmp = _P(tempfile.mkdtemp(prefix="jl_affname_"))
    for name, rng, want in (
        ("exact.csv", ("2026-11-05", "2026-11-11"), True),
        ("nextday.csv", ("2026-11-05", "2026-11-12"), False),
    ):
        f = tmp / name
        f.write_text("Product ID,Est. commission\n1,10\n", encoding="utf-8")
        prove.write_meta(f, shop_id="x", kind="affiliate", rng=rng, at="2026-11-12 16:10:00")
        picked, _ = loader._pick_latest([(f, rows)], ("2026-11-05", "2026-11-11"),
                                        "联盟佣金表", [])
        got = bool(picked)
        if got != want:
            if want:
                bad.append(f"{name}（{rng[0]}~{rng[1]}）该被选中却没选中")
            else:
                bad.append(f"{name}（{rng[0]}~{rng[1]}）差一天也被选中了 —— "
                           "容忍没删干净，拿错邻月的表会静默算错")
    return bad


def check_sku_chain() -> list[str]:
    """SKU 取数链的顺序、条件与参数。

    当前顺序（2026-11-13 定）：
      ① 商品管理接口（全量，含当期无销量；带实时签名，实际 100% 失败，5 秒退出）
      ①-a 页面点导出   → 产出 xlsx（**含已删除 SKU**，映射的权威来源）
      ①-b 接口分段取   → 产出 JSON 映射（当前在售 SKU + 链接资料）
      ②  后台 API 导出 → 只在 ①-a 没成时才启动
      ①-c 深度抓取     → 只在 ①-b 没拿到映射时才走

    为什么 ①-a 排第一：
      它历史上是最稳的 xlsx 来源（201 次）。0.24.133 把它降成兜底之后，
      分段又总是成功 → 它永不执行 → 目录里再没出现过表格。
      而 ② 那条 API 导出 100% 失败（code=0 但文件 bytes=0），要跑满 165 秒；
      以前它在分段取后就启动，于是报表出来两分多钟还冒一条假失败
      （2026-11-13 10:30 实测）。提到最前、成功即置 skuTableSaved，
      那条后台任务就不会启动了。

    两个条件都不能用 done.length：
      页面导出成功也会往 done 里 push，用 done.length 会把 ①-b 分段取跳过，
      而它产的是映射（跟 xlsx 是两种产物），跳过了报表就没归属。
    """
    js = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8")
    bad: list[str] = []

    # ① 顺序：页面点导出 必须在 接口分段取 之前
    a = js.find('exportViaApi("sku"')
    b = js.find("const all = await pagePullSkuRanged(")
    if a < 0:
        bad.append("找不到 exportViaApi(\"sku\" 的调用（页面点导出没了）")
    if b < 0:
        bad.append("找不到 pagePullSkuRanged 的调用（映射接口没了）")
    if a >= 0 and b >= 0 and a > b:
        bad.append("页面点导出又排到分段取后面了——它产 xlsx，应该先跑")

    # ② 分段取的条件必须是 !productListOk，不能退回 !done.length
    seg = js[b - 900:b] if b > 900 else js[:b]
    if "if (!productListOk) {" not in seg:
        bad.append("分段取的条件不是 !productListOk（用 done.length 会被页面导出顶掉，映射就没了）")
    if "listRanged = true;" not in js:
        bad.append("分段成功后没有置 listRanged（深抓取的条件就失效了）")
    if "if (!listRanged) {" not in js:
        bad.append("深抓取的条件不是 !listRanged（页面导出成功就再也不补映射了）")

    # ③ 后台 API 导出必须「只在页面导出没成时」启动
    i_call = js.find("try { startBgExportOnce(); } catch")
    if i_call < 0:
        bad.append("找不到 startBgExportOnce 的调用")
    else:
        head = js[max(0, i_call - 400):i_call]
        if "if (!skuTableSaved) {" not in head:
            bad.append("后台 API 导出没有包在 if (!skuTableSaved) 里"
                       "——页面导出成功后它还会跑满 165 秒然后报假失败")
    i_decl = js.find("const startBgExportOnce = ")
    if i_decl < 0:
        bad.append("找不到 startBgExportOnce 的声明（建导出任务没有幂等入口）")
    elif i_call >= 0 and not (i_decl < i_call):
        bad.append("startBgExportOnce 声明在调用之后（会撞 TDZ，导出永远起不来）")

    # ④ 页面导出要有进度上报（否则面板上只看得到「接口分段取」，以为没走这条路）
    if "页面导出中（约 20 秒）" not in js:
        bad.append("页面导出没有进度上报（面板上看不到它在跑）")

    # ⑤ SKU 完成时卡片要显示文件名（跟订单表/广告表一致）
    #    2026-11-15 14:21 实录：卡片停在「接口分段取 3/3」，看着像还在分段。
    #    原因是 runSkuStats 只返回 { via }，外层 shortDetail() 取不到
    #    saved/path/files/rows/count → detail 空 → 桥侧「空 detail 不覆盖旧值」
    #    → 保留最后一次分段上报。
    if 'path: skuSavedPath || ""' not in js:
        bad.append("SKU 完成时没把产物路径带出去（卡片会停在「接口分段取 3/3」）")
    if js.count("skuSavedPath = ") < 3:
        bad.append("skuSavedPath 赋值点不足 3 处（页面导出 / 分段 / 深抓取三条路都要记）")

    # ⑥ 导出接口的 size 不许再写死
    if "pagination: { size: 10000" in js:
        bad.append("SKU 导出接口的 size 又写死 10000 了（会生成 0 字节空文件）")
    if "pagination: { size, page: 0 }" not in js:
        bad.append("SKU 导出接口的 size 不是总条数（应传 total）")
    if "next_pagination" not in js or "let total = 0;" not in js:
        bad.append("没有从响应里取「总条数」（data.list_control.next_pagination.total）")

    # ⑥ 指定了页面（preferPath）就不能拿别的页凑合：
    #    2026-11-12 18:07 实测，SKU 导出从首页发出，status=None、reqBody=None，
    #    页面自己从 /compass/product-analysis 发，266ms 就 200 了。
    if "chrome.tabs.update(tab.id, { url: wantUrl })" not in js:
        bad.append("runInPage 挑到别的页时没有导航到目标页（导出会从首页发出，连 body 都发不出去）")
    if 'wantUrl = `https://${host}/${preferPath}`' not in js:
        bad.append("目标页地址没按 preferPath 拼（会被带回首页）")

    # ⑦ 联盟取完要自己上报：三项并行时外层要等最慢的 SKU，联盟会一直挂「—」
    if 'reportProgress("affiliate", "done"' not in js:
        bad.append("联盟取完没有立刻上报（卡片会一直停在「—」，看着像没取）")
    return bad


def check_injected_scope() -> list[str]:
    """注入到页面主世界的函数，不许引用扩展作用域的符号。

    背景（2026-11-13 09:47 实测）：SKU 导出函数里调了 `throwIfStopped()`。
    那段代码是 `chrome.scripting.executeScript({world:"MAIN"})` **塞进页面**执行
    的，页面里没有这个函数 → ReferenceError → 浏览器把它吞成空对象 `{}`
    → 外面只能看到「平台没返回文件内容」，于是查了两天平台（size/风控/签名/标签页）。

    规则：注入函数体里只允许出现它自己的参数、本地变量和浏览器 API。
    凡是扩展自己定义的（日志、进度、上传、存储、拼 URL、停止检查）都不行。

    这里把这些函数一个个切出来扫。切的时候要跳过字符串和注释，
    否则 `"…{…}"` 里的花括号会让边界算错（以前自己写扫描器就踩过）。
    """
    js = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8")

    def body_end(text: str, i: int) -> int:
        """从 i（指向 '{'）开始，返回配对 '}' 之后的位置；跳过字符串与注释。"""
        depth = 0
        prev_sig = ""      # 上一个「有意义」的字符，用来判断 / 是除号还是正则开头
        while i < len(text):
            c = text[i]
            # 正则字面量：/^\d{6,}$/ 里的 { } 会让配对算错（2026-11-13 实测）
            if c == "/" and prev_sig in ("", "=", "(", ",", ":", "[", "!", "&", "|", "?",
                                         "+", "-", "*", "%", "~", "^", "<", ">", ";", "{"):
                i += 1
                in_class = False
                while i < len(text):
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == "[":
                        in_class = True
                    elif text[i] == "]":
                        in_class = False
                    elif text[i] == "/" and not in_class:
                        break
                    i += 1
                i += 1
                prev_sig = "/"
                continue
            if c in "\"'`":
                q = c
                i += 1
                while i < len(text):
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == q:
                        break
                    i += 1
                i += 1
                continue
            if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                j = text.find("\n", i)
                i = len(text) if j < 0 else j + 1
                continue
            if c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = len(text) if j < 0 else j + 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            if not c.isspace():
                prev_sig = c
            i += 1
        return len(text)

    def code_only(seg: str) -> str:
        """去掉注释行，只留代码（注释里提函数名不算违规）。"""
        out = []
        for line in seg.splitlines():
            t = line.strip()
            if t.startswith("//"):
                continue
            out.append(line)
        return "\n".join(out)

    forbidden = [
        ("throwIfStopped(", "停止检查"), ("reportProgress(", "进度上报"),
        ("uploadToBridge(", "回传文件"), ("skGet(", "读扩展存储"),
        ("skSet(", "写扩展存储"), ("ensureHost(", "找站点域名"),
        ("buildUrl(", "拼接口地址"), ("newAutoTab(", "开标签页"),
        ("appendTaskLog(", "写任务日志"), ("log(", "写日志"),
    ]

    bad: list[str] = []
    scanned = 0

    # ① 内联箭头：runInPage(host, async (…) => { … }
    for m in re.finditer(r"runInPage\(\s*host\s*,\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", js):
        start = m.end() - 1                      # 指向 '{'
        end = body_end(js, start)
        seg = code_only(js[m.start():end])
        scanned += 1
        line = js[:m.start()].count("\n") + 1
        for tok, why in forbidden:
            if tok in seg:
                bad.append(f"background.js:{line} 注入函数里调了 {tok}（{why}）"
                           "——页面里没有这个函数，会 ReferenceError 并被吞成空对象")

    # ② 具名函数再传进去：const xxx = async (…) => { … } 然后 runInPage(host, xxx, …)
    for m in re.finditer(r"runInPage\(\s*host\s*,\s*([A-Za-z_$][\w$]*)\s*,", js):
        name = m.group(1)
        dm = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", js)
        if not dm:
            continue
        start = dm.end() - 1
        end = body_end(js, start)
        seg = code_only(js[dm.start():end])
        scanned += 1
        line = js[:dm.start()].count("\n") + 1
        for tok, why in forbidden:
            if tok in seg:
                bad.append(f"background.js:{line} 注入函数 {name} 里调了 {tok}（{why}）"
                           "——页面里没有这个函数，会 ReferenceError 并被吞成空对象")

    if scanned < 5:
        bad.append(f"只扫到 {scanned} 个注入函数，明显偏少（扫描逻辑可能失效了）")
    return bad


def check_report_columns() -> list[str]:
    """报表列配置必须带上「订单数」这类核心列。

    2026-11-13 实录：`report_columns` 里的 `orders` 在某次配置改写后消失了，
    「汇总」页于是不再渲染订单数——用户看到的像"订单数没了"，其实数据没算错。
    这种「配置被悄悄改小」的事，等看报表才发现，太晚。
    """
    import json
    required = ["gmv", "orders", "net"]
    bad: list[str] = []
    targets = [
        ROOT / "配置" / "formulas.json",
        ROOT / "release" / "净利助手" / "配置" / "formulas.json",
        ROOT / "release" / "净利助手" / "_internal" / "默认配置" / "formulas.json",
    ]
    for p in targets:
        if not p.is_file():
            bad.append(f"找不到配置 {p.relative_to(ROOT)}")
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.relative_to(ROOT)} 读不了：{e}")
            continue
        cols = [str(x) for x in (d.get("report_columns") or [])]
        if not cols:
            bad.append(f"{p.relative_to(ROOT)} 的 report_columns 是空的")
            continue
        for k in required:
            if k not in cols:
                bad.append(f"{p.relative_to(ROOT)} 的报表列少了 {k}"
                           "（订单数/GMV/净利 是核心列，不许被配置改写吃掉）")
    return bad


def check_bridge_structure() -> list[str]:
    """桥的 Handler 类不许被"模块级函数"从中间截断。

    2026-11-13 实录：把 `sku_json_to_xlsx` 插到了 `_finish_upload` 前面（顶格写），
    类定义就此结束，后面所有处理函数都掉到模块级 → 好几个接口 500
    （`/api/schedules` 直接挂），而 py_compile 一点问题都不报。
    """
    src = (ROOT / "bridge" / "server.py").read_text(encoding="utf-8")
    bad: list[str] = []
    if "\n    def _finish_upload(" not in src:
        bad.append("_finish_upload 不再是 Handler 的方法（类可能被顶格函数截断了）")
    i_cls = src.find("class Handler(")
    i_fn = src.find("def sku_json_to_xlsx(")
    i_up = src.find("    def _finish_upload(")
    if i_cls < 0 or i_up < 0:
        return bad or ["找不到 Handler / _finish_upload"]
    if not (i_cls < i_up):
        bad.append("_finish_upload 出现在 class Handler 之前（结构不对）")
    if i_fn > i_cls:
        bad.append("sku_json_to_xlsx 写在 Handler 类里面（会把类截断），应放到类前面")
    return bad


def check_config_intact() -> list[str]:
    """配置里 params / formulas / cost_fields 不许是空的。

    2026-11-15 实录：release 的 formulas.json 里这三块全成了 0 项
    （`save_config` 当时是 `out["params"] = params`，空也照写，没有任何兜底）。
    核算靠 formula.normalize() 的代码默认值兜住了，所以净利一直是对的 ——
    问题全在界面上：报表列设置页生成不出任何费用项，报表只剩「费用合计」，
    广告费/联盟佣金这些明细列全部消失。

    这种「文件残了、代码兜住了」的残局最难发现，所以单列一条盯着。
    """
    import json
    bad: list[str] = []
    targets = [
        ROOT / "配置" / "formulas.json",
        ROOT / "release" / "净利助手" / "配置" / "formulas.json",
        ROOT / "release" / "净利助手" / "_internal" / "默认配置" / "formulas.json",
        ROOT / "build" / "app" / "jingli" / "_internal" / "默认配置" / "formulas.json",
    ]
    for p in targets:
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.relative_to(ROOT)} 读不了：{e}")
            continue
        rel = p.relative_to(ROOT)
        if not d.get("params"):
            bad.append(f"{rel} 的 params 是空的（界面将生成不出任何费用列）")
        if not d.get("formulas"):
            bad.append(f"{rel} 的 formulas 是空的（净利公式丢失）")
        if not d.get("cost_fields"):
            bad.append(f"{rel} 的 cost_fields 是空的（成本表识别不全）")
    return bad


def check_report_cols_cover_fees() -> list[str]:
    """参数表里的每个费用项，都要能在报表列里勾选。

    用户 2026-11-15 的要求：只要配置里有的费率（交易手续费、物流费、
    商家成长…）都要能设置成展示，包括金额和占比。
    界面是按 params 动态生成列目录的，所以只要 params 完整就会全出来；
    这里额外盯住 report_column_order —— 它是「界面排列的稳定依据」，
    少一项就会在拖动排序时被打回默认位置。
    """
    import json
    p = ROOT / "release" / "净利助手" / "配置" / "formulas.json"
    if not p.is_file():
        return [f"找不到 {p.relative_to(ROOT)}"]
    d = json.loads(p.read_text(encoding="utf-8"))
    params = d.get("params") or []
    order = [str(x) for x in (d.get("report_column_order") or [])]
    cols = [str(x) for x in (d.get("report_columns") or [])]
    role_ids = {"gmv", "cost", "order_count", "items", "buyer_paid"}
    bad: list[str] = []
    for it in params:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "").strip()
        if not fid or fid in role_ids or str(it.get("role") or "") in role_ids:
            continue
        for key, where in (("fee:" + fid, "report_column_order"),
                           ("ratio:" + fid, "report_column_order")):
            if key not in order:
                bad.append(f"「{it.get('label') or fid}」的 {key} 不在 {where} 里，"
                           "界面无法把它排到指定位置")
    # 默认展示的 6 个费用列必须在 report_columns 里（用户明确要看到）
    for key in ("fee:dispatch", "ratio:dispatch", "fee:ads", "ratio:ads",
                "fee:affiliate", "ratio:affiliate"):
        if key not in cols:
            bad.append(f"报表默认列里缺 {key}（用户要求默认展示发单费/广告费/联盟佣金及占比）")
    if "rank" in cols:
        bad.append("报表默认列里又有「排名」了（用户 2026-11-15 明确不要这一列）")
    return bad


def check_injected_fetch_timeout() -> list[str]:
    """注入到页面的 fetch 必须带超时。

    2026-11-15 实录：8 家并行时 MYT-V1 / PHT-M2 卡在 fetch 上，
    14 分钟没有任何进展 —— fetch 没有超时，平台不响应就一直挂着，
    还占着并行名额不释放。这类卡死从界面上看就是「一直停在某一步」。
    """
    js = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8")

    def body_end(text: str, i: int) -> int:
        depth = 0
        prev = ""
        while i < len(text):
            c = text[i]
            if c == "/" and prev in ("", "=", "(", ",", ":", "[", "!", "&", "|", "?",
                                     "+", "-", "*", "%", "~", "^", "<", ">", ";", "{"):
                i += 1
                in_cls = False
                while i < len(text):
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == "[":
                        in_cls = True
                    elif text[i] == "]":
                        in_cls = False
                    elif text[i] == "/" and not in_cls:
                        break
                    i += 1
                i += 1
                prev = "/"
                continue
            if c in "\"'`":
                q = c
                i += 1
                while i < len(text):
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == q:
                        break
                    i += 1
                i += 1
                continue
            if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                j = text.find("\n", i)
                i = len(text) if j < 0 else j + 1
                continue
            if c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = len(text) if j < 0 else j + 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            if not c.isspace():
                prev = c
            i += 1
        return len(text)

    ranges = []
    for m in re.finditer(r"runInPage\(\s*host\s*,\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", js):
        s0 = m.end() - 1
        ranges.append((s0, body_end(js, s0)))
    for m in re.finditer(r"runInPage\(\s*host\s*,\s*([A-Za-z_$][\w$]*)\s*,", js):
        name = m.group(1)
        dm = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", js)
        if dm:
            s0 = dm.end() - 1
            ranges.append((s0, body_end(js, s0)))

    bad: list[str] = []
    scanned = 0
    for m in re.finditer(r"fetch\(", js):
        pos = m.start()
        if not any(s0 <= pos < e0 for s0, e0 in ranges):
            continue
        scanned += 1
        if "AbortSignal" not in js[pos:pos + 400]:
            line = js[:pos].count("\n") + 1
            bad.append(f"background.js:{line} 注入函数的 fetch 没有超时"
                       "（平台不响应会一直挂着，把并行名额占死）")
    if scanned < 5:
        bad.append(f"只扫到 {scanned} 个注入函数的 fetch，明显偏少（扫描逻辑可能失效）")
    return bad


def check_download_guard() -> list[str]:
    """下载拦截器必须拦住平台的四种触发方式。

    2026-11-15 跑 PHT-M2 弹了「另存为」，把整家店堵了 8 分钟：当时的拦截器
    只拦了 `<a>.click()` / `window.open` / `createObjectURL`，而平台用的是
    `a.dispatchEvent(new MouseEvent('click'))`、`form.submit()`、
    动态插 `<iframe src=文件>`。这项检查把真函数体抽出来在 node 里跑一遍，
    四种都要被记下且都不真下载。
    """
    import subprocess as _sp
    try:
        r = _sp.run([sys.executable, str(ROOT / "scripts" / "check_download_guard.py")],
                    capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return [f"跑不起来（{e}）"]
    if r.returncode == 0:
        return []
    bad = [l.strip() for l in (r.stdout or "").splitlines() if "FAIL" in l]
    return bad or ["拦截器自检没通过（见 scripts/check_download_guard.py 输出）"]


def check_adv_id() -> list[str]:
    """广告账户 ID 必须有多条来源。

    背景（2026-11-15）：两家 PH 店广告接口全挂，日志只有一句「11 列接口没成」。
    真因是取不到广告账户 ID（平台回 `req adv id missing`），于是接口被拒，
    一路退到「页面点导出」，最后弹了「另存为」把整家店堵死。

    抓包核实的关键事实（别再搞错）：`aadvid` **不在地址栏**。
    打开广告页后地址栏是 `/ads-creation/dashboard?origin=...&shop_region=PH`，
    账户 ID 只出现在**页面自己发出的 oec_ads 请求**查询串上：
        post_product_list_v2?aadvid=7532...&language=zh&oec_seller_id=7495...
    所以有效的取法是「抓页面请求学到」+「问 onboard 接口」，而不是读地址栏。

    这一项盯着四条，任何一条被删都会让新店第一次跑再次整条链崩掉：
      ① resolveAdvId 会扫当前标签页（个别站点地址栏确实带）
      ② learnAdsHeaders 会检查「学到的参数」而不是死等 12 秒
      ③ onboard 兜底必须带 oec_seller_id
      ④ 建任务失败时能识别「req adv id missing」并去自学再试
    """
    miss = []
    src = (ROOT / "浏览器扩展" / "background.js").read_text(encoding="utf-8", errors="replace")
    i = src.find("async function resolveAdvId")
    j = src.find("async function learnAdsHeaders")
    if i < 0:
        return ["找不到 resolveAdvId()"]
    body = src[i:j if j > i else i + 6000]
    if "chrome.tabs.query" not in body:
        miss.append("resolveAdvId 不再扫当前标签页地址（新店第一次跑会取不到账户 ID）")
    if "aadvid" not in body:
        miss.append("resolveAdvId 里没有 aadvid 的取法")
    k = src.find("async function createAdsExportByApi")
    if k > 0 and "adv id missing" not in src[k:k + 5000]:
        miss.append("建任务失败时不再识别「req adv id missing」并自学重试")
    if j > 0:
        tail = src[j:j + 4800]
        if "chrome.storage.local.get" not in tail:
            miss.append("learnAdsHeaders 不再检查「学到的参数」（开着广告页也拿不到账户 ID）")
    # 只找「带引号的接口路径」：注释里也提到 get_onboard_info，用裸词会看错地方
    k = src.find('"/oec_ads/shopping/v1/oec/get_onboard_info"')
    if k > 0 and "oec_seller_id" not in src[k - 400:k + 400]:
        miss.append("onboard 兜底没带 oec_seller_id（平台不知道该查哪个卖家）")
    return miss


def main() -> int:
    bad = 0
    targets = [(rel, check(ROOT / rel)) for rel in FILES]
    targets.append(("bridge/ui.py（内嵌界面脚本）", check_ui()))
    targets.append(("停止守卫", check_stop_guards()))
    targets.append(("广告取数顺序（11 列优先）", check_ads_order()))
    targets.append(("广告调度真跑（8 种场景）", check_ads_schedule()))
    targets.append(("订单待补合并（13 项）", check_order_pick()))
    targets.append(("待补写回不抹新任务（7 项）", check_pending_race()))
    targets.append(("公式实时提示真跑（26 项）", check_formula_ui()))
    targets.append(("计算项表格分清值与费率", check_param_chips()))
    targets.append(("试算必须手动触发", check_trial_manual()))
    targets.append(("并发闸门不挡占位", check_pending_gate()))
    targets.append(("联盟表两条路同名（末日·去横线）", check_affiliate_filename()))
    targets.append(("SKU 接口优先 + 导出用总条数", check_sku_chain()))
    targets.append(("注入函数不许跨作用域", check_injected_scope()))
    targets.append(("报表核心列不许丢", check_report_columns()))
    targets.append(("配置三块不许为空", check_config_intact()))
    targets.append(("报表列覆盖全部费用项", check_report_cols_cover_fees()))
    targets.append(("注入函数 fetch 必带超时", check_injected_fetch_timeout()))
    targets.append(("下载拦截器盖住四种触发", check_download_guard()))
    targets.append(("广告账户 ID 有多条来源", check_adv_id()))
    targets.append(("桥的类结构没被截断", check_bridge_structure()))
    targets.append(("订单类别双向联动", check_kinds_sync()))
    targets.append(("卡片占位/补名/重算（5 项）", check_card_seed()))
    targets.append(("模块导入", check_imports()))
    for rel, missing in targets:
        if missing:
            bad += 1
            print(f"[缺失] {rel}")
            for m in missing[:20]:
                print(f"    {m}(")
        else:
            print(f"[OK]   {rel}")
    if bad:
        print("\n前端脚本有问题：语法错会让整页失灵（停在「正在读取店铺…」），\n"
          "引用了不存在的函数则会在点到那个功能时才炸。都必须在打包前修掉。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
