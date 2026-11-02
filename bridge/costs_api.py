"""成本表接口：商品编码 → 成本单价（人民币）。

这是「商品成本 = 件数 × 该编码的成本」里那个映射表。
它变动不频繁（只在新增商品时补一条），所以设计成：

    · 能在界面里直接改单价、加一行、删一行
    · 也能整份 Excel 导进来，按编码合并（老编码不丢，新编码追加）
    · 还能告诉你哪些编码订单里有、成本表里没有——那才是真正要补的

数据落在 data/costs/master.csv，格式：code,cost,effective_from,updated_at
"""

from __future__ import annotations

import base64
import csv
import io
from collections import Counter
from datetime import datetime
from pathlib import Path

from src import paths

MASTER = paths.COSTS / "master.csv"
FIELDS = ["code", "cost", "effective_from", "updated_at"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_master() -> dict[str, dict]:
    if not MASTER.exists():
        return {}
    out: dict[str, dict] = {}
    with MASTER.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            try:
                cost = float(row.get("cost") or 0)
            except ValueError:
                continue
            out[code] = {
                "code": code,
                "cost": cost,
                "effective_from": row.get("effective_from") or "",
                "updated_at": row.get("updated_at") or "",
            }
    return out


def write_master(data: dict[str, dict]) -> None:
    paths.COSTS.mkdir(parents=True, exist_ok=True)
    tmp = MASTER.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for code in sorted(data):
            w.writerow({k: data[code].get(k, "") for k in FIELDS})
    tmp.replace(MASTER)


def read_costs(limit: int = 0) -> dict:
    """列出成本表。limit>0 时只回前 N 条（表格很大时先给个预览）。"""
    data = read_master()
    items = sorted(data.values(), key=lambda x: x["code"])
    total = len(items)
    if limit and total > limit:
        items = items[:limit]
    return {
        "ok": True,
        "count": total,
        "shown": len(items),
        "items": items,
        "path": str(MASTER),
    }


def save_costs(items: list[dict]) -> dict:
    """整表保存（界面里改完点保存时用）。

    以传进来的列表为准；但**空编码跳过**，避免一次误操作把整张表清空。
    """
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "没有可保存的内容"}
    old = read_master()
    now = _now()
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        try:
            cost = float(it.get("cost"))
        except (TypeError, ValueError):
            continue
        prev = old.get(code, {})
        out[code] = {
            "code": code,
            "cost": cost,
            "effective_from": prev.get("effective_from") or datetime.now().strftime("%Y-%m-%d"),
            # 价格没变就别刷新时间，否则「这条什么时候改的」全变成今天
            "updated_at": prev.get("updated_at") if prev.get("cost") == cost else now,
        }
    write_master(out)
    return {"ok": True, "count": len(out), "removed": max(0, len(old) - len(out))}


def import_costs(name: str, b64: str, mode: str = "merge") -> dict:
    """导入一份成本表（xlsx / csv）并按编码合并。

    mode=merge（默认）：新编码追加，已有编码价格变了才更新，没出现的编码保留。
    mode=replace：以这份文件为准，文件里没有的编码删掉（谨慎）。
    """
    from src import formula
    from src.loader import parse_costs, read_table

    if not name or not b64:
        return {"ok": False, "error": "缺少文件名或内容"}
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return {"ok": False, "error": "文件内容解码失败"}

    suffix = Path(name).suffix.lower() or ".xlsx"
    tmp = paths.COSTS / f"_import_{datetime.now():%Y%m%d%H%M%S}{suffix}"
    paths.COSTS.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)
    try:
        rows = read_table(tmp, sheet="成本") if suffix in (".xlsx", ".xls") else read_table(tmp)
        # 用界面上配的「哪一列是编码 / 哪一列是成本」去认表头
        cf = (formula.load_config(paths.CONFIG / "formulas.json").get("cost_fields") or {})
        parsed = parse_costs(rows, cf)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读不懂这个文件：{e}"}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    if not parsed:
        return {"ok": False, "error": "表里没解析出「编码 + 成本」两列，检查一下表头"}

    old = read_master() if mode != "replace" else {}
    now = _now()
    today = datetime.now().strftime("%Y-%m-%d")
    added = updated = same = 0
    out = dict(old)
    for code, cost in parsed.items():
        prev = out.get(code)
        if prev is None:
            out[code] = {"code": code, "cost": cost,
                         "effective_from": today, "updated_at": now}
            added += 1
        elif abs(float(prev.get("cost") or 0) - cost) > 1e-9:
            prev["cost"] = cost
            prev["updated_at"] = now
            updated += 1
        else:
            same += 1
    write_master(out)
    return {"ok": True, "added": added, "updated": updated,
            "unchanged": same, "total": len(out), "file": name}


