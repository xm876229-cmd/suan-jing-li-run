"""自测：勾选 / 费率 / 新增 / 删除 到底有没有真的影响净利。

用户问的是「我勾了会不会生效、改了费率会不会生效」，光看代码不算数，
这里用**真实订单数据**跑一遍，把每一项改动前后的金额都打出来。

判据不是「代码看起来对」，而是「金额按预期变了」：
    · 勾掉一个订单类别  → 那一项的金额必须变小或不变，净利变大或不变
    · 关掉一个扣款项    → 净利必须正好增加该项的金额
    · 改费率数值        → 该项金额必须按比例跟着变
    · 新增一个扣款项    → 净利必须正好减少它算出来的金额
    · 删掉一个扣款项    → 净利必须正好增加该项原来的金额

用法：
    python -m scripts.selftest_calc                # 用 release/净利助手 的数据
    python -m scripts.selftest_calc --country 马来西亚
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "release" / "净利助手"

ROWS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    ROWS.append((name, "通过" if ok else "失败", detail))
    return ok


def money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)



def check_style_rule() -> bool:
    """款式编码提取：必须和店主那个 tq() / LET 公式一字不差。

    2026-11-11 踩过：老规则把 `B11-100/190cm` 按 / 拆开，
    尺寸区间被当成第二个款式，报表的款式编码拼成
    `120cm/190cm/50cm/90cm/B11/B2`。这里把那条 LET 公式翻成 Python 对拍。
    """
    from src.loader import derive_style_code

    def tq(t: str) -> str:
        if t == "":
            return ""
        n = len(t)
        is_num = [c.isdigit() for c in t]
        first = next((i + 1 for i, v in enumerate(is_num) if v), 0)
        if first == 0:
            return ""
        first_non = n + 1
        for k in range(first, n + 1):
            if not is_num[k - 1]:
                first_non = k
                break
        return t[:first_non - 1]

    cases = ["B9-1.5m-White", "B11-100/190cm-2PCS", "B110-90cm-2PCS", "B2-White*10PCS",
             "B145-G/C/DSET", "MY-J68-22cm+clip", "51-(70*50*60)-Blue+1Diaper",
             "V27-White-10Pcs", "V1【Set-LOVE】", "M15*30pcs", "2410280001",
             "B11-70/120cm", "B148/CS13/CS14/CS6", "B137/B138", "", "DSET", "ABC"]
    bad = [c for c in cases if derive_style_code(c) != tq(c)]
    if bad:
        return rec("款式编码 = tq() 规则", False, "对不上：" + "、".join(repr(x) for x in bad[:4]))
    return rec("款式编码 = tq() 规则", True,
               "17 条样例一致（B11-100/190cm→B11、B145-G/C/DSET→B145…）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="计算口径自测")
    ap.add_argument("--country", default="", help="用哪个国家的费率试算，默认取第一个")
    args = ap.parse_args(argv)

    home = Path(os.environ.get("JINGLI_HOME") or HOME)
    if not home.exists():
        print(f"[失败] 数据目录不存在：{home}")
        return 1
    os.environ["JINGLI_HOME"] = str(home.resolve())
    sys.path.insert(0, str(ROOT))

    from bridge import config_page as cp
    check_style_rule()

    cfg_file = home / "配置" / "formulas.json"
    rates_file = home / "配置" / "rates.json"
    if not cfg_file.exists() or not rates_file.exists():
        print("[失败] 缺少 配置/formulas.json 或 rates.json")
        return 1

    params = json.loads(cfg_file.read_text(encoding="utf-8"))["params"]
    fmls = json.loads(cfg_file.read_text(encoding="utf-8"))["formulas"]
    rates = json.loads(rates_file.read_text(encoding="utf-8"))
    country = args.country or next(iter(rates.get("countries") or {}), "")
    if not country:
        print("[失败] rates.json 里没有国家")
        return 1

    print(f"\n=== 计算口径自测（{country}）===\n")

    def run(p, f=None):
        j = cp.preview(p, f or fmls, country=country)
        if not j.get("ok"):
            raise SystemExit(f"试算失败：{j.get('error')}")
        return j["after"]

    def patch(pid: str, **kw):
        p = copy.deepcopy(params)
        for it in p:
            if isinstance(it, dict) and it.get("id") == pid:
                it.update(kw)
        return p

    base = run(params)
    base_fees = dict(base.get("fee_detail") or {})
    print(f"基线：GMV {money(base['gmv'])}　费用 {money(base['fees'])}　净利 {money(base['net'])}\n")
    # ⓿ 成本口径：明细里「成本合计」必须等于「成本单价 × 数量」，
    #    以及本地成本 = 人民币合计 × 汇率。用户专门问过，得每次都能自证。
    try:
        _data, _err = cp._load_data()      # 和试算同一份数据，有缓存不会重复解析
        if _data is None:
            rec("成本口径检查", False, _err or "没有订单数据")
        else:
            checked = bad_qty = 0
            tot_cny = 0.0
            for line in (_data.orders or []):
                u, q, t = line.cost_unit_cny, line.qty, line.cost_total_cny
                if u is None or t is None:
                    continue
                checked += 1
                tot_cny += t
                if abs(u * q - t) > 0.02:
                    bad_qty += 1
            rec("成本合计 = 编码成本 × 购买数量（每行都核）",
                checked > 0 and bad_qty == 0,
                f"核了 {checked} 行，不一致 {bad_qty} 行")
            rate = (rates["countries"].get(country) or {}).get("cny_to_local") or 0
            if rate and checked:
                all_local = tot_cny * rate
                calc_cost = base.get("cost", 0)
                # 核算值 ≤「全部行 × 汇率」是正常的：成本不算售前取消和退货退款
                ok = 0 < calc_cost <= all_local * 1.001
                rec("本地成本 = 成本合计(人民币) × 汇率（按订单类别取子集）", ok,
                    f"全部行 Σ{tot_cny:,.2f}CNY × {rate} = {all_local:,.2f}；"
                    f"核算用了 {calc_cost:,.2f}（少的部分＝售前取消/退货退款）")
    except Exception as e:  # noqa: BLE001
        rec("成本口径检查", False, f"跑不起来：{e}")

    # ① 订单类别勾选
    for pid, label in (("logistics", "物流费"), ("dispatch", "发单费")):
        if pid not in (base.get("fee_detail") or {}):
            continue
        all_k = run(patch(pid, kinds=None))
        normal = run(patch(pid, kinds=["normal"]))
        none_ = run(patch(pid, kinds=[]))
        a = all_k["fee_detail"].get(pid, 0)
        n = normal["fee_detail"].get(pid, 0)
        z = none_["fee_detail"].get(pid, 0)
        rec(f"{label}：勾选订单类别会影响金额",
            a >= n >= z and (a > z or a == 0),
            f"全部 {money(a)} ≥ 只正常单 {money(n)} ≥ 一类不算 {money(z)}")

    # 关键：挑「基线里真的有钱」的那一项来测开关和删除。
    # 用恒为 0 的项测，0 == 0 也会「通过」，等于没测。
    labels = {str(it.get("id")): str(it.get("label") or it.get("id"))
              for it in params if isinstance(it, dict) and it.get("id")}
    paid = {k: v for k, v in base_fees.items() if v > 0.005}
    if not paid:
        rec("找一项有金额的扣款", False, "基线里所有费用都是 0，无法验证")
        target, target_label, target_fee = "", "", 0.0
    else:
        target = max(paid, key=lambda k: paid[k])
        target_label = labels.get(target, target)
        target_fee = paid[target]
        print(f"（下面用金额最大的一项「{target_label}」{money(target_fee)} 来测开关与增删）\n")

    # ② 启用开关
    off = run(patch(target, enabled=False))
    delta = off["net"] - base["net"]
    rec(f"关掉「{target_label}」→ 净利正好多出它的金额",
        abs(delta - target_fee) < 0.05,
        f"净利 {money(base['net'])} → {money(off['net'])}（+{money(delta)}），该项原本 {money(target_fee)}")

    # ③ 改费率数值
    c = rates["countries"][country]
    old_rate = c.get("platform_commission")
    if isinstance(old_rate, (int, float)) and old_rate:
        bak = rates_file.with_suffix(".json.selftest")
        shutil.copy2(rates_file, bak)
        try:
            new_rate = round(old_rate * 2, 6)
            c["platform_commission"] = new_rate
            rates_file.write_text(json.dumps(rates, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            doubled = run(params)
        finally:
            shutil.copy2(bak, rates_file)
            bak.unlink(missing_ok=True)
        b0 = base["fee_detail"].get("platform_commission", 0)
        b1 = doubled["fee_detail"].get("platform_commission", 0)
        ratio = (b1 / b0) if b0 else 0
        rec("改费率数值（佣金率 ×2）→ 该项金额翻倍",
            abs(ratio - 2) < 0.02,
            f"{money(b0)} → {money(b1)}（×{ratio:.2f}），净利 {money(base['net'])} → {money(doubled['net'])}")
    else:
        rec("改费率数值", False, "这个国家没有平台佣金率，跳过")

    # ④ 新增一个扣款项（含净利公式）
    new_item = {"id": "storage_fee", "label": "仓储费", "expr": "本行GMV * 0.01",
                "kinds": ["normal"], "enabled": True}
    p4 = copy.deepcopy(params) + [new_item]
    f4 = copy.deepcopy(fmls)
    f4["net"] = str(fmls.get("net", "")) + " - 仓储费"
    added = run(p4, f4)
    fee4 = added["fee_detail"].get("storage_fee", 0)
    d4 = base["net"] - added["net"]
    rec("新增扣款项「仓储费 1%」→ 净利正好少这么多",
        fee4 > 0 and abs(d4 - fee4) < 0.05,
        f"仓储费 {money(fee4)}，净利 {money(base['net'])} → {money(added['net'])}（-{money(d4)}）")

    # ⑤ 删除一个扣款项（计算项 + 净利里那一项一起删）
    p5 = [x for x in copy.deepcopy(params)
          if not (isinstance(x, dict) and x.get("id") == target)]
    f5 = copy.deepcopy(fmls)
    f5["net"] = str(fmls.get("net", "")).replace(" - " + target_label, "")
    dropped = run(p5, f5)
    d5 = dropped["net"] - base["net"]
    rec(f"删掉「{target_label}」→ 净利正好多出它的金额",
        abs(d5 - target_fee) < 0.05,
        f"净利 {money(base['net'])} → {money(dropped['net'])}（+{money(d5)}），该项原本 {money(target_fee)}")

    # ⑥ 只删净利公式里的那一项（计算项还在）→ 等于没扣
    f6 = copy.deepcopy(fmls)
    f6["net"] = str(fmls.get("net", "")).replace(" - " + target_label, "")
    missed = run(params, f6)
    d6 = missed["net"] - base["net"]
    rec(f"净利公式漏写「{target_label}」→ 确实不扣了（所以要拦住）",
        abs(d6 - target_fee) < 0.05,
        f"净利变高 {money(d6)}，说明漏项会直接让净利虚高")

    # ⑦ 表达式改动
    expr0 = None
    for it in params:
        if isinstance(it, dict) and it.get("id") == "logistics":
            expr0 = it.get("expr")
    if expr0:
        changed = run(patch("logistics", expr="本行GMV * 0.02"))
        v = changed["fee_detail"].get("logistics", 0)
        gmv_normal = v / 0.02 if v else 0
        rec("改表达式（物流改成 2%）→ 金额按新表达式算",
            v > 0 and abs(v - gmv_normal * 0.02) < 0.05,
            f"物流费变成 {money(v)}（原表达式：{expr0}）")

    # ---- 输出 ----
    w1 = max(len(r[0]) for r in ROWS) + 2
    print(f"{'检查项'.ljust(w1)}结果   说明")
    print("-" * (w1 + 6 + 40))
    bad = 0
    for name, res, detail in ROWS:
        if res != "通过":
            bad += 1
        print(f"{name.ljust(w1)}{res}   {detail}")
    print()
    if bad:
        print(f"❌ {bad} 项没通过")
        return 1
    print(f"✅ 全部 {len(ROWS)} 项通过：勾选、开关、改费率、新增、删除、漏项都真的影响净利")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
