"""汇率：自动抓取实时汇率，也支持手动固定。

三种模式（config/rates.json 的 fx.mode）：
    auto    优先抓实时汇率；抓不到（断网/接口挂了）退回 rates.json 里写的值
    manual  完全不联网，只用 rates.json 里的值
    off     同 manual（留着更直白）

手动固定的两个层次：
    1. fx.mode = "manual"        整个程序都不联网
    2. fx.manual.<国家> = {...}  只固定某个国家，其余仍自动

为什么要有缓存：
    一次核算里会用到"人民币→本地"和"美金→本地"两个汇率，
    但一个报表只要一组。缓存 12 小时既省请求，也避免同一天两次跑出不同结果。

缓存文件：data/fx_cache.json（不入库）
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from src import paths

CACHE_FILE = paths.DATA / "fx_cache.json"
DEFAULT_PROVIDER = "open.er-api.com"
DEFAULT_TTL_HOURS = 12
TIMEOUT = 8


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(obj: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
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
              ttl_hours: int = DEFAULT_TTL_HOURS, log=None) -> tuple[dict | None, str]:
    """取汇率表。缓存优先，过期才联网。返回 (表, 说明)。"""
    if mode in ("manual", "off", "fixed"):
        # manual 的语义是「完全不联网，就用配置里写死的值」，所以这里**不能**
        # 退回缓存——缓存是之前联网抓来的，一旦退回它，manual 就名存实亡了
        # （用户填的固定值永远不生效，且每次跑出来的数还会随上次联网时间漂移）。
        return None, "手动模式，使用 config/rates.json 里的汇率"

    cache = _read_cache()
    age_h = (time.time() - float(cache.get("at") or 0)) / 3600 if cache.get("at") else 1e9
    if cache.get("rates") and age_h < max(1, int(ttl_hours)):
        return cache, f"汇率缓存（{age_h:.1f} 小时前）"

    fresh = fetch_cny_rates(provider)
    if fresh:
        _write_cache(fresh)
        if log:
            log(f"已从 {provider} 更新汇率（{fresh.get('updated') or ''}）")
        return fresh, "实时汇率"

    if cache.get("rates"):
        return cache, f"联网失败，改用 {age_h:.0f} 小时前的缓存汇率"
    return None, "联网失败且无缓存，使用 rates.json 里的汇率"


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
            return cny, usd, f"实时汇率（1 CNY = {cny:g} {cur}）"

    return (float(m_cny) if m_cny is not None else base_cny,
            float(m_usd) if m_usd is not None else base_usd,
            "配置里的固定值")


def apply_to_params(params, country: str, fx_cfg: dict,
                    mode_override: str | None = None, log=None) -> str:
    """把汇率写进 Params，返回一句说明（用于写进报表口径页）。"""
    mode = (mode_override or fx_cfg.get("mode") or "auto").lower()
    table, table_note = cny_rates(
        mode=mode,
        provider=fx_cfg.get("provider") or DEFAULT_PROVIDER,
        ttl_hours=fx_cfg.get("ttl_hours") or DEFAULT_TTL_HOURS,
        log=log,
    )
    cny, usd, src = resolve(country, params, fx_cfg, table)
    params.cny_to_local = cny
    params.usd_to_local = usd
    cur = str(_g(params, "currency", "") or "?")
    note = f"{src}｜1 CNY = {cny:g} {cur}，1 USD = {usd:g} {cur}（{table_note}）"
    params.fx_note = note
    return note
