"""桌面窗口。

双击程序就是打开这个窗口，不是命令行，也不是浏览器标签页。

实现方式：用 pywebview 开一个原生窗口，里面装的是桥提供的那套界面
（http://127.0.0.1:8765/）。为什么这么做：

  · 界面已经做好了（控制台 + 参数配置），重写成原生控件要重做一遍，
    而且表格里一堆复选框，原生控件反而更啰嗦
  · 窗口是系统的原生窗口——有标题栏、任务栏图标、能最小化，
    和别的电脑程序用起来没区别
  · 用的是 Windows 自带的 WebView2，不用打包 Chromium
    （PySide6 的 QtWebEngine 要多 150MB，启动还慢）

桥在后台跑，关掉窗口只是关界面，定时取数照常进行。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

WINDOW_TITLE = "净利助手"
WIDTH, HEIGHT = 1200, 840
MIN_W, MIN_H = 940, 620

# 本机请求一律绕开系统代理。
# 有些电脑装了抓包/代理软件，它会把 127.0.0.1 也当外部流量转发，
# 结果「桥明明在跑，程序说连不上」——这种问题查起来极费时间。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _health(timeout: float = 1.5) -> dict:
    """读桥的 /health。连不上返回空 dict。"""
    try:
        url = f"http://127.0.0.1:{_port()}/health"
        with _OPENER.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _port() -> int:
    import os
    return int(os.environ.get("JINGLI_PORT") or 8765)


def _same_dir(a: str, b: str) -> bool:
    import os
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _bridge_alive(timeout: float = 1.5) -> bool:
    return bool(_health(timeout).get("ok"))


def ensure_bridge(progress=None) -> bool:
    """确保「本程序这个目录」的桥在跑。

    为什么要比对目录：电脑上可能同时装着免安装版和安装版，
    两边都有自己的桥、自己的配置和数据库。旧的那个先抢到 8765，
    新的程序打开的就是别人的数据——报表看着正常，店铺却是另一套。
    所以只认「根目录跟当前程序一致」的那一个，不是就关掉重起。
    """
    def say(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    from src import paths
    from src.app import start_bridge_hidden, _kill_bridge_on_port

    info = _health()
    if info.get("ok"):
        if _same_dir(str(info.get("root") or ""), str(paths.ROOT)):
            say("取数服务已在运行")
            return True
        say("发现另一个目录的程序在跑，先关掉它…")
        try:
            _kill_bridge_on_port(_port())
        except Exception:  # noqa: BLE001
            pass
        for _ in range(40):
            if not _bridge_alive(0.6):
                break
            time.sleep(0.25)

    say("正在启动取数服务…")
    if not start_bridge_hidden():
        return False
    for _ in range(40):
        time.sleep(0.5)
        if _bridge_alive(0.8):
            say("正在打开界面…")
            return True
    return False


def _fatal(message: str) -> int:
    """窗口都开不了时的兜底：弹个系统对话框，别让人看着黑框发呆。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, WINDOW_TITLE, 0x10)
    except Exception:  # noqa: BLE001
        print(message)
    return 1


