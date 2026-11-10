"""净利助手 · 程序入口。

子命令：
    jingli.exe                  双击默认进菜单
    jingli.exe                  打开窗口（双击就用这个）
    jingli.exe menu             命令行菜单
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
from src import platform_os  # noqa: E402

APP_TITLE = "净利助手"


def _relax_stdout() -> None:
    """让输出不会因为个别字符或没有控制台而崩。

    中文 Windows 的控制台是 GBK，打印 `⚠`、`「」` 这类字符会抛
    UnicodeEncodeError；而打包成无控制台（windowed）后，stdout/stderr 直接是 None，
    print 一调用就 AttributeError。两种都统一兜住，显示成 ? 也好过崩掉。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
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


def _no_window_flags() -> int:
    """子进程不弹命令行窗口。

    Windows 上只要父进程没有控制台（打包成 windowed exe 就是这种），
    每起一个 netstat / tasklist / taskkill / 核算子进程，系统都会给它
    分配一个新控制台——表现就是启动桥的时候"啪啪啪闪好几下黑窗口"。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

def bridge_health(timeout: float = 2.0) -> dict:
    """端口上那个桥的自报身份；没有桥就返回空 dict。

    绕开系统代理——有的抓包软件会把 127.0.0.1 也转发，
    结果「桥明明开着，程序说连不上」这种问题极难查。
    """
    import json
    import urllib.request
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f"http://127.0.0.1:{_port()}/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def bridge_running() -> bool:
    """桥在不在跑（不关心是谁的）。"""
    return bridge_health().get("ok") is True


def bridge_is_mine() -> bool:
    """端口上的桥是不是**本程序目录**起的。

    必须区分：电脑上同时装免安装版和安装版时，两边都有桥、都有自己的
    配置和数据。先启动的那个抢到 8765，后启动的程序如果只看「端口通不通」
    就会连到别人的数据 —— 界面一切正常，店铺和报表却是另一套。
    """
    info = bridge_health()
    if not info.get("ok"):
        return False
    return _same_dir(str(info.get("root") or ""), str(paths.ROOT))


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:  # noqa: BLE001
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
    """启动本地桥。

    两种角色：
      · 子进程（JINGLI_BRIDGE_CHILD=1）：真正干活的桥
      · 守护者（默认）：拉起子进程，并在它崩了/源码变了时重新拉起

    为什么要有守护者：桥是整套自动化的地基——扩展把抓到的数据交给它、
    它负责落盘并触发核算。桥一死，取数就静默停摆，用户只会看到「点了没反应」。
    之前试过在桥进程内部用 os.execv 自重启，三种方式都出事：
      · execv 后新进程起不来（端口/句柄竞争），桥直接没了
      · 连续改几次文件会连锁重启，最后把桥重启到死
      · 桥是后台常驻进程，重启后不再挂在任何任务管理器下，日志也跟着丢
    换成「守护者 + 子进程」后，重启由父进程做：父进程不碰端口，
    子进程死了父进程重新拉一个，端口随旧进程一起释放，最稳。
    """
    if os.environ.get("JINGLI_BRIDGE_CHILD") == "1":
        from bridge.server import main as bridge_main
        return bridge_main()
    return _serve_daemon()


def _serve_daemon() -> int:
    """守护者：拉起桥，桥退出就重拉。"""
    import subprocess
    import time

    # 先看端口上是谁。三种情况：
    #   ① 没有桥 → 正常起
    #   ② 本目录的桥已在 → 直接返回（再起一个守护者只会互相打架）
    #   ③ 别的目录的桥占着端口 → 接管：关掉它、起本目录的
    # 第 ③ 种是必须处理的：不处理的话子进程 bind 失败退出、守护者又拉起来，
    # 无限循环刷日志，而用户以为「程序开着」——实际连的是另一个版本的数据。
    info = bridge_health()
    if info.get("ok"):
        _other = str(info.get("root") or "")
        if _same_dir(_other, str(paths.ROOT)):
            print(f"取数服务已在运行（端口 {_port()}，本程序目录）")
            return 0
        print(f"端口 {_port()} 上是**另一个版本**的取数服务：")
        print(f"    {_other}")
        print(f"    本程序目录：{paths.ROOT}")
        print("  两个版本各有各的配置和数据，不能共用一个服务。现在关掉它、换成当前目录的…")
        killed = _kill_bridge_on_port(_port())
        if killed:
            print("  已关闭 PID：" + "、".join(killed))
        for _ in range(40):
            if not bridge_running():
                break
            time.sleep(0.25)

    env = dict(os.environ)
    env["JINGLI_BRIDGE_CHILD"] = "1"
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "serve"]
    else:
        cmd = [sys.executable, "-m", "src.app", "serve"]

    print("取数桥守护已启动（桥意外退出会自动拉起，Ctrl+C 退出守护者）")

    def _stamp() -> tuple:
        """把所有会改的源码都盯上。

        早先只盯了四个文件，结果新加的 bridge/settings_api.py、
        bridge/ui.py 改了不重启，出现「代码明明改了、行为没变」的怪事。
        干脆整个目录扫一遍：多扫几个文件的开销可以忽略，
        漏掉一个才麻烦。
        """
        out = []
        for d in (paths.ROOT / "bridge", paths.ROOT / "src"):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.py")):
                try:
                    out.append((f.name, f.stat().st_mtime_ns))
                except OSError:
                    out.append((f.name, 0))
        return tuple(out)

    last = _stamp()
    fails = 0          # 连续启动失败次数：防止「端口被别的程序占着」时无限重启
    while True:
        started_at = time.time()
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(paths.ROOT),
                                    creationflags=_no_window_flags())
        except OSError as e:
            print(f"桥启动失败：{e}（5 秒后重试）")
            time.sleep(5)
            continue

        try:
            while proc.poll() is None:
                time.sleep(2.0)
                now = _stamp()
                if now != last:
                    changed = [a[0] for a, b in zip(now, last) if a[1] != b[1]]
                    print(f"源码有变化（{changed}），重启桥")
                    last = now
                    proc.terminate()
                    break
        except KeyboardInterrupt:
            print("\n停止桥…")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return 0

        rc = proc.poll()
        if rc == 0:
            print("桥正常退出，守护者随之退出")
            return 0

        # 活过一分钟算「正常跑过」，之前的失败计数清零，
        # 免得偶尔一次自重启把后面的重试额度用光
        if time.time() - started_at > 60:
            fails = 0

        # 端口上已经有一个健康的桥：多半是另一个 jingli.exe 刚起来，
        # 我这份守护再抢就是死循环（两个进程互踢）。
        if bridge_running():
            print("端口上已有另一个桥在正常服务，本守护退出（不会重复拉起）")
            return 0

        fails += 1
        if fails >= 6:
            print(f"桥连续 {fails} 次启动失败，停止重试。")
            print("  常见原因：8765 被别的程序占用，或被杀毒软件拦了。")
            print("  可以双击程序文件夹里的「修复-扩展连不上时点我.bat」看提示。")
            return 1
        wait = min(2 * fails, 30)
        print(f"桥意外退出（码 {rc}），{wait} 秒后重新拉起（第 {fails} 次）")
        time.sleep(wait)


def _is_bridge_proc(pid: str) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             creationflags=_no_window_flags(),
                             capture_output=True, text=True, timeout=10,
                             errors="replace").stdout.lower()
    except Exception:  # noqa: BLE001
        return False
    return ("jingli.exe" in out) or ("python" in out)


def _parent_pid(pid: str) -> str:
    """取父进程 PID。

    不用 wmic：新 Windows 已经弃用，部分安全策略还会直接拦。
    改用 kernel32 的进程快照，稳定且无外部依赖。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class _PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return ""
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(_PE32)
        target = int(pid)
        parent = ""
        try:
            ok = k32.Process32First(snap, ctypes.byref(pe))
            while ok:
                if pe.th32ProcessID == target:
                    parent = str(pe.th32ParentProcessID)
                    break
                ok = k32.Process32Next(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return parent
    except Exception:  # noqa: BLE001
        return ""


def _kill_bridge_on_port(port: int) -> list[str]:
    """关掉占用端口的旧桥（连同拉起它的守护者进程），返回被关掉的 PID。

    为什么要连父进程一起关：桥是「守护者 + 子进程」结构，只杀监听端口那个子进程，
    守护者 2 秒后又把它拉起来，端口马上又被占。所以父进程（serve 守护者）也要关。
    只认 jingli.exe / python，别的程序碰巧占用时一律不动，避免误杀。
    """
    killed: list[str] = []
    if sys.platform == "darwin":
        # macOS：没有 netstat/taskkill，用 lsof 找监听者。
        # 同样只杀「自己人」（jingli / python），别误伤别人的服务。
        try:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=15).stdout
        except Exception:  # noqa: BLE001
            return killed
        me = str(os.getpid())
        for ln in out.splitlines()[1:]:
            parts = ln.split()
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            pid, cmd = parts[1], parts[0].rsplit("/", 1)[-1].lower()
            if pid == me or pid in killed:
                continue
            if not cmd.startswith(("jingli", "python")):
                continue
            try:
                subprocess.run(["kill", "-TERM", pid], capture_output=True,
                               timeout=10)
                killed.append(pid)
            except Exception:  # noqa: BLE001
                pass
        return killed
    if os.name != "nt":
        return killed
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15, errors="replace",
                             creationflags=_no_window_flags()).stdout
    except Exception:  # noqa: BLE001
        return killed

    pids: list[str] = []
    for ln in out.splitlines():
        parts = ln.split()
        if (len(parts) >= 5 and parts[0].upper().startswith("TCP")
                and parts[1].endswith(f":{port}")
                and ("LISTEN" in parts[3].upper() or "监听" in parts[3])):
            pid = parts[4]
            if pid.isdigit() and pid not in pids:
                pids.append(pid)

    me = str(os.getpid())
    for pid in pids:
        targets = [pid, _parent_pid(pid)]
        for t in targets:
            if not t or t == "0" or t == me or t in killed:
                continue
            if not _is_bridge_proc(t):
                continue
            try:
                subprocess.run(["taskkill", "/PID", t, "/F", "/T"],
                               creationflags=_no_window_flags(),
                               capture_output=True, timeout=15)
                killed.append(t)
            except Exception:  # noqa: BLE001
                pass
    return killed