def make_template() -> bytes:
    """生成一份成本表导入模板（xlsx 字节）。

    为什么要有模板：以前只说「导入 Excel / CSV」，没告诉人表格该长什么样，
    于是每个人导出来的表都不一样，认列全靠猜。
    模板把两列定死：第一列商品编码、第二列成本（人民币），照着填就行。
    """
    import io

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "成本"
    head = ["商品编码", "成本（人民币）"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8F3EE")
        c.alignment = Alignment(horizontal="center")
    # 两行示例，够看懂格式就行
    ws.append(["MY-J68-22cmOilspillnet", 3.63])
    ws.append(["V1-100*120", 2.5])
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18
    ws["B2"].number_format = "0.00"
    ws["B3"].number_format = "0.00"

    doc = wb.create_sheet("说明")
    doc.column_dimensions["A"].width = 110
    lines = [
        ("成本表怎么填", True),
        ("", False),
        ("1. 第一列写「商品编码」，要和订单表里的「卖家编码 / Seller SKU」一模一样，一个字都不能差。", False),
        ("   编码对不上，这行就认不出来，成本会按 0 计，报表「异常」页会列出来。", False),
        ("2. 第二列写「单个成本」，单位是人民币。", False),
        ("   买 3 件不是写总价 —— 数量在订单表里，程序自己乘。", False),
        ("3. 表里不要有合并单元格、空行、合计行；多余的行会被忽略。", False),
        ("4. 同一个编码出现多次，以最后一行为准。", False),
        ("5. 导入是增量的：", True),
        ("   · 表里没有的旧编码 → 保留不动", False),
        ("   · 表里新出现的编码 → 新增", False),
        ("   · 表里已有、这次又给了的编码 → 以这次导入的为准（覆盖旧成本）", False),
        ("", False),
        ("导入完，报表「异常」页会列出还没成本的编码，可以照着补。", False),
    ]
    for i, (text, bold) in enumerate(lines, 1):
        c = doc.cell(row=i, column=1, value=text)
        if bold:
            c.font = Font(bold=True)
    doc.row_dimensions[1].height = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def check_costs(sample: int = 20) -> dict:
    """拿当前订单里的编码去对一遍成本表：哪些没成本、哪些是推导出来的。

    没有这个，缺成本的编码只能靠翻报表「异常」页一条条找。
    """
    from src.loader import load_from_dir, resolve_cost

    inbox = _latest_inbox()
    if inbox is None:
        return {"ok": False, "error": "还没有订单数据，先跑一次取数"}

    mapping = paths.MAPPING / "master.csv"
    try:
        data = load_from_dir(inbox, MASTER if MASTER.exists() else None, 0.61,
                             mapping if mapping.exists() else None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取订单失败：{e}"}

    missing: Counter = Counter()
    derived: Counter = Counter()
    how_label = {"multipack": "按多件装推导", "combo": "按组合装相加"}
    for l in data.orders:
        unit, how = resolve_cost(l.seller_sku, data.costs)
        if unit is None:
            missing[l.seller_sku or l.sku_id] += 1
        elif how != "exact":
            derived[f"{l.seller_sku}（{how_label.get(how, how)}）"] += 1

    return {
        "ok": True,
        "order_lines": len(data.orders),
        "codes_in_orders": len({l.seller_sku for l in data.orders if l.seller_sku}),
        "missing_total": sum(missing.values()),
        "missing": [{"code": k, "lines": v} for k, v in missing.most_common(sample)],
        "derived_total": sum(derived.values()),
        "derived": [{"code": k, "lines": v} for k, v in derived.most_common(sample)],
        "source": str(inbox),
    }


def _latest_inbox() -> Path | None:
    """最新有订单数据的目录：优先店铺目录，其次公共 inbox。"""
    cands: list[tuple[float, Path]] = []
    try:
        for d in paths.STORES_ROOT.iterdir():
            if not d.is_dir():
                continue
            sub = paths.store_inbox(d)
            if sub.is_dir() and any(sub.iterdir()):
                cands.append((max(f.stat().st_mtime for f in sub.iterdir()), sub))
    except OSError:
        pass
    if paths.INBOX.is_dir() and any(paths.INBOX.iterdir()):
        cands.append((max(f.stat().st_mtime for f in paths.INBOX.iterdir()), paths.INBOX))
    if not cands:
        return None
    return max(cands)[1]