class NativeApi:
    """窗口内 JS 可调的原生能力（目前只有「另存为模板」）。

    为什么要它：WebView2 不处理网页里的下载链接，所以「下载导入模板」在窗口里点了
    没反应。这里直接弹系统「另存为」对话框，选完就把模板写到那个位置。
    """

    def save_template(self, default_dir: str = "") -> str:
        try:
            import webview
            from pathlib import Path

            w = webview.windows[0] if webview.windows else None
            if w is None:
                return ""
            fd = getattr(webview, "FileDialog", None)
            kind = getattr(fd, "SAVE", None) if fd is not None else None
            if kind is None:
                kind = getattr(webview, "SAVE_DIALOG", 30)

            kw: dict = {"save_filename": "成本表导入模板.xlsx"}
            if default_dir:
                kw["directory"] = default_dir
            try:
                res = w.create_file_dialog(kind, file_types=("Excel 文件 (*.xlsx)",), **kw)
            except TypeError:
                res = w.create_file_dialog(kind, **kw)
            if not res:
                return ""
            path = res if isinstance(res, str) else (res[0] if res else "")
            if not path:
                return ""
            from bridge import costs_api
            Path(path).write_bytes(costs_api.make_template())
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    def pick_folder(self, default_dir: str = "") -> str:
        """弹系统「选择文件夹」对话框，返回选中的路径（取消返回空字符串）。

        报表页的「保存到」用它——窗口里不能靠浏览器的下载框，
        但又得让人自己选存哪儿。
        """
        try:
            import webview

            w = webview.windows[0] if webview.windows else None
            if w is None:
                return ""
            fd = getattr(webview, "FileDialog", None)
            kind = getattr(fd, "FOLDER", None) if fd is not None else None
            if kind is None:
                kind = getattr(webview, "FOLDER_DIALOG", 20)
            kw: dict = {}
            if default_dir:
                kw["directory"] = default_dir
            try:
                res = w.create_file_dialog(kind, **kw)
            except TypeError:
                res = w.create_file_dialog(kind)
            if not res:
                return ""
            return res if isinstance(res, str) else (res[0] if res else "")
        except Exception:  # noqa: BLE001
            return ""

    def open_folder(self, path: str = "") -> bool:
        """在资源管理器里打开一个文件夹（不存在就当失败）。"""
        try:
            from pathlib import Path as _P
            d = _P(str(path or "")).expanduser()
            if not d.is_dir():
                return False
            import os as _os
            _os.startfile(str(d))  # noqa: S606  只在 Windows 桌面窗口里用
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop_bridge(self) -> bool:
        """关闭当前取数桥（含守护进程）。窗口进程本身不受影响。"""
        try:
            from src.app import _kill_bridge_on_port, _port
            return bool(_kill_bridge_on_port(_port()))
        except Exception:  # noqa: BLE001
            return False

    def app_dir(self) -> str:
        """当前程序（exe）所在目录，界面用来判断「桥是不是这个程序起的」。"""
        try:
            from pathlib import Path
            return str(Path(sys.executable).resolve().parent)
        except Exception:  # noqa: BLE001
            return ""

    def restart_bridge(self) -> str:
        """关掉别的桥，用「当前程序目录」这个桥重启。"""
        try:
            import subprocess
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "nt" else 0
            subprocess.run([sys.executable, "bridge", "--force"], timeout=90,
                           creationflags=flags,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL)
            return "已用本程序的桥重启"
        except Exception as e:  # noqa: BLE001
            return f"重启失败：{e}"


# ---------------- 启动页 ----------------
#
# 以前的启动是「先双击启动桥，等黑窗口里出现一行字，再双击程序」，
# 而且程序打开的头两秒是白屏——不知道是在启动还是卡死了。
# 现在双击一次：先出这个启动页，桥就绪后自动跳到主界面。
# 不用 tkinter：打包时为了体积把它排除了，pywebview 的窗口本身就能装 HTML。

