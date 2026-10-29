"""把公共目录里的旧数据归到正确的店铺目录。

背景：早期版本上传文件时认不出店铺就落公共目录 data/exports/_inbox/，
多店铺一起用的时候这些文件会混在一起，说不清哪份属于哪家店。

现在上传一律按店铺分目录，公共目录只剩历史遗留。
这个脚本做一次性认领：按表头认出是什么表 → 挪到对应店铺的分类子目录。

用法：
    python -m scripts.adopt_legacy                 # 只有一家店时自动归
    python -m scripts.adopt_legacy --store 7496027585645218321   # 指定店铺
    python -m scripts.adopt_legacy --dry          # 只看不挪
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402
from src.loader import classify_rows, read_table  # noqa: E402

KIND_CN = {"order": "订单表", "ads": "广告表", "affiliate": "联盟佣金表",
           "mapping": "商品映射表"}


def main() -> int:
    ap = argparse.ArgumentParser(description="认领公共目录里的旧数据")
    ap.add_argument("--store", default="", help="目标店铺 id（不填则只有一家店时才自动归）")
    ap.add_argument("--dry", action="store_true", help="只看不挪")
    args = ap.parse_args()

    legacy = paths.INBOX
    if not legacy.is_dir():
        print(f"公共目录不存在：{legacy}")
        return 0
    files = [f for f in legacy.iterdir()
             if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv", ".txt", ".json")
             and not f.name.startswith("~$")]
    if not files:
        print("公共目录里没有文件，没什么可认领的")
        return 0

    # 候选店铺：stores.json 里配置过的
    import json
    stores = []
    try:
        cfg = json.loads(paths.STORES_FILE.read_text(encoding="utf-8"))
        stores = [s for s in cfg.get("stores", []) if s.get("enabled", True)]
    except Exception:
        pass

    target = (args.store or "").strip()
    if not target:
        real = [s for s in stores if s.get("id") != "demo-my"]
        if len(real) != 1:
            print(f"有 {len(stores)} 家店，没法自动判断归属。请用 --store 指定：")
            for s in stores:
                print(f"   {s.get('id')}  {s.get('name')}")
            return 1
        target = real[0]["id"]

    print(f"归属店铺：{target}")
    moved = 0
    for f in files:
        try:
            rows = read_table(f)
        except Exception as e:  # noqa: BLE001
            print(f"  ？{f.name}：读不出来（{e}）")
            continue
        kind = classify_rows(rows) or ""
        label = KIND_CN.get(kind, "没认出来")
        if kind == "mapping":
            dest_dir = paths.MAPPING
        else:
            dest_dir = paths.store_kind_dir(target, kind or "orders")
        print(f"  {f.name} → {label} → {dest_dir}")
        if args.dry:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f.name
        i = 2
        while dest.exists():
            dest = dest_dir / f"{dest.stem} ({i}){dest.suffix}"
            i += 1
        try:
            shutil.move(str(f), str(dest))
            moved += 1
        except OSError as e:
            print(f"    挪不动：{e}")
    print(f"完成，挪了 {moved} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
