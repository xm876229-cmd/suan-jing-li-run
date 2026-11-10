# -*- mode: python ; coding: utf-8 -*-
"""macOS 版打包配置（Apple Silicon / arm64）。

为什么单写一个 spec 而不是脚本里堆参数
--------------------------------------
Mac 版要打的是 .app 包，涉及 BUNDLE、bundle_identifier、图标、datas 的摆放，
用 spec 表达最清楚，也方便以后调（比如要改 bundle id、加 entitlements）。

datas 为什么放在 `_MEIPASS` 根下
--------------------------------
`src/paths.resource_root()` 在 macOS 打包后取 `sys._MEIPASS`，
所以这里 dest 一律写 "."，运行时就是 `_MEIPASS/浏览器扩展`、`_MEIPASS/默认配置`。
Windows 版不受影响（它的资源在 exe 同级目录，逻辑没改）。
"""

import os
import sys
from pathlib import Path

# spec 放在 packaging/ 下，项目根是上一级
ROOT = Path(SPECPATH).resolve().parent
STAGE = ROOT / "build" / "mac_stage"  # 中间产物仍放 build/（该目录不入库）
ICON = STAGE / "icon.icns"

datas = [
    (str(ROOT / "浏览器扩展"), "."),
    (str(ROOT / "文档"), "."),
    (str(ROOT / "assets"), "."),
    (str(STAGE / "默认配置"), "默认配置"),
    (str(STAGE / "默认数据"), "默认数据"),
]
datas = [(src, dst) for src, dst in datas if os.path.exists(src)]

# pywebview 的 macOS 后端靠运行时动态导入 pyobjc，PyInstaller 静态分析看不到
hiddenimports = [
    "webview",
    "webview.platforms.cocoa",
    "objc",
    "Foundation",
    "AppKit",
    "WebKit",
    "PyObjCTools",
    "Quartz",
]

a = Analysis(
    [str(ROOT / "jingli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JingLiHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # 窗口模式：双击不弹终端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",      # Apple Silicon
    codesign_identity=None,   # 自己人用，不签名（首次右键打开即可）
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JingLiHelper",
)

app = BUNDLE(
    coll,
    name="净利助手.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="com.jingli.helper",
    info_plist={
        "CFBundleName": "净利助手",
        "CFBundleDisplayName": "净利助手",
        "CFBundleShortVersionString": os.environ.get("JINGLI_VERSION", "0.0.0"),
        "CFBundleVersion": os.environ.get("JINGLI_VERSION", "0.0.0"),
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # 不走 App Translocation 的「随机只读路径」那一套的最好办法是不签名 + 用户手动信任；
        # 这里只声明这是个普通前台应用，不要 Dock 里显示成后台代理。
        "LSUIElement": False,
    },
)
