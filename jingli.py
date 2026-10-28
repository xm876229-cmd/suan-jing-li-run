"""打包入口。

单独放一个文件而不是直接打包 src/app.py，原因是：
    PyInstaller 会把「入口脚本所在目录」加进模块搜索路径。
    入口若是 src/app.py，就只有 src/ 在路径里，`import src.cli` 会失败；
    入口放项目根，`src` 包才能被正确解析。

直接运行也等价：
    python jingli.py doctor
"""

from __future__ import annotations

import sys

from src.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
