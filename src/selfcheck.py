"""启动自检：收拾上一次「没正常退出」留下的残局。

断电、蓝屏、任务管理器强杀之后，磁盘上可能留下这些垃圾：

  半截配置文件      settings.json 只写了一半 → 下次读不出来 → 参数全变默认
  半截报表          .xlsx 写到一半 → 界面能看到、点开报错
  上传临时文件      exports/_tmp/upload-* 没被移走 → 白占空间
  合并锁残留        mapping/.merge.lock 里躺着一个早就死掉的 PID → 核算干等
  日志无限长        bridge_run.log 追加了一年 → 占满 C 盘 → 什么都写不进去

这个模块在每次启动时跑一遍（几百毫秒），把上面这些处理掉；
处理不了的坏文件挪进「_损坏文件 / _隔离_日期」目录，**绝不直接删**。

另外它会在 `数据/.running` 留一个标记：
有标记 = 上次没走到正常退出这一步 → 下次启动就知道「上次是异常结束的」，
可以据此做一次更彻底的自检。
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from src import paths, safeio

MARKER_NAME = ".running"

LOG_LIMIT = 5 * 1024 * 1024        # 单个日志超过 5MB 就轮转
TMP_MAX_AGE = 2 * 3600             # 临时文件超过 2 小时视为垃圾
REPORT_MIN_SIZE = 1024             # 小于 1KB 的 xlsx 基本是半截


def _config_files() -> list[Path]:
    """需要体检的 JSON 配置/状态文件。"""
    out = [
        paths.STORES_FILE,
        paths.RATES_FILE,
        paths.CONFIG / "formulas.json",
        paths.CONFIG / "sites.json",
        paths.CONFIG / "mapping.json",
        paths.DATA / "fx_cache.json",
    ]
    try:
        from src import presets
        out.append(presets.presets_path())
    except Exception:  # noqa: BLE001
        pass
    try:
        from src import settings as cfg
        out.append(cfg.SETTINGS_FILE)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src import auto_run
        out.append(auto_run.state_file())
    except Exception:  # noqa: BLE001
        pass
    out.append(paths.DATA / "pending_runs.json")
    return [p for p in out if p]


def _check_json(f: Path, actions: list[str]) -> None:
    """文件存在但读不出来 = 上次写到一半。交给 safeio 从 .bak 恢复。"""
    if not f.is_file():
        return
    raw = safeio.read_text(f, default="\x00")
    if raw == "\x00":
        return
    try:
        json.loads(raw)
        return
    except (json.JSONDecodeError, ValueError):
        pass
    got = safeio.read_json(f, default=None, repair=True)
    if got is not None:
        actions.append(f"修复损坏的配置：{f.name}（已用备份恢复）")
    else:
        actions.append(f"隔离无法修复的配置：{f.name}")


def _clean_temp(actions: list[str]) -> None:
    n = 0
    for d in (paths.DATA, paths.CONFIG, paths.MAPPING, paths.EXPORTS,
              paths.ROOT, paths.OUTPUT):
        if d.is_dir():
            n += safeio.clean_tmp(d, ".*.tmp*", TMP_MAX_AGE)
            n += safeio.clean_tmp(d, ".*.saving*", TMP_MAX_AGE)
    # 快照复制到一半留下的 .part（见 src/snapshots.py）
    try:
        now2 = time.time()
        for f in paths.STORES_ROOT.rglob("*.part"):
            try:
                if now2 - f.stat().st_mtime > TMP_MAX_AGE:
                    f.unlink()
                    n += 1
            except OSError:
                continue
    except OSError:
        pass

    # 上传中转站：断电时正好在这里写一半的 upload-*
    tmp_dir = paths.EXPORTS / "_tmp"
    if tmp_dir.is_dir():
        now = time.time()
        for f in tmp_dir.glob("upload-*"):
            try:
                if now - f.stat().st_mtime > TMP_MAX_AGE:
                    f.unlink()
                    n += 1
            except OSError:
                continue
    if n:
        actions.append(f"清理断电残留的临时文件 {n} 个")


def _check_reports(actions: list[str]) -> None:
    """报表目录里打不开的 xlsx：挪进隔离目录，别让它挂在列表里骗人。"""
    if not paths.OUTPUT.is_dir():
        return
    # 必须先把文件列表取出来：rglob 是边走边扫的，
    # 一边扫一边把文件挪进新建的 _隔离_ 目录，它会自己钻进去再挪一遍
    # ——结果同一份坏报表被挪了十几层（真踩过）。
    skip = ("_隔离_", "_已删除", "_损坏文件")
    files: list[Path] = []
    for f in paths.OUTPUT.rglob("*.xlsx"):
        if any(part.startswith(skip) for part in f.parts):
            continue
        if f.name.startswith("~$") or f.name.startswith("."):
            continue
        files.append(f)

    bad = 0
    checked = 0
    for f in files[:400]:          # 报表特别多时只查前 400 份，别让启动变慢
        checked += 1
        try:
            size = f.stat().st_size
        except OSError:
            continue

        ok = size >= REPORT_MIN_SIZE
        if ok:
            try:
                # xlsx 本质是个 zip：能打开就说明目录结构完整
                with zipfile.ZipFile(f) as z:
                    ok = "xl/workbook.xml" in z.namelist()
            except (zipfile.BadZipFile, OSError):
                ok = False
        if not ok:
            moved = safeio.quarantine(f, "报表文件不完整（可能是上次生成时断电）")
            if moved:
                bad += 1
    if bad:
        actions.append(f"隔离 {bad} 份不完整的报表（在 报表\\_隔离_日期 里，可手工删除）")


def _check_locks(actions: list[str]) -> None:
    """清理上一个进程留下的锁文件。"""
    lock = paths.MAPPING / ".merge.lock"
    if not lock.exists():
        return
    alive_pid = 0
    try:
        raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
        alive_pid = int(raw.split()[0]) if raw else 0
    except (OSError, ValueError):
        alive_pid = 0
    if alive_pid:
        # 复用 import_mapping 里的判断，避免两套逻辑
        try:
            from scripts.import_mapping import _pid_alive
            if _pid_alive(alive_pid):
                return          # 真有进程在跑，别动
        except Exception:  # noqa: BLE001
            pass
    try:
        lock.unlink()
        actions.append("清理上次异常退出留下的合并锁（.merge.lock）")
    except OSError:
        pass


CAPTURE_KEEP_DAYS = 30
CAPTURE_MAX_MB = 300


def _trim_capture(actions: list[str]) -> None:
    """抓包文件瘦身。

    `数据/抓包记录/capture-YYYYMMDD.jsonl` 是把扩展抓到的接口响应原样存下来，
    纯粹给排查问题用的，程序运行本身不读它。但一天几十 MB，
    跑上一年就是十几 G —— C 盘一满，连配置都存不进去，
    表现就是「程序突然说什么都保存失败」。所以按两条规则清：
      · 只留最近 30 天
      · 总量超过 300MB 时，从最老的开始删到 300MB 以下
    删的都是抓包原文，订单/成本/报表一律不碰。
    """
    d = paths.CAPTURE
    if not d.is_dir():
        return
    files = sorted(d.glob("capture-*.jsonl"), key=lambda f: f.name)
    now = time.time()
    removed = 0
    kept: list[Path] = []
    for f in files:
        try:
            if now - f.stat().st_mtime > CAPTURE_KEEP_DAYS * 86400:
                f.unlink()
                removed += 1
                continue
        except OSError:
            continue
        kept.append(f)

    total = 0
    sizes: list[tuple[Path, int]] = []
    for f in kept:
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        sizes.append((f, sz))
        total += sz
    limit = CAPTURE_MAX_MB * 1024 * 1024
    if total > limit:
        for f, sz in sizes:                      # 从最老的开始删
            if total <= limit:
                break
            try:
                f.unlink()
                total -= sz
                removed += 1
            except OSError:
                continue

    if removed:
        actions.append(f"清理过期抓包记录 {removed} 个（只动 数据\\capture 里的诊断文件）")


def _rotate_logs(actions: list[str]) -> None:
    """日志超过 5MB 就改名成 .1（覆盖上一份）。只留两代，够查问题。"""
    cands = [paths.CAPTURE / "bridge_run.log", paths.CAPTURE / "task.log"]
    if paths.LOGS.is_dir():
        cands += list(paths.LOGS.glob("*.log"))
    for f in cands:
        try:
            if not f.is_file() or f.stat().st_size < LOG_LIMIT:
                continue
            old = f.with_name(f.name + ".1")
            try:
                if old.exists():
                    old.unlink()
            except OSError:
                pass
            f.rename(old)
            actions.append(f"日志过大已轮转：{f.name}（旧内容在 {old.name}）")
        except OSError:
            continue


def mark_running() -> None:
    """启动时留个「我正在跑」的标记。正常退出会删掉它。"""
    try:
        safeio.write_json(paths.DATA / MARKER_NAME,
                          {"pid": __import__("os").getpid(),
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          backup=False)
    except OSError:
        pass


def clear_running() -> None:
    try:
        (paths.DATA / MARKER_NAME).unlink()
    except OSError:
        pass


def last_exit_was_clean() -> bool:
    return not (paths.DATA / MARKER_NAME).exists()


def run(verbose: bool = False, check_crash: bool = True) -> dict:
    """跑一遍自检。返回做过的事，界面/日志都能看。

    check_crash 只在「刚启动、还没写运行标记」时有意义。
    界面上点「立即体检」时程序正开着，标记当然在——
    那时候再判「上次异常退出」就是误报，所以传 False。
    """
    actions: list[str] = []
    crashed = check_crash and not last_exit_was_clean()

    try:
        paths.ensure_dirs()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"目录创建失败：{e}", "actions": actions}

    for f in _config_files():
        try:
            _check_json(f, actions)
        except Exception as e:  # noqa: BLE001
            if verbose:
                actions.append(f"检查 {f.name} 时出错（已跳过）：{e}")

    # 坏配置被隔离之后，这里立刻把随包的默认配置补上，
    # 否则程序会「能打开、但没有国家费率」，算出来的净利全是错的。
    try:
        fixed = paths.restore_defaults()
        for f in fixed or []:
            actions.append(f"已用随包默认值补回：{f}")
    except Exception:  # noqa: BLE001
        pass

    try:
        _clean_temp(actions)
    except Exception:  # noqa: BLE001
        pass
    try:
        _check_locks(actions)
    except Exception:  # noqa: BLE001
        pass
    try:
        _rotate_logs(actions)
    except Exception:  # noqa: BLE001
        pass
    try:
        _trim_capture(actions)
    except Exception:  # noqa: BLE001
        pass

    # 报表体检比较费磁盘，只在「上次是异常退出」或用户手动点自检时做
    if crashed or verbose:
        try:
            _check_reports(actions)
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True, "crashed": crashed, "actions": actions,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
