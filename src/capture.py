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
