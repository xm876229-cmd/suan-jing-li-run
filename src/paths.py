"""统一路径解析：源码运行与打包运行都能正确工作。

为什么需要这个模块：
    原来每个文件都写 `ROOT = Path(__file__).resolve().parents[1]`。
    这在源码模式下没问题，但 PyInstaller 打包后：
      · 代码被塞进 exe，`__file__` 指向的还是源码路径（甚至不存在）
      · 资源（config、extension 模板）和可写数据（data、output）必须在真实目录里
    所以凡是需要"程序目录"的地方，都改从这里取。

两种模式：
    源码   python -m src.cli …        ROOT = 项目根目录
    打包   净利助手.exe run …          ROOT = exe 所在目录

数据目录可以用环境变量挪走（多台机器共享一个数据盘时有用）：
    JINGLI_DATA=C:\\jingli-data
    JINGLI_OUTPUT=D:\\净利报表
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序目录：源码模式是项目根，打包模式是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()

# ---- 可写数据 ----
DATA = Path(os.environ.get("JINGLI_DATA") or (ROOT / "data")).resolve()
CAPTURE = DATA / "capture"
EXPORTS = DATA / "exports"
INBOX = EXPORTS / "_inbox"
COSTS = DATA / "costs"
MAPPING = DATA / "mapping"
LOGS = DATA / "logs"

# ---- 产出 ----
OUTPUT = Path(os.environ.get("JINGLI_OUTPUT") or (ROOT / "output")).resolve()

# ---- 只读资源 ----
CONFIG = ROOT / "config"
EXTENSION = ROOT / "extension"
RATES_FILE = CONFIG / "rates.json"
STORES_FILE = CONFIG / "stores.json"


def ensure_dirs() -> None:
    """创建所有需要的目录。打包后首次运行必须调用一次。"""
    for d in (DATA, CAPTURE, EXPORTS, INBOX, COSTS, MAPPING, LOGS, OUTPUT):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 只读目录（比如装在 Program Files 又没权限）不该让整个程序崩掉，
            # 后续写入失败时会给出更具体的错误
            pass


def describe() -> dict[str, str]:
    """诊断用：把关键路径一次性列出来。"""
    return {
        "运行模式": "打包 exe" if is_frozen() else "源码",
        "程序目录": str(ROOT),
        "数据目录": str(DATA),
        "产出目录": str(OUTPUT),
        "配置目录": str(CONFIG),
        "扩展目录": str(EXTENSION),
    }
