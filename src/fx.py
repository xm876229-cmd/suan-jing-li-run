"""汇率：自动抓取汇率，也支持手动固定。

**口径：核算用「操作日的前一天」的汇率。**
    2 号导表核算 → 用 1 号的汇率；3 号核算 → 用 2 号的。
    为什么：平台的结算/账单是按前一天的中间价结的，用当天的实时价去算，
    同一批订单今天算和明天算会差一点，对账时说不清是谁的错。

三种模式（config/rates.json 的 fx.mode）：
    auto    按 T-1 联网取；取不到依次退：本机存档 → 最新价 → rates.json
    manual  完全不联网，只用 rates.json 里（或 fx.manual）的值
    off     同 manual（留着更直白）

手动固定的两个层次：
    1. fx.mode = "manual"        整个程序都不联网
    2. fx.manual.<国家> = {...}  只固定某个国家，其余仍自动

为什么要存档（数据/汇率存档/YYYY-MM-DD.json）：
    同一天里点几次「重算」必须用同一个汇率，否则净利会因为汇率微动而变，
    看着像 bug。存了档就是「这一天用这个数」，跨进程、跨重启都一致。

取数源：
    frankfurter.app（欧洲央行参考价，免 key，支持按日期取）
    它只覆盖 ECB 那批币种；越南盾(VND)等没有的，退回 open.er-api 的最新价并注明。

缓存文件：数据/fx_cache.json（最近一次取到的汇率，界面用）
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from src import paths

CACHE_FILE = paths.DATA / "fx_cache.json"
# 最近一次拿到的汇率表（CNY 基准）。广告表的币种如果既不是本地币也不是美金，
# 就用它算交叉汇率（例如泰铢→马币 = rate_MYR / rate_THB）。
LAST_TABLE: dict = {}
ARCHIVE_DIR = paths.DATA / "汇率存档"
DEFAULT_PROVIDER = "open.er-api.com"
DEFAULT_TTL_HOURS = 12          # 保留：老配置里可能还写着，新逻辑按「日期」判新旧
TIMEOUT = 8
HISTORY_PROVIDER = "frankfurter.app"
HISTORY_URL = "https://api.frankfurter.app"


def _read_cache() -> dict:
    from src import safeio
    got = safeio.read_json(CACHE_FILE, default={})
    return got if isinstance(got, dict) else {}


def _write_cache(obj: dict) -> None:
    try:
        from src import safeio
        safeio.write_json(CACHE_FILE, obj)
    except OSError:
        pass


def fetch_cny_rates(provider: str = DEFAULT_PROVIDER, timeout: int = TIMEOUT) -> dict | None:
    """抓「1 人民币 = 多少外币」的汇率表。失败返回 None，从不抛异常。

    用的是 open.er-api.com 的免费接口：无需 key，返回结构简单。
    企业网络常会拦外网，所以这里必须容错——抓不到就用配置里的兜底值，
    不能让核算因为汇率抓不到而整次失败。
    """
    if provider != "open.er-api.com":
        return None
    url = "https://open.er-api.com/v6/latest/CNY"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jingli-helper"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("result") != "success" or not isinstance(data.get("rates"), dict):
            return None
        return {
            "at": time.time(),
            "provider": provider,
            "base": "CNY",
            "rates": data["rates"],
            "updated": data.get("time_last_update_utc", ""),
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None


def cny_rates(mode: str = "auto", provider: str = DEFAULT_PROVIDER,
              ttl_hours: int = DEFAULT_TTL_HOURS, log=None,
              day: "date | None" = None) -> tuple[dict | None, str]:
    """取汇率表。T-1 优先 → 本机存档 → 最新价。返回 (表, 说明)。

    ttl_hours 只为兼容老配置保留：现在按「目标日期」判新旧，同一天永远是同一组数。
    """
    if mode in ("manual", "off", "fixed"):
        # manual 的语义是「完全不联网，就用配置里写死的值」，所以这里**不能**
        # 退回缓存——缓存是之前联网抓来的，一旦退回它，manual 就名存实亡了
        # （用户填的固定值永远不生效，且每次跑出来的数还会随上次联网时间漂移）。
        return None, "手动模式，使用 config/rates.json 里的汇率"

    target = day or rate_day()
    global LAST_TABLE
    archived = _read_archive(target)
    if archived:
        LAST_TABLE = archived
        rd = str(archived.get("rate_date") or target.isoformat())
        return archived, f"取 {rd} 的汇率（T-1，本机存档）"

    fresh = fetch_on(target)
    if fresh:
        LAST_TABLE = fresh
        _write_archive(target, fresh)
        _write_cache(fresh)
        rd = str(fresh.get("rate_date") or target.isoformat())
        if log:
            log(f"已取到 {rd} 的汇率（{HISTORY_PROVIDER}）")
        note = f"取 {rd} 的汇率（T-1）"
        if rd != target.isoformat():
            note += f"；{target.isoformat()} 无报价（周末/节假日），用的是最近一个工作日"
        return fresh, note

    # 按日期取不到（断网 / 接口变了）：退回最新价，说明里写清楚是哪天的
    latest = fetch_cny_rates(provider)
    if latest:
        _write_cache(latest)
        LAST_TABLE = latest
        rd = str(latest.get("updated") or "")[:16]
        return latest, "T-1 取不到，改用最新价" + (f"（{rd}）" if rd else "")

    cache = _read_cache()
    age_h = (time.time() - float(cache.get("at") or 0)) / 3600 if cache.get("at") else 1e9
    if cache.get("rates"):
        LAST_TABLE = cache
        return cache, f"联网失败，改用 {age_h:.0f} 小时前的缓存汇率"
    return None, "联网失败且无缓存，使用 rates.json 里的汇率"


def rate_day(now: datetime | None = None) -> date:
    """这次核算该用哪一天的汇率：操作日的前一天（T-1）。

    2 号导表核算 → 1 号；3 号核算 → 2 号。平台账单是按前一天中间价结的。
    """
    return (now or datetime.now()).date() - timedelta(days=1)


def _archive_path(day: date) -> Path:
    return ARCHIVE_DIR / f"{day.isoformat()}.json"


def _read_archive(day: date) -> dict | None:
    from src import safeio
    got = safeio.read_json(_archive_path(day), default={})
    if isinstance(got, dict) and isinstance(got.get("rates"), dict) and got["rates"]:
        return got
    return None


def _write_archive(day: date, table: dict) -> None:
    try:
        from src import safeio
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        safeio.write_json(_archive_path(day), table)
    except OSError:
        pass


def fetch_on(day: date) -> dict | None:
    """按日期取汇率（欧洲央行参考价，免 key）。失败返回 None，从不抛异常。

    周末/节假日 ECB 不发价，接口会自己回落到最近一个工作日，
    返回里的 rate_date 是**真正取到的那一天**，不是请求的那一天。
    """
    url = f"{HISTORY_URL}/{day.isoformat()}?from=CNY"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jingli-helper"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rates = data.get("rates") if isinstance(data, dict) else None
        if not isinstance(rates, dict) or not rates:
            return None
        got = {str(k).upper(): float(v) for k, v in rates.items()
               if isinstance(v, (int, float)) and v}
        got["CNY"] = 1.0
        return {
            "at": time.time(),
            "provider": HISTORY_PROVIDER,
            "base": "CNY",
            "rates": got,
            "rate_date": str(data.get("date") or day.isoformat()),
            "requested": day.isoformat(),
            "updated": str(data.get("date") or ""),
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None


def _g(obj, key: str, default=None):
    """从 dict 或对象上取字段，两种都支持（Params 是对象，配置是 dict）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def load_fx_cfg(path: Path) -> dict:
    """从 rates.json 里读 fx 段。读不到就用默认（自动 + 12 小时）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = data.get("fx")
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {"mode": "auto", "provider": DEFAULT_PROVIDER, "ttl_hours": DEFAULT_TTL_HOURS, "manual": {}}


def resolve(country: str, cfg, fx_cfg: dict, table: dict | None) -> tuple[float, float, str]:
    """算出一国要用的 (人民币→本地, 美金→本地, 来源说明)。

    优先级：fx.manual[国家] 指定值 > 实时汇率 > 配置里的兜底值。
    """
    cur = str(_g(cfg, "currency", "") or "").upper()
    base_cny = float(_g(cfg, "cny_to_local", 0) or 0)
    base_usd = float(_g(cfg, "usd_to_local", 0) or 0)

    manual = (fx_cfg.get("manual") or {}).get(country) or {}
    m_cny = manual.get("cny_to_local")
    m_usd = manual.get("usd_to_local")
    if m_cny is not None and m_usd is not None:
        return float(m_cny), float(m_usd), "手动固定"

    if table and cur:
        rates = table.get("rates") or {}
        r_local = rates.get(cur)
        r_usd = rates.get("USD") or 1.0
        if isinstance(r_local, (int, float)) and r_local > 0:
            cny = float(r_local)
            usd = cny / float(r_usd) if r_usd else base_usd
            if m_cny is not None:
                cny = float(m_cny)
            if m_usd is not None:
                usd = float(m_usd)
            # 说清是哪一天的价：对账时要能回答「你这个 4.06 是哪来的」
            tag = str(table.get("rate_date") or "")
            src = (f"{tag} 的汇率" if tag else "实时汇率")
            return cny, usd, f"{src}（1 CNY = {cny:g} {cur}）"

    # 拿到汇率表却没有这个币种（比如越南盾不在欧洲央行那批里）：
    # 不能装作取到了，得让人看见「这国用的是配置固定值」。
    miss = ""
    if table and cur and cur not in (table.get("rates") or {}):
        miss = f"{cur} 没有 T-1 报价，"
    return (float(m_cny) if m_cny is not None else base_cny,
            float(m_usd) if m_usd is not None else base_usd,
            f"{miss}价格取配置里的固定值" if miss else "配置里的固定值")


def status() -> dict:
    """界面上要显示「这次核算到底用哪天的汇率」，只读，不改任何配置。"""
    try:
        table, note = cny_rates()
        if not table:
            return {"ok": False, "note": note}
        keep = ("USD", "MYR", "PHP", "THB", "SGD", "IDR", "VND")
        return {
            "ok": True,
            "note": note,
            "rate_date": str(table.get("rate_date") or ""),
            "provider": str(table.get("provider") or ""),
            "rates": {k: float(v) for k, v in (table.get("rates") or {}).items()
                      if k in keep},
        }
    except Exception as e:  # noqa: BLE001  界面提示而已，不能把页面拖崩
        return {"ok": False, "note": str(e)}


def apply_to_params(params, country: str, fx_cfg: dict,
                    mode_override: str | None = None, log=None,
                    day: "date | None" = None) -> str:
    """把汇率写进 Params，返回一句说明（用于写进报表口径页）。"""
    mode = (mode_override or fx_cfg.get("mode") or "auto").lower()
    table, table_note = cny_rates(
        mode=mode,
        provider=fx_cfg.get("provider") or DEFAULT_PROVIDER,
        ttl_hours=fx_cfg.get("ttl_hours") or DEFAULT_TTL_HOURS,
        log=log,
        day=day,
    )
    cny, usd, src = resolve(country, params, fx_cfg, table)
    params.cny_to_local = cny
    params.usd_to_local = usd
    cur = str(_g(params, "currency", "") or "?")
    note = f"{src}｜1 CNY = {cny:g} {cur}，1 USD = {usd:g} {cur}（{table_note}）"
    params.fx_note = note
    return note


def last_rates() -> dict:
    """最近一次汇率表的 rates（CNY 基准，1 CNY = X 外币）。没有就返回空。"""
    try:
        return dict((LAST_TABLE or {}).get("rates") or {})
    except Exception:  # noqa: BLE001
        return {}
