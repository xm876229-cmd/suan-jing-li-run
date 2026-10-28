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



COST_DIR = ROOT / "data" / "costs"


def pick_costs(explicit: Path | None) -> tuple[Path | None, str]:
    """决定这次用哪份成本表。

    优先用 data/costs/master.csv（主表，增量合并过、带变更流水）。
    没有主表但目录里有原始成本表时，先自动导入一次再算，不用手工执行命令。
    """
    if explicit:
        return explicit, "命令行指定"

    master = COST_DIR / "master.csv"
    if not master.exists() and COST_DIR.exists():
        raw = [
            f for f in COST_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv")
            and not f.name.startswith("~$")
        ]
        if raw:
            try:
                from scripts.import_costs import main as import_main  # 延迟导入，避免循环
                src = max(raw, key=lambda f: f.stat().st_mtime)
                print(f"首次使用：正在把 {src.name} 导入主成本表…")
                import_main([str(src)])
            except Exception as e:
                print(f"⚠ 自动导入成本表失败：{e}")

    if master.exists():
        try:
            with master.open("r", encoding="utf-8-sig") as f:
                n = max(0, sum(1 for _ in f) - 1)
        except OSError:
            n = 0
        return master, f"主成本表（{n} 条）"

    if COST_DIR.exists():
        raw = [
            f for f in COST_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv")
            and not f.name.startswith("~$")
        ]
        if raw:
            newest = max(raw, key=lambda f: f.stat().st_mtime)
            return newest, "直接取目录里最新的一份（未建主表）"
        return None, "data/costs 里没有成本表"
    return None, "未找到 data/costs 目录"



MAPPING_DIR = ROOT / "data" / "mapping"
INBOX_DIR = ROOT / "data" / "exports" / "_inbox"



def refresh_link_info() -> None:
    """主工作簿更新过就重新导出「链接资料」表。

    这张表是店铺导出的当前全部链接（实测 1382 条 SKU），
    比接口取到的全得多，是映射的最佳来源。但它只反映"现在"，
    删减过的旧 SKU 不会出现——所以必须与累积主表配合使用。

    只在新工作簿出现时才读它：打开大工作簿要一两秒，
    每次算账都读一遍没有必要。
    """
    try:
        from scripts.import_link_info import OUT, find_workbook, main as link_main

        cands = [
            f for f in ROOT.glob("*.xlsx")
            if f.is_file() and not f.name.startswith("~$")
        ]
        if not cands:
            return
        newest = max(cands, key=lambda f: f.stat().st_mtime)
        if OUT.exists() and newest.stat().st_mtime <= OUT.stat().st_mtime:
            return

        wb = find_workbook()
        if not wb:
            return
        print(f"检测到工作簿更新（{wb.name}），重新导出「链接资料」…")
        link_main([str(wb)])
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 链接资料导出失败：{e}")


def pick_mapping(explicit: Path | None) -> tuple[Path | None, str]:
    """决定这次用哪份 SKU→商品 映射。

    优先用累积主表 data/mapping/master.csv。
    链接一编辑，SKU 归属就可能变，所以每次取数都会产生新的 sku_list_*.json；
    这里把比主表更新的那些合并进去（增量，幂等），再交主表去算。
    这样新商品当天就能算对，老 SKU 因为链接改过而消失的也不会丢归属。
    """
    if explicit:
        return explicit, "命令行指定"

    # 先看看主工作簿的「链接资料」表有没有更新（它是当前全量映射的最佳来源）
    refresh_link_info()

    master = MAPPING_DIR / "master.csv"

    # 候选文件的判断统一交给 import_mapping，避免两处规则不一致
    # （扩展可能产出 sku_list_*.json，也可能产出 sku_export_*.xlsx）
    fresh: list[Path] = []
    try:
        from scripts.import_mapping import find_sources
        fresh = [
            f for f in find_sources()
            if not master.exists() or f.stat().st_mtime > master.stat().st_mtime
        ]
    except Exception:
        fresh = []

    if fresh:
        try:
            from scripts.import_mapping import main as import_main  # 延迟导入，避免循环
            print(f"映射有更新，合并进主表：{'、'.join(f.name for f in fresh)}")
            import_main([str(f) for f in fresh])
        except Exception as e:
            print(f"⚠ 映射合并失败：{e}")

    if master.exists():
        try:
            with master.open("r", encoding="utf-8-sig") as f:
                n = max(0, sum(1 for _ in f) - 1)
        except OSError:
            n = 0
        return master, f"累积主表（{n} 条）"
    return None, "未建累积主表（本次用导出目录里的映射表，链接改过后可能缺老 SKU 归属）"


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
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON 摘要（供本地桥调用）")
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

    costs_path, costs_note = pick_costs(args.costs)
    if costs_path:
        print(f"成本表：{costs_path.name}（{costs_note}）")
    else:
        print(f"⚠ {costs_note}，成本将按 0 计，净利会偏高")

    map_path, map_note = pick_mapping(args.mapping)
    if map_path:
        print(f"映射表：{map_path.name}（{map_note}）")
    else:
        print(f"⚠ {map_note}")

    # 读数据
    if args.workbook:
        if not args.workbook.exists():
            print(f"找不到工作簿：{args.workbook}")
            return 2
        data = load_from_workbook(args.workbook, params.usd_to_local, costs_path)
        source = args.workbook.name
    elif args.orders:
        if not args.orders.exists():
            print(f"找不到订单文件：{args.orders}")
            return 2
        data = load_from_files(args.orders, args.ads, args.commissions,
                               map_path, costs_path, params.usd_to_local)
        source = "、".join(data.sources.values()) or args.orders.name
    else:
        if not args.dir.exists():
            print(f"找不到目录：{args.dir}")
            return 2
        data = load_from_dir(args.dir, costs_path, params.usd_to_local, map_path)
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
        "成本表": f"{costs_path.name}（{costs_note}）" if costs_path else "未提供，成本按 0 计",
        "映射表": f"{map_path.name}（{map_note}）" if map_path else "未提供",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = build_report(res, res.lines, out, switches, subtitle, extra)

    t = res.total

    if getattr(args, "json", False):
        print(json.dumps({
            "ok": True,
            "file": str(path),
            "country": country,
            "period": period,
            "gmv": round(t.gmv, 2),
            "net_local": round(t.net_local, 2),
            "net_cny": round(res.net_cny_total, 2),
            "net_margin": round(t.net_margin, 6),
            "products": len(res.products),
            "orders": res.order_count,
            "lines": res.line_count,
            "issues": len(res.issues),
            "sources": data.sources,
        }, ensure_ascii=False))
        return 0

    print(f"已生成：{path}")
    print(f"  GMV        {t.gmv:>12,.2f} {country[:2]}")
    print(f"  净利        {t.net_local:>12,.2f}  （人民币 {res.net_cny_total:,.2f}）")
    print(f"  净利率      {t.net_margin:>11.2%}")
    print(f"  商品数      {len(res.products):>12,}")
    print(f"  异常        {len(res.issues):>12,} 条（详见报表「异常」页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
