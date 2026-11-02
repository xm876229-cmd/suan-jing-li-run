"""把各个版本 `数据\\` 下的英文子目录统一改成中文名。

为什么需要单独一个脚本
----------------------
改名原本只在「程序启动、import paths」那一刻发生，而且只处理**当前那个**数据根。
所以很容易出现这种迷惑场面：程序里已经是 `数据\\店铺`，但打开 release 目录一看，
里面还是 `stores` ——「怎么还是英文文件夹？」

这个脚本把机器上所有找得到的数据目录都迁一遍：

    项目目录\\数据
    release\\净利助手\\数据
    build\\app\\jingli\\数据
    %LOCALAPPDATA%\\JingLiHelper\\数据

只改名，不删除。两边都有内容就合并（同名的留新的、旧的加 `.old` 后缀）；
文件被占用改不了就跳过，下次再跑一次即可。

用法
----
    python -m scripts.migrate_dirs            # 迁移所有已知位置
    python -m scripts.migrate_dirs --check    # 只看有哪些还是英文（不改）
    python -m scripts.migrate_dirs <目录>     # 只迁指定目录（可以是任意数据根）
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_paths():
    """import src.paths，但**不让它顺手改真目录**。

    paths 在被 import 时就会对「当前数据根」做一次改名。检查模式下那等于
    先改了再检查，永远看不出问题；而且用户只是想看一眼有哪些英文目录。
    所以先把数据根指到一个空的临时目录，import 完再把环境变量撤掉。
    """
    saved = {k: os.environ.get(k) for k in ("JINGLI_HOME", "JINGLI_DATA")}
    spy = Path(tempfile.gettempdir()) / "_jingli_migrate_probe"
    os.environ["JINGLI_HOME"] = str(spy)
    os.environ.pop("JINGLI_DATA", None)
    try:
        from src import paths
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return paths


def data_roots() -> list[tuple[str, Path]]:
    """所有可能存在「数据」目录的地方，标签用中文，输出好认。"""
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    out: list[tuple[str, Path]] = [
        ("项目目录", ROOT / "数据"),
        ("免安装版", ROOT / "release" / "净利助手" / "数据"),
        ("构建中间产物", ROOT / "build" / "app" / "jingli" / "数据"),
        ("安装版", home / "JingLiHelper" / "数据"),
    ]
    seen: set[str] = set()
    uniq: list[tuple[str, Path]] = []
    for label, p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((label, p))
    return uniq


def find_english(root: Path, paths) -> list[str]:
    """这个数据根下还有哪些英文目录（只查一层 + 店里的 inbox）。"""
    found: list[str] = []
    if not root.is_dir():
        return found
    for cn, en in paths.SUB_DIRS.items():
        if (root / en).is_dir():
            found.append(en)
    if (root / "导出" / "_inbox").is_dir():
        found.append("导出/_inbox")
    stores = root / "店铺"
    if stores.is_dir():
        for shop in stores.iterdir():
            try:
                if shop.is_dir() and (shop / "inbox").is_dir():
                    found.append(f"店铺/{shop.name}/inbox")
            except OSError:
                continue
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把数据目录下的英文子目录改成中文名")
    ap.add_argument("paths", nargs="*", help="只迁指定目录（默认全部已知位置）")
    ap.add_argument("--check", action="store_true", help="只看不改")
    a = ap.parse_args(argv)

    paths = _load_paths()

    if a.paths:
        targets = [("指定目录", Path(p)) for p in a.paths]
    else:
        targets = data_roots()

    if a.check:
        bad = 0
        for label, root in targets:
            if not root.is_dir():
                print(f"  {label:<14} 跳过（没有 {root}）")
                continue
            left = find_english(root, paths)
            if left:
                bad += 1
                print(f"  {label:<14} 还是英文：{'、'.join(left)}")
            else:
                print(f"  {label:<14} 已是中文名")
        if bad:
            print(f"\n{bad} 处需要迁移，跑一次 `python -m scripts.migrate_dirs` 即可。")
        return 1 if bad else 0

    total = 0
    for label, root in targets:
        if not root.is_dir():
            print(f"  {label:<14} 跳过（没有 {root}）")
            continue
        moved = paths.migrate_data_dirs(root)
        if moved:
            total += len(moved)
            print(f"  {label:<14} 改了 {len(moved)} 处：")
            for m in moved:
                print(f"                 · {m}")
        else:
            print(f"  {label:<14} 已经是中文名")

    print("\n完成。" if total else "\n没有需要改的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
