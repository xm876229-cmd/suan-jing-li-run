"""安装 / 卸载 / 开机自启的核心逻辑。

被两处复用：
    installer/installer.py   安装包运行时调用
    src/app.py               exe 自己的 `install` / `uninstall` 子命令

设计约定（都是为了避开 Windows 上的编码与权限坑）：
    · 安装目录用纯 ASCII：%LOCALAPPDATA%\\JingLiHelper
      中文只出现在快捷方式名字上，因为 VBS / 计划任务 / 注册表对中文路径
      的处理各不相同，一旦出问题极难排查
    · 全程按用户安装，不写 Program Files，因此不需要管理员权限、不弹 UAC
    · 开机自启用 schtasks 的登录触发器 + VBS 隐藏窗口启动
      直接让计划任务跑控制台程序会闪一个黑窗口
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

APP_NAME = "净利助手"
APP_ID = "JingLiHelper"                 # ASCII，用于目录名 / 任务名 / 注册表
TASK_NAME = "JingLiHelper-Bridge"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
EXE_NAME = "jingli.exe"
VBS_NAME = "start-bridge.vbs"

# 安装时从发布目录拷贝的内容（data / output 不在其中，避免升级时覆盖用户数据）
# 源码与文档也带上：装到别的电脑后，那份目录本身就是完整的，能独立维护
COPY_ITEMS = ["jingli.exe", "_internal", "config", "extension", "docs",
              "src", "scripts", "bridge", "README.md", "CHANGELOG.md", "jingli.py"]
# 升级时需要保留的目录
KEEP_DIRS = ["data", "output"]


def default_install_dir() -> Path:
    """默认安装位置：当前用户目录下，按用户安装，不需要管理员权限。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID


def read_registered_dir() -> Path | None:
    """从卸载信息里读出实际安装位置。

    允许用户装到任意盘（D:\\软件\\净利助手 之类），所以不能假定目录名固定。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "InstallLocation")
            p = Path(str(val))
            return p if str(val).strip() else None
    except OSError:
        return None


def install_dir() -> Path:
    """当前这台机器上的安装目录。

    优先级：注册表登记 > 程序自己所在目录（打包运行时）> 默认位置。
    打包后 exe 永远知道自己装在哪，这条兜底保证自启、快捷方式不会指错地方。
    """
    reg = read_registered_dir()
    if reg is not None:
        return reg
    from src import paths
    if paths.is_frozen():
        return paths.ROOT
    return default_install_dir()


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """跑一个系统命令，失败也不抛异常。

    有些环境会把 schtasks 之类的系统工具列入黑名单（企业安全策略、沙箱等），
    那种情况下 subprocess 会直接抛 OSError。这里统一兜成 returncode=-1，
    由调用方决定怎么提示——宁可「自启没装上」，也不能让整个程序崩掉。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **kw)
    except (OSError, ValueError) as e:
        return subprocess.CompletedProcess(cmd, -1, "", f"无法执行 {Path(cmd[0]).name}：{e}")


# ---------------- 文件部署 ----------------

def copy_app_files(dest: Path, source: Path, log=print) -> None:
    """把发布文件拷到安装目录。data / output 不碰。"""
    dest.mkdir(parents=True, exist_ok=True)

    for name in COPY_ITEMS:
        src = source / name
        if not src.exists():
            continue
        dst = dest / name
        if src.is_dir():
            # _internal 里是运行库，必须整目录替换，否则新旧混用会崩
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        log(f"  {name}")


def ensure_layout(dest: Path) -> None:
    """建好所有数据目录，并放一份说明文件。"""
    for rel in ("data/costs", "data/mapping", "data/exports/_inbox", "data/capture"):
        (dest / rel).mkdir(parents=True, exist_ok=True)
    (dest / "output").mkdir(parents=True, exist_ok=True)

    notes = {
        "data/costs/放成本表.txt":
            "把成本表（xlsx / csv，两列：商品编码 + 成本）放进这个文件夹，"
            "然后在「净利助手」菜单里选『导入成本表』。\n",
        "data/mapping/放映射表.txt":
            "把商品管理导出的 SKU 清单（product_sku_list.xlsx）放进这个文件夹，"
            "然后在菜单里选『导入映射表』。\n",
        "data/exports/_inbox/导出文件放这里.txt":
            "扩展自动取数会把文件回传到这个目录。\n"
            "手工导出时也放这里（订单 / 广告 / 联盟 三类，按表头自动识别，文件名随意）。\n",
    }
    for rel, text in notes.items():
        p = dest / rel
        if not p.exists():
            p.write_text(text, encoding="utf-8")


# ---------------- 快捷方式 ----------------

def create_shortcut(lnk: Path, target: Path, args: str = "", workdir: Path | None = None) -> bool:
    """用 PowerShell 的 WScript.Shell 建 .lnk（不依赖 pywin32）。"""
    workdir = workdir or target.parent
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';"
        "$s.Arguments='{args}';"
        "$s.WorkingDirectory='{wd}';"
        "$s.IconLocation='{target}';"
        "$s.Save()"
    ).format(lnk=lnk, target=target, args=args, wd=workdir)
    r = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    return r.returncode == 0


def desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ---------------- 开机自启 ----------------

