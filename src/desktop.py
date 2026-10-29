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

import sys
import time
import urllib.request

WINDOW_TITLE = "净利助手"
WIDTH, HEIGHT = 1200, 840
MIN_W, MIN_H = 940, 620


def _bridge_alive(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=timeout) as r:
            return b'"ok"' in r.read() or b"true" in r.read().lower()
    except Exception:  # noqa: BLE001
        return False


def ensure_bridge() -> bool:
    """确保桥在跑。没有就拉起来（后台，不占窗口）。"""
    if _bridge_alive():
        return True

    from src.app import start_bridge_hidden

    if not start_bridge_hidden():
        return False
    # 桥是子进程，起来需要一点时间，最多等 15 秒
    for _ in range(30):
        time.sleep(0.5)
        if _bridge_alive():
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


def main() -> int:
    if getattr(sys, "frozen", False):
        import multiprocessing
        multiprocessing.freeze_support()

    if not ensure_bridge():
        return _fatal(
            "取数服务没能启动。\n\n"
            "常见原因：8765 端口被别的程序占用。\n"
            "可以先用「诊断信息」看看，或者重启电脑后再试。"
        )

    try:
        import webview
    except ImportError:
        return _fatal(
            "缺少界面组件（pywebview）。\n\n"
            "如果是源码运行，执行：pip install pywebview"
        )

    webview.create_window(
        WINDOW_TITLE,
        "http://127.0.0.1:8765/",
        width=WIDTH,
        height=HEIGHT,
        min_size=(MIN_W, MIN_H),
        text_select=True,
        confirm_close=False,
        js_api=NativeApi(),
    )
    # 关窗口就退出界面进程；桥是独立子进程，会继续在后台跑
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
