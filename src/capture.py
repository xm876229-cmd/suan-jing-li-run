"""今天已经取到过哪些表（按「店铺 + 区间 + 当天」记一本账）。

为什么需要这本账
----------------
同一家店、同一个区间，一天里常常要算好几次：改一下佣金费率、补一条成本、
换个汇总口径，都得重新出一份报表。每次都去平台重导一遍既慢（大店订单光等
平台生成就要 5 分钟），又容易撞风控——而当天早上导的那批数据本来就没变。

所以取数成功时记一笔：哪天、哪个区间、哪一类表、落在哪个文件。
下次同店同区间再跑，只要还是**同一天**，这几类表就直接复用，不再去平台导。

规矩只有一条：**跨天就失效**。订单每时每刻都在涨，昨天的数不能当今天用。
要强制重导，界面上有「强制重新取数」的勾，或者换个区间。

台账落在店铺目录里：
    数据/店铺/<店名>/取数记录.json
跟店一起搬走，换电脑也不丢。写盘走 safeio（原子写 + .bak），
断电最多丢最后一次记账，不会把整本账读坏。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from . import safeio

# 这四类表是核算的原料。名字跟扩展里的取数任务一一对应，
# 不能随便改——改了扩展那边的复用判断就对不上了。
KINDS = ("orders", "sku", "ads", "affiliate")
KIND_CN = {
    "orders": "订单表",
    "sku": "商品SKU表",
    "ads": "广告表",
    "affiliate": "联盟佣金表",
}

DAY_FMT = "%Y-%m-%d"
_TS_FMT = "%Y-%m-%d %H:%M:%S"
KEEP_DAYS = 14          # 台账只留最近两周，再早的没有复用价值
KEEP_MAX = 400          # 兜底上限，免得反复换区间把文件堆大

_LOCK = threading.Lock()
# 文件 → (mtime, 解析后的 doc)。界面上每 3 秒拉一次卡片墙，
# 每家店都读一遍磁盘没必要；mtime 变了才重读。
_CACHE: dict[str, tuple[float, dict]] = {}


def ledger_file(shop_id: str) -> Path:
    """某家店的台账文件。"""
    return paths.store_root(shop_id) / "取数记录.json"


def _empty(shop_id: str) -> dict:
    return {"version": 1, "shop_id": shop_id, "shop_name": "", "entries": []}


def load(shop_id: str) -> dict:
    """读台账。读不出来就当空账——绝不让它挡住取数。"""
    sid = (shop_id or "").strip()
    if not sid:
        return _empty("")
    f = ledger_file(sid)
    try:
        mt = f.stat().st_mtime
    except OSError:
        return _empty(sid)
    hit = _CACHE.get(str(f))
    if hit and hit[0] == mt:
        return hit[1]
    doc = safeio.read_json(f, default=None)
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        doc = _empty(sid)
    doc["entries"] = [e for e in doc["entries"] if isinstance(e, dict)]
    _CACHE[str(f)] = (mt, doc)
    return doc


def _resolve(rel: str) -> Path | None:
    """把台账里记的相对路径还原成真实文件，不存在就返回 None。

    一定要验存在：用户会手动删文件、杀毒软件会隔离文件。
    账上写着「今天取过」但文件已经没了，还跳过取数，就成了一笔糊涂账。
    """
    raw = str(rel or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = paths.DATA / p
    try:
        p = p.resolve()
    except OSError:
        return None
    try:
        return p if p.is_file() else None
    except OSError:
        return None


def _rel(path) -> str:
    """记成「相对数据根」的路径：换盘符、换安装位置都还认得。"""
    try:
        return str(Path(path).resolve().relative_to(paths.DATA.resolve()))
    except Exception:  # noqa: BLE001
        return str(path)


def mark(shop_id: str, kind: str, dfrom: str, dto: str, path,
         shop_name: str = "", now: datetime | None = None) -> bool:
    """记一笔「这类表今天取过了」。

    同一天、同区间、同类型只留一条（后取的覆盖先取的）——
    台账记的是「最新那份文件在哪」，不是流水账。
    """
    sid = (shop_id or "").strip()
    k = (kind or "").strip()
    if not sid or k not in KINDS:
        return False
    a = str(dfrom or "").strip()
    b = str(dto or "").strip()
    if not a or not b:
        # 没有区间就没法判断「是不是同一批」，不记。
        # 记了反而会让「本次全量」和「某个月」互相冒用。
        return False
    if b < a:
        a, b = b, a
    p = Path(path)
    if not p.exists():
        return False

    ts = (now or datetime.now())
    day = ts.strftime(DAY_FMT)
    entry = {
        "day": day,
        "from": a,
        "to": b,
        "kind": k,
        "file": _rel(p),
        "at": ts.strftime(_TS_FMT),
    }
    with _LOCK:
        doc = load(sid)
        entries = [e for e in doc.get("entries", [])
                   if not (e.get("day") == day and e.get("from") == a
                           and e.get("to") == b and e.get("kind") == k)]
        entries.append(entry)
        # 只留最近 KEEP_DAYS 天；顺手按天裁剪，避免越攒越大
        cut = (ts - timedelta(days=KEEP_DAYS)).strftime(DAY_FMT)
        entries = [e for e in entries if str(e.get("day") or "") >= cut][-KEEP_MAX:]
        doc = {"version": 1, "shop_id": sid,
               "shop_name": shop_name or doc.get("shop_name") or "",
               "entries": entries}
        try:
            safeio.write_json(ledger_file(sid), doc, indent=1)
        except OSError:
            return False
        try:
            _CACHE[str(ledger_file(sid))] = (ledger_file(sid).stat().st_mtime, doc)
        except OSError:
            _CACHE.pop(str(ledger_file(sid)), None)
    return True


def status(shop_id: str, dfrom: str, dto: str,
           now: datetime | None = None) -> dict:
    """这个店、这个区间，今天还能复用哪几类表。

    只认「同一天 + 起止日期完全一样 + 文件还在」。三条缺一条就老老实实重取。
    返回的 reuse 是能跳过的类型，missing 是需要重新导的。
    """
    ts = now or datetime.now()
    day = ts.strftime(DAY_FMT)
    a = str(dfrom or "").strip()
    b = str(dto or "").strip()
    out: dict = {
        "ok": True, "day": day, "from": a, "to": b,
        "tables": {}, "reuse": [], "missing": list(KINDS),
        "reason": "",
    }
    if not (shop_id or "").strip():
        out.update(ok=False, reason="还没认出是哪家店，这次照常全部重新取")
        return out
    if not a or not b:
        out.update(ok=False, reason="这次没限定区间（全部数据），判断不了能不能复用")
        return out
    if b < a:
        a, b = b, a
        out["from"], out["to"] = a, b

    doc = load(shop_id)
    best: dict[str, dict] = {}
    for e in doc.get("entries", []):
        if e.get("day") != day or e.get("from") != a or e.get("to") != b:
            continue
        k = str(e.get("kind") or "")
        if k not in KINDS:
            continue
        if _resolve(e.get("file") or "") is None:
            continue          # 文件被删/被隔离了，不能算「取过」
        prev = best.get(k)
        if prev is None or str(e.get("at") or "") > str(prev.get("at") or ""):
            best[k] = e

    for k, e in best.items():
        out["tables"][k] = {
            "file": e.get("file") or "",
            "at": str(e.get("at") or "")[11:16],     # 只给 HH:MM，界面上一行放得下
            "at_full": e.get("at") or "",
            "label": KIND_CN.get(k, k),
        }
    out["reuse"] = [k for k in KINDS if k in best]
    out["missing"] = [k for k in KINDS if k not in best]
    return out


def today(shop_id: str, now: datetime | None = None) -> dict:
    """今天这家店取过什么、最后一次是几点——驾驶舱卡片上那行小字用。"""
    ts = now or datetime.now()
    day = ts.strftime(DAY_FMT)
    kinds: dict[str, str] = {}
    first = last = ""
    doc = load(shop_id)
    for e in doc.get("entries", []):
        if e.get("day") != day:
            continue
        if _resolve(e.get("file") or "") is None:
            continue
        at = str(e.get("at") or "")
        k = str(e.get("kind") or "")
        if k in KINDS:
            kinds[k] = at
        if at and (not first or at < first):
            first = at
        if at > last:
            last = at
    return {
        "day": day,
        "count": len(kinds),
        "kinds": [k for k in KINDS if k in kinds],
        "labels": [KIND_CN[k] for k in KINDS if k in kinds],
        "first": first[11:16] if first else "",
        "last": last[11:16] if last else "",
        "ranges": sorted({f"{e.get('from')}~{e.get('to')}"
                          for e in doc.get("entries", [])
                          if e.get("day") == day}),
    }


# ---------------- 取数批次（2026-11-04） ----------------
#
# 一批 = 同一家店、**同一天落盘、同一个区间**的那几类表。
# 为什么要有这个概念：最常见的用法是「先算 10 月，再回头算 8 月」。
# 收件箱是滚动累积的，程序靠区间挑文件——这已经能挡住串月，但人没法确认
# 「这次到底用的是哪一批」。批次把这件事变成可点、可复现的一行字。
#
# 批次**只记不搬**：文件还在收件箱原位。不按「最新/历史」两个文件夹整包搬，是因为：
#   ① 算 8 月时 8 月那批已经被搬到历史里，程序要么找不到、要么还得去历史捞；
#   ② 跨月区间（7/1~8/15）天然需要两份，整包搬就少一段；
#   ③ SKU 表是长期有效的，跟着搬走会让老月份全变「无ID」；
#   ④ 多店并行取数时搬目录要抢锁，搬一半断电两处都不全。
#
# 批次不依赖「取数台账」：台账只记通过 /upload 落盘的文件，
# 手动导入、老版本留下的文件它根本没有。这里直接按**文件本身**归组
# （身份证里的区间优先，其次文件名），所以历史数据也能分出批次。
BATCH_SEP = "__"

# 收件箱里这四类子目录 → 台账里的 kind
_DIR_KIND = {
    "订单表": "orders",
    "广告表": "ads",
    "联盟佣金表": "affiliate",
    "商品SKU表": "sku",
}

_RANGE_RE = re.compile(
    r"(\d{4})[-_./]?(\d{2})[-_./]?(\d{2}).{0,12}?(\d{4})[-_./]?(\d{2})[-_./]?(\d{2})")


def batch_id(day: str, dfrom: str, dto: str) -> str:
    return f"{day}{BATCH_SEP}{dfrom}{BATCH_SEP}{dto}"


def parse_batch_id(bid: str) -> tuple[str, str, str] | None:
    parts = str(bid or "").split(BATCH_SEP)
    if len(parts) != 3:
        return None
    day, a, b = (p.strip() for p in parts)
    if not day or not a or not b:
        return None
    return day, a, b


def _file_range(p: Path) -> tuple[str, str] | None:
    """这份文件盖的是哪一段：身份证（落盘时写死）优先，其次文件名。"""
    try:
        from . import provenance
        meta = provenance.read_meta(p)
        rng = meta.get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            a, b = str(rng[0]), str(rng[1])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", b):
                return (a, b) if a <= b else (b, a)
    except Exception:  # noqa: BLE001
        pass
    for m in _RANGE_RE.finditer(Path(p).name):
        try:
            a = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            b = f"{int(m.group(4)):04d}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
            datetime.strptime(a, "%Y-%m-%d")
            datetime.strptime(b, "%Y-%m-%d")
        except ValueError:
            continue
        return (a, b) if a <= b else (b, a)
    return None


def batches(shop_id: str, limit: int = 40) -> list[dict]:
    """这家店取过的批次，新的在前（newest first）。

    归组依据是「文件落盘那天 + 文件自己写的区间」，跟取数台账无关——
    手动导入的、老版本留下的文件也要能分进批次，否则点进去必然缺东西。
    """
    root = paths.store_root(shop_id)
    if not root.is_dir():
        return []
    ranged: list[tuple] = []      # (day, rng, kind, path, at)
    sku_only: list[tuple] = []    # SKU 表没有区间概念，按天归
    for f in root.rglob("*"):
        if not f.is_file() or f.name.startswith(".") or f.name.startswith("~$"):
            continue
        if f.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            continue
        if f.name.endswith(".srcmeta.json"):
            continue
        kind = _DIR_KIND.get(f.parent.name) or _DIR_KIND.get(f.parent.parent.name)
        if not kind:
            continue
        try:
            mt = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        day = mt.strftime(DAY_FMT)
        at = mt.strftime(_TS_FMT)
        rng = _file_range(f)
        if rng is None:
            if kind == "sku":
                sku_only.append((day, kind, f, at))
            continue
        ranged.append((day, rng, kind, f, at))

    groups: dict[str, dict] = {}
    for day, rng, kind, f, at in ranged:
        bid = batch_id(day, rng[0], rng[1])
        g = groups.setdefault(bid, {
            "id": bid, "day": day, "from": rng[0], "to": rng[1],
            "at": "", "files": {}, "kinds": [],
        })
        if at > g["at"]:
            g["at"] = at
        if kind not in g["files"]:
            g["files"][kind] = str(f)
            g["kinds"].append(kind)

    # SKU 表（长期有效、文件名不带区间）挂到同一天「最新那个批次」上。
    # 一天只有一个批次时就是它；有多个时挂最近的那个——SKU 表本来就是那时候导的。
    for day, kind, f, at in sku_only:
        # 挂到「区间最近」的那个批次：SKU 表是给最新一期用的，
        # 同一天里既有 6 月批又有 7 月批时，7 月才是它该在的地方。
        same = [g for g in groups.values() if g["day"] == day and "sku" not in g["files"]]
        if same:
            g = max(same, key=lambda x: (x["to"], x["at"]))
            g["files"]["sku"] = str(f)
            g["kinds"].append("sku")
            continue
        bid = batch_id(day, "", "")
        g = groups.setdefault(bid, {
            "id": bid, "day": day, "from": "", "to": "",
            "at": "", "files": {}, "kinds": [],
        })
        if at > g["at"]:
            g["at"] = at
        if "sku" not in g["files"]:
            g["files"]["sku"] = str(f)
            g["kinds"].append("sku")

    out = []
    for g in groups.values():
        g["kinds"] = [k for k in KINDS if k in g["kinds"]]
        g["labels"] = [KIND_CN[k] for k in g["kinds"]]
        g["missing"] = [KIND_CN[k] for k in KINDS if k not in g["kinds"]]
        g["complete"] = not g["missing"]
        out.append(g)
    out.sort(key=lambda x: (x["at"], x["day"]), reverse=True)
    return out[:limit]


def batch_files(shop_id: str, bid: str) -> list[Path]:
    """某一批实际落在磁盘上的文件（绝对路径，已验存在）。"""
    for g in batches(shop_id, limit=500):
        if g["id"] != bid:
            continue
        return [Path(p) for p in g["files"].values() if Path(p).is_file()]
    return []