def cmd_bridge(argv: list[str]) -> int:
    """启动 / 重启取数桥（后台、无窗口）。

    参数里带 --force（双击「修复-扩展连不上时点我.bat」就是走这条）：
      · 先关掉占用 8765 的旧桥（可能是别的目录启动的、读错配置的那个）
      · 再起「当前这个目录」的桥，保证连的是同一份数据
    不带 --force 时是幂等的：已经在跑就什么都不做。

    桥是整套自动化的地基：浏览器扩展抓到的数据交给它、它负责落盘并触发核算。
    """
    force = any(a.lower() in ("--force", "-f", "restart", "重启桥") for a in argv)

    if bridge_running() and not force:
        if bridge_is_mine():
            print(f"取数桥已在运行（端口 {_port()}，本程序目录）")
            print("  想重启：双击「修复-扩展连不上时点我.bat」，或 jingli.exe bridge --force")
            return 0
        # 端口被别的目录的桥占着：不能当成「已经在跑」直接返回，
        # 否则本程序会一直连另一个版本的数据（界面看不出任何异常）。
        _other = str(bridge_health().get("root") or "")
        print(f"端口 {_port()} 上是另一个版本的取数服务：{_other or '（未报告路径）'}")
        print(f"   本程序目录：{paths.ROOT}")
        print("  两个版本各有各的配置和数据。正在关掉它、换成当前目录的…")
        force = True

    if force:
        print("先关闭占用端口的旧桥…")
        killed = _kill_bridge_on_port(_port())
        if killed:
            print("  已关闭 PID：" + "、".join(killed))
        else:
            print("  端口上没有需要关闭的桥")
        import time
        for _ in range(40):
            if not bridge_running():
                break
            time.sleep(0.25)

    ok = start_bridge_hidden()
    if ok:
        print(f"取数桥已启动（端口 {_port()}，后台运行，不弹窗口）")
        print(f"  参数配置页：http://127.0.0.1:{_port()}/config")
        print(f"  程序首页  ：http://127.0.0.1:{_port()}")
    else:
        print("取数桥启动失败，请用「诊断信息 / doctor」看看 8765 端口是不是被占了")
    return 0 if ok else 1


