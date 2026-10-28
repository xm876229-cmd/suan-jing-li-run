"""净利助手 · 程序入口。

子命令：
    jingli.exe                  双击默认进菜单
    jingli.exe menu             菜单（桌面快捷方式用这个）
    jingli.exe serve            启动本地桥（开机自启调这个，无窗口）
    jingli.exe run [参数]       核算（参数与 src.cli 完全一致，本地桥也用这个）
    jingli.exe install          安装 / 升级
    jingli.exe uninstall        卸载
    jingli.exe doctor           诊断信息
    jingli.exe autostart on|off 开关开机自启

为什么要一个统一入口：
    打包成一个 exe 之后，sys.executable 就是这个 exe，不再有 python 解释器。
    桥要去调核算、开机自启要拉起桥、桌面快捷方式要开菜单——都走这里分派。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内
if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from src import paths  # noqa: E402

APP_TITLE = "净利助手"


def _relax_stdout() -> None:
    """让输出不会因为个别字符而崩。

    中文 Windows 的控制台是 GBK，程序里打印 `⚠`、`「」` 这类字符会直接抛
    UnicodeEncodeError，整个进程退出。打包成 exe 后在别人电脑上必然复现，
    所以统一把 stdout/stderr 设成 errors=replace —— 显示成 ? 也好过崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------- 工具 ----------------

def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _pause() -> None:
    try:
        input("\n按回车继续…")
    except (EOFError, KeyboardInterrupt):
        pass


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f'（默认 {default}）' if default else ''}：").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return v or default


def bridge_running() -> bool:
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_port()}/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _port() -> int:
    return int(os.environ.get("JINGLI_PORT") or 8765)


