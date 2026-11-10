"""带公式报表的「体检」：跑一份真报表，检查公式有没有自己算自己。

为什么单独做一个脚本
--------------------
报表列是用户随时能勾的（取消「排名」、去掉「成本」…），每勾一次整行就
往左移一格。2026-11-04 那次事故：取消「排名」后商品ID 从 C 变成 B，
而公式里写死了 C，`SUMIFS(明细!$Y:$Y, 明细!$C:$C, $C3)` 变成
「拿自己的 GMV 当匹配条件」，Excel 判循环引用，整张表全变成 0.00。

出表时已经内置了一道自检（`src.report.audit_formulas`），这个脚本是
在**打包/发布前**主动跑一遍，避免把会出坏报表的版本发出去。

用法：
    python -m scripts.check_formula            # 自动挑一家数据最全的店
    python -m scripts.check_formula --store 7494478694302778633 --from 2026-08-01 --to 2026-08-31
    python -m scripts.check_formula --all-columns   # 顺便把「取消各列」也测一遍

没有可用数据时返回 0（跳过），不会因为没数据就把发布卡住。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "release" / "净利助手"
EXE = APP_DIR / "jingli.exe"


def _pick_store() -> tuple[str, str, str, str] | None:
    """挑一家「四类数据都齐」的店，返回 (店名, shop_id, 国家, 区间)。"""
    shops = APP_DIR / "数据" / "店铺"
    if not shops.is_dir():
        return None
    best = None
    for meta in sorted(shops.glob("*/meta.json")):
        d = shops / meta.parent.name
        inbox = d / "收件箱"
        orders = sorted((inbox / "订单表").glob("*")) if (inbox / "订单表").is_dir() else []
        if not orders:
            continue
        # 从订单表文件名里认区间：…-20260801-20260831-…
        m = re.search(r"(\d{8})-(\d{8})", orders[-1].name)
        if not m:
            continue
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        frm = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        to = f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}"
        item = (meta.parent.name, str(info.get("shop_id") or ""),
                str(info.get("country") or ""), frm, to)
        # 明细行数多的优先（更能暴露列错位）
        lines = 0
        try:
            lines = orders[-1].stat().st_size
        except OSError:
            pass
        if best is None or lines > best[0]:
            best = (lines, item)
    return best[1] if best else None


def _audit(path: Path) -> list[str]:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter, column_index_from_string

    wb = load_workbook(path)
    ws, det = wb["汇总"], wb["明细"]
    maxc, det_max = ws.max_column, det.max_column
    det_hdr = {c for c in range(1, det_max + 1) if det.cell(2, c).value}
    hdr = {c: ws.cell(2, c).value for c in range(1, maxc + 1)}
    probs: list[str] = []

    for r in range(1, ws.max_row + 1):
        for c in range(1, maxc + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str) or not v.startswith("="):
                continue
            own = get_column_letter(c)
            if re.search(rf"\${own}\$?{r}(?![0-9])", v):
                probs.append(f"自引用 {own}{r}（{hdr.get(c)}）")
            for m in re.finditer(r"明细!\$([A-Z]{1,2}):\$([A-Z]{1,2})", v):
                ci = column_index_from_string(m.group(1))
                if ci > det_max or ci not in det_hdr:
                    probs.append(f"{own}{r} 引用明细空列 {m.group(1)}")
            # 只查汇总页自己的引用：先把「明细!$X:$X」这些片段摘掉，
            # 否则明细列会被当成汇总页的列，整页全是假告警。
            own_refs = re.sub(r"明细!\$[A-Z]{1,2}:\$[A-Z]{1,2}", "", v)
            for m in re.finditer(r"\$([A-Z]{1,2})(?![A-Za-z0-9])", own_refs):
                if column_index_from_string(m.group(1)) > maxc:
                    probs.append(f"{own}{r} 引用不存在的列 {m.group(1)}")

    # 合计行不该有常数（除了「合计」这两个字）
    consts = [get_column_letter(c) for c in range(1, maxc + 1)
              if hdr.get(c) and isinstance(ws.cell(1, c).value, (int, float))]
    if consts:
        probs.append(f"合计行有 {len(consts)} 列是常数（{','.join(consts[:6])}），切口径不会联动")
    return sorted(set(probs))


def _render(shop: str, sid: str, country: str, frm: str, to: str,
            out: Path) -> tuple[bool, str]:
    """调 release 里的 exe 出一份带公式报表。用文件传摘要（打包版 stdout 是空的）。"""
    summ = out.with_suffix(".json")
    cmd = [str(EXE), "run",
           "--dir", str(APP_DIR / "数据" / "店铺" / shop / "收件箱"),
           "--store", sid, "--country", country,
           "--from", frm, "--to", to,
           "--with-formula", "--json-out", str(summ), "--out", str(out)]
    env = {**os.environ, "JINGLI_HOME": str(APP_DIR)}
    r = subprocess.run(cmd, capture_output=True, env=env, timeout=600)
    ok = out.exists() and r.returncode == 0
    msg = ""
    if summ.exists():
        try:
            _d = json.loads(summ.read_text(encoding="utf-8"))
            msg = str(_d.get("net_local") or _d.get("net") or "")
        except Exception:  # noqa: BLE001
            msg = ""
        finally:
            try:
                summ.unlink()
            except OSError:
                pass
    return ok, msg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="带公式报表体检")
    ap.add_argument("--store", default="", help="店铺名；不给就自动挑一家")
    ap.add_argument("--from", dest="dfrom", default="")
    ap.add_argument("--to", dest="dto", default="")
    ap.add_argument("--all-columns", action="store_true",
                    help="额外把「取消各列」的 7 种组合各跑一遍（慢，但最彻底）")
    args = ap.parse_args(argv)

    if not EXE.is_file():
        print("  · 没有 exe，跳过公式体检")
        return 0

    if args.store and args.dfrom and args.dto:
        meta = APP_DIR / "数据" / "店铺" / args.store / "meta.json"
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            print(f"  · 读不到 {args.store} 的 meta.json，跳过")
            return 0
        pick = (args.store, str(info.get("shop_id") or ""),
                str(info.get("country") or ""), args.dfrom, args.dto)
    else:
        pick = _pick_store()
    if not pick:
        print("  · 收件箱里没有可用订单表，跳过公式体检")
        return 0

    shop, sid, country, frm, to = pick
    print(f"  · 用 {shop} {frm}~{to} 出一份带公式报表…")
    tmp = Path(tempfile.mkdtemp(prefix="jingli_formula_"))
    out = tmp / "check.xlsx"
    ok, net = _render(shop, sid, country, frm, to, out)
    if not ok:
        print("  ✗ 报表没生成出来，体检做不了")
        return 1

    probs = _audit(out)
    if probs:
        print(f"  ✗ 公式体检发现 {len(probs)} 项问题：")
        for x in probs[:8]:
            print("     -", x)
        return 1
    print(f"  ✓ 公式体检通过（{shop}，净利 {net or '?'}，无自引用、合计行全是公式）")

    if args.all_columns:
        print("  · 再测 7 种「取消某列」的组合…")
        bad = _sweep_columns(shop, sid, country, frm, to, tmp)
        if bad:
            print(f"  ✗ 取消列后有 {len(bad)} 种情况出问题：")
            for x in bad[:8]:
                print("     -", x)
            return 1
        print("  ✓ 7 种取消列的组合全部正常")
    return 0


def _sweep_columns(shop, sid, country, frm, to, tmp: Path) -> list[str]:
    """逐个取消一列再出表，检查会不会退化成「合计是常数」。"""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    cfg_path = APP_DIR / "配置" / "formulas.json"
    if not cfg_path.is_file():
        return []
    backup = tmp / "formulas.bak"
    backup.write_bytes(cfg_path.read_bytes())
    orig = json.loads(cfg_path.read_text(encoding="utf-8"))
    cols = list(orig.get("report_columns") or [])
    bad: list[str] = []
    try:
        for drop in ("rank", "style", "orders", "cost", "net",
                     "fee:ads", "product"):
            if drop not in cols:
                continue
            doc = json.loads(json.dumps(orig))
            doc["report_columns"] = [c for c in cols if c != drop]
            cfg_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
            o = tmp / f"drop_{drop.replace(':', '_')}.xlsx"
            ok, _ = _render(shop, sid, country, frm, to, o)
            if not ok:
                bad.append(f"取消「{drop}」后出不了表")
                continue
            s = load_workbook(o)["汇总"]
            hdr = {c: s.cell(2, c).value for c in range(1, s.max_column + 1)}
            consts = [get_column_letter(c) for c in range(1, s.max_column + 1)
                      if hdr.get(c) and isinstance(s.cell(1, c).value, (int, float))]
            if consts:
                bad.append(f"取消「{drop}」后合计行有常数列 {','.join(consts[:5])}")
            if drop == "product" and not any("链接（商品ID）" in str(h)
                                             for h in hdr.values() if h):
                bad.append("取消「商品ID」后没有被自动补回（带公式版会算不出来）")
    finally:
        cfg_path.write_bytes(backup.read_bytes())
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
