"""数据凭证：每份数据「是谁的、盖的哪一段」，以及出表前的硬闸门。

为什么要有这一层
================
这个项目反复出的错，全都是**同一类错**：报表照常生成、每列都有数，
但底下的数据不是这一家店的、或不是这一个月的。几个真实案例：

  · PHT-W1 收件箱里只有 8 月的广告表，算 7 月时被直接拿去用，
    广告费 228,601 一分不差，月份却是错的；
  · 收件箱里混着一份读不出区间的老文件时，`_pick_latest` 曾经
    「分不清就不丢」→ 把全部文件读进来 → 几个月的数据相加；
  · 广告表被判定为「区间不覆盖」丢掉了，但 `sources` 里仍然写着
    「广告 = xxx.xlsx」，缺表闸门看的是 `sources`，于是放行，
    广告费按 0 计，净利虚高。

这些错都不是「某一行算错」，而是**来源不对**。所以修法也不能是再补一个 if：
只要判据还散在各个函数里（sources / ranges / issues 三套），就一定会再漏一处。

这里改成一条唯一判据：
    1. 文件落盘时写下「身份证」（shop_id + 导出区间 + 类型 + 时间）；
    2. 读数据时以身份证为准，没有身份证才退回文件名；
    3. 出表前只看一件事——**这次真正参与计算的数据，区间盖不盖得住报表区间**。
       盖不住就一张表都不出，并把「目录里有什么、缺什么」原样写出来。

「有文件」不等于「有数据」，这是这一层存在的全部意义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 身份证跟着文件走，文件名就是「原文件名 + 这个后缀」。
# 注意必须让扫描器跳过它（否则会被当成映射表 JSON 去解析）。
META_SUFFIX = ".srcmeta.json"

# 一行数据都没有、但目录里有文件时的状态
ST_OK = "ok"                  # 用了，区间对得上
ST_MISSING = "missing"        # 目录里压根没有这类表
ST_OUT_OF_RANGE = "oor"       # 有文件，但都不覆盖报表区间（这次一份没用）
ST_UNREADABLE = "unread"      # 有文件，但读不出区间（老文件）
ST_EMPTY = "empty"            # 文件认出来了，但解析出 0 行（列标题变了之类）
ST_DECLARED = "declared"      # 该店声明「本店没有这类数据」，如不投广告


@dataclass
class TableUse:
    """一类数据这次实际用了什么。"""

    kind: str                       # order / ads / affiliate / mapping
    label: str = ""                 # 中文名，给人看
    status: str = ST_MISSING
    files: list[str] = field(default_factory=list)   # 真正参与计算的文件名
    range: Optional[tuple[str, str]] = None          # 这些文件合起来的导出区间
    rows: int = 0                                    # 参与计算的数据行数
    note: str = ""
    # 硬拦的理由（比如广告费里有一块币种认不出、被按 0 计了）。
    # 这类问题不会让报表缺列，只会让数字偏——所以必须拦住，不能只提示。
    blocker: str = ""
    # 目录里看过、但没用的（文件名 + 原因），出表失败时原样展示
    rejected: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in (ST_OK, ST_DECLARED)

    def describe(self) -> str:
        rng = f"{self.range[0]}~{self.range[1]}" if self.range else "无区间"
        files = "、".join(self.files[:3]) or "无"
        return f"{self.label}：{files}（{rng}，{self.rows} 行）"


def meta_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + META_SUFFIX)


def read_meta(path: Path) -> dict:
    """读身份证。没有 / 坏了都返回空 dict——老文件本来就没有。"""
    mp = meta_path(path)
    try:
        if not mp.is_file():
            return {}
        data = json.loads(mp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_meta(path: Path, *, shop_id: str = "", kind: str = "",
               rng: Optional[tuple[str, str]] = None,
               at: str = "", source: str = "") -> bool:
    """给刚落盘的文件写身份证。

    写失败不能影响主流程（数据还是能用，只是少了防串店的依据），
    所以这里 swallow 掉异常，返回是否写成。
    """
    payload = {
        "shop_id": str(shop_id or ""),
        "kind": str(kind or ""),
        "range": list(rng) if rng else None,
        "at": at or "",
        "source": source or "",
        "name": Path(path).name,
    }
    try:
        from src import safeio
        safeio.write_json(meta_path(path), payload, backup=False)
        return True
    except Exception:  # noqa: BLE001
        try:
            meta_path(path).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------- 闸门 ----------------

# 一个「没数据也能出表」的显式声明：店铺自己说「本店不投广告 / 没有联盟」。
# 只认这两种，别的缺口一律拦住——缺了就是数字不对，不是玄学。
DECLARABLE = ("ads", "affiliate")


def gate(uses: dict[str, TableUse], window: Optional[tuple[str, str]],
         declared: set[str] | None = None) -> tuple[list[str], list[str]]:
    """出表前的唯一判据。

    返回 (拦下来的理由, 提示)。理由非空 = **不许出表**。

    declared 是该店显式声明「本来就没有」的类别（见 stores.json 的
    `no_ads` / `no_affiliate`，或 meta.json 同名键）。不作声明时，
    广告表缺席一律拦——因为「缺广告表」和「广告费为 0」在报表上
    长得一模一样，而后者会让净利凭空变高。
    """
    declared = declared or set()
    blockers: list[str] = []
    notes: list[str] = []

    for kind in ("order", "ads", "affiliate", "mapping"):
        u = uses.get(kind)
        if u is None:
            continue
        if u.blocker:
            blockers.append(u.blocker)
            continue
        if kind in declared and u.status in (ST_MISSING, ST_OUT_OF_RANGE, ST_EMPTY):
            u.status = ST_DECLARED
            notes.append(f"{u.label}：已声明本店没有，按 0 计")
            continue
        if u.status == ST_DECLARED:
            notes.append(f"{u.label}：已声明本店没有，按 0 计")
            continue
        if u.status == ST_OK:
            # 映射表（SKU→商品ID）跟时间无关，只要求「有」；广告/佣金/订单
            # 是整段汇总或按区间导的，盖不住就是拿别的月份在算，必须拦。
            if window and u.range and kind != "mapping":
                lo, hi = window
                if u.range[0] > lo or u.range[1] < hi:
                    # 盖不住还用了：广告/佣金是整段汇总，直接用必然串月
                    blockers.append(
                        f"{u.label}：导出的区间是 {u.range[0]}~{u.range[1]}，"
                        f"报表要算 {lo}~{hi}，对不上")
            continue

        # 没数据的情况，把「为什么」说清楚，别只写「缺」
        why = {
            ST_MISSING: "目录里没有这类表",
            ST_OUT_OF_RANGE: "目录里有，但都不覆盖这次报表区间",
            ST_UNREADABLE: "目录里有，但读不出导出区间（文件名里没有日期）",
            ST_EMPTY: "目录里有，但解析出 0 行（列标题可能变了）",
        }.get(u.status, u.status)
        # note 是 loader 补的具体原因（缺哪几天 / 哪两份区间重叠），
        # 比「都不覆盖」有用得多——按周/按日取数的店一眼就知道该补哪份。
        if u.note:
            why = f"{why}（{u.note}）"
        detail = f"；现有：{'、'.join(u.rejected[:3])}" if u.rejected else ""
        hint = ""
        if kind == "ads":
            hint = ("。若这家店确实不投广告，在 config/stores.json 该店加 "
                    "\"no_ads\": true 后就不会再拦")
        elif kind == "affiliate":
            hint = ("。若这家店确实没有联盟佣金，在 config/stores.json 该店加 "
                    "\"no_affiliate\": true 后就不会再拦")
        blockers.append(f"{u.label}：{why}{detail}{hint}")

    return blockers, notes


def summarize(uses: dict[str, TableUse]) -> dict[str, str]:
    """给报表「本次运行」那一段用的几行说明。"""
    out: dict[str, str] = {}
    for kind, label in (("order", "订单"), ("ads", "广告"), ("affiliate", "联盟佣金")):
        u = uses.get(kind)
        if not u:
            continue
        if u.status == ST_DECLARED:
            out[f"{label}数据"] = "本店无此数据（已在店铺设置里声明）"
            continue
        if not u.usable:
            out[f"{label}数据"] = "缺（这次没有出表）"
            continue
        rng = f"{u.range[0]}~{u.range[1]}" if u.range else "无区间（老文件）"
        out[f"{label}数据"] = f"{'、'.join(u.files[:2])}（{rng}，{u.rows} 行）"
    return out
