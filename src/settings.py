"""应用设置。

存在 config/settings.json，界面上能改，改完即时生效。

包含：
  · 报表文件名模板（可以用店铺名、日期区间等变量）
  · 核算完自动把报表复制到一个固定位置（比如 D:\\净利报表）
  · 汇率取法
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from src import paths

SETTINGS_FILE = paths.CONFIG / "settings.json"

DEFAULTS: dict = {
    "report": {
        # 文件名模板。可用变量：
        #   {店铺} {店铺ID} {国家} {开始} {结束} {生成时间} {区间}
        # 也认英文 {store} {shop_id} {country} {from} {to} {date} {range}
        "name_template": "{店铺}_{区间}",
        # 核算完自动复制一份到 download_dir
        "auto_copy": False,
        "download_dir": "",
        # 手动核算的报表放哪儿（跟自动报表分开记：两边常放不同文件夹）
        "manual_download_dir": "",
        # 「下载导入模板」存到哪儿（空 = 存到程序里的「报表」目录）
        "template_dir": "",
        # 导出的报表带不带 Excel 公式。
        # 带公式：明细页有辅助列，汇总页用 SUMIFS，改「口径」页的开关就能在 Excel 里切口径
        "with_formula": False,
    },
    "fx": {
        "mode": "auto",
    },
    # 自动运行：到点自动抓数据 → 核算 → 出报表
    # （0.24 起改成「多条计划任务」，这份旧配置只用来一次性迁移）
    "auto": {
        "enabled": False,
        # daily 每天 / weekly 每周某天
        "freq": "daily",
        "time": "09:00",
        # 星期几（0=周一 … 6=周日），freq=weekly 时用
        "weekday": 0,
        # 算哪个区间：last_month 上个月 / month 本月到今天 /
        #            yesterday 昨天 / last7 近 7 天 / all 全量
        "range": "last_month",
        # 只对某几家店跑（空 = 全部）
        "stores": [],
        # 报表下载到哪儿（空 = 不额外下载，只在「报表」目录里）
        "download_dir": "",
        # 报表带不带公式（跟随「报表」页的同名设置）
        "with_formula": False,
    },
    # 计划任务：可以配多条，每条各自定时、各自选店铺和区间。
    # 结构见 src/auto_run.py 的 DEFAULT_RULE。
    "schedules": [],
}

# 模板变量别名 → 统一取值用的键
_ALIASES = {
    "店铺": "store", "store": "store", "shop": "store",
    "店铺id": "shop_id", "shop_id": "shop_id", "id": "shop_id",
    "店铺编码": "shop_code", "shop_code": "shop_code",
    "国家": "country", "country": "country", "region": "country",
    "开始": "from", "from": "from", "start": "from",
    "结束": "to", "to": "to", "end": "to",
    "区间": "range", "range": "range",
    "生成时间": "now", "now": "now", "date": "now",
}


def _doc() -> dict:
    from src import safeio
    got = safeio.read_json(SETTINGS_FILE, default={})
    return got if isinstance(got, dict) else {}


def load() -> dict:
    """读设置，缺的用默认值补上（老配置文件也能用）。"""
    raw = _doc()
    out = json.loads(json.dumps(DEFAULTS))
    for section, values in raw.items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict):
            out.setdefault(section, {}).update(values)
        else:
            out[section] = values

    # 老版本只有一条「自动运行」，新版本改成了计划任务列表。
    # 这里在内存里翻译成一条规则给界面看；用户点了保存才真正落盘，
    # 不保存就一直按旧配置跑（旧配置也还在文件里，随时能退回去）。
    if not out.get("schedules"):
        try:
            from src import auto_run
            legacy = auto_run.legacy_to_rule(out.get("auto") or {})
            if legacy and (out.get("auto") or {}).get("enabled"):
                out["schedules"] = [legacy]
        except Exception:  # noqa: BLE001
            pass
    return out


_MISSING = object()


def save(patch: dict) -> dict:
    """合并保存。只传想改的部分就行，其余保留。

    **只认 DEFAULTS 里有的分节和字段。** 以前是「传什么就写什么」，
    实测一个杂字段的请求就能把 `a`、`id`、`rule`、`stores` 这些键
    永久写进 settings.json，越堆越多；更糟的是 `stores` 这种键
    本来是列表，被写成字符串后，读配置的地方会拿到意外类型。
    配置是长期存在的文件，宁可丢掉一个不认识的字段，也不要往里存垃圾。
    """
    cur = _doc()
    if not cur:
        cur = json.loads(json.dumps(DEFAULTS))
    for section, values in (patch or {}).items():
        if section.startswith("_"):
            continue
        default = DEFAULTS.get(section, _MISSING)
        if default is _MISSING:
            continue                      # 不认识的分节：不存
        if isinstance(default, dict):
            if not isinstance(values, dict):
                continue                  # 类型不对：不存
            cur.setdefault(section, {}).update(
                {k: v for k, v in values.items() if k in default})
        else:
            if type(values) is not type(default):
                continue                  # 比如 schedules 必须是列表
            cur[section] = values
    try:
        # 原子写：断电/强杀也不会留下半截 JSON（见 src/safeio.py）
        from src import safeio
        safeio.write_json(SETTINGS_FILE, cur)
    except OSError as e:
        return {"ok": False, "error": f"保存失败（磁盘满或被占用？）：{e}"}
    return {"ok": True, "settings": load()}


def _safe_name(s: str) -> str:
    """文件名里不能出现的字符换成下划线，顺便去掉多余空白。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:80] or "未命名"


