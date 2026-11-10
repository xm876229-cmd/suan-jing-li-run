"""独立对账：从原始导出文件自己算一遍，跟程序出的报表比。

为什么要有这个脚本
------------------
这个项目历史上出过的错，全都不是「某一行算错」，而是**拿错了数据**
（8 月的广告算进 7 月、别家店的文件混进来）。这种情况下报表每列都有数、
不报任何异常，只有拿原始文件另算一遍才能发现。

所以这里刻意**不 import src 里的任何模块**：表头怎么认、金额怎么加、
成本怎么配，全部照原始文件重写一遍。如果这里和程序算出来一样，
说明程序用的确实是这个区间、这家店的数据。

比对项（都是「有原始依据、能独立算出」的）：
    订单数 / GMV / 广告花费 / 联盟佣金 / 商品成本(CNY) / 明细行数

用法：
    python -m scripts.reconcile --store 7495895952415426722 \
        --from 2026-07-01 --to 2026-07-31 --report 报表/自动/.../xxx.xlsx

退出码：0 全部对上；1 有差异（会把差多少打出来）；2 找不到文件。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 这些状态不算「收入」：跟 src/calc.py 的默认口径一致（cancelled/refunded 收入为 0）
DEAD_STATUS = ("未支付", "待付款", "已取消", "已关闭", "已退款", "取消", "失败",
               "cancelled", "canceled", "refund", "unpaid")

RANGE_RE = re.compile(r"(\d{4})[-_./]?(\d{2})[-_./]?(\d{2}).{0,12}?(\d{4})[-_./]?(\d{2})[-_./]?(\d{2})")


# ---------------- 基础 ----------------

def data_home() -> Path:
    env = (os.environ.get("JINGLI_HOME") or "").strip()
    return Path(env).expanduser().resolve() if env else ROOT


def num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def clean(v) -> str:
    return str(v or "").strip().strip("\t").strip()


def file_range(p: Path) -> tuple[str, str] | None:
    for m in RANGE_RE.finditer(p.name):
        y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
        try:
            a = datetime(y1, m1, d1).date().isoformat()
            b = datetime(y2, m2, d2).date().isoformat()
        except ValueError:
            continue
        return (a, b) if a <= b else (b, a)
    return None


def read_rows(p: Path) -> list[dict]:
    """CSV / XLSX 都读成 [{列名: 值}]。平台两种格式都会给。"""
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        hi = -1
        keys = ("商品 ID", "Product ID", "Order ID", "广告计划名称", "SKU ID")
        for i, r in enumerate(rows[:8]):
            cells = [clean(c) for c in r]
            if sum(1 for c in cells if c) >= 2 and any(k in cells for k in keys):
                hi = i
                break
        if hi < 0:
            return []
        hdr = [clean(c) for c in rows[hi]]
        out = []
        for r in rows[hi + 1:]:
            if not any(clean(c) for c in r):
                continue
            out.append({hdr[i] if i < len(hdr) else f"col{i}":
                        (r[i] if i < len(r) else None) for i in range(len(r))})
        return out
    return read_csv(p)


def read_csv(p: Path) -> list[dict]:
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(p, encoding=enc, newline="") as fh:
                return list(csv.DictReader(fh))
        except UnicodeDecodeError:
            continue
    return []


def pick(files: list[Path], must: tuple[str, ...], window) -> Path | None:
    """挑出盖得住区间的那份（没有区间信息时就挑唯一那份）。"""
    hit = []
    for p in files:
        try:
            head = read_rows(p)[:1]
        except Exception:  # noqa: BLE001
            head = []
        cols = set(head[0].keys()) if head else set()
        if head and must and not all(any(m.lower() == clean(c).lower() for c in cols) for m in must):
            continue
        hit.append(p)
    if not hit:
        return None

    # 和程序同一套判据（2026-11-05 起）：区间必须**一字不差**，同样相等的取导出时间最新。
    # 以前这里是「盖得住 + 最窄」，程序改成精确区间后两边会对不上：
    # 报表用的是 15:01 新导的，对账却挑了 14:24 那份，GMV 差几十块，看着像算错。
    exact = [p for p in hit
             if (r := file_range(p)) and r[0] == window[0] and r[1] == window[1]]
    if exact:
        return max(exact, key=lambda p: p.stat().st_mtime)
    if len(hit) == 1:
        return hit[0]
    covering = [p for p in hit if (r := file_range(p)) and r[0] <= window[0] and r[1] >= window[1]]
    if covering:
        # 老报表（区间不是精确匹配那一代）仍按最窄优先，保证还能对旧报表
        return min(covering, key=lambda p: (datetime.fromisoformat(file_range(p)[1])
                                            - datetime.fromisoformat(file_range(p)[0])).days)
    return max(hit, key=lambda p: p.stat().st_mtime)


def store_dir(shop_id: str) -> Path | None:
    root = data_home() / "数据" / "店铺"
    if not root.is_dir():
        return None
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if d.name == shop_id:
            return d
        mf = d / "meta.json"
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if str(meta.get("shop_id") or "") == str(shop_id):
            return d
    return None


# ---------------- 独立计算 ----------------

def date_formats(rows: list[dict]) -> tuple[str, ...]:
    """判断这份订单表用的是「日/月/年」还是「月/日/年」。

    马来站是 31/07/2026（日在前），菲律宾站是 07/31/2026 11:58:53 PM（月在前）。
    光看一行猜不出来（05/07 两边都合法），所以扫一遍：谁出现过 >12 的分量，
    就是它。两边的日期都不超过 12 时，按国家习惯退回「日/月/年」。
    """
    dmy = mdy = False
    for r in rows[:4000]:
        t = clean(r.get("Created Time") or "")
        m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", t)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            dmy = True
        if b > 12:
            mdy = True
        if dmy or mdy:
            break
    if mdy and not dmy:
        return ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
                "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
    return ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
            "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S")


def _day_of(r: dict, fmts: tuple[str, ...] = ()) -> str | None:
    t = clean(r.get("Created Time") or "")
    for fmt in (fmts or date_formats([r])):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _kind(r: dict) -> str:
    """照 src/calc.py 的 order_kind 重写一遍（这里是独立的第二份实现）。

    为什么要照抄而不是「合理猜测」：口径不是猜出来的，是配置里写死的。
    对不上时能立刻分清是「程序算错」还是「我这份对照写错了口径」。
    """
    g = num(r.get("SKU Subtotal Before Discount")) - num(r.get("SKU Seller Discount"))
    amt = num(r.get("Order Amount"))
    if clean(r.get("Cancelation/Return Type")):
        c = clean(r.get("Cancelation/Return Type")).lower()
        if "return" in c or "refund" in c:
            return "refunded"
        # 有物流单号/物流商 = 已打包出单，属售后；再看有没有发货/签收时间
        if clean(r.get("Tracking ID")) or clean(r.get("Shipping Provider Name")):
            shipped = clean(r.get("Shipped Time")) or clean(r.get("Delivered Time"))
            return "cancelled_shipped" if shipped else "cancelled_unshipped"
        return "cancelled"
    if g == 0 and amt == 0:
        return "zero_amount"
    status = clean(r.get("Order Status"))
    dead = ("未支付", "待付款", "已取消", "已关闭", "已退款", "取消", "失败")
    if amt > 0:
        paid = True
    else:
        paid = bool(status) and not any(k in status for k in dead)
    return "normal" if paid else "unpaid"


# 默认口径（DEFAULT_ORDER_FILTER）：收入只算 normal / zero_amount；
# 成本取消和退款也要算（货已经发了）；订单数除未付款外都算。
# 这里的默认值只在全国配置读不到时兜底；真正生效的是 formulas.json 里
# 每个参数的 kinds（对账脚本会去读它，保证跟程序同一口径）。
INCOME_KINDS = ("normal", "zero_amount")
COST_KINDS = ("normal", "zero_amount", "cancelled_shipped")
COUNT_KINDS = ("normal", "zero_amount", "refunded", "cancelled_shipped")


def calc_orders(p: Path, window, gmv_kinds=INCOME_KINDS,
                count_kinds=COUNT_KINDS) -> dict:
    rows = read_rows(p)
    fmts = date_formats(rows)
    lo, hi = window
    gmv = 0.0
    lines = 0
    cats: dict[str, int] = {}
    # 订单行数按**整份文件**统计：一个订单 3 行、其中 2 行落在本期，
    # 程序是按 3 行分摊的（1/3 + 1/3），不是按 2 行。这里必须跟它一致。
    per_order: dict[str, int] = {}
    for r in rows:
        oid = clean(r.get("Order ID"))
        per_order[oid] = per_order.get(oid, 0) + 1
    kept = []
    for r in rows:
        day = _day_of(r, fmts)
        if day is None or not (lo <= day <= hi):
            continue
        lines += 1
        k = _kind(r)
        cats[k] = cats.get(k, 0) + 1
        kept.append((r, k, clean(r.get("Order ID"))))
    orders = 0.0
    for r, k, oid in kept:
        g = num(r.get("SKU Subtotal Before Discount")) - num(r.get("SKU Seller Discount"))
        if k in gmv_kinds:
            gmv += g
        if k in count_kinds:
            orders += 1.0 / (per_order.get(oid) or 1)
    return {"gmv": gmv, "orders": orders, "lines": lines, "cats": cats, "file": p.name}


def calc_ads(p: Path) -> tuple[float, dict]:
    rows = read_rows(p)
    by_cur: dict[str, float] = {}
    for r in rows:
        c = clean(r.get("Currency") or r.get("currency") or "未标注") or "未标注"
        spend = r.get("Spend")
        if spend in (None, ""):
            spend = r.get("成本") or r.get("Cost") or r.get("花费)")
        by_cur[c] = by_cur.get(c, 0.0) + num(spend)
    return sum(by_cur.values()), by_cur


def calc_comm(p: Path) -> tuple[float, str]:
    rows = read_rows(p)
    total = 0.0
    cur = ""
    for r in rows:
        total += num(r.get("Est. commission") or r.get("Commission"))
        cur = cur or clean(r.get("Currency"))
    return total, cur


def sku_cost_map(sku_table: Path) -> dict:
    """SKU ID → 卖家编码。用来把订单行对到成本表。"""
    wb = openpyxl.load_workbook(sku_table, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    hdr_i = -1
    for i, r in enumerate(rows[:8]):
        cells = [clean(c) for c in r]
        if any("SKU ID" == c for c in cells) and any("Seller SKU" == c or "卖家" in c for c in cells):
            hdr_i = i
            break
    if hdr_i < 0:
        return {}
    hdr = [clean(c) for c in rows[hdr_i]]
    def idx(*names):
        for n in names:
            for i, h in enumerate(hdr):
                if h == n:
                    return i
        return -1
    i_sid = idx("SKU ID", "SKU Id")
    i_sku = idx("Seller SKU", "卖家 SKU", "SKU")
    i_pid = idx("Product ID", "商品 ID")
    out = {}
    if i_sid < 0:
        return out
    for r in rows[hdr_i + 1:]:
        if i_sid >= len(r):
            continue
        sid = clean(r[i_sid])
        if not sid:
            continue
        sku = clean(r[i_sku]) if 0 <= i_sku < len(r) else ""
        pid = clean(r[i_pid]) if 0 <= i_pid < len(r) else ""
        out[sid] = (sku, pid)
    return out


def cost_map() -> dict[str, float]:
    f = data_home() / "数据" / "成本" / "master.csv"
    out: dict[str, float] = {}
    if not f.is_file():
        return out
    for r in read_csv(f):
        code = clean(r.get("code"))
        if code:
            out[code] = num(r.get("cost"))
    return out


def calc_cost(p_order: Path, window, sku_map: dict, costs: dict,
              kinds=None) -> tuple[float, int]:
    """成本合计(CNY)：按「SKU ID → 卖家编码 → 成本表」一条条配。"""
    rows = read_rows(p_order)
    fmts = date_formats(rows)
    lo, hi = window
    total = 0.0
    matched = 0
    # 忽略大小写的索引（和程序一致），只在精确匹配不到时用
    _norm: dict[str, float] = {}
    for _k, _v in (costs or {}).items():
        _norm.setdefault(str(_k).strip().upper(), _v)
    for r in rows:
        day = _day_of(r, fmts)
        if day is None or not (lo <= day <= hi):
            continue
        # 明细页的「成本合计」是逐行写的，不按口径过滤；
        # 这里也不过滤，否则永远对不上（差额就是被筛掉那几类的成本）。
        if kinds is not None and _kind(r) not in kinds:
            continue          # 未付款的单没有成本；取消/退款的有（货已发）
        sid = clean(r.get("SKU ID"))
        sku = clean(r.get("Seller SKU"))
        code = (sku_map.get(sid) or ("", ""))[0] or sku
        qty = num(r.get("Quantity"))
        c = costs.get(code)
        if c is None:
            # 跟程序一致：忽略大小写再试一次（2026-11-05 起程序这么做）
            c = _norm.get(str(code).strip().upper())
        if c is None:
            c = costs.get(sid)
        if c is None:
            continue
        matched += int(qty)
        total += c * qty
    return total, matched


# ---------------- 报表取值 ----------------

def read_report(p: Path, costs: dict | None = None) -> dict:
    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    out: dict = {}
    ws = wb["汇总"]
    rows = ws.iter_rows(min_row=1, max_row=2, values_only=True)
    first = next(rows)
    hdr = next(rows)
    col = {clean(h): i for i, h in enumerate(hdr) if h is not None}
    def g(name):
        for k, i in col.items():
            if k.startswith(name):
                return num(first[i])
        return 0.0
    out["gmv"] = g("GMV")
    out["orders"] = g("订单数")
    out["ads"] = g("广告花费")
    out["comm"] = g("联盟佣金")
    out["net"] = g("净利（MYR") or g("净利（")
    ws2 = wb["明细"]
    cost_sum = 0.0
    det_lines = 0
    derived: dict[str, tuple[float, int]] = {}
    _cost_up = {str(k).strip().upper() for k in (costs or {})}
    hdr2 = None
    i_cost = -1
    for i, r in enumerate(ws2.iter_rows(min_row=1, max_row=3, values_only=True), 1):
        if i == 2:
            hdr2 = [clean(c) for c in r]
    if hdr2:
        i_cost = next((i for i, h in enumerate(hdr2) if h.startswith("成本合计")), -1)
    for r in ws2.iter_rows(min_row=3, values_only=True):
        if not r or not clean(r[0]):
            continue
        det_lines += 1
        if 0 <= i_cost < len(r):
            cost_sum += num(r[i_cost])
            # 成本表里没有这个编码、但报表里给了单价 → 程序按规则推导出来的
            if costs is not None and num(r[i_cost]) > 0:
                sku = clean(r[4] if len(r) > 4 else "")
                unit = num(r[12] if len(r) > 12 else 0)
                if (sku and sku not in (costs or {})
                        and sku.strip().upper() not in _cost_up and unit > 0):
                    prev = derived.get(sku)
                    derived[sku] = (unit, (prev[1] if prev else 0) + 1)
    out["cost_cny"] = cost_sum
    out["det_lines"] = det_lines
    out["cost_derived"] = derived
    wb.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--from", dest="dfrom", required=True)
    ap.add_argument("--to", dest="dto", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--rate", type=float, default=0.0,
                    help="广告币种→本地币的汇率（USD→MYR/PHP），不给就从 rates.json 推")
    a = ap.parse_args()

    window = (a.dfrom, a.dto)
    sd = store_dir(a.store)
    if sd is None:
        print(f"找不到店铺目录：{a.store}")
        return 2
    inbox = sd / "收件箱"
    if not inbox.is_dir():
        print(f"没有收件箱：{inbox}")
        return 2

    def look(kind: str, *musts: tuple[str, ...]) -> Path | None:
        """同一类表在不同站点中英文表头不一样，所以给几组候选，命中任一组就算。"""
        d = inbox / kind
        if not d.is_dir():
            return None
        files = [p for ext in ("*.csv", "*.xlsx") for p in d.glob(ext)]
        got = []
        for p in files:
            try:
                head = read_rows(p)[:1]
            except Exception:  # noqa: BLE001
                continue
            cols = {clean(c).lower() for c in (head[0].keys() if head else [])}
            for m in musts:
                if all(x.lower() in cols for x in m):
                    got.append(p)
                    break
        return pick(got, (), window)

    order_f = look("订单表", ("Order ID",), ("Order ID", "Seller SKU"))
    ads_f = look("广告表", ("Product ID", "Spend"), ("商品 ID", "成本"))
    comm_f = look("联盟佣金表", ("Est. commission",), ("Product ID", "Est. commission"))

    print(f"店铺目录：{sd}")
    print(f"区间：{window[0]} ~ {window[1]}")
    for label, f in (("订单", order_f), ("广告", ads_f), ("联盟", comm_f)):
        if f is None:
            print(f"  {label}：没找到")
        else:
            r = file_range(f)
            print(f"  {label}：{f.name}（{r[0]}~{r[1]}）" if r else f"  {label}：{f.name}（文件名无区间）")
    if order_f is None:
        return 2

    # 口径以「计算参数」为准：GMV/成本/订单数各自算哪些订单类别，
    # 是 formulas.json 里写死的 kinds，不是代码里猜的。
    kinds_of: dict[str, tuple] = {}
    try:
        fcfg = json.loads((data_home() / "配置" / "formulas.json").read_text(encoding="utf-8"))
        for it in fcfg.get("params") or []:
            if isinstance(it, dict) and it.get("id"):
                kinds_of[str(it["id"])] = tuple(it.get("kinds") or ())
    except Exception:  # noqa: BLE001
        pass
    gmv_kinds = kinds_of.get("gmv") or INCOME_KINDS
    count_kinds = kinds_of.get("order_count") or COUNT_KINDS
    raw = calc_orders(order_f, window, gmv_kinds, count_kinds)
    cmap = cost_map()
    rep = read_report(Path(a.report), cmap)

    # 汇率 / 广告账号币种：口径必须跟程序一样，否则「对不上」是我这边算错
    rate = a.rate
    acct = ""
    local = ""
    if not rate:
        try:
            rates = json.loads((data_home() / "配置" / "rates.json").read_text(encoding="utf-8"))
            country = json.loads((sd / "meta.json").read_text(encoding="utf-8")).get("country", "")
            c = (rates.get("countries") or {}).get(country) or {}
            rate = float(c.get("usd_to_local") or 1)
            acct = str(c.get("ads_currency") or "").upper()
            local = str(c.get("currency") or "").upper()
            # 程序核算用的是**当天存档的 T-1 汇率**（frankfurter 交叉），
            # 不是 rates.json 里那个固定值。对账要用同一组数，否则永远差一点。
            try:
                arch = sorted((data_home() / "数据" / "汇率存档").glob("*.json"))
                if arch:
                    doc = json.loads(arch[-1].read_text(encoding="utf-8"))
                    rr = doc.get("rates") or {}
                    if rr.get("USD") and local and rr.get(local):
                        rate = float(rr[local]) / float(rr["USD"])
                        print(f"汇率存档 {arch[-1].stem}：1 USD = {rate:.4f} {local}")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            rate = 1.0

    _raw_total, ads_by_cur = calc_ads(ads_f) if ads_f else (0.0, {})
    ads_local = 0.0
    for cur, amount in ads_by_cur.items():
        code = (cur or "").upper()
        # 平台把「账号本位币」写成 LOCAL（或空列）。实测 MY/PH 六个广告账号都是 USD：
        # 后台显示 12,450.49 USD，导出表 Currency 写 LOCAL。不换算会少算 4~63 倍。
        if code in ("", "LOCAL", "未标注"):
            code = local if acct == "LOCAL" else (acct if acct not in ("", "AUTO") else "USD")
        factor = 1.0 if (not local or code == local) else (rate if code in ("USD", "US$", "$") else 1.0)
        ads_local += amount * factor
    ads_sum = sum(ads_by_cur.values())
    comm_sum, _ = calc_comm(comm_f) if comm_f else (0.0, "")

    sku_dir = sd / "商品SKU表"
    sku_map = {}
    if sku_dir.is_dir():
        tabs = sorted(sku_dir.glob("*.xlsx"))
        if tabs:
            sku_map = sku_cost_map(tabs[-1])
    cost_sum, cost_qty = calc_cost(order_f, window, sku_map, cmap)

    def line(name, mine, theirs, tol=0.6):
        d = theirs - mine
        flag = "✓" if abs(d) <= tol else "✗ 差 " + f"{d:,.2f}"
        print(f"  {name:<12}{mine:>16,.2f}{theirs:>16,.2f}   {flag}")
        return abs(d) <= tol

    print("\n             {:>16}{:>16}".format("独立算（原始文件）", "程序算（报表）"))
    ok = True
    ok = line("GMV", raw["gmv"], rep["gmv"])
    ok = line("订单数", float(raw["orders"]), rep["orders"], tol=0.01) and ok
    ok = line(f"广告花费@{rate:.4f}", ads_local, rep["ads"]) and ok
    ok = line("联盟佣金", comm_sum, rep["comm"]) and ok
    cost_ok = line("成本(逐行合计,CNY)", cost_sum, rep["cost_cny"])
    print(f"  明细行数      {raw['lines']:>16,}{rep['det_lines']:>16,}   "
          f"{'✓' if raw['lines'] == rep['det_lines'] else '✗ 差 ' + str(raw['lines'] - rep['det_lines'])}")
    if raw["lines"] != rep["det_lines"]:
        ok = False
    # 成本差能不能被「程序按规则推导的那几个编码」完整解释？
    explained = sum(u * n for u, n in (rep.get("cost_derived") or {}).values())
    cost_diff = rep["cost_cny"] - cost_sum
    if (not cost_ok) and explained and abs(abs(cost_diff) - explained) < 1.0:
        cost_ok = True
        print(f"  （成本差 {cost_diff:,.2f} 全部是推导价，不算差异）")
    ok = ok and cost_ok
    if not ok and rep.get("cost_derived"):
        # 程序会对「J22-1-18pCS」这种带后缀的编码按 J22 推导成本，
        # 独立的对照只做精确匹配，所以这一项天然会有小额差——不是串数据。
        items = list(rep["cost_derived"].items())[:4]
        print("  成本差额来源：成本表里查不到、由程序按规则推导的编码——"
              + "、".join(f"{k}(单价{v:g}，{n}行)" for k, (v, n) in items))
    print(f"\n  广告币种：{ads_by_cur}（账号本位币 {acct or 'AUTO→USD'}，汇率 {rate}）"
          f"　成本匹配件数：{cost_qty}")
    print("  结论：" + ("全部对上，这份报表用的就是这一批原始数据" if ok
                      else "有差异——先看上面哪一行不对，再查那类表用的是哪一份"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
