"""平台差异集中在这一个文件里：Windows / macOS 只在这里分叉。

为什么单独拆一个模块
--------------------
以前这些调用散在 app.py / desktop.py / setup_core.py 里，
每处都写 `if os.name == "nt"`。做 Mac 版时最怕的就是漏掉一处——
漏了不会报错，只会在 Mac 上静默失效（比如点了「打开文件夹」没反应）。
所以约定：**凡是跟操作系统打交道的，统一走这里**，其它模块不再直接
调 `os.startfile` / `netstat` / `MessageBox` / `schtasks`。

三个能力：
    open_path(str)      用系统默认方式打开文件、文件夹或网址
    alert(msg, title)   弹一个系统提示框（出错时才用）
    kill_listen(port)   杀掉监听这个端口的进程，返回被杀掉的 PID 列表

Windows 的行为与 0.24.82 完全一致，没有改逻辑，只是搬了位置。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# 不弹黑窗口：桥在后台跑，每核算一次冒一个窗口很吓人
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WIN else 0


def open_path(target: str | Path) -> bool:
    """用系统默认程序打开：文件夹、网页、Excel 都走这里。

    Windows 用 os.startfile，macOS 用 `open`，其它 Unix 用 `xdg-open`。
    打不开就返回 False，调用方负责给提示——不要抛异常，
    这个函数基本都出现在「用户点了一下」的路径上，崩了比不打不开更糟。
    """
    p = str(target)
    try:
        if IS_WIN:
            os.startfile(p)  # type: ignore[attr-defined]  # noqa: S606
            return True
        cmd = "open" if IS_MAC else "xdg-open"
        subprocess.Popen([cmd, p], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


def alert(message: str, title: str = "净利助手") -> None:
    """弹一个系统提示框。只在「必须让人看到」的错误上用。

    macOS 没有 MessageBox，用 osascript 的 display alert；
    传参走 argv 而不是拼字符串，避免消息里的引号把脚本弄坏。
    """
    msg = str(message or "")
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            print(f"{title}：{msg}")
        return
    if IS_MAC:
        try:
            script = ('on run argv\n'
                      'display alert (item 1 of argv) message (item 2 of argv) as critical\n'
                      'end run')
            subprocess.run(["osascript", "-e", script, title, msg],
                           check=False, timeout=600,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"{title}：{msg}")


def kill_listen(port: int) -> list[str]:
    """杀掉监听指定端口的进程，返回 PID 字符串列表。

    只用于「端口被上一次的自己占着」这一种情况：换目录启动、
    桥没退出干净。**不区分是谁**——和 Windows 版的历史行为一致。
    """
    pids: list[str] = []
    if IS_WIN:
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=_NO_WINDOW).stdout
        except Exception:  # noqa: BLE001
            return pids
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local, state, pid = parts[1], parts[-2], parts[-1]
            if not local.endswith(f":{port}") or state.upper() != "LISTENING":
                continue
            if pid.isdigit() and pid not in pids:
                pids.append(pid)
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", pid, "/F", "/T"],
                               capture_output=True, timeout=10,
                               creationflags=_NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass
        return pids

    # macOS / Linux：lsof 查监听者
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return pids
    pids = [x.strip() for x in out.split() if x.strip().isdigit()]
    for pid in pids:
        for sig in ("-TERM", "-KILL"):
            try:
                subprocess.run(["kill", sig, pid], capture_output=True,
                               timeout=5)
                break
            except Exception:  # noqa: BLE001
                continue
    return pids


def port_in_use(port: int) -> bool:
    """端口上有没有人在监听（不区分是不是自己）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def app_data_dir(name: str = "JingLiHelper") -> Path:
    """macOS 上「用户数据该放哪」的标准位置。

    .app 在 /Applications 里是只读的，而且未签名应用从「下载」直接双击时
    会被系统搬到随机只读路径（App Translocation）——数据绝不能写在程序旁边。
    Windows 不用这个函数（那边沿用「数据跟着程序走」）。
    """
    base = Path.home() / "Library" / "Application Support"
    return base / name
