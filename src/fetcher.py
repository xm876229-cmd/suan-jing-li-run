"""取数规格解释器。

扩展不再写死任何接口——它只负责两件事：
    1. 在页面上下文里把请求发出去（带 Cookie 与风控参数，只有那里有）
    2. 把原始响应原样带回来

「取哪条路径、怎么映射字段、怎么汇总、出什么文件」全部由规格决定，
规格在 config/api_specs.json 里。加新数据源 = 加一段 JSON，不用改扩展。

为什么把解析放在 Python 侧而不是扩展里：
    · 改解析逻辑不用刷新扩展（刷新扩展是调试里最烦的一步）
    · Python 侧的代码可以单测、能直接跑命令行，出问题一眼看得见
    · 扩展越薄越不容易出「只有这台机器能跑」的玄学问题
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import paths

SPECS_FILE = paths.CONFIG / "api_specs.json"

# {{变量}} 占位符
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][\w]*)\s*\}\}")


# ---------------- 规格读取 ----------------

def load_specs(path: Path | None = None) -> dict:
    """读规格文件。读不到就返回空字典（调用方自己报错）。"""
    p = Path(path or SPECS_FILE)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_task_spec(task: str, specs: dict | None = None) -> dict:
    """取某个任务的规格。找不到直接抛错——静默失败最难查。"""
    specs = specs if specs is not None else load_specs()
    t = (specs.get("tasks") or {}).get(task)
    if not t:
        have = "、".join((specs.get("tasks") or {}).keys()) or "（空）"
        raise KeyError(f"api_specs.json 里没有任务「{task}」，已定义：{have}")
    return t


def list_tasks(specs: dict | None = None) -> list[tuple[str, str]]:
    """列出所有已定义的任务：(id, 中文名)。"""
    specs = specs if specs is not None else load_specs()
    out = []
    for k, v in (specs.get("tasks") or {}).items():
        out.append((k, str(v.get("label") or k)))
    return out


# ---------------- 变量 ----------------

def make_vars(range_: dict | None = None, store: str = "") -> dict:
    """把任务区间换算成规格里能用的变量。"""
    r = range_ or {}
    start = str(r.get("startDate") or "")
    end_excl = str(r.get("endDateExclusive") or "")
    end = str(r.get("to") or "")
    if end_excl and not end:
        end = _shift_day(end_excl, -1)
    if start and end and not end_excl:
        end_excl = _shift_day(end, 1)

    tz = int(r.get("tzOffset") or 0) or (-_local_tz_offset())

    def _ts(d: str) -> int:
        if not d:
            return 0
        try:
            # 平台要的是「当地 00:00」对应的 unix 时间戳。
            # datetime 按 UTC 解析出的是 UTC 00:00，东八区就差 8 小时，
            # 抓包对比过：2026-10-01 页面发的是 1790784000（= 09-30T16:00Z），
            # 不是 1790812800（= 10-01T00:00Z）。所以要减掉时区偏移。
            base = int(datetime.strptime(d, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp())
            return base - tz
        except ValueError:
            return 0

    def _compact(d: str) -> str:
        return d.replace("-", "") if d else ""

    return {
        "startDate": start,
        "endDate": end,
        "endDateExclusive": end_excl,
        "startDateCompact": _compact(start),
        "endDateCompact": _compact(end),
        "startTs": _ts(start),
        # 结束用「末日次日 00:00」（开区间），跟页面一致：
        # 10/01~10/21 的 end_time 是 1792598400（= 10-21T16:00Z = 10-22 00:00 当地）
        "endTs": _ts(end_excl) if end_excl else 0,
        "tzOffset": tz,
        "storeId": store or "",
    }


def _local_tz_offset() -> int:
    return -int(datetime.now().astimezone().utcoffset().total_seconds())


def _shift_day(d: str, days: int) -> str:
    """日期加减天数。解析不了就返回空串（调用方会当成「没有区间」）。"""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return ""
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d")


def fill_vars(obj: Any, vars_: dict) -> Any:
    """递归替换字符串里的 {{变量}}。

    只替换「整个字符串就是一个占位符」的情况才保持原类型（比如时间戳要是数字），
    夹在文本中间就当字符串替换。
    """
    if isinstance(obj, dict):
        return {k: fill_vars(v, vars_) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill_vars(v, vars_) for v in obj]
    if isinstance(obj, str):
        whole = _VAR_RE.fullmatch(obj.strip())
        if whole:
            key = whole.group(1)
            if key in vars_:
                return vars_[key]
        return _VAR_RE.sub(lambda m: str(vars_.get(m.group(1), m.group(0))), obj)
    return obj


# ---------------- 解析 ----------------

def take_path(data: Any, path: str) -> Any:
    """按路径取值。

    路径里可以带 *，表示「穿透数组，把所有匹配项摊平成一份」——
    联盟的数据就是这种结构：
        data.product_list_segments.*.product_performances
    （每个 segment 里各有一批商品行，要拍平）

    规则：
      · 路径直接指向数组时，数组原样返回（data.table_v2 → 整个列表）
      · 只有出现 * 时才摊平数组
    """
    if not path:
        return data
    parts = str(path).split(".")

    def walk(node: Any, idx: int) -> list:
        if idx >= len(parts):
            return [node]
        part = parts[idx]
        if part == "*":
            items = node if isinstance(node, list) else [node]
            out: list = []
            for it in items:
                for x in walk(it, idx + 1):
                    # * 的语义就是把数组摊平：结果若是数组就展开一层
                    if isinstance(x, list):
                        out.extend(x)
                    else:
                        out.append(x)
            return out
        if isinstance(node, dict):
            v = node.get(part)
            return [] if v is None else walk(v, idx + 1)
        if isinstance(node, list):
            # 后面还有 * 才穿透，否则数组就是终点
            if any(p == "*" for p in parts[idx + 1:]):
                out = []
                for it in node:
                    out.extend(walk(it, idx + 1))
                return out
            return [node]
        return []

    res = walk(data, 0)
    if not res:
        return None
    if len(res) == 1:
        return res[0]
    return res


def normalize_rows(raw: Any, layout: str = "objects") -> list[dict]:
    """把响应里的行统一成「每行一个 dict」。

    columnar：[[列名…], [行…], …] —— 第一行是列名
    objects ：[{列名: 值}, …]
    cells   ：[[{data, name}, …], …] —— 每个格子自带字段名，而且**每行列顺序随机**，
              只能按 name 认，不能按位置认。广告接口 post_product_list_v2 就是这种。
    """
    if not isinstance(raw, list) or not raw:
        return []

    if layout == "cells":
        out = []
        for r in raw:
            if not isinstance(r, (list, tuple)):
                continue
            row: dict[str, Any] = {}
            for cell in r:
                if isinstance(cell, dict) and "name" in cell:
                    row[str(cell["name"])] = cell.get("data")
            if row:
                out.append(row)
        return out

    if layout == "columnar" or isinstance(raw[0], (list, tuple)):
        head = [str(h) for h in raw[0]]
        out = []
        for r in raw[1:]:
            if not isinstance(r, (list, tuple)):
                continue
            out.append({head[i]: r[i] for i in range(min(len(head), len(r)))})
        return out
    return [r for r in raw if isinstance(r, dict)]


def _get_field(row: dict, path: str) -> Any:
    """按点路径取字段：'product_base.id' 或 'product_id'。"""
    cur: Any = row
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _to_num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def map_fields(rows: list[dict], fields: dict) -> list[dict]:
    """按规格把源列映射成逻辑字段。

    fields: {逻辑字段名: [源列名候选, …]}，按顺序取第一个取得到的。
    候选支持点路径（'product_base.id'）。
    """
    out = []
    for row in rows:
        o: dict = {}
        for logical, cands in (fields or {}).items():
            val = None
            for c in cands:
                val = _get_field(row, c)
                if val is not None and val != "":
                    break
                val = None
            o[logical] = val
        out.append(o)
    return out


def aggregate(rows: list[dict], group_by: list[str] | None,
              sum_fields: list[str] | None) -> list[dict]:
    """分组汇总：group_by 相同的行合成一行，sum_fields 求和，其余取第一个非空。"""
    group_by = group_by or []
    sum_fields = sum_fields or []
    if not group_by:
        return rows

    buckets: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows:
        key = tuple("" if r.get(g) is None else str(r.get(g)) for g in group_by)
        if key not in buckets:
            buckets[key] = {g: r.get(g) for g in group_by}
            for s in sum_fields:
                buckets[key][s] = 0.0
            order.append(key)
        b = buckets[key]
        for s in sum_fields:
            n = _to_num(r.get(s))
            if n is not None:
                b[s] = (b.get(s) or 0.0) + n
        for k, v in r.items():
            if k in group_by or k in sum_fields:
                continue
            if (b.get(k) in (None, "")) and v not in (None, ""):
                b[k] = v
    return [buckets[k] for k in order]


# ---------------- 出文件 ----------------

def build_csv(rows: list[dict], output: dict) -> str:
    """按 output.header / output.columns 生成 CSV（带 BOM，Excel 打开不乱码）。"""
    header = list(output.get("header") or [])
    cols = list(output.get("columns") or [])
    lines = ["\uFEFF" + ",".join(header)]

    def esc(v: Any) -> str:
        s = "" if v is None else str(v)
        if any(ch in s for ch in ',"\r\n'):
            s = '"' + s.replace('"', '""') + '"'
        return s

    for r in rows:
        lines.append(",".join(esc(r.get(c)) for c in cols))
    return "\r\n".join(lines) + "\r\n"


def write_result(name: str, content: str, out_dir: Path | None = None) -> Path:
    """把结果落到 inbox。"""
    d = Path(out_dir or paths.INBOX)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    p = d / safe
    if p.exists():
        stem, suf = p.stem, p.suffix
        i = 2
        while p.exists():
            p = d / f"{stem} ({i}){suf}"
            i += 1
    # build_csv 已经在首行写了 BOM；再按 utf-8-sig 写一遍就是两个 BOM，
    # Excel 打开表头会变成 "﻿﻿Product ID"，程序按表头认表也会认不出。
    p.write_text(content.lstrip("\ufeff"), encoding="utf-8-sig")
    return p


# ---------------- 主流程 ----------------

def _dump_resp(task: str, idx: int, resp) -> None:
    """把这一步的原始响应留一份，方便排查解析问题。

    广告 / 联盟这两个接口平台经常改字段，结果可疑时（0 行、只解析出 1 行）
    留个样本，下次排查不用再让用户重跑一遍。固定文件名覆盖写，不会越积越多。
    """
    try:
        d = paths.DATA / "capture"
        d.mkdir(parents=True, exist_ok=True)
        txt = json.dumps(resp, ensure_ascii=False)
        (d / f"_last_{task}_step{idx}.json").write_text(txt[:400_000], encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def process_results(task: str, results: list[dict], range_: dict | None = None,
                    store: str = "", specs: dict | None = None,
                    out_dir: Path | None = None) -> dict:
    """把扩展带回来的原始响应，按规格解析成文件。

    results: [{path, method, request_body, response, error}, …]
    返回 {ok, files: [...], issues: [...]}
    """
    spec = get_task_spec(task, specs)
    steps = spec.get("steps") or []
    vars_ = make_vars(range_, store)

    # 扩展是按步骤顺序发请求的，这里按顺序对齐
    by_step: dict[str, list[dict]] = {}
    for i, r in enumerate(results or []):
        key = str(r.get("path") or "")
        by_step.setdefault(key, []).append(r)

    files: list[str] = []
    issues: list[str] = []
    used = set()

    for idx, step in enumerate(steps):
        path = step.get("path") or ""
        cand = by_step.get(path) or []
        # 同路径多步（翻页）时按出现顺序配对
        hit = None
        for r in cand:
            if id(r) not in used:
                hit = r
                used.add(id(r))
                break
        if hit is None:
            issues.append(f"步骤「{step.get('name') or path}」没有拿到响应")
            continue
        if hit.get("error"):
            issues.append(f"步骤「{step.get('name') or path}」请求失败：{hit['error']}")
            continue

        resp = hit.get("response")
        raw = take_path(resp, step.get("take") or "")
        if raw is None:
            # 取不到路径时也留样本——多半是平台把结构改了，
            # 没样本只能靠猜，有样本一眼就能看出该取哪。
            _dump_resp(task, idx, resp)
            issues.append(
                f"步骤「{step.get('name') or path}」响应里找不到 {step.get('take')}"
                f"（顶层键：{list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__}）"
            )
            continue

        rows = normalize_rows(raw, step.get("layout") or "objects")
        if not rows:
            _dump_resp(task, idx, resp)
            issues.append(f"步骤「{step.get('name') or path}」取到 0 行")
            continue
        objs = map_fields(rows, step.get("fields") or {})
        # 规格里可以补固定列：广告接口不返回币种，但它给的就是本地货币，
        # 不补这一列的话下游会按「空=美元」再乘一次汇率，金额直接翻几倍。
        const_cols = step.get("set") or {}
        if const_cols:
            for o in objs:
                for k, v in const_cols.items():
                    if not o.get(k):
                        o[k] = v
        agg = aggregate(objs, step.get("group_by"), step.get("sum"))
        # 广告/佣金这两步的接口经常改，解析结果可疑时留一份原始响应，
        # 下次排查不用再让用户重跑一遍（覆盖写，不会越积越多）。
        if task in ("ads", "affiliate") and len(agg) <= 1:
            _dump_resp(task, idx, resp)
        csv_text = build_csv(agg, step.get("output") or {})

        out = step.get("output") or {}
        fname = fill_vars(str(out.get("file") or f"{task}_{idx}.csv"), vars_)
        p = write_result(fname, csv_text, out_dir)
        files.append(str(p))
        issues.append(f"步骤「{step.get('name') or path}」→ {len(agg)} 行，{p.name}")

    return {"ok": bool(files), "files": files, "issues": issues, "rows": len(objs) if files else 0}
