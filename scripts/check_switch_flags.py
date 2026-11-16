"""参数表的「启用」不能被代码偷偷改掉，三份参数表也必须一致。

为什么要立这一支（2026-11-16）
----------------------------
超级优惠券曾经被一行代码强行关掉：

    it["enabled"] = bool(switches.super_coupon_enabled)      # calc.py 旧版

`switches.super_coupon_enabled` 出厂默认 false、界面没有任何入口，
而界面上「计算项」里那一行的“启用”勾**显示是勾着的**。
于是马来西亚店填了 1.08% 的费率从来不算：每期少扣约 1.08% GMV，
MYT-J1 一期虚高 3755.54、MYT-V1 虚高 243.96。
（根源是照抄模板《V1-净利》净利润页公式末尾的 `*0`。）

现在定了：**填了就扣，不想扣就填 0**，界面那个勾说了算。

这一支盯住三件事：
    ① 配置里没写 enabled=False 的参数，算出来也必须是启用的
       —— 谁再写一行“强制关闭”，这里立刻红
    ② 三份参数表（主计算 / 明细含样品 / 明细不含样品）的启用状态必须一致
       —— 漏同步会让明细列出汇总没扣的钱
    ③ 超级优惠券必须真的参与计算（默认启用）

跑法：
    python -m scripts.check_switch_flags
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import calc, formula  # noqa: E402
from src.loader import RawData, SkuIndex  # noqa: E402

bad: list[str] = []


def make_params() -> calc.Params:
    return calc.Params(
        country="测试国", cny_to_local=1.0, usd_to_local=1.0,
        dispatch_fee_cny=0.0, dispatch_fee_local=0.0,
        platform_commission=0.0, tx_fee=0.0, cashback=0.0,
        super_coupon=0.0108, growth_fee=0.0, logistics=0.0, order_service=0.0,
    )


def run() -> calc.CalcResult:
    """跑一次空数据的核算，只为拿到三份算完的参数表（不需要订单）。"""
    fcfg = copy.deepcopy(formula.DEFAULTS)
    fcfg["exclude_zero_amount"] = False
    data = RawData(orders=[], costs={}, sku_index=SkuIndex(),
                   ads={}, commissions={}, order_titles=[], window=None)
    return calc.compute(data, make_params(), calc.Switches(),
                        None, None, "created", fcfg=fcfg, with_compare=False)


def on_off(cfg) -> dict:
    return {str(it.get("id")): bool(it.get("enabled", True))
            for it in (cfg or []) if isinstance(it, dict) and it.get("id")}


def check() -> None:
    res = run()
    main = on_off(res.params_cfg)
    with_s = on_off(res.cfg_with_sample)
    without_s = on_off(res.cfg_without_sample)

    # ① 没人被偷偷关掉
    off = sorted(k for k, v in main.items() if not v)
    if off:
        bad.append(f"① 这些参数算出来是「关闭」状态，但配置里没写 enabled=False："
                   f"{'、'.join(off)}（多半是代码里又加了强制关闭）")

    # ② 三份一致
    for name, other in (("明细·含样品", with_s), ("明细·不含样品", without_s)):
        diff = sorted(k for k in main if other.get(k) != main[k])
        if diff:
            bad.append(f"② {name}的启用状态跟主计算不一致：{'、'.join(diff)}"
                       f"（明细会列出汇总没扣的费用）")

    # ③ 超级优惠券必须真的算
    if not main.get("super_coupon", True):
        bad.append("③ 超级优惠券被关掉了。填了费率就该扣，不想扣就把费率填 0"
                   "（别再拿 switches 当第二个开关）")


def main() -> int:
    check()
    if bad:
        print("[缺失] 参数表启用状态：")
        for b in bad:
            print("       -", b)
    else:
        print("[OK]   参数表启用状态：没人被偷偷关掉，三份一致，超级优惠券参与计算")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