def write_launcher(dest: Path) -> Path:
    """写一个 VBS：以隐藏窗口启动桥。

    计划任务直接跑控制台程序会闪黑窗口；VBS 的 Run(cmd, 0, False)
    是窗口样式 0 = 隐藏，这是最稳的做法，且不需要额外组件。

    VBS 内容刻意不含任何具体路径：用 WScript.ScriptFullName 反推自己所在目录。
    这样无论装在 C 盘还是 D 盘、路径有没有中文和空格，都照样能用
    （之前把路径写死进 VBS，路径一变自启就指向不存在的文件）。
    """
    vbs = dest / VBS_NAME
    content = (
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'Set sh = CreateObject("WScript.Shell")\r\n'
        "dir = fso.GetParentFolderName(WScript.ScriptFullName)\r\n"
        "sh.CurrentDirectory = dir\r\n"
        'sh.Run """" & dir & "\\jingli.exe"" serve", 0, False\r\n'
    )
    vbs.write_text(content, encoding="ascii", errors="replace")
    return vbs


def register_autostart(dest: Path, log=print) -> tuple[bool, str]:
    vbs = dest / VBS_NAME
    if not vbs.exists():
        write_launcher(dest)

    # 先删后建，保证幂等
    _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    r = _run([
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/TR", f'wscript.exe "{vbs}"',
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    ])
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "schtasks 执行失败").strip()
    return True, "已注册登录自启"


def unregister_autostart() -> tuple[bool, str]:
    r = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    ok = r.returncode == 0 or "找不到" in (r.stdout + r.stderr) or "cannot find" in (r.stdout + r.stderr).lower()
    return ok, (r.stdout or r.stderr or "").strip()


def task_exists() -> bool:
    """自启任务是否已注册。

    宁可返回 False 也不能抛异常——菜单一进来就查这个，
    查不到顶多显示「未注册」，抛出去会让整个程序打不开。
    （有些安全策略会直接拦掉 schtasks 进程，连 OSError 都不给你。）
    """
    try:
        r = _run(["schtasks", "/Query", "/TN", TASK_NAME])
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def can_manage_tasks() -> bool:
    """能不能调用 schtasks。被安全策略禁用时给用户明确提示，而不是静默失败。"""
    try:
        r = _run(["schtasks", "/Query", "/TN", "___nonexistent___"])
        return r.returncode != -1
    except Exception:  # noqa: BLE001
        return False


# ---------------- 卸载信息（控制面板里能看到） ----------------

def write_uninstall_entry(dest: Path) -> None:
    exe = dest / EXE_NAME
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, "0.17.2")
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "净利助手")
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(dest))
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{exe}" uninstall')
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    except OSError:
        pass


def remove_uninstall_entry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


# ---------------- 总入口 ----------------

def target_dir() -> Path:
    """本次要安装/卸载到哪个目录。

    打包运行时就是 exe 自己所在的目录——用户把安装包装到 D 盘某处，
    运行的就是那份 exe，自启和快捷方式必须以它为准，不能去问注册表
    （否则「之前装在 A、现在装到 B」会变成给 A 建快捷方式）。
    源码模式下才退回默认位置。
    """
    from src import paths
    if paths.is_frozen():
        return paths.ROOT
    return default_install_dir()


def install(dest: Path | None = None, source: Path | None = None,
            autostart: bool = True, log=print) -> tuple[bool, list[str]]:
    """安装或升级。source=None 表示文件已经在 dest 里（安装包解压后直接调）。"""
    dest = Path(dest or target_dir())
    msgs: list[str] = []

    def note(s: str) -> None:
        msgs.append(s)
        log(s)

    note(f"安装目录：{dest}")
    if source is not None:
        note("复制程序文件…")
        copy_app_files(dest, Path(source), log=lambda s: note("  " + s.strip()))

    note("创建数据目录…")
    ensure_layout(dest)

    exe = dest / EXE_NAME
    if not exe.exists():
        return False, msgs + ["安装失败：找不到 jingli.exe"]

    write_launcher(dest)

    if autostart:
        ok, msg = register_autostart(dest, log=note)
        note(("开机自启：" if ok else "开机自启失败：") + msg)

    ok = create_shortcut(desktop_dir() / f"{APP_NAME}.lnk", exe, "app", dest)
    note("桌面快捷方式：" + ("已创建" if ok else "创建失败"))
    sm = start_menu_dir()
    if sm.exists():
        create_shortcut(sm / f"{APP_NAME}.lnk", exe, "app", dest)

    write_uninstall_entry(dest)
    note("已写入卸载信息（设置 → 应用 里可卸载）")

    return True, msgs


def uninstall(dest: Path | None = None, log=print, remove_files: bool = True) -> tuple[bool, list[str]]:
    dest = Path(dest or target_dir())
    msgs: list[str] = []

    def note(s: str) -> None:
        msgs.append(s)
        log(s)

    ok, msg = unregister_autostart()
    note(("已移除开机自启" if ok else "移除开机自启失败") + (f"：{msg}" if msg else ""))

    for lnk in (desktop_dir() / f"{APP_NAME}.lnk", start_menu_dir() / f"{APP_NAME}.lnk"):
        try:
            if lnk.exists():
                lnk.unlink()
                note(f"已删除 {lnk.name}")
        except OSError:
            pass

    remove_uninstall_entry()
    note("已清除卸载信息")

    if remove_files and dest.exists():
        # 数据目录单独处理：先删程序，数据保留在 data 里由用户决定是否删除
        note(f"程序文件保留在 {dest}（数据未删除，需要彻底清理就手工删掉这个目录）")

    return True, msgs
