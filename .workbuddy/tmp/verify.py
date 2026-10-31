"""全量核对：报表里的公式（含/不含样品两套口径）逐格 vs 程序数值版。"""
import json, re, subprocess, sys, tempfile
from pathlib import Path
import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string
sys.path.insert(0, ".workbuddy/tmp")
import fxeval as fx

COLS = ["rank","style","product","gmv","orders","items","aov","buyer_paid","cost",
        "gross_profit","gross_margin","fee:dispatch","ratio:dispatch",
        "fee:platform_commission","fee:tx_fee","fee_total","net_rate","net","net_cny"]
CHK = ["GMV（MYR）","GMV（PHP）","订单数","件数","客单价（MYR）","客单价（PHP）",
       "买家实付（MYR）","买家实付（PHP）","成本（MYR）","成本（PHP）",
       "毛利（MYR）","毛利（PHP）","链接毛利率","发单费（MYR）","发单费（PHP）",
       "发单费占比","平台佣金（MYR）","平台佣金（PHP）","交易手续费（MYR）",
       "交易手续费（PHP）","费用合计（MYR）","费用合计（PHP）","净利率",
       "净利（MYR）","净利（PHP）","净利（CNY）"]


def run(shop_dir, sid, frm, to, out, ez, formula):
    from src import paths
    cfgf = paths.CONFIG / "formulas.json"
    bak = cfgf.read_text(encoding="utf-8")
    d = json.loads(bak)
    d["report_columns"] = COLS
    d["exclude_zero_amount"] = ez
    cfgf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = [sys.executable, "-m", "src.cli", "--dir", shop_dir, "--store", sid,
           "--from", frm, "--to", to, "--out", str(out), "--json"]
    if formula:
        cmd.append("--with-formula")
    subprocess.run(cmd, capture_output=True, cwd=".")
    cfgf.write_text(bak, encoding="utf-8")


def rows_of(p):
    ws = openpyxl.load_workbook(p, data_only=True)["汇总"]
    hd = {str(c.value): c.column for c in ws[2] if c.value}
    out, tot = {}, None
    for r in range(3, ws.max_row + 1):
        pid = ws.cell(row=r, column=hd["链接（商品ID）"]).value
        if str(ws.cell(row=r, column=hd["款式编码"]).value or "") == "合计":
            tot = {k: ws.cell(row=r, column=v).value for k, v in hd.items()}
            continue
        if pid:
            out[str(pid)] = {k: ws.cell(row=r, column=v).value for k, v in hd.items()}
    return out, tot, hd


def check(shop_name, shop_dir, sid, frm, to):
    tmp = Path(tempfile.mkdtemp(prefix="vf_"))
    vw, fm, vo = tmp/"a.xlsx", tmp/"b.xlsx", tmp/"c.xlsx"
    run(shop_dir, sid, frm, to, vw, False, False)
    run(shop_dir, sid, frm, to, fm, False, True)
    run(shop_dir, sid, frm, to, vo, True, False)
    mw, tw, hd = rows_of(vw)
    mo, to_, _ = rows_of(vo)
    wb = openpyxl.load_workbook(fm)
    ws = wb["汇总"]
    wsd, heads, rp = fx.build_detail(wb)
    sc = get_column_letter(heads["是否样品"])
    swc = next((get_column_letter(c.column + 1) for c in ws[1] if c.value == "汇总口径"), None)
    if not swc:
        print(f"[{shop_name}] 没找到口径开关"); return
    tr = next(r for r in range(3, ws.max_row + 1)
              if str(ws.cell(row=r, column=hd["款式编码"]).value or "") == "合计")
    bad = chk = 0
    for r in range(3, tr):
        pid = ws.cell(row=r, column=hd["链接（商品ID）"]).value
        if not pid:
            continue
        pid = str(pid)
        for scope, ref in (("含样品", mw.get(pid)), ("不含样品", mo.get(pid))):
            if not ref:
                continue
            for name in CHK:
                ci = hd.get(name)
                if not ci:
                    continue
                cell = ws.cell(row=r, column=ci)
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                got = fx.eval_formula(cell.value, wsd, rp, sc, swc, scope, ws)
                want = ref.get(name)
                chk += 1
                tol = 0.02 if name in ("订单数", "件数") else 0.1
                if want is None or abs(float(got) - float(want)) > tol:
                    bad += 1
                    if bad <= 3:
                        print(f"   ✗{shop_name} 行{r}[{scope}] {name}: {float(got):.4f} vs {want}")
    print(f"[{shop_name}] 数据行 {chk} 格 → 不一致 {bad}")
    # 合计行
    dv = {}
    for scope in ("含样品", "不含样品"):
        for ci in hd.values():
            for r in range(3, tr):
                c = ws.cell(row=r, column=ci)
                if isinstance(c.value, str) and c.value.startswith("="):
                    dv[(scope, ci, r)] = fx.eval_formula(c.value, wsd, rp, sc, swc, scope, ws)

    def eval_total(ci, scope):
        f = ws.cell(row=tr, column=ci).value
        if not (isinstance(f, str) and f.startswith("=")):
            return None
        def repl(m):
            col, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            cidx = column_index_from_string(col)
            t = 0.0
            for rr in range(a, b + 1):
                v = dv.get((scope, cidx, rr))
                if v is None:
                    v = ws.cell(row=rr, column=cidx).value
                if isinstance(v, (int, float)):
                    t += v
            return repr(t)
        return fx.eval_formula(re.sub(r"SUM\(([A-Z]{1,2})(\d+):\1(\d+)\)", repl, f),
                               wsd, rp, sc, swc, scope, ws)

    b2 = c2 = 0
    for scope, ref in (("含样品", tw), ("不含样品", to_)):
        for name in CHK:
            ci = hd.get(name)
            if not ci or not ref:
                continue
            got = eval_total(ci, scope)
            if got is None:
                continue
            want = ref.get(name)
            c2 += 1
            tol = 0.05 if name in ("订单数", "件数") else 0.5
            if want is None or abs(float(got) - float(want)) > tol:
                b2 += 1
                print(f"   ✗{shop_name} 合计[{scope}] {name}: {float(got):.2f} vs {want}")
    print(f"[{shop_name}] 合计行 {c2} 格 → 不一致 {b2}")
    print(f"[{shop_name}] 两口径净利: 含 {sum(v.get('净利（MYR）') or v.get('净利（PHP）') or 0 for v in mw.values()):,.2f}"
          f" ｜ 不含 {sum(v.get('净利（MYR）') or v.get('净利（PHP）') or 0 for v in mo.values()):,.2f}")
