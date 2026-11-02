"""原子写 + 读修复。

为什么需要这一层
----------------
`path.write_text(...)` 是「先把文件清空，再往里写」。如果断电、蓝屏、
任务管理器强杀正好落在中间，留下的是一个**半截 JSON**：

    {"countries": {"马来西亚": {"fx": 0.61, "fees": {"plat

下次启动读它 → JSONDecodeError → 代码退回默认值 → 你辛苦配的费率、
参数、计划全变回出厂设置，而且**不报错**，只是净利突然算得不对。

正确做法是老程序员的常识：写到临时文件 → fsync 落盘 → 原子替换原文件。
Windows 上 `os.replace` 是原子的：要么是完整的旧文件，要么是完整的新文件，
不存在「半截」。再留一份 `.bak`，连「写坏了」都能退回去。

这个模块只做三件事，别的什么都不管：
  · write_text / write_bytes / write_json —— 原子写，可选留 .bak
  · read_json —— 读不出来就自动用 .bak 恢复，恢复不了才给默认值
  · quarantine —— 坏文件挪到隔离目录，不直接删（用户的文件不能凭空消失）
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

CHUNK = 1024 * 1024


def _fsync(f) -> None:
    """把数据真正刷到盘上。

    不 fsync 的话，write 返回成功只代表「进了系统缓存」，
    断电时缓存里的内容可能还没落盘。配置这种一天改几次的小文件，
    多这一次系统调用的开销可以忽略。
    """
    try:
        f.flush()
        os.fsync(f.fileno())
    except OSError:
        pass


def _tmp_path(path: Path) -> Path:
    # 临时文件必须和目标是同一个目录（同一个卷），否则 os.replace 不是原子的
    return path.with_name(f".{path.name}.tmp{os.getpid()}")


def _copy_atomic(src: Path, dst: Path) -> None:
    """把 src 复制成 dst，复制过程本身也是原子的（.bak 不能是半截）。"""
    tmp = _tmp_path(dst)
    try:
        with src.open("rb") as fi, tmp.open("wb") as fo:
            shutil.copyfileobj(fi, fo, CHUNK)
            _fsync(fo)
        os.replace(tmp, dst)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def write_bytes(path: Path | str, data: bytes, *, backup: bool = True) -> bool:
    """原子写字节。失败抛 OSError（调用方自己决定要不要提示用户）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with tmp.open("wb") as f:
            f.write(data)
            _fsync(f)
        if backup and path.is_file():
            _copy_atomic(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def swap_report(new: Path | str, final: Path | str, trash_dir: Path | str) -> Path:
    """用刚算好的 `new` 顶掉正式报表 `final`，旧的留一份到 `trash_dir`。

    为什么不在算之前就把旧报表挪走：算到一半失败（表头不对、文件被 Excel 占着）
    时旧报表已经进了回收站，报表页上就凭空少一份，用户还以为算成功了。
    先算到临时文件、成功了再换名，失败最多多一个临时文件（调用方删掉）。

    换名这一步也不再「先挪走旧的」：
      · 旧文件先**复制**一份进回收站（留着能找回上一版），原件原地不动
      · 再用 os.replace 原子替换——Windows 上要么是旧文件、要么是新文件，
        不会出现「文件夹里什么都没有」的中间状态，也不怕这一步断电
      · 替换失败（文件正被 Excel 独占）就另存一份 (2)，旧的原样还在

    返回最终落地的路径。
    """
    new, final, trash_dir = Path(new), Path(final), Path(trash_dir)
    if new == final or not new.exists():
        return final if final.exists() else new

    if final.exists():
        # 历史副本：存不下也不影响这次出报表，别因为回收站满了就算不出来
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
            dst = trash_dir / final.name
            i = 2
            while dst.exists():
                dst = trash_dir / f"{final.stem} ({i}){final.suffix}"
                i += 1
            shutil.copy2(final, dst)
        except OSError:
            pass

        try:
            os.replace(new, final)
            return final
        except OSError:
            # 旧的可能正被 Excel 打开：另存一份，旧的原样保留
            i = 2
            alt = final.with_name(f"{final.stem} ({i}){final.suffix}")
            while alt.exists():
                i += 1
                alt = final.with_name(f"{final.stem} ({i}){final.suffix}")
            try:
                os.replace(new, alt)
                return alt
            except OSError:
                return new

    try:
        os.replace(new, final)
        return final
    except OSError:
        return new


def write_text(path: Path | str, text: str, *, encoding: str = "utf-8",
               backup: bool = True) -> bool:
    """原子写文本。CSV 请先用 io.StringIO 拼好再交给这里，别自己 open("w")。"""
    return write_bytes(path, text.encode(encoding), backup=backup)


def write_json(path: Path | str, obj, *, indent: int = 2, backup: bool = True) -> bool:
    text = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    return write_text(path, text, backup=backup)


def read_bytes(path: Path | str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def read_text(path: Path | str, default: str = "",
              encodings: tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk")) -> str:
    """按几种编码依次试。读不到就返回 default，不抛异常。"""
    p = Path(path)
    for enc in encodings:
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except OSError:
            return default
    return default


def _parse_json(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def read_json(path: Path | str, default=None, *, repair: bool = True):
    """读 JSON；读坏了自动用 .bak 恢复，并把坏文件留一份证据。

    返回 default 表示「主文件和备份都读不出来」。
    """
    p = Path(path)
    raw = read_text(p, default="\x00")     # \x00 保证和空文件区分开
    if raw.strip() and raw != "\x00":
        got = _parse_json(raw)
        if got is not None:
            return got

    # 主文件不存在：安静返回默认值，这不是错误
    if not p.exists():
        return default

    bak = p.with_name(p.name + ".bak")
    if bak.is_file():
        got = _parse_json(read_text(bak, default=""))
        if got is not None:
            # 用备份顶上去，顺便把坏文件留个证据（不删，占不了多少地方）
            _keep_corrupt(p)
            try:
                write_json(p, got, backup=False)
            except OSError:
                pass
            return got

    if repair:
        _keep_corrupt(p)
    return default


def _keep_corrupt(path: Path) -> None:
    """把坏文件改名留档，最多留最近 3 份，免得越积越多。"""
    try:
        d = path.parent / "_损坏文件"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{path.name}.{time.strftime('%Y%m%d_%H%M%S')}"
        os.replace(path, dst)
        olds = sorted(d.glob(path.name + ".*"), key=lambda x: x.stat().st_mtime)
        for f in olds[:-3]:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def quarantine(path: Path | str, why: str = "") -> Path | None:
    """把有问题的文件挪进 `_隔离_<日期>`，返回新位置。

    为什么不是直接删：用户的数据不能因为程序觉得它坏了就消失。
    挪走之后目录干净了，想查还能查，想恢复也能恢复。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = p.parent / f"_隔离_{time.strftime('%Y%m%d')}"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / p.name
        i = 2
        while dst.exists():
            dst = d / f"{p.stem} ({i}){p.suffix}"
            i += 1
        os.replace(p, dst)
        if why:
            try:
                (d / "_说明.txt").open("a", encoding="utf-8").write(f"{dst.name}\t{why}\n")
            except OSError:
                pass
        return dst
    except OSError:
        return None


def clean_tmp(parent: Path | str, pattern: str = ".*.tmp*", older_than: float = 3600) -> int:
    """清掉断电留下的半截临时文件。只动 `.xxx.tmpNNN` 这种，不碰正常文件。"""
    n = 0
    now = time.time()
    try:
        for f in Path(parent).glob(pattern):
            try:
                if f.is_file() and now - f.stat().st_mtime > older_than:
                    f.unlink()
                    n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n