def cmd_stop_bridge(argv: list[str]) -> int:
    """关闭当前正在跑的取数桥（含守护进程），不再启动新的。"""
    print("正在关闭取数桥…")
    killed = _kill_bridge_on_port(_port())
    if killed:
        print("已关闭 PID：" + "、".join(killed))
    else:
        print("端口上没有正在跑的桥")
    return 0


def cmd_run(argv: list[str]) -> int:
    from src.cli import main as cli_main
    return cli_main(argv)


def cmd_install(argv: list[str]) -> int:
    from src import setup_core
    # dest 交给 setup_core 决定：打包运行时就是 exe 所在目录，
    # 这样安装到任意自定义路径都能正确注册自启与快捷方式。
    # 源码模式下没有 jingli.exe，install 只登记自启与快捷方式，不拷文件
    source = None if paths.is_frozen() else paths.ROOT
    ok, _ = setup_core.install(source=source)
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
    ok, msg = setup_core.register_autostart(setup_core.target_dir())
    print(("已开启开机自启" if ok else "开启失败") + f"：{msg}")
    return 0 if ok else 1


def cmd_doctor() -> int:
    from src import setup_core
    # 先建一遍目录并补默认配置：第一次装完先跑 doctor 的人，
    # 应该看到「配置 8 个、成本 9125 条」，而不是空目录。
    # 只补「不存在」的，已存在的一律不动。
    try:
        paths.ensure_dirs()
    except Exception:  # noqa: BLE001
        pass
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

