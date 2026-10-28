"""净利助手 · 安装包。

打包方式（由 scripts/build_app.py 自动执行）：
    把 release/净利助手/ 整个目录压成 app.zip，作为数据打进本程序，
    编译成单文件 exe → release/净利助手-安装包.exe

运行时的动作：
    1. 把 app.zip 解压到 %LOCALAPPDATA%\\JingLiHelper（按用户安装，不需要管理员）
    2. 调 jingli.exe install：建数据目录、注册开机自启、建快捷方式、写卸载信息
    3. 打印「加载浏览器扩展」的步骤（这一步无法代劳）

为什么安装目录用 ASCII：
    中文目录在 VBS、计划任务、注册表三处的编码处理各不相同，出问题极难排查。
    用户看到的名字仍然是中文（快捷方式叫「净利助手」）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

APP_ID = "JingLiHelper"
APP_NAME = "净利助手"
EXE_NAME = "jingli.exe"


def bundle_dir() -> Path:
    """PyInstaller 单文件运行时，资源解压在 sys._MEIPASS。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def install_dir() -> Path:
    """默认安装位置；已装过就沿用注册表里登记的位置（原地升级）。"""
    reg = None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "InstallLocation")
            if str(val).strip():
                reg = Path(str(val))
    except OSError:
        reg = None
    if reg is not None:
        return reg
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID


def read_arg_dir(argv: list[str]) -> Path | None:
    """支持 `--dir D:\\某处` 指定安装位置（静默安装时用）。"""
    for i, a in enumerate(argv):
        low = a.lower()
        if low in ("--dir", "/dir", "-d") and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
        if low.startswith("--dir="):
            return Path(a.split("=", 1)[1]).expanduser()
    return None


def check_writable(dest: Path) -> tuple[bool, str]:
    """确认目标目录能写。装在只读位置（如 Program Files 无权限）要提前说清。"""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def extract(zip_path: Path, dest: Path, log=print) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        for i, info in enumerate(infos, 1):
            z.extract(info, dest)
            total += info.file_size
            if i % 200 == 0 or i == len(infos):
                print(f"  解压 {i}/{len(infos)} …", end="\r")
    print(" " * 40, end="\r")
    return total


def main() -> int:
    # 中文 Windows 控制台是 GBK，打印到某些字符（如替换符、特殊符号）会抛
    # UnicodeEncodeError 直接崩掉。安装程序绝不能崩在半路，统一降级成 ?
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    print("=" * 56)
    print(f"  {APP_NAME} 安装程序")
    print("=" * 56)

    zip_path = bundle_dir() / "app.zip"
    if not zip_path.exists():
        print(f"\n安装包损坏：找不到 app.zip（{zip_path}）")
        return 1

    silent = any(a.lower() in ("--silent", "/s", "-s") for a in sys.argv[1:])
    arg_dir = read_arg_dir(sys.argv[1:])

    # 默认位置：已装过就沿用原位置（升级），否则用用户目录
    dest = arg_dir or install_dir()
    exists = (dest / EXE_NAME).exists()

    if not silent:
        print(f"\n默认安装位置：{dest}")
        if exists:
            print("该位置已有安装，本次为「升级」：程序文件会替换，data 与 output 保持不变。")
        print("直接回车用默认位置，也可以输入其他路径（支持把文件夹拖进本窗口）。")
        try:
            ans = input("\n安装位置：").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if ans:
            dest = Path(ans.strip().strip('"').strip("'")).expanduser()

    dest = dest.absolute()
    ok, why = check_writable(dest)
    if not ok:
        print(f"\n安装失败：{dest}")
        print(f"  原因：{why}")
        print("  换一个位置，例如 D:\\净利助手，或用默认位置。")
        return 1

    print(f"\n将安装到：{dest}")
    print("安装过程不需要管理员权限，也不会改动系统设置之外的东西。")

    if not silent:
        try:
            ans = input("\n回车开始安装，输入 n 取消：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 1
        if ans == "n":
            print("已取消。")
            return 0

    print("\n[1/3] 解压程序文件…")
    try:
        size = extract(zip_path, dest)
        print(f"  完成，共 {human(size)}")
    except Exception as e:  # noqa: BLE001
        print(f"  解压失败：{e}")
        return 1

    exe = dest / EXE_NAME
    if not exe.exists():
        print(f"\n安装失败：解压后找不到 {EXE_NAME}")
        return 1

    print("\n[2/3] 注册开机自启、创建快捷方式…")
    # 不捕获子进程输出：让它直接写控制台。
    # 父子进程的控制台代码页可能不一致（GBK / UTF-8），一旦经过 Python 转码
    # 就会变成一堆问号。直接继承标准输出反而最稳。
    r = subprocess.run([str(exe), "install"], cwd=str(dest))
    if r.returncode != 0:
        print("  自启注册有失败项，可以先继续，之后再从菜单里重试。")

    print("\n[3/3] 完成。\n")
    print("=" * 56)
    print("  还需要你手工做一件事：在战斧浏览器里加载扩展（只需一次）")
    print("=" * 56)
    print("  1. 打开 chrome://extensions")
    print("  2. 打开右上角「开发者模式」")
    print("  3. 点「加载已解压的扩展程序」，选择：")
    print(f"     {dest / 'extension'}")
    print("\n  装好后，扩展面板选日期点「立即运行」，会自动取数并出报表。")
    print(f"\n  报表目录：{dest / 'output'}")
    print(f"  成本表放：{dest / 'data' / 'costs'}，然后在菜单里选『导入成本表』")

    try:
        ans = input("\n现在打开「净利助手」菜单？(Y/n)：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans != "n" and not silent:
        subprocess.Popen([str(exe), "menu"], cwd=str(dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
