"""自动运行：到点自动抓数据 → 核算 → 出报表。

为什么由桥派任务，而不是扩展自己定时
------------------------------------
扩展的定时器只在「浏览器开着、扩展刚被刷新过」时才可靠；
浏览器一关、扩展一重载，定时就哑了。
本地桥是常驻后台进程，浏览器关着它也活着，所以定时交给桥。

桥这边只做两件事：
  1. 到点了，往待办队列里塞一个任务（扩展会来领）
  2. 剩下的取数、核算、出报表，扩展那边照常跑

自动下载
--------
报表算完后会复制一份到设定目录（见 settings.copy_to_download_dir），
带不带公式也跟着设置走。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

# 区间选项 → 中文名（界面上用）
RANGE_LABELS = {
    "last_month": "上个月",
    "month": "本月到今天",
    "last7": "最近 7 天",
    "yesterday": "昨天",
    "all": "全部数据",
}


def should_run(cfg: dict, now: datetime) -> bool:
    """到点了没有。

    只判断「是不是这一分钟」，不判断「今天是不是已经跑过」——
    那是调用方的责任（要记住上次跑的时间，免得同一分钟重复触发）。
    """
    if not (cfg or {}).get("enabled"):
        return False
    want = str(cfg.get("time") or "09:00").strip()
    if now.strftime("%H:%M") != want:
        return False
    freq = str(cfg.get("freq") or "daily").strip()
    if freq == "weekly":
        # 星期几：0=周一 … 6=周日（跟界面上的排法一致）
        if now.weekday() != int(cfg.get("weekday") or 0):
            return False
    return True


def build_range(kind: str, now: datetime | None = None) -> dict:
    """按区间选项算出取数用的起止日期。

    返回 {"startDate": "2026-10-01", "to": "2026-10-31"}；
    全量返回空 dict（不传日期 = 全量）。
    """
    now = now or datetime.now()
    k = str(kind or "last_month").strip()
    if k == "all":
        return {}
    if k == "yesterday":
        d = now - timedelta(days=1)
        return {"startDate": d.strftime("%Y-%m-%d"), "to": d.strftime("%Y-%m-%d")}
    if k == "last7":
        end = now - timedelta(days=1)
        start = end - timedelta(days=6)
        return {"startDate": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")}
    if k == "month":
        start = now.replace(day=1)
        end = now
        return {"startDate": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")}
    # 默认：上个月
    first = now.replace(day=1) - timedelta(days=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    start = first.replace(day=1)
    end = first.replace(day=last_day)
    return {"startDate": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")}


def describe_range(kind: str, now: datetime | None = None) -> str:
    """区间选项的中文描述，给日志看。"""
    r = build_range(kind, now)
    if not r:
        return "全部数据"
    return f"{r.get('startDate')} ~ {r.get('to')}"
