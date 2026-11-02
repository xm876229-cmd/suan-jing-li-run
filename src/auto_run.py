"""计划任务：到点自动抓数据 → 核算 → 出报表。

为什么由桥派任务，而不是扩展自己定时
------------------------------------
扩展的定时器只在「浏览器开着、扩展刚被刷新过」时才可靠；
浏览器一关、扩展一重载，定时就哑了。
本地桥是常驻后台进程，浏览器关着它也活着，所以定时交给桥。

桥这边只做两件事：
  1. 到点了，往待办队列里塞一个任务（扩展会来领，按店铺投递）
  2. 剩下的取数、核算、出报表，扩展那边照常跑

区间怎么算（不存死日期，只存规则）
----------------------------------
一条计划只记三件事：
  · 周期 period：按天 / 按周 / 按月 / 最近 7 天 / 全部
  · 偏移 offset：本期=0，上一期=1，前 N 期=N
  · 截到哪天 cut：整期 / 到今天 / 第 N 天

于是「每月 1 号导上个月」这种计划，明年、后年、换台电脑都还是对的：
  每月 1 号 09:00 触发 → 周期=月、偏移=1、截=整期 → 2026-11-01 ~ 2026-11-30
「每月 3 号导本月 1 号到昨天」：
  每月 3 号 09:00 触发 → 周期=月、偏移=0、截=到今天 → 2026-11-01 ~ 2026-11-02
"""

from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

# 频率：多久触发一次
FREQS = ("daily", "weekly", "monthly")
FREQ_LABELS = {"daily": "每天", "weekly": "每周", "monthly": "每月"}

# 周期：算哪一段数据
PERIODS = ("day", "week", "month", "last7", "all")
PERIOD_LABELS = {
    "day": "按天", "week": "按周", "month": "按月",
    "last7": "最近 7 天", "all": "全部数据",
}

# 截到哪天
CUTS = ("end", "today", "day")
CUT_LABELS = {"end": "整期（到期末）", "today": "到今天", "day": "第 N 天"}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

DEFAULT_RULE: dict = {
    "id": "",
    "name": "",
    "enabled": True,
    # 什么时候触发
    "freq": "monthly",
    "time": "09:00",
    "day": 1,          # freq=monthly：每月几号
    "weekday": 0,      # freq=weekly：0=周一
    # 算什么区间
    "period": "month",
    "offset": 1,       # 0=本期，1=上一期，2=上两期…
    "cut": "end",      # end / today / day
    "cut_day": 1,      # cut=day 时截到第几号（周：第几天）
    # 跑哪些店（空 = 全部）
    "stores": [],
    # 报表落到哪儿（空 = 只用程序里的「报表」目录）
    "download_dir": "",
    # 带不带公式；None = 跟随「设置 → 报表保存位置」的开关
    "with_formula": None,
}


# ---------------- 规整 ----------------

def _hm(rule: dict) -> tuple[int, int]:
    """把 "09:00" 拆成 (9, 0)。写错了就当 09:00，不让一条脏数据把整轮打掉。"""
    raw = str((rule or {}).get("time") or "09:00").strip()
    try:
        h, m = raw.split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    return 9, 0


