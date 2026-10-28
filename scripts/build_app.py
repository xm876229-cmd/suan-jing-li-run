"""一键构建：免安装版 + 安装包。

产物全部落在 release/，项目源码与运行数据不受影响：

    release/
    ├── 净利助手/                 ← 免安装版（拷走就能用）
    │   ├── jingli.exe
    │   ├── _internal/            运行库
    │   ├── config/               费率与店铺配置（可直接改）
    │   ├── extension/            浏览器扩展（战斧里加载）
    │   └── docs/ README.md
    ├── 净利助手-安装包.exe        ← 双击安装到本机
    └── 净利助手-免安装.zip        ← 整个目录打包

用法：
    python -m scripts.build_app            # 完整构建
    python -m scripts.build_app --app-only # 只重建 exe，不重打安装包
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
BUILD = ROOT / "build"
APP_DIR_NAME = "净利助手"
EXE_NAME = "jingli.exe"
INSTALLER_NAME = "净利助手-安装包.exe"
PORTABLE_ZIP = "净利助手-免安装.zip"

# 拷到发布目录里的资源（运行时要按 exe 同目录读取）
# 源码与文档一并带上：整个 release/净利助手 拷到别的电脑即可完全接管维护，
# 不依赖本机这份项目目录。
RESOURCES = ["config", "extension", "docs", "src", "scripts", "bridge"]
RESOURCE_FILES = ["README.md", "CHANGELOG.md", "jingli.py"]

APP_ENTRY = "jingli.py"
INSTALLER_ENTRY = "installer/installer.py"

PYI_BASE = [
    "--noconfirm", "--clean",
    "--exclude-module", "tkinter",
    "--exclude-module", "pandas",
    "--exclude-module", "numpy",
    "--exclude-module", "matplotlib",
    "--exclude-module", "PIL",
    "--exclude-module", "pytest",
]


def run(cmd: list[str], **kw) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT), **kw)
    if r.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {r.returncode}）")


def pyinstaller(args: list[str]) -> None:
    run([sys.executable, "-m", "PyInstaller"] + PYI_BASE + args)


def build_app() -> Path:
    """构建免安装版。

    刻意不整目录删除 release/净利助手：批量删除会被安全策略拦下，
    而且那目录里可能有用户自己放进去的数据。改成：
      · PyInstaller 输出到 build/app（它自己会清理）
      · 逐文件覆盖到 release/净利助手
    残留的旧文件不会造成问题（同名覆盖），需要彻底干净时手工删目录即可。
    """
    out = RELEASE / APP_DIR_NAME
    stage = BUILD / "app"
    RELEASE.mkdir(parents=True, exist_ok=True)

    pyinstaller([
        "--onedir",
        "--name", "jingli",                      # 英文名，构建更稳；发布时再改名
        "--hidden-import", "openpyxl.cell._writer",
        "--distpath", str(stage),
        "--workpath", str(BUILD / "pyi-app"),
        "--specpath", str(BUILD / "pyi-app"),
        APP_ENTRY,
    ])

    built = stage / "jingli"
    if not built.exists():
        raise SystemExit("构建失败：没找到输出目录")

    out.mkdir(parents=True, exist_ok=True)
    for f in built.rglob("*"):
        if f.is_file():
            dst = out / f.relative_to(built)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

    for name in RESOURCES:
        src = ROOT / name
        if src.exists():
            shutil.copytree(src, out / name, dirs_exist_ok=True)
    for name in RESOURCE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, out / name)

    if not (out / EXE_NAME).exists():
        raise SystemExit(f"构建失败：缺少 {EXE_NAME}")
    print(f"\n[OK] 免安装版：{out}")
    return out


def make_app_zip(app_dir: Path) -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    zpath = BUILD / "app.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(app_dir))
    print(f"[OK] app.zip：{zpath.stat().st_size / 1024 / 1024:.1f} MB")
    return zpath


def build_installer(app_zip: Path) -> Path:
    pyinstaller([
        "--onefile",
        "--name", "setup",
        "--add-data", f"{app_zip};.",
        "--distpath", str(BUILD / "pyi-inst"),
        "--workpath", str(BUILD / "pyi-inst"),
        "--specpath", str(BUILD / "pyi-inst"),
        INSTALLER_ENTRY,
    ])
    src = BUILD / "pyi-inst" / "setup.exe"
    if not src.exists():
        raise SystemExit("构建失败：没生成 setup.exe")
    dst = RELEASE / INSTALLER_NAME
    shutil.copy2(src, dst)
    print(f"[OK] 安装包：{dst}（{dst.stat().st_size / 1024 / 1024:.1f} MB）")
    return dst


def make_portable_zip(app_dir: Path) -> Path:
    zpath = RELEASE / PORTABLE_ZIP
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                z.write(f, app_dir.name / f.relative_to(app_dir))
    print(f"[OK] 免安装压缩包：{zpath}（{zpath.stat().st_size / 1024 / 1024:.1f} MB）")
    return zpath


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-only", action="store_true", help="只构建程序，不重打安装包")
    args = ap.parse_args()

    app_dir = build_app()
    if args.app_only:
        return 0
    make_portable_zip(app_dir)
    app_zip = make_app_zip(app_dir)
    build_installer(app_zip)

    print("\n" + "=" * 56)
    print(f"  产物目录：{RELEASE}")
    print("=" * 56)
    for f in sorted(RELEASE.iterdir()):
        if f.is_file():
            print(f"  {f.name}  {f.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            print(f"  {f.name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
