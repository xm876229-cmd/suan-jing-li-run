"""命令行入口。

三种运行方式共用本模块：
    1. 命令行     python -m src.cli --workbook "V1-净利  -15.xlsx" --from 2026-10-01 --to 2026-10-31
    2. 一键       scripts\\run.bat（本质是带默认参数的调用）
    3. 定时任务   Windows 任务计划程序调用 scripts\\run.bat scheduled

数据来源两种：
    --workbook  直接读主工作簿里的原始表（历史数据 / 对账）
    --dir       读一个放满导出文件的目录（自动化产线）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.calc import compute, load_rates  # noqa: E402
from src.loader import load_from_dir, load_from_files, load_from_workbook  # noqa: E402
from src.report import build_report  # noqa: E402


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"无法识别的日期：{s}（示例：2026-10-01）")


def load_stores(path: Path) -> dict:
    if not path.exists():
        return {"stores": [], "defaults": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="suan-jing-li-run",
        description="按商品（链接）核算净利润，输出 Excel 报表",
    )
    src = ap.add_argument_group("数据来源（三选一）")
    src.add_argument("--workbook", type=Path, help="主工作簿路径（旧版，读其中的原始工作表）")
    src.add_argument("--dir", type=Path, help="导出文件目录（含订单/广告/联盟/映射四类文件）")
    src.add_argument("--orders", type=Path, help="订单导出文件")
    src.add_argument("--ads", type=Path, help="广告花费导出文件")
    src.add_argument("--commissions", type=Path, help="联盟佣金导出文件")
    src.add_argument("--mapping", type=Path, help="SKU→商品ID 映射表（如「找id用的.xlsx」）")
    src.add_argument("--costs", type=Path, help="成本表（编码/成本两列的表，或含「成本」工作表的工作簿）")

    ap.add_argument("--country", default=None, help="核算国家：马来西亚 / 菲律宾 / 泰国")
    ap.add_argument("--store", default=None, help="店铺 id（读取 config/stores.json）")
    ap.add_argument("--from", dest="dfrom", type=parse_date, help="起始日期，含")
    ap.add_argument("--to", dest="dto", type=parse_date, help="结束日期，含")
    ap.add_argument("--date-field", choices=["created", "paid"], default="created",
                    help="按哪个时间字段筛选区间（默认订单创建时间）")
    ap.add_argument("--out", type=Path, help="输出 Excel 路径；不填则自动命名到 output/")
    ap.add_argument("--rates", type=Path, default=ROOT / "config" / "rates.json")
    ap.add_argument("--stores", type=Path, default=ROOT / "config" / "stores.json")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    if not args.workbook and not args.dir and not args.orders:
        print("请用 --workbook、--dir 或 --orders 指定数据来源")
        return 2

    # 国家 / 店铺
    stores_cfg = load_stores(args.stores)
    country = args.country
    store_note = ""
    if args.store:
        for s in stores_cfg.get("stores", []):
            if s.get("id") == args.store:
                country = country or s.get("country")
                store_note = f"{s.get('name', '')}（{s.get('id')}）"
                break
        else:
            print(f"config/stores.json 里没有 id 为 {args.store} 的店铺")
            return 2
    country = country or stores_cfg.get("defaults", {}).get("country") or "马来西亚"

    rates, switches = load_rates(args.rates)
    if country not in rates:
        print(f"config/rates.json 里没有「{country}」的参数。")
        print(f"已配置的国家：{'、'.join(rates)}")
        print(f"新增方法：在 {args.rates} 的 countries 下加一段即可，键名与 --country 保持一致。")
        return 2
    params = rates[country]

    # 读数据
    if args.workbook:
        if not args.workbook.exists():
            print(f"找不到工作簿：{args.workbook}")
            return 2
        data = load_from_workbook(args.workbook, params.usd_to_local)
        source = args.workbook.name
    elif args.orders:
        if not args.orders.exists():
            print(f"找不到订单文件：{args.orders}")
            return 2
        data = load_from_files(args.orders, args.ads, args.commissions,
                               args.mapping, args.costs, params.usd_to_local)
        source = "、".join(data.sources.values()) or args.orders.name
    else:
        if not args.dir.exists():
            print(f"找不到目录：{args.dir}")
            return 2
        data = load_from_dir(args.dir, args.costs, params.usd_to_local)
        source = args.dir.name

    if not data.orders:
        print("没有解析出任何订单行，请检查订单文件的表头（需要 Order ID / Seller SKU / Quantity 等列）")
        return 1

    res = compute(data, params, switches, args.dfrom, args.dto, args.date_field)
    if not res.products:
        print("区间内没有可核算的订单，请检查日期范围或数据文件")
        return 1

    # 输出路径
    period = "全量"
    if args.dfrom or args.dto:
        period = f"{args.dfrom or '起'}~{args.dto or '今'}"
    stamp = f"{args.dfrom or 'all'}_{args.dto or 'all'}".replace("-", "")
    out = args.out or (ROOT / "output" / f"净利报表_{country}_{stamp}.xlsx")

    subtitle = f"{country}｜{period}｜{source}"
    extra = {
        "数据来源": source,
        "区间": period,
        "时间字段": "订单创建时间" if args.date_field == "created" else "付款时间",
        "订单行数": str(res.line_count),
        "订单数": str(res.order_count),
        "店铺": store_note or "-",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = build_report(res, res.lines, out, switches, subtitle, extra)

    t = res.total
    print(f"已生成：{path}")
    print(f"  GMV        {t.gmv:>12,.2f} {country[:2]}")
    print(f"  净利        {t.net_local:>12,.2f}  （人民币 {res.net_cny_total:,.2f}）")
    print(f"  净利率      {t.net_margin:>11.2%}")
    print(f"  商品数      {len(res.products):>12,}")
    print(f"  异常        {len(res.issues):>12,} 条（详见报表「异常」页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
