"""把 release/净利助手 的程序部分同步到**安装版**（%LOCALAPPDATA%\\JingLiHelper）。

为什么单独一个脚本：
    sync_all 只管扩展和默认配置——它假设安装版由「安装包」更新。
    但日常跑的往往就是安装版：只更新扩展、exe 还是老的，
    结果就是「界面是新的，算出来的数是旧的」，而且看不出来。

同步什么：
    jingli.exe
    _internal/            运行库 + 源码副本（排除 默认配置 / 默认数据）
    顶层 src/bridge/scripts/jingli.py
                          安装版是「源码模式布局」，这几个目录在顶层

不动什么：
    数据/ 配置/ 报表/    用户的东西
    _internal/默认配置、_internal/默认数据
                          由 sync_all 管，而且安装版里可能有用户改过的值

规则：**按内容哈希比对**，不看大小和时间。
    只比大小漏过事：base_library.zip 两次构建大小一模一样，
    内容却不同，于是安装版一直在用旧的标准库包。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REL = ROOT / "release" / "净利助手"
KEEP_SUB = ("默认配置", "默认数据")          # 由 sync_all 负责，不在这里覆盖
TOP_SRC = ("src", "bridge", "scripts")      # 安装版顶层的源码布局


def _home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sync_file(src: Path, dst: Path) -> bool:
    """内容不同才拷。返回是否真的动了。"""
    try:
        if dst.is_file() and _md5(dst) == _md5(src):
            return False
    except OSError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _sync_tree(src_root: Path, dst_root: Path, skip_top: tuple[str, ...] = ()) -> list[str]:
    changed: list[str] = []
    for f in sorted(src_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src_root)
        if rel.parts and rel.parts[0] in skip_top:
            continue
        if "__pycache__" in rel.parts:
            continue
        if _sync_file(f, dst_root / rel):
            changed.append(str(rel).replace("\\", "/"))
    return changed


def _drop_orphans(dst_root: Path, src_root: Path, trash: Path,
                  skip_top: tuple[str, ...] = ()) -> list[str]:
    """目标里多出来的旧文件挪进回收目录（不硬删）。"""
    keep = {str(f.relative_to(src_root)).replace("\\", "/")
            for f in src_root.rglob("*") if f.is_file()}
    moved: list[str] = []
    for f in sorted(dst_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dst_root)
        if rel.parts and rel.parts[0] in skip_top:
            continue
        rel_s = str(rel).replace("\\", "/")
        if rel_s in keep:
            continue
        if "__pycache__" in rel.parts or f.suffix == ".pyc":
            f.unlink(missing_ok=True)
            continue
        dst = trash / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dst))
        moved.append(rel_s)
    return moved


def sync(verbose: bool = True) -> dict:
    installed = _home() / "JingLiHelper"
    if not REL.is_dir() or not installed.is_dir():
        return {"ok": False, "reason": "没有 release 或没有安装版"}

    trash = installed / f"_已删除_{datetime.now():%Y%m%d_%H%M%S}"
    report: dict = {"ok": True, "files": [], "orphans": [], "trash": str(trash)}

    # 1) exe
    exe = REL / "jingli.exe"
    if exe.is_file() and _sync_file(exe, installed / "jingli.exe"):
        report["files"].append("jingli.exe")

    # 2) _internal（排除默认配置/默认数据）
    report["files"] += _sync_tree(REL / "_internal", installed / "_internal", KEEP_SUB)
    report["orphans"] += _drop_orphans(installed / "_internal", REL / "_internal",
                                       trash, KEEP_SUB)

    # 3) 顶层源码布局（安装版跑源码时用这层）
    src_copy = REL / "_internal" / "源码"
    for name in TOP_SRC:
        p = src_copy / name
        if p.is_dir():
            report["files"] += _sync_tree(p, installed / name)
            report["orphans"] += _drop_orphans(installed / name, p, trash)
    if (src_copy / "jingli.py").is_file():
        if _sync_file(src_copy / "jingli.py", installed / "jingli.py"):
            report["files"].append("jingli.py")

    # 4) 明文源码目录不要留缓存
    for pc in installed.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)

    if verbose:
        print(f"  安装版：更新 {len(report['files'])} 个文件"
              + (f"，挪走 {len(report['orphans'])} 个旧文件（{trash.name}）"
                 if report["orphans"] else ""))
        if report["orphans"]:
            for o in report["orphans"][:6]:
                print(f"     - {o}")
    return report


def main(argv: list[str] | None = None) -> int:
    r = sync()
    if not r.get("ok"):
        print(f"跳过安装版同步：{r.get('reason')}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