def render_report_name(
    template: str,
    *,
    store: str = "",
    shop_id: str = "",
    shop_code: str = "",
    country: str = "",
    dfrom: str = "",
    dto: str = "",
) -> str:
    """按模板生成报表文件名（不含扩展名）。

    模板长这样：{店铺}_{开始}_{结束}
    认不出的变量原样保留，方便用户看出自己写错了哪个。
    """
    season = f"{dfrom or '全部'}_{dto or '全部'}" if (dfrom or dto) else "全部数据"
    values = {
        "store": store or shop_id or "未识别店铺",
        "shop_id": shop_id,
        "shop_code": shop_code,
        "country": country,
        "from": dfrom or "全部",
        "to": dto or "全部",
        "range": season,
        "now": datetime.now().strftime("%Y%m%d_%H%M"),
    }

    def repl(m: re.Match) -> str:
        raw = m.group(1).strip()
        key = _ALIASES.get(raw.lower()) or _ALIASES.get(raw)
        if not key:
            return m.group(0)          # 不认识的变量原样留着
        return str(values.get(key, ""))

    out = re.sub(r"\{([^{}]+)\}", repl, str(template or ""))
    return _safe_name(out)


def copy_to_download_dir(src: Path, dest_dir: str) -> Path | None:
    """把报表复制到用户指定的位置。目标为空或失败就返回 None（不影响主流程）。"""
    if not dest_dir:
        return None
    try:
        d = Path(dest_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        target = d / src.name
        data = src.read_bytes()

        def _write(dst: Path) -> None:
            """先写唯一的临时名再原子换名。

            临时名带 pid + 纳秒：两家店同时算完、模板又没带店名时，
            两个进程会往同一个目标写；共用一个固定 .tmp 名会互相截断，
            换名后拿到的可能是对方写了一半的内容。
            """
            tmp = d / f".{dst.name}.{os.getpid()}-{time.time_ns()}.tmp"
            try:
                tmp.write_bytes(data)
                os.replace(tmp, dst)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

        if target.exists():
            # 同名 = 同一份报表重算了一次（区间、店名都没变）：
            # 直接覆盖，别在下载目录里堆 (2)(3)(4)——用户找的是「最新那版」。
            try:
                _write(target)
                return target
            except OSError:
                # 目标正被 Excel 打开：退回另存一份，至少这次下载不算失败
                stem, suf = target.stem, target.suffix
                i = 2
                while target.exists():
                    target = d / f"{stem} ({i}){suf}"
                    i += 1
        _write(target)
        return target
    except OSError:
        return None


def _safe_name(s: str) -> str:
    """文件名里不能出现的字符换成下划线，顺便去掉多余空白。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:80] or "未命名"


def render_report_name(
    template: str,
    *,
    store: str = "",
    shop_id: str = "",
    shop_code: str = "",
    country: str = "",
    dfrom: str = "",
    dto: str = "",
) -> str:
    """按模板生成报表文件名（不含扩展名）。

    模板长这样：{店铺}_{开始}_{结束}
    认不出的变量原样保留，方便用户看出自己写错了哪个。
    """
    season = f"{dfrom or '全部'}_{dto or '全部'}" if (dfrom or dto) else "全部数据"
    values = {
        "store": store or shop_id or "未识别店铺",
        "shop_id": shop_id,
        "shop_code": shop_code,
        "country": country,
        "from": dfrom or "全部",
        "to": dto or "全部",
        "range": season,
        "now": datetime.now().strftime("%Y%m%d_%H%M"),
    }

    def repl(m: re.Match) -> str:
        raw = m.group(1).strip()
        key = _ALIASES.get(raw.lower()) or _ALIASES.get(raw)
        if not key:
            return m.group(0)          # 不认识的变量原样留着
        return str(values.get(key, ""))

    out = re.sub(r"\{([^{}]+)\}", repl, str(template or ""))
    return _safe_name(out)


def copy_to_download_dir(src: Path, dest_dir: str) -> Path | None:
    """把报表复制到用户指定的位置。目标为空或失败就返回 None（不影响主流程）。"""
    if not dest_dir:
        return None
    try:
        d = Path(dest_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        target = d / src.name
        if target.exists():
            # 同名 = 同一份报表重算了一次（区间、店名都没变）：
            # 直接覆盖，别在下载目录里堆 (2)(3)(4)——用户找的是「最新那版」。
            # 先写临时名再换，避免写到一半断电留下半截文件。
            try:
                tmp = d / f".{target.name}.tmp"
                tmp.write_bytes(src.read_bytes())
                os.replace(tmp, target)
                return target
            except OSError:
                # 目标正被 Excel 打开：退回另存一份，至少这次下载不算失败
                try:
                    tmp.unlink()
                except OSError:
                    pass
                stem, suf = target.stem, target.suffix
                i = 2
                while target.exists():
                    target = d / f"{stem} ({i}){suf}"
                    i += 1
        target.write_bytes(src.read_bytes())
        return target
    except OSError:
        return None