def start_bridge_hidden() -> bool:
    """后台启动桥，不弹新窗口。已经在跑就直接返回 True。"""
    if bridge_running():
        return True
    if paths.is_frozen():
        cmd = [sys.executable, "serve"]
    else:
        cmd = [sys.executable, "-m", "src.app", "serve"]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.Popen(cmd, cwd=str(paths.ROOT), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        print(f"启动失败：{e}")
        return False

    import time
    for _ in range(20):
        time.sleep(0.4)
        if bridge_running():
            return True
    return False


# ---------------- 子命令 ----------------

def cmd_serve() -> int:
    from bridge.server import main as bridge_main
    return bridge_main()


def cmd_run(argv: list[str]) -> int:
    from src.cli import main as cli_main
    return cli_main(argv)


def cmd_install(argv: list[str]) -> int:
    from src import setup_core
    dest = setup_core.install_dir()
    # 源码模式下没有 jingli.exe，install 只登记自启与快捷方式，不拷文件
    source = None if paths.is_frozen() else paths.ROOT
    ok, _ = setup_core.install(dest=dest, source=source)
    return 0 if ok else 1


def cmd_uninstall(argv: list[str]) -> int:
    from src import setup_core
    setup_core.uninstall()
    print("\n卸载完成。如果是通过安装包安装的，程序目录可以手工删除。")
    return 0


def cmd_autostart(argv: list[str]) -> int:
    from src import setup_core
    action = (argv[0] if argv else "on").lower()
    if action == "off":
        ok, msg = setup_core.unregister_autostart()
        print(("已关闭开机自启" if ok else "关闭失败") + (f"：{msg}" if msg else ""))
        return 0 if ok else 1
    ok, msg = setup_core.register_autostart(setup_core.install_dir())
    print(("已开启开机自启" if ok else "开启失败") + f"：{msg}")
    return 0 if ok else 1


def cmd_doctor() -> int:
    from src import setup_core
    print(f"\n=== {APP_TITLE} 诊断 ===\n")
    for k, v in paths.describe().items():
        print(f"{k:8} {v}")
    print(f"{'本地桥':8} {'运行中' if bridge_running() else '未运行'}"
          f"（端口 {_port()}）")
    can_task = setup_core.can_manage_tasks()
    task_state = ("已注册" if setup_core.task_exists() else "未注册") if can_task \
        else "不可用（系统禁止调用计划任务工具）"
    print(f"{'开机自启':8} {task_state}")
    print(f"{'安装目录':8} {setup_core.install_dir()}"
          f"{'（存在）' if setup_core.install_dir().exists() else '（不存在）'}")

    print("\n配置：")
    import json
    try:
        d = json.loads(paths.RATES_FILE.read_text(encoding="utf-8"))
        print(f"  已配置国家：{'、'.join(d.get('countries', {}).keys()) or '（空）'}")
    except Exception as e:
        print(f"  读取 rates.json 失败：{e}")

    print("\n数据：")
    for label, p in (("成本主表", paths.COSTS / "master.csv"),
                     ("映射主表", paths.MAPPING / "master.csv")):
        if p.exists():
            n = max(0, sum(1 for _ in p.open(encoding="utf-8-sig")) - 1)
            print(f"  {label}：{n} 条")
        else:
            print(f"  {label}：未建立")
    inbox = list(paths.INBOX.glob("*")) if paths.INBOX.exists() else []
    print(f"  待核算文件：{len(inbox)} 个")
    return 0


# ---------------- 菜单 ----------------

def _menu_calc() -> None:
    print("\n区间留空表示全部数据。日期格式 2026-10-01")
    dfrom = _ask("开始日期", "")
    dto = _ask("结束日期", "")
    country = _ask("核算国家", "马来西亚")

    argv = ["--dir", str(paths.INBOX), "--country", country]
    if dfrom:
        argv += ["--from", dfrom]
    if dto:
        argv += ["--to", dto]

    print()
    rc = cmd_run(argv)
    if rc == 0:
        print(f"\n报表已生成，目录：{paths.OUTPUT}")
    else:
        print("\n核算失败，检查上面的输出。")


def _menu_import_costs() -> None:
    from scripts.import_costs import main as import_main
    src = _ask("成本表路径（可直接把文件拖进这个窗口）", "").strip('"').strip("'")
    if not src:
        return
    import_main([src])


def _menu_import_mapping() -> None:
    from scripts.import_mapping import main as import_main
    p = _ask("映射表路径（留空则自动扫描 data/mapping 与 _inbox）", "")
    p = p.strip('"').strip("'")
    import_main([p] if p else [])


def _menu_extension_help() -> None:
    print("\n在战斧浏览器里加载扩展（只需做一次）：")
    print("  1. 打开 chrome://extensions")
    print("  2. 打开右上角「开发者模式」")
    print("  3. 点「加载已解压的扩展程序」，选择这个目录：")
    print(f"     {paths.EXTENSION}")
    print("\n装好后，扩展面板上选日期点「立即运行」即可。")
    try:
        os.startfile(paths.EXTENSION)  # noqa: S606
    except Exception:
        pass


def _open(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)  # noqa: S606
    except Exception as e:
        print(f"打开失败：{e}")


def menu() -> int:
    paths.ensure_dirs()
    while True:
        _clear()
        from src import setup_core
        state = "运行中" if bridge_running() else "未运行"
        auto = "开" if setup_core.task_exists() else "关"
        print("=" * 52)
        print(f"  {APP_TITLE}")
        print(f"  取数桥：{state}    开机自启：{auto}")
        print(f"  数据：{paths.DATA}")
        print("=" * 52)
        print("  1. 启动取数桥（后台运行，不占窗口）")
        print("  2. 立即核算（用已有文件出报表）")
        print("  3. 导入成本表")
        print("  4. 导入映射表")
        print("  5. 打开数据目录")
        print("  6. 打开报表目录")
        print("  7. 开 / 关开机自启")
        print("  8. 加载浏览器扩展说明")
        print("  9. 诊断信息")
        print("  0. 退出")
        print("-" * 52)
        choice = _ask("请选择", "0")

        if choice == "1":
            ok = start_bridge_hidden()
            print("\n取数桥已启动" if ok else "\n启动失败，试试用「诊断信息」看看端口占用")
            _pause()
        elif choice == "2":
            try:
                _menu_calc()
            except SystemExit:
                pass
            _pause()
        elif choice == "3":
            try:
                _menu_import_costs()
            except Exception as e:
                print(f"\n导入失败：{e}")
            _pause()
        elif choice == "4":
            try:
                _menu_import_mapping()
            except Exception as e:
                print(f"\n导入失败：{e}")
            _pause()
        elif choice == "5":
            _open(paths.DATA)
        elif choice == "6":
            _open(paths.OUTPUT)
        elif choice == "7":
            if setup_core.task_exists():
                ok, msg = setup_core.unregister_autostart()
                print(("已关闭开机自启" if ok else "关闭失败") + (f"：{msg}" if msg else ""))
            else:
                ok, msg = setup_core.register_autostart(setup_core.install_dir())
                print(("已开启开机自启" if ok else "开启失败") + f"：{msg}")
            _pause()
        elif choice == "8":
            _menu_extension_help()
            _pause()
        elif choice == "9":
            cmd_doctor()
            _pause()
        elif choice == "0":
            return 0


def main(argv: list[str] | None = None) -> int:
    _relax_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return menu()

    cmd, rest = argv[0].lower(), argv[1:]
    if cmd in ("menu", "--menu"):
        return menu()
    if cmd == "serve":
        return cmd_serve()
    if cmd == "run":
        return cmd_run(rest)
    if cmd == "install":
        return cmd_install(rest)
    if cmd == "uninstall":
        return cmd_uninstall(rest)
    if cmd == "autostart":
        return cmd_autostart(rest)
    if cmd == "doctor":
        return cmd_doctor()

    # 不认识的子命令：当成 cli 参数，兼容老用法
    return cmd_run(argv)


if __name__ == "__main__":
    paths.ensure_dirs()
    raise SystemExit(main())
