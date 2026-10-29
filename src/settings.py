"""应用设置。

存在 config/settings.json，界面上能改，改完即时生效。

包含：
  · 报表文件名模板（可以用店铺名、日期区间等变量）
  · 核算完自动把报表复制到一个固定位置（比如 D:\\净利报表）
  · 汇率取法
"""

from __future__ import annotations

import json
import re
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
        # 导出的报表带不带 Excel 公式。
        # 带公式：明细页有辅助列，汇总页用 SUMIFS，改「口径」页的开关就能在 Excel 里切口径
        "with_formula": False,
    },
    "fx": {
        "mode": "auto",
    },
    # 自动运行：到点自动抓数据 → 核算 → 出报表
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
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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
    return out


def save(patch: dict) -> dict:
    """合并保存。只传想改的部分就行，其余保留。"""
    cur = _doc()
    if not cur:
        cur = json.loads(json.dumps(DEFAULTS))
    for section, values in (patch or {}).items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict) and isinstance(cur.get(section), dict):
            cur[section].update(values)
        else:
            cur[section] = values
    SETTINGS_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
        if target.exists():
            stem, suf = target.stem, target.suffix
            i = 2
            while target.exists():
                target = d / f"{stem} ({i}){suf}"
                i += 1
        target.write_bytes(src.read_bytes())
        return target
    except OSError:
        return None
