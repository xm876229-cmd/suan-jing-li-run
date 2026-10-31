"""把「计划维度」的广告导出表，转成按商品的广告表。

为什么需要
----------
老版广告后台能直接给「商品维度花费」（product_id + spend），新版 GMV Max 只给
计划维度：广告计划 ID / 广告计划名称 / 成本 / 净成本 / SKU 订单数 / 总收入 / ROI，
**没有商品 ID**。而核算引擎是按商品扣广告费的，所以中间要有人把计划的花费落到商品上。

怎么落
------
1. 计划名里通常带款式编码（"55-59%-B9-9.21" → B9），拿它去映射主表找商品；
   一个款式编码对应多个商品时，按商品数平均分。
2. 对不上的计划，按各商品 GMV 占比分摊（总广告费不会丢，只是分配是估算）。

用法
----
    python -m scripts.import_ads_campaign "C:\\...\\Product campaign data x.xlsx" --store 7495938282526575046
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import paths  # noqa: E402


def load_style_index() -> dict[str, list[str]]:
    """款式编码 → 商品 ID 列表（来自映射主表）。"""
    out: dict[str, list[str]] = {}
    f = paths.MAPPING / "master.csv"
    if not f.is_file():
        return out
    with f.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("style_code") or "").strip().upper()
            pid = (r.get("product_id") or "").strip()
            if code and pid and pid not in out.setdefault(code, []):
                out[code].append(pid)
    return out


def read_campaign_rows(xlsx: Path) -> list[dict]:
    import openpyxl
    # 不能开 read_only：平台导出的 xlsx 没写列宽信息，只读模式会把表读成 1 列
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    hdr = [str(c or "").strip() for c in rows[0]]

    def idx(*names, default=-1):
        for n in names:
            if n in hdr:
                return hdr.index(n)
        return default

    i_name = idx("广告计划名称", "Campaign name", "campaign_name")
    i_cost = idx("成本", "花费", "Cost", "Spend", "stat_cost")
    i_gmv = idx("总收入", "GMV", "gross_revenue")
    if i_name < 0 or i_cost < 0:
        raise SystemExit(f"表头里找不到「广告计划名称」或「成本」，实际表头：{hdr}")
    out = []
    for r in rows[1:]:
        name = str(r[i_name] or "").strip()
        if not name:
            continue
        try:
            cost = float(str(r[i_cost]).replace(",", "") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            gmv = float(str(r[i_gmv]).replace(",", "") or 0) if i_gmv >= 0 else 0.0
        except (TypeError, ValueError):
            gmv = 0.0
        out.append({"name": name, "cost": cost, "gmv": gmv})
    return out


def product_gmv(shop_id: str) -> dict[str, float]:
    """从这家店最新的订单表里算各商品 GMV（用来给对不上的计划分摊）。"""
    d = paths.store_kind_dir(shop_id, "orders")
    if not d.is_dir():
        return {}
    files = sorted(d.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {}
    from src.loader import read_table, build_colmap
    rows = read_table(files[0])
    if not rows:
        return {}
    cm = build_colmap(rows[0])
    i_pid = cm.get("product_id")
    i_amt = cm.get("order_amount") or cm.get("subtotal_before")
    if i_pid is None or i_amt is None:
        return {}
    out: dict[str, float] = {}
    for r in rows[1:]:
        try:
            pid = str(r[i_pid] or "").strip()
            amt = float(str(r[i_amt]).replace(",", "") or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if pid:
            out[pid] = out.get(pid, 0.0) + amt
    return out


def convert(xlsx: Path, shop_id: str, out_dir: Path | None = None) -> Path:
    camps = read_campaign_rows(xlsx)
    if not camps:
        raise SystemExit("表里没有计划数据")

    style_idx = load_style_index()
    by_pid: dict[str, float] = {}
    unmatched: list[dict] = []
    matched_cost = 0.0

    for c in camps:
        parts = [p.strip().upper() for p in re.split(r"[-_%/\\\s.]+", c["name"]) if p.strip()]
        hit = next((p for p in parts if p in style_idx), "")
        pids = style_idx.get(hit) or []
        if pids:
            share = c["cost"] / len(pids)
            for pid in pids:
                by_pid[pid] = by_pid.get(pid, 0.0) + share
            matched_cost += c["cost"]
        else:
            unmatched.append(c)

    total_cost = sum(c["cost"] for c in camps)
    rest = total_cost - matched_cost

    # 对不上的部分按 GMV 占比分摊（GMV 为 0 时按商品数平均）
    if rest > 0.005:
        gmv = product_gmv(shop_id) if shop_id else {}
        base = {p: v for p, v in gmv.items() if v > 0}
        if not base:
            base = {p: 1.0 for p in by_pid} or {}
        s = sum(base.values())
        if s > 0:
            for pid, v in base.items():
                by_pid[pid] = by_pid.get(pid, 0.0) + rest * (v / s)

    target = Path(out_dir or paths.store_kind_dir(shop_id, "ads"))
    target.mkdir(parents=True, exist_ok=True)
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*-\s*(\d{4}-\d{2}-\d{2})", xlsx.name)
    tag = f"{m.group(1)}_{m.group(2)}" if m else "导出"
    out = target / f"creative data for product campaigns ({tag}).csv"

    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Product ID", "Spend", "Currency", "Product name"])
        for pid, spend in sorted(by_pid.items(), key=lambda x: -x[1]):
            w.writerow([pid, f"{spend:.2f}", "LOCAL", ""])

    print(f"计划 {len(camps)} 个｜总花费 {total_cost:.2f}")
    print(f"  按款式编码对上：{matched_cost:.2f}（{len(camps) - len(unmatched)} 个计划）")
    print(f"  按 GMV 分摊：{rest:.2f}（{len(unmatched)} 个计划）")
    print(f"  落到 {len(by_pid)} 个商品")
    print(f"输出：{out}")
    return out


def autoconvert_in_store(shop_id: str) -> list[str]:
    """核算前自动处理：广告表目录里如果有「计划维度」的 xlsx，转成按商品的 CSV。

    返回做了什么（给日志用）。已经有更新 CSV 的跳过，重复跑不会重复转。
    """
    notes: list[str] = []
    sid = (shop_id or "").strip()
    if not sid:
        return notes
    d = paths.store_kind_dir(sid, "ads")
    if not d.is_dir():
        return notes
    for f in sorted(d.glob("*.xlsx")):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(f, data_only=True, read_only=False)
            ws = wb.worksheets[0]
            first = next(ws.iter_rows(values_only=True), ())
            wb.close()
            hdr = [str(c or "") for c in first]
            # 已经是商品维度（带商品 ID 列）→ 不能转，否则会被再摊一次
            if any("商品 ID" in h or "商品ID" in h or "Product ID" in h
                   or "product_id" in h.lower() for h in hdr):
                continue
            if not any("广告计划" in h for h in hdr):
                continue          # 既不是计划维度、也不是商品维度，不认识，跳过
            target = convert(f, sid)
            notes.append(f"{f.name} → {target.name}")
        except Exception as e:    # noqa: BLE001
            notes.append(f"{f.name} 转换失败：{e}")
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把计划维度广告导出表转成按商品")
    ap.add_argument("xlsx")
    ap.add_argument("--store", default="", help="店铺 ID（分摊时读它的订单表）")
    a = ap.parse_args(argv)
    convert(Path(a.xlsx), a.store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