def _load_stores() -> list[dict]:
    """菜单用的店铺清单：先读 stores.json，里面没有就扫 数据/店铺/*/meta.json。

    为什么要有第二条：0.24.55 起随包不再带开发机的店铺清单
    （stores.json 的 stores 留空，避免把别人的 shop_id 发出去），
    但菜单只认这份文件的话会变成「一家店都没有」，只能手填国家。
    店铺身份本来就以 meta.json 为准，扫一遍就能补回来。
    """
    from src import safeio
    out: list[dict] = []
    try:
        d = safeio.read_json(paths.STORES_FILE, default={}) or {}
        if isinstance(d, dict):
            out = [s for s in (d.get("stores") or [])
                   if isinstance(s, dict) and s.get("enabled", True)]
    except Exception:  # noqa: BLE001
        out = []
    if out:
        return out
    try:
        for meta in sorted((paths.DATA / "店铺").glob("*/meta.json")):
            m = safeio.read_json(meta, default={}) or {}
            sid = str(m.get("shop_id") or "")
            if not sid:
                continue
            out.append({
                "id": sid,
                "name": str(m.get("shop_name") or meta.parent.name),
                "country": str(m.get("country") or ""),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def _load_countries() -> list[str]:
    import json
    try:
        d = json.loads(paths.RATES_FILE.read_text(encoding="utf-8"))
        return list(d.get("countries", {}).keys())
    except Exception:
        return []


def _pick_store() -> tuple[str | None, str | None]:
    """选店铺或选国家。返回 (store_id, country)。

    店铺清单里已经写了每家店对应哪个国家，所以正常情况下只需选店铺，
    国家自动带出。只有一个店铺时直接用它，不问。
    返回 store_id 就表示「国家由店铺决定」，不再单独传 --country。
    """
    stores = _load_stores()

    if len(stores) == 1:
        s = stores[0]
        print(f"\n店铺：{s.get('name', '')}　国家：{s.get('country', '')}")
        return s.get("id"), s.get("country")

    if stores:
        print("\n选择店铺（国家随之确定）：")
        for i, s in enumerate(stores, 1):
            print(f"  {i}. {s.get('name', '')}　[{s.get('country', '')}]　id={s.get('id')}")
        print(f"  {len(stores) + 1}. 不用店铺，手动填国家")
        pick = _ask("请选择", "1")
        try:
            idx = int(pick)
        except ValueError:
            idx = 1
        if 1 <= idx <= len(stores):
            s = stores[idx - 1]
            print(f"→ {s.get('name', '')}（{s.get('country', '')}）")
            return s.get("id"), s.get("country")

    # 手动选国家：把已配置的列出来，省得拼错
    countries = _load_countries()
    if countries:
        print("\n已配置国家（费率见 config/rates.json）：")
        for i, c in enumerate(countries, 1):
            print(f"  {i}. {c}")
        pick = _ask("选国家编号，或直接输入国家名", "1")
        try:
            idx = int(pick)
            if 1 <= idx <= len(countries):
                return None, countries[idx - 1]
        except ValueError:
            pass
        return None, pick

    return None, _ask("国家", "马来西亚")


def _menu_calc() -> None:
    print("\n区间留空表示全部数据。日期格式 2026-10-01")
    store_id, _country = _pick_store()
    dfrom = _ask("开始日期", "")
    dto = _ask("结束日期", "")

    argv = ["--dir", str(paths.INBOX)]
    if store_id:
        # 有店铺就传店铺，国家由 config/stores.json 决定，避免手填两处不一致
        argv += ["--store", store_id]
    else:
        argv += ["--country", _country or "马来西亚"]
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
    print("\n在浏览器里加载扩展（只需做一次）：")
    print("  1. 打开 chrome://extensions")
    print("  2. 打开右上角「开发者模式」")
    print("  3. 点「加载已解压的扩展程序」，选择这个目录：")
    print(f"     {paths.EXTENSION}")
    print("\n装好后，扩展面板上选日期点「立即运行」即可。")
    platform_os.open_path(paths.EXTENSION)


def _open(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"打开失败：{e}")
        return
    if not platform_os.open_path(p):
        print(f"打开失败：{p}")


def _menu_config() -> None:
    """打开参数配置页。

    配置页是桥提供的本地网页（http://127.0.0.1:8765/config）。
    放浏览器里而不是做成独立窗口，是因为配口径时最需要的是
    「改一个勾选、立刻看到净利差多少」，网页能做到即时试算，
    而且不用额外装任何东西。
    """
    if not bridge_running():
        print("\n取数桥没在跑，先启动它…")
        if not start_bridge_hidden():
            print("启动失败。先用「诊断信息」看看 8765 端口是不是被占了。")
            return
        import time
        for _ in range(20):
            time.sleep(0.5)
            if bridge_running():
                break
        else:
            print("桥启动超时，请稍后再试。")
            return

    url = "http://127.0.0.1:8765/config"
    print(f"\n在浏览器里打开：{url}")
    print("  每个参数一行，勾选它算哪些订单类别；")
    print("  改完点「试算」立刻看到净利变化，满意再点「保存配置」。")
    if not platform_os.open_path(url):
        print("自动打开失败，请手动把上面的地址粘到浏览器里。")


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
        print("  3. 参数配置（每项费用算哪些订单，勾选即可）")
        print("  4. 导入成本表")
        print("  5. 导入映射表")
        print("  6. 打开数据目录")
        print("  7. 打开报表目录")
        print("  8. 开 / 关开机自启")
        print("  9. 加载浏览器扩展说明")
        print(" 10. 诊断信息")
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
            _menu_config()
            _pause()
        elif choice == "4":
            try:
                _menu_import_costs()
            except Exception as e:
                print(f"\n导入失败：{e}")
            _pause()
        elif choice == "5":
            try:
                _menu_import_mapping()
            except Exception as e:
                print(f"\n导入失败：{e}")
            _pause()
        elif choice == "6":
            _open(paths.DATA)
        elif choice == "7":
            _open(paths.OUTPUT)
        elif choice == "8":
            if setup_core.task_exists():
                ok, msg = setup_core.unregister_autostart()
                print(("已关闭开机自启" if ok else "关闭失败") + (f"：{msg}" if msg else ""))
            else:
                ok, msg = setup_core.register_autostart(setup_core.target_dir())
                print(("已开启开机自启" if ok else "开启失败") + f"：{msg}")
            _pause()
        elif choice == "9":
            _menu_extension_help()
            _pause()
        elif choice in ("10", "diagnose"):
            cmd_doctor()
            _pause()
        elif choice == "0":
            return 0


def _report_crash() -> None:
    """把未捕获异常写成日志，并用一个原生弹窗说人话。

    为什么要这个：打包版是 --windowed（没有控制台），PyInstaller 会直接弹
    「Failed to execute script jingli」+ 一大段 traceback。用户只看到一堆
    File "...", line ... 根本不知道发生了什么，也不知道去哪找日志。
    实测 2026-11-04 我传错一个目录参数就弹了一次，被当成程序坏了。
    """
    import traceback
    from datetime import datetime
    try:
        d = paths.DATA / "日志"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"异常_{datetime.now():%Y%m%d_%H%M%S}.log"
        f.write_text(traceback.format_exc(), encoding="utf-8")
        where = str(f)
    except Exception:  # noqa: BLE001
        where = "（日志没写成）"
    msg = ("程序这次没跑完，已经记下原因：\n\n"
           f"{where}\n\n"
           "常见原因：数据目录里没有订单表、配置被改坏、文件被占用。\n"
           "把上面那个日志文件发给开发者即可定位。")
    platform_os.alert(msg, "净利助手 - 出错了")


def main(argv: list[str] | None = None) -> int:
    _relax_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(argv)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        _report_crash()
        return 1


def _dispatch(argv: list[str]) -> int:

    # 不带参数 = 打开窗口。
    # 双击程序应该看到界面，而不是一个要输数字的黑框。
    # 命令行菜单还在，用 `jingli.exe menu` 打开。
    if not argv:
        from src import desktop
        return desktop.main()

    cmd, rest = argv[0].lower(), argv[1:]
    if cmd in ("app", "gui", "window", "--app"):
        from src import desktop
        return desktop.main()
    if cmd in ("menu", "--menu"):
        return menu()
    if cmd == "serve":
        return cmd_serve()
    if cmd in ("bridge", "启动桥", "重启桥"):
        return cmd_bridge(rest)
    if cmd in ("stop-bridge", "stop_bridge", "停止桥", "关闭桥"):
        return cmd_stop_bridge(rest)
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
    if cmd in ("selfcheck-ui", "ui-check"):
        # 只验「窗口这一层」能不能加载：进了包里的 pyobjc / WKWebView 缺一块，
        # 双击时才会闪一下就退，现场很难看出原因。这个命令能把问题提前暴露，
        # 真机上打不开时也让用户有一条能一次说清的命令。
        try:
            import webview
            if sys.platform == "darwin":
                from webview.platforms import cocoa  # noqa: F401
            # Windows 的后端依赖 pythonnet（pip 环境里可能没装），
            # 这个命令的意义主要在 Mac 上，别的平台只验 webview 本身能导入。
            # pywebview 没有 __version__，用包元数据；取不到就写「已加载」
            try:
                from importlib.metadata import version as _pkg_ver
                _wv = _pkg_ver("pywebview")
            except Exception:  # noqa: BLE001
                _wv = "已加载"
            try:
                from importlib.metadata import version as _pkg_ver2
                _py = _pkg_ver2("pyobjc-core")
            except Exception:  # noqa: BLE001
                _py = ""
            _suffix = " + cocoa 后端 可用" if sys.platform == "darwin" else ""
            print(f"窗口组件：pywebview {_wv}{_suffix}"
                  + (f"（pyobjc {_py}）" if _py else ""))
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"窗口组件不可用：{e}")
            return 1

    # 不认识的子命令：当成 cli 参数，兼容老用法
    return cmd_run(argv)


if __name__ == "__main__":
    paths.ensure_dirs()
    raise SystemExit(main())
