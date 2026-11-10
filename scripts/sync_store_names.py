"""把店铺改名同步到各个版本 —— 每个版本只改自己的目录，不共享数据。

为什么单独一个脚本
------------------
店铺目录名就是用户起的名字（`数据\\店铺\\MYT-J1\\`），改名时 `_rename_store_dirs()`
只会改**当前这个程序**的目录。电脑上同时装免安装版 / 安装版 / 构建目录时，
另外两边还是旧名字 —— 界面显示新名、文件夹是旧名，翻文件时对不上。

这个脚本做的事，严格是「各自改各自的」：

    1. 以 release\\净利助手\\配置\\stores.json 为正本（用户在哪台机器上改的，
       哪台就是正本；这里固定取 release，因为开发机上它就是主用的那份）
    2. 把这份「shop_id → 名字」写进每个版本自己的 配置\\stores.json
    3. 再扫每个版本**自己的** 数据\\店铺\\*/meta.json，按 shop_id 找到该叫的名字，
       把自己那个目录改名（报表\\自动、报表\\手动 一起）

不复制任何店铺数据、不动 _internal、不跨目录读写。所以三个版本依旧是
三份独立的数据，只是名字对齐了。

用法：
    python -m scripts.sync_store_names            # 同步（会打印每一处改动）
    python -m scripts.sync_store_names --check    # 只看差异，不改
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "release" / "净利助手"


def _targets() -> list[tuple[str, Path]]:
    out = [("免安装版", SOURCE), ("构建目录", ROOT / "build" / "app" / "jingli")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(("安装版", Path(local) / "JingLiHelper"))
    return [(n, p) for n, p in out if (p / "配置").is_dir()]


def _safe(name: str, fallback: str = "未识别店铺") -> str:
    """跟 src.paths._safe_folder 同一套规则，避免两边算出不同的名字。"""
    from src import paths
    return paths._safe_folder(name, fallback)


def _read_names(p: Path) -> dict[str, str]:
    try:
        d = json.loads((p / "配置" / "stores.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for s in d.get("stores") or []:
        sid, nm = str(s.get("id") or ""), str(s.get("name") or "").strip()
        if sid and nm:
            out[sid] = nm
    return out


def _rename_dirs(base: Path, sid2name: dict[str, str], check: bool,
                 log: list[str]) -> None:
    """把 base 这个版本自己 数据/店铺 下的目录改成该有的名字。"""
    stores = base / "数据" / "店铺"
    if not stores.is_dir():
        return
    for d in sorted(stores.iterdir()):
        if not d.is_dir() or d.name == "未识别店铺":
            continue
        meta = d / "meta.json"
        if not meta.is_file():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        sid = str(m.get("shop_id") or "")
        want_name = sid2name.get(sid)
        if not want_name:
            continue                     # 这份清单里没有它，别动
        want = _safe(want_name)
        if d.name == want:
            continue
        target = d.parent / want
        if target.exists():
            log.append(f"    ! {d.name} → {want}：目标已存在，跳过（数据不动）")
            continue
        log.append(f"    · {d.name} → {want}")
        if check:
            continue
        try:
            d.rename(target)
        except OSError as e:
            log.append(f"    ! 改不了（可能有文件被占用）：{e}")
            continue
        # 报表两个房间里的同名目录一起改（跟 _rename_store_dirs 一致）
        for room in ("自动", "手动"):
            a = base / "报表" / room / d.name
            b = base / "报表" / room / want
            if a.is_dir() and not b.exists():
                try:
                    a.rename(b)
                    log.append(f"    · 报表/{room} {d.name} → {want}")
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步店铺改名到各版本")
    ap.add_argument("--check", action="store_true", help="只看差异，不改")
    args = ap.parse_args(argv)

    src_names = _read_names(SOURCE)
    if not src_names:
        print("正本里没有店铺名（release\\净利助手\\配置\\stores.json），无从同步")
        return 1
    print(f"正本：{SOURCE}")
    for sid, nm in src_names.items():
        print(f"  {sid} → {nm}")

    changed = 0
    for label, base in _targets():
        log: list[str] = []
        print(f"\n=== {label}：{base}")
        if base.resolve() != SOURCE.resolve():
            # ① 把别名写进它自己的 stores.json（保留它原有的其他字段顺序）
            try:
                p = base / "配置" / "stores.json"
                doc = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"stores": []}
                stores = doc.setdefault("stores", [])
                hit = {str(s.get("id")): s for s in stores}
                for sid, nm in src_names.items():
                    if sid in hit:
                        if hit[sid].get("name") != nm:
                            log.append(f"    · 配置 {sid} 名字：{hit[sid].get('name')} → {nm}")
                            hit[sid]["name"] = nm
                    else:
                        # 这个版本还没这家店：只补别名，不动它的数据
                        stores.append({"id": sid, "name": nm, "country": "",
                                       "shop_code": "", "shop_name": ""})
                        log.append(f"    + 配置补一条 {sid} → {nm}")
                if not args.check:
                    from src import safeio
                    safeio.write_json(p, doc)
            except Exception as e:  # noqa: BLE001
                log.append(f"    ! 写配置失败：{e}")
        # ② 改它自己目录的名字
        _rename_dirs(base, src_names, args.check, log)
        if log:
            changed += 1
            print("\n".join(log))
        else:
            print("    （已一致，无需改动）")

    print()
    if args.check:
        print("以上是差异（--check，未改动）")
    else:
        print(f"完成：{changed} 个版本有改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
