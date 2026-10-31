"""导入 SKU→商品 映射：合并进主表，并记录异常流水。

为什么映射也要主表，而不是直接用最新抓下来的那份：
    最新那份只含「这一段有销量的 SKU」。一旦某个老 SKU 从里面消失
    （这几个月没卖），历史订单里那些行就再也找不到商品ID，
    会被整片归进「未识别商品」——报表看起来没错，实际少算了一条链接的净利。

    注意：SKU ID 是平台唯一的，一个 SKU 只会属于一条链接。
    把同一个规格加到另一条链接上，平台会生成**新的** SKU ID。
    所以映射是一劳永逸的对应关系，不会随时间"改挂"。

所以这里做增量合并：
    · 新 SKU        → 追加
    · 商品ID 对不上  → 更新，并记一条 rebind 流水（这属于数据脏了，要留痕）
    · 卖家SKU/商品名 → 变了就更新（不记流水，属描述性字段）
    · 主表永远保留全部见过的 SKU

用法：
    python -m scripts.import_mapping                       # 自动取 _inbox 里最新的 sku_list_*.json
    python -m scripts.import_mapping "sku_list_xxx.json"
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 源码模式把项目根加进 sys.path；打包后模块已在 exe 内，不需要也不能这么做
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402

ROOT = paths.ROOT

from src.loader import (  # noqa: E402
    derive_style_code, parse_mapping, parse_mapping_json, read_table,
)

# 订单表里提取的 sku_id → 卖家SKU（main 里初始化）
ORDER_SKU: dict[str, str] = {}

MAPPING_DIR = paths.MAPPING
MASTER = MAPPING_DIR / "master.csv"
HISTORY = MAPPING_DIR / "history.jsonl"
INBOX = paths.INBOX
FIELDS = ["sku_id", "product_id", "seller_sku", "product_name", "style_code", "updated_at"]


def _best_master() -> Path:
    """实际该读哪份主表。

    主表被别的程序占着（Excel/WPS 后台进程、预览窗口都算）时替换不掉，
    那一轮的结果会落在 master.new.csv。**读的时候要认得它**，
    否则就会出现「明明合并成功了，报表还是用老归属」——而且毫无提示。

    两份都在的时候，谁的修改时间新用谁。
    """
    pending = MAPPING_DIR / "master.new.csv"
    try:
        if pending.exists():
            if not MASTER.exists() or pending.stat().st_mtime >= MASTER.stat().st_mtime:
                return pending
    except OSError:
        pass
    return MASTER


def load_master() -> dict[str, dict]:
    _promote_pending()
    src = _best_master()
    if not src.exists():
        return {}
    out: dict[str, dict] = {}
    with src.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("sku_id") or "").strip()
            if not sid:
                continue
            out[sid] = {k: (row.get(k) or "") for k in FIELDS}
    return out


def _promote_pending() -> None:
    """把上次没能写进主表的结果补写进去。

    主表被 Excel / WPS 打开着的时候是替换不掉的（Windows 锁文件），
    那一轮的结果会先存成 master.new.csv；下次启动先把它顶上去，
    就不会出现「每次都重新合并一遍、主表永远停在旧版本」。
    """
    pending = MAPPING_DIR / "master.new.csv"
    if not pending.exists():
        return
    try:
        os.replace(pending, MASTER)
    except OSError:
        pass


def write_master(data: dict[str, dict]) -> bool:
    """写主表：先写临时文件再原子替换。

    直接 open("w") 写着写着失败（磁盘满 / 被占用），主表就成了写了一半的
    半张表，下次读进来 SKU 归属整片丢失，而且报错还看不出来。
    所以一律走「临时文件 + os.replace」。

    返回 False 表示主表被占用、没替换成功——结果已留在 master.new.csv，
    下次启动会自动顶上去，不会丢。
    """
    MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MAPPING_DIR / "master.csv.tmp"
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for sid in sorted(data):
            w.writerow(data[sid])

    for _ in range(3):
        try:
            os.replace(tmp, MASTER)
            return True
        except PermissionError:
            time.sleep(0.4)
        except OSError:
            break

    pending = MAPPING_DIR / "master.new.csv"
    try:
        os.replace(tmp, pending)
    except OSError:
        pending = tmp
    print(f"⚠ 主表被占用（可能在 Excel/WPS 里开着），这一轮结果已存到 {pending.name}，"
          f"下次启动会自动顶上去")
    return False


SUFFIXES = (".json", ".xlsx", ".xls", ".csv")


def find_sources() -> list[Path]:
    """所有可能的映射来源，按时间从旧到新（后者覆盖前者）。

    两种形态都收：
      · 扩展自动抓的 sku_list_*.json
      · 用户手工导的 product_sku_list.xlsx（接口取不到时的退路）
    """
    out: list[Path] = []
    for d in (MAPPING_DIR, INBOX):
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file() or f.suffix.lower() not in SUFFIXES:
                continue
            low = f.name.lower()
            # 兼容几种命名：扩展自动抓的 sku_list_*.json、
            # 页面导出改名后的 sku_export_*.xlsx、
            # 以及历史文件 sku_20261030.xlsx（早期命名，漏合并过，这里一并认）
            if ("sku_list" in low or "sku list" in low or "sku_export" in low
                    or low.startswith("sku_") or "产品" in f.name):
                out.append(f)
    return sorted(out, key=lambda f: f.stat().st_mtime)


# 来源优先级（用户明确）：
#   主来源 = 商品管理接口的全量 SKU 表（sku_id / product_id / seller_sku）
#   补充   = 主工作簿「链接资料」表、区间内的 SKU 分析导出
# 归属冲突时以主来源为准；补充来源只补主表里没有的 SKU，不改已有归属。
# 两边都找不到，才归入「无ID」。
PRIMARY_HINTS = ("商品管理",)


def is_primary(path: Path) -> bool:
    return any(h in path.name for h in PRIMARY_HINTS)


def load_seller_sku_from_orders(limit_files: int = 10) -> dict[str, str]:
    """从订单表里提取 sku_id → 卖家SKU。

    为什么从这里取：订单表每行都带「SKU ID」和「Seller SKU」，
    而款式编码（V27-White → V27）就藏在卖家SKU里。
    有了它就不用再单独导一张「链接资料 / 批量编辑商品」表了。

    只读最近 N 份订单表（每轮取数都会新增一份，全读会越来越慢）。
    """
    out: dict[str, str] = {}
    try:
        store_dirs = list(paths.STORES_ROOT.iterdir())
    except OSError:
        return out

    files: list[Path] = []
    for d in store_dirs:
        if not d.is_dir():
            continue
        for sub in ("订单表", "orders"):
            p = d / "inbox" / sub
            if not p.is_dir():
                continue
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in (".csv", ".xlsx", ".xls"):
                    files.append(f)
    if not files:
        return out
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    for f in files[:limit_files]:
        try:
            rows = read_table(f)
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        # 找表头里「SKU ID」「Seller SKU」两列
        hidx = -1
        c_sid = c_sku = -1
        for i, r in enumerate(rows[:6]):
            low = [str(x or "").strip().lower().replace(" ", "") for x in r]
            a = next((j for j, h in enumerate(low) if h in ("skuid", "sku_id")), -1)
            b = next((j for j, h in enumerate(low)
                      if h in ("sellersku", "seller_sku", "卖家sku", "卖家 SKU".lower())), -1)
            if a >= 0 and b >= 0:
                hidx, c_sid, c_sku = i, a, b
                break
        if hidx < 0:
            continue
        for r in rows[hidx + 1:]:
            if len(r) <= max(c_sid, c_sku):
                continue
            sid = str(r[c_sid] or "").strip()
            sku = str(r[c_sku] or "").strip()
            if sid and sku and sid not in out:
                out[sid] = sku
    return out


def merge_file(path: Path, master: dict[str, dict], events: list[dict], now: str,
               overwrite: bool = True) -> tuple[int, int, int]:
    if path.suffix.lower() in (".xlsx", ".xls", ".csv"):
        idx = parse_mapping(read_table(path))
    else:
        idx = parse_mapping_json(path)
    added, rebound, same = 0, 0, 0

    for sid, item in idx.by_sku_id.items():
        row = {
            "sku_id": sid,
            "product_id": item["product_id"],
            "seller_sku": "",
            "product_name": item.get("product_name", ""),
            "style_code": item.get("style_code", ""),
            "updated_at": now,
        }
        # 反查卖家SKU（同一个 item 对象可能挂在多个卖家SKU下，取第一个即可）
        for sku, bucket in idx.by_seller_sku.items():
            if any(b is item for b in bucket):
                row["seller_sku"] = sku
                break
        # 表里没有卖家SKU（比如「商品数据分析」导出的表）→ 用订单表里的补上，
        # 再据此推出款式编码。这样就不用额外导「链接资料」那张表了。
        if not row["seller_sku"]:
            from_orders = ORDER_SKU.get(sid)
            if from_orders:
                row["seller_sku"] = from_orders
        if not row["style_code"] and row["seller_sku"]:
            row["style_code"] = derive_style_code(row["seller_sku"])

        old = master.get(sid)
        if old is None:
            master[sid] = row
            added += 1
            events.append({"at": now, "kind": "new", "sku_id": sid,
                           "old": None, "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
        elif old["product_id"] != row["product_id"]:
            if not overwrite:
                # 非主来源：只补缺，不改已有归属（归属以主来源为准）
                same += 1
                continue
            # 同一个 SKU ID 指向了不同商品：SKU ID 不会改挂，这只可能是数据脏了
            events.append({"at": now, "kind": "rebind", "sku_id": sid,
                           "old": old["product_id"], "new": row["product_id"],
                           "seller_sku": row["seller_sku"]})
            row["updated_at"] = now
            master[sid] = row
            rebound += 1
        else:
            # 同商品，只更新描述字段。
            #
            # 关键：**空值绝不覆盖已有值**。
            # 商品数据分析接口返回的行没有「卖家SKU」（seller_sku 为空），
            # 如果按「有新值就整行替换」，它会把老数据里辛苦攒下的
            # seller_sku / style_code 全冲成空——报表的款式编码列就废了。
            desc = ("seller_sku", "product_name", "style_code")
            merged = dict(old)
            touched = False
            for k in desc:
                nv = (row.get(k) or "").strip()
                if nv and nv != (old.get(k) or ""):
                    merged[k] = row[k]
                    touched = True
            if touched:
                merged["updated_at"] = now
                master[sid] = merged
            same += 1
    return added, rebound, same


def _acquire_merge_lock(timeout: float = 90.0):
    """跨进程互斥：两家店同时核算时，主表只能由一个进程写。

    没有锁的时候，两个核算子进程会各读一份主表、各写一遍——
    后写的把先写的覆盖掉（少一批 SKU），或者读到写了一半的文件（整个表废掉）。
    锁文件放在 mapping 目录里，进程崩了超过 10 分钟自动作废，不会永远卡住。
    """
    lock = MAPPING_DIR / ".merge.lock"
    try:
        MAPPING_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    t0 = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() - t0 > timeout:
                return None
            time.sleep(0.3)
        except OSError:
            return None


def _release_merge_lock(lock) -> None:
    if lock is None:
        return
    try:
        lock.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    lock = _acquire_merge_lock()
    try:
        return _main_unlocked(argv)
    finally:
        _release_merge_lock(lock)


def _main_unlocked(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    sources = [Path(a) for a in argv] if argv else find_sources()

    if not sources:
        print(f"没找到 sku_list_*.json：{INBOX}")
        return 2

    master = load_master()
    # 从订单表补卖家SKU（用来推款式编码），免得再导一张链接资料
    global ORDER_SKU
    ORDER_SKU = load_seller_sku_from_orders()
    if ORDER_SKU:
        print(f"从订单表读到 {len(ORDER_SKU)} 个 SKU 的卖家编码（用于推款式编码）")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events: list[dict] = []
    total = [0, 0, 0]

    for src in sources:
        if not src.exists():
            print(f"跳过不存在的文件：{src}")
            continue
        primary = is_primary(src)
        added, rebound, same = merge_file(src, master, events, now, overwrite=primary)
        print(f"      {'主来源' if primary else '补充（只补缺）'}")
        total[0] += added
        total[1] += rebound
        total[2] += same
        print(f"  {src.name}：新增 {added}｜改绑 {rebound}｜未变 {same}")

    ok = write_master(master)
    if events and ok:
        MAPPING_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"\n合计：新增 {total[0]} 条｜改绑 {total[1]} 条｜未变 {total[2]} 条｜主表现有 {len(master)} 条")
    print(f"主表：{MASTER}")

    rebinds = [e for e in events if e["kind"] == "rebind"]
    if rebinds:
        print(f"\n⚠ 本次有 {len(rebinds)} 个 SKU 的归属跟主表对不上"
              f"（SKU ID 不会改挂链接，多半是导出数据叠错了；已按这次的表覆盖）：")
        for e in rebinds[:8]:
            print(f"  SKU {e['sku_id']}（{e['seller_sku'] or '无卖家SKU'}）：商品 {e['old']} → {e['new']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