SPLASH_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{background:linear-gradient(160deg,#f7fbf9 0%,#eef4f3 55%,#e9f1f0 100%);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    color:#111827;display:flex;align-items:center;justify-content:center;
    -webkit-font-smoothing:antialiased;user-select:none}
  .wrap{text-align:center;width:100%;padding:0 34px}
  .mark{width:62px;height:62px;margin:0 auto 18px;border-radius:19px;
    background:linear-gradient(145deg,#13b48c,#0a6d57);
    box-shadow:0 12px 28px -10px rgba(14,138,109,.65);
    display:flex;align-items:center;justify-content:center;
    animation:pop .5s cubic-bezier(.2,.9,.3,1.3) both}
  .mark svg{width:34px;height:34px}
  @keyframes pop{from{transform:scale(.86);opacity:0}to{transform:scale(1);opacity:1}}
  h1{font-size:19px;font-weight:600;margin:0 0 6px;letter-spacing:-.2px}
  .sub{font-size:12.5px;color:#8c95a3;margin-bottom:22px}
  .track{height:4px;border-radius:4px;background:#dfe7e6;overflow:hidden;margin:0 auto;
    width:190px}
  .fill{height:100%;width:38%;border-radius:4px;
    background:linear-gradient(90deg,#13b48c,#0e8a6d);
    animation:slide 1.25s ease-in-out infinite}
  @keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(320%)}}
  .status{font-size:12.5px;color:#5a6472;margin-top:16px;min-height:20px}
  .hint{font-size:11.5px;color:#a4acb8;margin-top:6px}
</style></head><body>
<div class="wrap">
  <div class="mark">
    <svg viewBox="0 0 24 24" fill="none">
      <rect x="4" y="13" width="3.6" height="7" rx="1.3" fill="#fff" opacity=".82"/>
      <rect x="10.2" y="8.5" width="3.6" height="11.5" rx="1.3" fill="#fff"/>
      <rect x="16.4" y="4" width="3.6" height="16" rx="1.3" fill="#fff" opacity=".92"/>
    </svg>
  </div>
  <h1>净利助手</h1>
  <div class="sub">正在启动，请稍候</div>
  <div class="track"><div class="fill"></div></div>
  <div class="status" id="st">检查取数服务…</div>
  <div class="hint" id="hint">第一次启动会慢一些，之后就快了</div>
</div>
<script>
  function setStatus(t){ document.getElementById('st').textContent = t; }
  function setError(t){
    document.getElementById('st').textContent = t;
    document.getElementById('st').style.color = '#c0462c';
    document.querySelector('.fill').style.animation = 'none';
    document.querySelector('.hint').textContent = '关掉这个窗口，重启程序再试；杀毒软件拦截时也会这样';
  }
</script></body></html>"""


def _boot(win) -> None:
    """启动页里跑的后台流程：起桥 → 跳主界面。"""
    def say(msg: str) -> None:
        try:
            win.evaluate_js(f"setStatus({json.dumps(msg, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001
            pass

    ok = ensure_bridge(progress=say)
    if not ok:
        try:
            win.evaluate_js("setError('取数服务没能启动（8765 端口可能被占用）')")
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        win.load_url(f"http://127.0.0.1:{_port()}/")
        win.resize(WIDTH, HEIGHT)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if getattr(sys, "frozen", False):
        import multiprocessing
        multiprocessing.freeze_support()

    try:
        import webview
    except ImportError:
        # 没有界面组件时退回「先起桥再说」，至少不耽误取数
        if ensure_bridge():
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{_port()}/")
            return 0
        return _fatal(
            "缺少界面组件（pywebview）。\n\n"
            "如果是源码运行，执行：pip install pywebview"
        )

    win = webview.create_window(
        WINDOW_TITLE,
        html=SPLASH_HTML,
        width=430,
        height=340,
        resizable=True,
        min_size=(430, 330),
        text_select=False,
        confirm_close=False,
        background_color="#f2f6f5",
        js_api=NativeApi(),
    )
    # 关窗口就退出界面进程；桥是独立子进程，会继续在后台跑
    try:
        webview.start(_boot, win)
    except Exception as e:  # noqa: BLE001
        # WebView2 起不来（运行时缺失、被杀软拦、系统组件损坏）时，
        # 不能让用户对着一个闪一下就没的窗口发呆。退回浏览器打开，
        # 功能一模一样，只是没有独立窗口。
        if ensure_bridge():
            import webbrowser
            try:
                webbrowser.open(f"http://127.0.0.1:{_port()}/")
            except Exception:  # noqa: BLE001
                pass
            _fatal(
                "窗口组件没能启动，已经改用浏览器打开。\n\n"
                f"如果浏览器也没打开，手动访问：http://127.0.0.1:{_port()}/\n\n"
                f"（技术原因：{e}）"
            )
            return 0
        return _fatal(f"界面和取数服务都没起来：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