def _int(v, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def normalize_rule(raw: dict) -> dict:
    """把界面上/旧配置里拿到的规则补全成完整结构。

    宁可把不认识的值换成默认值，也不要抛异常——一条规则写坏了，
    不该连累其余几条计划不跑。
    """
    r = _norm_fields(raw)
    if not r["name"]:
        r["name"] = _fmt_when(r)     # 不回调 normalize_rule，免得自己套自己
    return r


def _norm_fields(raw: dict) -> dict:
    r = dict(DEFAULT_RULE)
    # raw 不是字典就整条按默认值处理。看着像多余的防御，实际踩过：
    # settings.json 的 schedules 里混进一个字符串（手改、别的版本写坏、
    # 或旧格式迁移），normalize_rule 就抛 AttributeError，
    # 于是**整个计划任务页 500、自动运行也 500**——一条坏规则废掉全部功能。
    # 这个函数的 docstring 早就写着「不该抛异常」，这里是补上实现。
    if not isinstance(raw, dict):
        raw = {}
    r.update({k: v for k, v in raw.items() if v is not None or k == "with_formula"})

    if r.get("freq") not in FREQS:
        r["freq"] = "monthly"
    if r.get("period") not in PERIODS:
        r["period"] = "month"
    if r.get("cut") not in CUTS:
        r["cut"] = "end"

    h, m = _hm(r)
    r["time"] = f"{h:02d}:{m:02d}"
    r["day"] = _int(r.get("day"), 1, 1, 31)
    r["weekday"] = _int(r.get("weekday"), 0, 0, 6)
    r["offset"] = _int(r.get("offset"), 0, 0, 60)
    r["cut_day"] = _int(r.get("cut_day"), 1, 1, 31)
    r["enabled"] = bool(r.get("enabled", True))
    r["stores"] = [str(s) for s in (r.get("stores") or []) if str(s).strip()]
    r["download_dir"] = str(r.get("download_dir") or "").strip()
    wf = r.get("with_formula")
    r["with_formula"] = None if wf is None else bool(wf)
    r["name"] = str(r.get("name") or "").strip()
    return r


# ---------------- 日期区间 ----------------

def _shift(d: date, days: int) -> date:
    return d + timedelta(days=days)


def period_span(period: str, offset: int, now: datetime) -> tuple[date, date] | None:
    """周期 + 偏移 → 自然起止（还没截）。返回 None 表示「全部数据」。

    offset=0 是本期，1 是上一期，2 是上两期，依此类推。
    """
    today = now.date()
    period = period if period in PERIODS else "month"
    offset = max(0, int(offset or 0))

    if period == "all":
        return None

    if period == "last7":
        # 「最近 7 天」本身就是一段固定长度，再偏移就是再往前挪 7 天
        end = _shift(today, -1 - 7 * offset)
        return _shift(end, -6), end

    if period == "day":
        d = _shift(today, -offset)
        return d, d

    if period == "week":
        monday = _shift(today, -today.weekday())      # 本周一
        start = _shift(monday, -7 * offset)
        return start, _shift(start, 6)

    # month
    y, m = now.year, now.month
    for _ in range(offset):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start, end


def _apply_cut(rule: dict, start: date, natural_end: date, now: datetime) -> date:
    """按「截到哪天」收窄结束日，并且绝不超过自然期末、也不超过今天。"""
    today = now.date()
    cut = str(rule.get("cut") or "end")
    end = natural_end

    if cut == "today":
        end = min(today, natural_end)
    elif cut == "day":
        n = max(1, _int(rule.get("cut_day"), 1))
        if str(rule.get("period")) == "week":
            end = _shift(start, min(n, 7) - 1)
        elif str(rule.get("period")) == "day":
            end = start
        else:
            end = date(start.year, start.month,
                       min(n, calendar.monthrange(start.year, start.month)[1]))
        end = min(end, natural_end)

    # 本期还没过完时，「整期」也不该把未来的日期算进去
    end = min(end, today)
    return max(start, end)


def build_range(rule: dict, now: datetime | None = None) -> dict:
    """按规则算出这次取数用的区间。

    返回带全套字段的区间对象（startDate / to / endDateExclusive / startMs / endMs），
    扩展那边几种任务各取各的：订单要毫秒，SKU/联盟要开区间，广告要含末日。
    全部数据返回空 dict（不传日期 = 全量）。
    """
    now = now or datetime.now()
    r = normalize_rule(rule)
    span = period_span(r["period"], r["offset"], now)
    if not span:
        return {}
    start, natural_end = span
    end = _apply_cut(r, start, natural_end, now)
    return normalize_range({
        "startDate": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
    })


def _parse_day(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _ms(d: date, end_of_day: bool = False) -> int:
    """本地时区的毫秒时间戳。结束日取当天 23:59:59.999，含末日。"""
    if end_of_day:
        dt = datetime.combine(d, time(23, 59, 59, 999000))
    else:
        dt = datetime.combine(d, time(0, 0, 0))
    return int(dt.timestamp() * 1000)


def normalize_range(rng: dict | None) -> dict:
    """把各种写法的区间补成完整一份。

    为什么要在桥这边补：界面只传 {startDate, to}，而扩展里
    订单任务要 startMs/endMs、SKU 与联盟要 endDateExclusive（开区间）。
    少一个字段，订单就按 undefined 去查，报的是「该时段没有订单」，
    看起来像真没单——这种错最难查。所以在这里一次性补齐。
    """
    r = dict(rng or {})
    s = _parse_day(r.get("startDate") or "")
    t = _parse_day(r.get("to") or "")
    ex = _parse_day(r.get("endDateExclusive") or "")

    if not s and r.get("startMs"):
        try:
            s = datetime.fromtimestamp(int(r["startMs"]) / 1000).date()
        except (ValueError, OSError, TypeError):
            s = None
    if not t and ex:
        t = _shift(ex, -1)
    if not t and r.get("endMs"):
        try:
            t = datetime.fromtimestamp(int(r["endMs"]) / 1000).date()
        except (ValueError, OSError, TypeError):
            t = None
    if not ex and t:
        ex = _shift(t, 1)
    if not s and t:
        s = t

    if not s or not t:
        return r if r.get("startMs") else {}

    if t < s:
        s, t = t, s

    out = dict(r)
    out["startDate"] = s.strftime("%Y-%m-%d")
    out["to"] = t.strftime("%Y-%m-%d")
    out["endDateExclusive"] = ex.strftime("%Y-%m-%d")
    out["startMs"] = _ms(s)
    out["endMs"] = _ms(t, end_of_day=True)
    if not out.get("tzOffset"):
        try:
            off = datetime.now().astimezone().utcoffset()
            out["tzOffset"] = -int(off.total_seconds()) if off else 0
        except Exception:  # noqa: BLE001
            out["tzOffset"] = 0
    return out


# ---------------- 触发时间 ----------------

def fires_on(rule: dict, d: date) -> bool:
    r = normalize_rule(rule)
    freq = r["freq"]
    if freq == "daily":
        return True
    if freq == "weekly":
        return d.weekday() == r["weekday"]
    # monthly：31 号在只有 30 天的月份自动落到月末
    last = calendar.monthrange(d.year, d.month)[1]
    return d.day == min(int(r["day"]), last)


def last_due(rule: dict, now: datetime | None = None) -> datetime | None:
    """最近一次「本该触发」的时刻（<= 现在）。用来判断有没有漏跑。"""
    now = now or datetime.now()
    r = normalize_rule(rule)
    h, m = _hm(r)
    at = time(h, m)
    for i in range(0, 400):
        d = _shift(now.date(), -i)
        if fires_on(r, d):
            dt = datetime.combine(d, at)
            if dt <= now:
                return dt
    return None


def next_due(rule: dict, now: datetime | None = None) -> datetime | None:
    """下一次触发时刻（> 现在）。界面上显示「下次 11-03 09:00」。"""
    now = now or datetime.now()
    r = normalize_rule(rule)
    h, m = _hm(r)
    at = time(h, m)
    for i in range(0, 400):
        d = _shift(now.date(), i)
        if fires_on(r, d):
            dt = datetime.combine(d, at)
            if dt > now:
                return dt
    return None


# ---------------- 文字描述 ----------------

def describe_range(rule: dict, now: datetime | None = None) -> str:
    """这次会算哪一段，直接给日期。"""
    r = normalize_rule(rule)
    rng = build_range(r, now)
    if not rng:
        return "全部数据"
    return f"{rng['startDate']} ~ {rng['to']}"


def range_label(rule: dict, now: datetime | None = None) -> str:
    """区间的中文说法（不带具体日期），卡片上一眼能读懂。"""
    r = _norm_fields(rule)
    period, offset = r["period"], r["offset"]
    today = (now or datetime.now()).date()

    if period == "all":
        return "全部历史数据"
    if period == "last7":
        return "最近 7 天" if offset == 0 else f"{offset * 7} 天前的 7 天"

    # 本期：期末还没到，天然就是「1 号到今天」，别再写成「整月」骗人
    if offset == 0:
        if period == "day":
            return "今天"
        if period == "week":
            if r["cut"] == "day" and r["cut_day"] < today.weekday() + 1:
                return f"本周周一到周{'一二三四五六日'[min(r['cut_day'], 7) - 1]}"
            return "本周到今天"
        if r["cut"] == "day" and r["cut_day"] < today.day:
            return f"本月 1 号到 {r['cut_day']} 号"
        return "本月 1 号到今天"

    if period == "day":
        if offset == 1:
            return "昨天"
        if offset == 2:
            return "前天"
        return f"{offset} 天前"

    head = {"week": {1: "上周", 2: "上上周"}, "month": {1: "上月", 2: "上上月"}}[period]
    unit = {"week": "周", "month": "个月"}[period]
    prefix = head.get(offset) or f"{offset} {unit}前"

    if r["cut"] == "day":
        if period == "week":
            return f"{prefix}周一到周{'一二三四五六日'[min(r['cut_day'], 7) - 1]}"
        return f"{prefix} 1 号到 {r['cut_day']} 号"
    return f"{prefix}整周" if period == "week" else f"{prefix}整月"


def _fmt_when(r: dict) -> str:
    """「每月 1 号 09:00」这种触发说法（不再规整，避免递归）。"""
    h, m = _hm(r)
    when = f"{h:02d}:{m:02d}"
    freq = str(r.get("freq") or "monthly")
    if freq == "daily":
        return f"每天 {when}"
    if freq == "weekly":
        return f"每{WEEKDAYS[_int(r.get('weekday'), 0, 0, 6)]} {when}"
    return f"每月 {_int(r.get('day'), 1, 1, 31)} 号 {when}"


def describe_rule(rule: dict) -> str:
    """「每月 1 号 09:00」这种触发说法。"""
    return _fmt_when(_norm_fields(rule))


def describe(rule: dict, now: datetime | None = None) -> str:
    """一句话说完：什么时候跑 + 算哪段。"""
    r = normalize_rule(rule)
    return f"{describe_rule(r)} · {range_label(r, now)}（{describe_range(r, now)}）"


# ---------------- 旧配置迁移 ----------------

# 界面上那几个「快捷区间」按钮（上月 / 本月至今 / 近 7 天 / 昨天）
QUICK_RANGES = {
    "last_month": {"period": "month", "offset": 1, "cut": "end"},
    "month": {"period": "month", "offset": 0, "cut": "today"},
    "last7": {"period": "last7", "offset": 0, "cut": "end"},
    "yesterday": {"period": "day", "offset": 1, "cut": "end"},
    "all": {"period": "all", "offset": 0, "cut": "end"},
}


def quick_rule(kind: str) -> dict:
    """把快捷区间（"last_month" 这种）变成一条只有区间、不带定时的规则。

    驾驶舱的总控只传 {kind: "last_month"}，这里转成规则再算日期。
    """
    spec = QUICK_RANGES.get(str(kind or "").strip(), QUICK_RANGES["last_month"])
    return normalize_rule({**spec, "name": "临时"})


def legacy_to_rule(auto_cfg: dict) -> dict | None:
    """把 0.24 之前那份单条 auto 配置翻译成新规则。

    只在设置里还没有 schedules 时用一次，之后以新配置为准。
    """
    cfg = auto_cfg or {}
    if not cfg:
        return None
    rng = str(cfg.get("range") or "last_month")
    mapping = {
        "last_month": {"period": "month", "offset": 1, "cut": "end"},
        "month": {"period": "month", "offset": 0, "cut": "today"},
        "last7": {"period": "last7", "offset": 0, "cut": "end"},
        "yesterday": {"period": "day", "offset": 1, "cut": "end"},
        "all": {"period": "all", "offset": 0, "cut": "end"},
    }
    rule = dict(DEFAULT_RULE)
    rule.update(mapping.get(rng, mapping["last_month"]))
    rule["freq"] = "weekly" if str(cfg.get("freq")) == "weekly" else "daily"
    rule["weekday"] = _int(cfg.get("weekday"), 0, 0, 6)
    rule["time"] = str(cfg.get("time") or "09:00")
    rule["enabled"] = bool(cfg.get("enabled"))
    rule["stores"] = [str(s) for s in (cfg.get("stores") or [])]
    rule["download_dir"] = str(cfg.get("download_dir") or "")
    wf = cfg.get("with_formula")
    rule["with_formula"] = None if wf is None else bool(wf)
    rule["name"] = "原来的自动运行"
    return normalize_rule(rule)


# ---------------- 跑没跑过（防重复触发） ----------------

def state_file() -> Path:
    from src import paths
    return paths.DATA / "schedule_state.json"


def load_state() -> dict:
    from src import safeio
    got = safeio.read_json(state_file(), default={})
    return got if isinstance(got, dict) else {}


def save_state(state: dict) -> None:
    try:
        from src import safeio
        safeio.write_json(state_file(), state)
    except OSError:
        pass
