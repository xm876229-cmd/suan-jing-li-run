"""把所有「版本」同步到一致。

为什么要这个脚本
----------------
同一个程序在机器上有多份，改一处不同步，就会出现「明明改了、刷新了、还是旧的」：

    项目目录\\浏览器扩展                         ← 开发时改的这份（源头）
    release\\净利助手\\浏览器扩展                 ← 免安装版，浏览器里加载的常常是这份
    build\\app\\jingli\\浏览器扩展                ← PyInstaller 中间产物
    %LOCALAPPDATA%\\JingLiHelper\\extension      ← 安装版

配置（各国费率、计算公式、商品成本）也一样：删了要能自动补回，就必须同步进
各版本的 `_internal\\默认配置`、`_internal\\默认数据`。

用法
----
    python -m scripts.sync_all             # 只同步（扩展 / 配置 / 文档），秒级
    python -m scripts.sync_all --build     # 先重打 exe 与安装包，再同步（几分钟）
    python -m scripts.sync_all --check     # 只看差异，不动文件

原则：**只覆盖、不删除**。任何一份目标目录里多出来的东西都不动，
免得把运行中的数据（数据/报表/快照）当成旧文件清掉。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT_SRC = ROOT / "浏览器扩展"
CONFIG_SRC = ROOT / "配置"
COST_SRC = ROOT / "数据" / "costs" / "master.csv"
DOCS = ("CHANGELOG.md", "README.md")

# 各版本的落点。不存在就跳过（换台机器、只装了其中一种都正常）。
def targets() -> list[dict]:
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    installed = home / "JingLiHelper"
    out = [
        {"name": "免安装版", "dir": ROOT / "release" / "净利助手", "ext": "浏览器扩展"},
        {"name": "构建中间产物", "dir": ROOT / "build" / "app" / "jingli", "ext": "浏览器扩展"},
        {"name": "安装版", "dir": installed, "ext": "extension"},
    ]
    # 安装版也有中文目录名的老布局，两种都认
    if (installed / "浏览器扩展").is_dir():
        out[2]["ext"] = "浏览器扩展"
    return [t for t in out if Path(t["dir"]).is_dir()]


def _manifest_version(p: Path) -> str:
    try:
        return str(json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _copy_tree_files(src: Path, dst: Path) -> list[str]:
    """只覆盖同名文件，不删目标里多出来的东西。返回相对路径列表。"""
    changed: list[str] = []
    if not src.is_dir():
        return changed
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        t = dst / rel
        try:
            if t.is_file() and t.stat().st_size == f.stat().st_size \
                    and int(t.stat().st_mtime) >= int(f.stat().st_mtime):
                continue
        except OSError:
            pass
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        changed.append(str(rel))
    return changed


def check() -> int:
    src_v = _manifest_version(EXT_SRC)
    print(f"源扩展版本：{src_v}")
    bad = 0
    for t in targets():
        d = Path(t["dir"])
        ext = d / t["ext"]
        v = _manifest_version(ext)
        flag = "一致" if v == src_v else "不一致"
        if v != src_v:
            bad += 1
        exe = next(iter(d.glob("*.exe")), None)
        stamp = ""
        if exe:
            import datetime as _dt
            stamp = _dt.datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {t['name']:<10} 扩展 {v or '无':<8} [{flag}]　exe {stamp or '无'}")
    return bad


def sync(build: bool) -> int:
    if build:
        print("重打包（exe + 安装包 + 免安装 zip）…")
        r = subprocess.run([sys.executable, "-m", "scripts.build_app"], cwd=str(ROOT))
        if r.returncode != 0:
            print("打包失败，后面的同步照做（至少扩展和配置能先对齐）")

    src_v = _manifest_version(EXT_SRC)
    print(f"源扩展版本：{src_v}")
    for t in targets():
        d = Path(t["dir"])
        ext = d / t["ext"]
        changed = _copy_tree_files(EXT_SRC, ext)
        print(f"  {t['name']:<10} 扩展 → {_manifest_version(ext) or '无'}"
              f"（更新 {len(changed)} 个文件）")

        # 默认配置 / 默认成本：程序启动时会拿它们补回被删掉的配置
        for sub, src in (("_internal/默认配置", CONFIG_SRC),
                         ("_internal/默认数据/costs", COST_SRC.parent)):
            dst = d / sub
            if not (d / "_internal").is_dir() and sub.startswith("_internal"):
                continue
            n = _copy_tree_files(src, dst)
            if n:
                print(f"             {sub}：更新 {len(n)} 个")

        # 文档
        for name in DOCS:
            f = ROOT / name
            if f.is_file():
                try:
                    shutil.copy2(f, d / name)
                except OSError:
                    pass

    # 安装版平时跑的就是 exe / 顶层源码：只同步扩展不改程序，
    # 会出现「界面是新的、算出来的数是旧的」。这一步不能省。
    try:
        from scripts import sync_installed
        sync_installed.sync(verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"  安装版程序同步失败（忽略）：{e}")

    print("\n完成。浏览器里还要点一次扩展的「刷新」才会用上新代码。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步扩展/配置/文档到所有版本")
    ap.add_argument("--build", action="store_true", help="先重打 exe 与安装包")
    ap.add_argument("--check", action="store_true", help="只看差异")
    a = ap.parse_args(argv)
    if a.check:
        return 1 if check() else 0
    return sync(a.build)


if __name__ == "__main__":
    raise SystemExit(main())
