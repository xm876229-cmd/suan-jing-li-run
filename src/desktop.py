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
    )
    # 关窗口就退出界面进程；桥是独立子进程，会继续在后台跑
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
