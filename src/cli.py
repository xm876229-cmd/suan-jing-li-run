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
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

# 源码模式要把项目根加进 sys.path；打包后模块已在 exe 里，不需要也不能这么做
if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from src import paths  # noqa: E402
from src import provenance as prove  # noqa: E402
from src.calc import compute, load_rates  # noqa: E402
from src.loader import (load_from_dir, load_from_dirs, load_from_files,  # noqa: E402
                        load_from_workbook)
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



ROOT = paths.ROOT
COST_DIR = paths.COSTS


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



MAPPING_DIR = paths.MAPPING
INBOX_DIR = paths.INBOX



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


def pick_mapping(explicit: Path | None, store: str = "") -> tuple[Path | None, str]:
    """决定这次用哪份 SKU→商品 映射。

    优先用累积主表 data/mapping/master.csv。
    链接一编辑，SKU 归属就可能变，所以每次取数都会产生新的 sku_list_*.json；
    这里把比主表更新的那些合并进去（增量，幂等），再交主表去算。
    这样新商品当天就能算对，老 SKU 因为链接改过而消失的也不会丢归属。

    店铺目录下的「商品SKU表」同样要归档进主表——那是每轮取数新导出来的，
    在核算时是第一选择（见 loader.load_from_dirs），主表只负责兜底；
    但换了电脑、或者那张表被清掉之后，能兜底的也就只有主表了。
    """
    if explicit:
        return explicit, "命令行指定"

    # 先看看主工作簿的「链接资料」表有没有更新（它是当前全量映射的最佳来源）
    refresh_link_info()

    master = MAPPING_DIR / "master.csv"
    pending = MAPPING_DIR / "master.new.csv"

    # 上一轮如果因为「主表正被 Excel 打开」没能替换成功，结果留在 master.new.csv。
    # 这里先试着把它顶回主表。不顶的话这个文件会一直躺着，
    # 每次核算都提示「主表被占用，用的是待写入的那份」——
    # 用户以为有文件被锁着，其实早就没人占用了。
    # 真被占着就顶不上，按下面的逻辑继续用 pending，不影响这次核算。
    try:
        from scripts.import_mapping import _promote_pending
        _promote_pending()
    except Exception:  # noqa: BLE001
        pass

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # 判断「有没有更新」要跟**实际在用的那份**比。
    # 主表被占用时新数据都在 master.new.csv，还拿老主表的时间戳去比，
    # 每轮都会把同一批文件重新合并一遍（大店要好几秒）。
    base_mt = max(_mtime(master), _mtime(pending))

    # 候选文件的判断统一交给 import_mapping，避免两处规则不一致
    # （扩展可能产出 sku_list_*.json，也可能产出 sku_export_*.xlsx）
    fresh: list[Path] = []
    try:
        from scripts.import_mapping import find_sources
        fresh = [f for f in find_sources() if _mtime(f) > base_mt]
    except Exception:
        fresh = []

    # 各店铺的「商品SKU表」也一起归档
    if store:
        try:
            d = paths.store_root(store) / "商品SKU表"
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if (f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv")
                            and not f.name.startswith("~$")
                            and _mtime(f) > base_mt):
                        fresh.append(f)
        except Exception:  # noqa: BLE001
            pass

    if fresh:
        try:
            from scripts.import_mapping import main as import_main  # 延迟导入，避免循环
            print(f"映射有更新，合并进主表：{'、'.join(f.name for f in fresh)}")
            import_main([str(f) for f in fresh])
        except Exception as e:
            print(f"⚠ 映射合并失败：{e}")

    # 主表可能被 Excel/WPS 占着替换不掉，那一轮的结果落在 master.new.csv。
    # 读的时候要认得它，否则「合并成功了但报表还用老归属」，而且没有任何提示。
    use = master
    try:
        if pending.exists() and master.exists() and pending.stat().st_mtime >= master.stat().st_mtime:
            use = pending
        elif pending.exists() and not master.exists():
            use = pending
    except OSError:
        pass

    if use.exists():
        try:
            with use.open("r", encoding="utf-8-sig") as f:
                n = max(0, sum(1 for _ in f) - 1)
        except OSError:
            n = 0
        note = f"累积主表（{n} 条）"
        if use.name != "master.csv":
            note += "，主表被占用，用的是待写入的那份"
        return use, note
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
    src.add_argument("--snapshot", default="",
                    help="用哪一份取数快照（目录名，见 data/stores/<店>/snapshots）。"
                         "不填就用当前 inbox 的实时数据")
    src.add_argument("--snapshots", default="",
                    help="合并多份快照一起算，目录名用逗号分隔（重叠订单以最新为准）。")
    src.add_argument("--orders", type=Path, help="订单导出文件")
    src.add_argument("--ads", type=Path, help="广告花费导出文件")
    src.add_argument("--commissions", type=Path, help="联盟佣金导出文件")
    src.add_argument("--mapping", type=Path, help="SKU→商品ID 映射表（如「找id用的.xlsx」）")
    src.add_argument("--costs", type=Path, help="成本表（编码/成本两列的表，或含「成本」工作表的工作簿）")

    ap.add_argument("--country", default=None, help="核算国家：马来西亚 / 菲律宾 / 泰国")
    ap.add_argument("--store", default=None, help="店铺 id（读取 config/stores.json）")
    ap.add_argument("--fx", choices=["auto", "manual"], default=None,
                    help="汇率来源：auto 用核算日前一天（T-1）的汇率"
                         "（取不到依次退回本机存档、最新价、配置值）；"
                         "manual 只用 config/rates.json 里的值。不填则按 fx.mode 走")
    # 特殊口径：--fx-day today 用当天汇率（默认 prev=T-1）。只给排障用，界面上不暴露。
    ap.add_argument("--fx-day", choices=["prev", "today"], default=None,
                    help="用哪天的汇率：prev=T-1（默认），today=当天")
    ap.add_argument("--from", dest="dfrom", type=parse_date, help="起始日期，含")
    ap.add_argument("--to", dest="dto", type=parse_date, help="结束日期，含")
    ap.add_argument("--date-field", choices=["created", "paid"], default="created",
                    help="按哪个时间字段筛选区间（默认订单创建时间）")
    ap.add_argument("--out", type=Path, help="输出 Excel 路径；不填则自动命名到 output/")
    ap.add_argument("--with-formula", action="store_true",
                    help="导出「带公式」版：明细页多出订单类别/是否样品两组辅助列，"
                         "汇总页的净利用 SUMIFS 引用明细，改「口径」页的开关即切换口径")
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON 摘要（供本地桥调用）")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="把 JSON 摘要写到这个文件。打包版是无控制台窗口模式，"
                         "子进程的 stdout 是空的（sys.stdout 为 None），"
                         "桥拿不到摘要——写文件才靠得住")
    ap.add_argument("--final-name", default="",
                    help="报表的最终文件名（核算先写临时文件再改名，缓存公式版要用最终名）")
    ap.add_argument("--allow-partial", action="store_true",
                    help="数据不全也照算（默认拒绝：缺表的报表数字必然不对）")
    ap.add_argument("--merge-all", action="store_true",
                    help="同一目录里累计的多份表全部参与合并（手动核算用）。"
                         "默认只取最新导出的那一份——那是自动取数的规矩，"
                         "而手动抽屉本来就是「攒着几批表，按区间挑着算」。")
    ap.add_argument("--batch", default="",
                    help="只用某一次取数的文件重算（批次号见界面「店铺 → 取数批次」）。"
                         "带上它时，区间以那一批自己的区间为准，收件箱后来的文件不参与。")
    ap.add_argument("--rates", type=Path, default=paths.RATES_FILE)
    ap.add_argument("--stores", type=Path, default=paths.STORES_FILE)
    return ap


def _declared_for(store_id: str, stores_cfg: dict) -> set[str]:
    """这家店显式声明「本来就没有」的类别。

    只认两种：不投广告（no_ads）、没有联盟佣金（no_affiliate）。
    为什么必须显式声明：报表上「没有广告表」和「广告费为 0」长得一模一样，
    而后者会让净利凭空变高。默认一律拦住，让店主自己说「我家不投广告」。
    声明位置二选一：config/stores.json 里那条 `no_ads: true`，
    或者 数据/店铺/<店>/meta.json 里的同名键。
    """
    out: set[str] = set()
    entry = next((s for s in (stores_cfg.get("stores") or [])
                  if str(s.get("id") or "") == str(store_id)), None) or {}
    meta: dict = {}
    try:
        _mf = paths.store_meta_file(store_id)
        if _mf.is_file():
            meta = json.loads(_mf.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    for key, kind in (("no_ads", "ads"), ("no_affiliate", "affiliate")):
        if entry.get(key) or meta.get(key):
            out.add(kind)
    return out


def _missing_kinds(data) -> list[str]:
    """这一轮有什么数据是**没真正用上**的（缺 / 区间不对 / 解析出 0 行）。

    为什么默认就不让算：缺一块的报表**不会报错**，只会悄悄把那一块按 0 计——
    联盟佣金缺席 → 净利虚高；广告缺席 → 净利虚高；SKU 缺席 → 广告费摊不到链接上。
    数字看着正常，拿去对账就是错的。宁可不出表。

    判据是 `data.uses`，**不是 `data.sources`**：
    sources 只说明「这类表认出来了」，广告表认出来了却被区间筛掉时，
    sources 里照样有名字，闸门就放行了——PHT-W1 拿 8 月广告算 7 月
    那个错，就是顺着这个口子出的报表。
    """
    uses = getattr(data, "uses", None) or {}
    if not uses:
        # 老调用方（工作簿）没填 uses，退回按 sources 判
        have = getattr(data, "sources", None) or {}
        miss: list[str] = []
        for key, label in (("订单", "订单表"), ("映射", "商品SKU表"),
                           ("广告", "广告表"), ("联盟佣金", "联盟佣金表")):
            if key not in have:
                miss.append(label)
        return miss

    blocks, _notes = prove.gate(uses, getattr(data, "window", None),
                                 getattr(data, "declared", set()))
    return blocks


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    if not args.workbook and not args.dir and not args.orders and not args.batch:
        print("请用 --workbook、--dir 或 --orders 指定数据来源（也可以给 --batch 用某一批取数）")
        return 2

    # 国家 / 店铺
    stores_cfg = load_stores(args.stores)
    country = args.country
    store_note = ""
    if args.store:
        hit = next((s for s in stores_cfg.get("stores", [])
                    if s.get("id") == args.store), None)
        if hit:
            country = country or hit.get("country")
            store_note = f"{hit.get('name', '')}（{hit.get('id')}）"
        else:
            # 扩展自动识别出来的店铺只写了 stores/<店名>/meta.json，
            # 未必在 stores.json 里。以前这里直接报错退出，新店第一次核算就卡死。
            # 按 meta 认（店名 + 国家都在里面），认不出来才真的报错。
            _meta: dict = {}
            try:
                _mf = paths.store_meta_file(args.store)
                if _mf.is_file():
                    _meta = json.loads(_mf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _meta = {}
            if str(_meta.get("shop_id") or "") != str(args.store):
                print(f"config/stores.json 里没有 id 为 {args.store} 的店铺，"
                      f"也没有对应的数据目录（stores/…/meta.json）")
                return 2
            country = country or _meta.get("country") or ""
            store_note = f"{_meta.get('shop_name') or args.store}（{args.store}）"
    country = country or stores_cfg.get("defaults", {}).get("country") or "马来西亚"

    rates, switches = load_rates(args.rates)
    if country not in rates:
        print(f"config/rates.json 里没有「{country}」的参数。")
        print(f"已配置的国家：{'、'.join(rates)}")
        print(f"新增方法：在 {args.rates} 的 countries 下加一段即可，键名与 --country 保持一致。")
        return 2
    params = rates[country]

    # 汇率必须在读数据之前定下来：广告花费是美金额，换算要用这一次的汇率，
    # 否则同一份数据两次跑会算出两个结果。
    try:
        from src import fx
        fx_cfg = fx.load_fx_cfg(args.rates)
        note = fx.apply_to_params(params, country, fx_cfg, mode_override=args.fx,
                                  log=None if args.json else print,
                                  day=None if getattr(args, "fx_day", None) != "today"
                                  else date.today())
        if not args.json:
            print(f"汇率：{note}")
    except Exception as e:  # noqa: BLE001
        # 汇率不是致命项：抓不到就用配置里的固定值，账照算，报表里会写明来源
        params.fx_note = f"汇率获取失败，使用配置里的固定值（{e}）"
        if not args.json:
            print(f"⚠ {params.fx_note}")

    # 广告账号本位币：导出表里的 LOCAL / 空，都按它换算（默认 USD）。
    # 实测：店铺 MYR、广告账号 USD 时，LOCAL 指的是 USD —— 不换算就少算 4 倍广告费。
    from src import loader as _ld
    # 每国可设：auto（默认，先看导出表写的币种）/ USD / LOCAL
    from src import fx as _fxmod
    _ld.set_ads_currency(getattr(params, "ads_currency", "") or "AUTO",
                         getattr(params, "currency", ""),
                         _fxmod.last_rates())

    # 这个国家生效的配置：参数表、订单表标题、成本表列标题、净利公式。
    # 公式里写的是订单表的列标题，所以解析订单之前就得知道这套标题。
    from src import formula as _formula
    fcfg = _formula.load_config(paths.CONFIG / "formulas.json")
    cc = _formula.country_config(fcfg, country)
    order_fields = cc.get("order_fields") or {}
    cost_fields = cc.get("cost_fields") or {}
    ads_fields = cc.get("ads_fields") or {}
    affiliate_fields = cc.get("affiliate_fields") or {}
    mapping_fields = cc.get("mapping_fields") or {}

    # 快照：指定后就只用那一批文件算，而不是当前 inbox。
    # inbox 是「实时」的，扩展每取一次就多一份文件，而核算同类只取最新一份——
    # 想重算某一批老数据，就必须能锁定那批文件，不然只会对着最新的一直算。
    snap_note = ""
    snap_dirs: list[Path] = []
    snap_list: list[str] = []
    _sid = str(args.store or "").strip()
    _snap_ids = [s.strip() for s in str(getattr(args, "snapshots", "") or "").split(",") if s.strip()]
    if _snap_ids:
        from src import snapshots
        _meta = []
        for _sid_item in _snap_ids:
            _d = snapshots.resolve(_sid, _sid_item)
            if _d is None:
                print(f"找不到快照：{_sid_item}（店铺 {_sid or '未指定'}）")
                return 2
            snap_dirs.append(_d)
            snap_list.append(snapshots.describe(_sid, _sid_item))
            _meta.append(snapshots.manifest(_sid, _sid_item) or {})
        # 合并顺序按取数时间从旧到新：重叠的订单以最新那份的状态为准
        snap_dirs.sort(key=lambda p: str(p.name))
        snap_note = " + ".join(snap_list)
    elif getattr(args, "snapshot", ""):
        from src import snapshots
        _d = snapshots.resolve(_sid, args.snapshot)
        if _d is None:
            print(f"找不到快照：{args.snapshot}（店铺 {_sid or '未指定'}）")
            return 2
        args.dir = _d
        snap_note = snapshots.describe(_sid, args.snapshot)

    costs_path, costs_note = pick_costs(args.costs)
    if costs_path:
        print(f"成本表：{costs_path.name}（{costs_note}）")
    else:
        print(f"⚠ {costs_note}，成本将按 0 计，净利会偏高")
    map_path, map_note = pick_mapping(args.mapping, str(args.store or ""))
    if map_path:
        print(f"映射表：{map_path.name}（{map_note}）")
    else:
        print(f"⚠ {map_note}")

    # ---- 「用这批重算」：只认那一批落盘的文件 ----
    #
    # 批次是「同店 + 同区间 + 同一天」取的那几类表。带上它时：
    #   · 区间用那一批自己的（不接受外面再传一个对不上的区间）；
    #   · 只读那一批的文件，收件箱后来落的文件一概不参与——这样才叫复现；
    #   · 不再从快照补文件、不再自动转换广告表（那些都会往目录里加新文件）。
    _only: set[str] = set()
    if str(getattr(args, "batch", "") or "").strip():
        from src import capture as _cap
        _bid = str(args.batch).strip()
        _parsed = _cap.parse_batch_id(_bid)
        if not _parsed:
            print(f"批次号看不懂：{_bid}（格式应为 日期__起__止，例如 2026-11-04__2026-07-01__2026-07-31）")
            return 2
        _day, _a, _b = _parsed
        _files = _cap.batch_files(_sid, _bid)
        if not _files:
            print(f"这一批（{_day} 取的 {_a}~{_b}）的文件都不在了，没法重算。请重新取一次数。")
            return 2
        try:
            args.dfrom = date.fromisoformat(_a)
            args.dto = date.fromisoformat(_b)
        except ValueError:
            print(f"批次里的区间不合法：{_a}~{_b}")
            return 2
        if not args.dir:
            args.dir = paths.store_inbox(paths.store_root(_sid))
        _only = {str(Path(f).resolve()) for f in _files}
        print(f"只使用 {_day} 那一批取数的 {len(_files)} 个文件（{_a}~{_b}）")

    # 文件夹里的数据源被删了也不怕：程序自己留着快照，缺了就补回来。
    # （快照是权威副本，文件夹里那些只是给人看的）
    if _sid and not getattr(args, "snapshot", "") and not args.workbook and not _only:
        try:
            from src import snapshots
            _restored = snapshots.restore_inbox(_sid)
            if _restored:
                print(f"已从最新快照补回 {_restored} 个数据文件")
        except Exception:  # noqa: BLE001
            pass

    # 这家店声明「本店不投广告 / 没有联盟佣金」时才允许缺这两类；
    # 没声明就一律拦住——报表上「没有广告表」和「广告费 0」看起来一模一样。
    _declared = _declared_for(str(args.store or ""), stores_cfg)

    # 读数据
    if args.workbook:
        if not args.workbook.exists():
            print(f"找不到工作簿：{args.workbook}")
            return 2
        data = load_from_workbook(args.workbook, params.usd_to_local, costs_path,
                                  order_fields=order_fields, cost_fields=cost_fields,
                                  ads_fields=ads_fields, affiliate_fields=affiliate_fields,
                                  mapping_fields=mapping_fields)
        source = args.workbook.name
    elif args.orders:
        if not args.orders.exists():
            print(f"找不到订单文件：{args.orders}")
            return 2
        data = load_from_files(args.orders, args.ads, args.commissions,
                               map_path, costs_path, params.usd_to_local,
                               order_fields=order_fields, cost_fields=cost_fields,
                               ads_fields=ads_fields, affiliate_fields=affiliate_fields,
                               mapping_fields=mapping_fields)
        source = "、".join(data.sources.values()) or args.orders.name
    else:
        # 广告表如果是新版后台的「计划维度」导出（没有商品ID），先转成按商品，
        # 否则广告费进不了报表。老版后台直接给商品维度，这一步会自动跳过。
        # 「用这批重算」时不能转：转换会往目录里写新文件，这一批就不复现了。
        try:
            if _only:
                raise RuntimeError("按批次重算，跳过自动转换")
            from scripts.import_ads_campaign import autoconvert_in_store
            for _n in autoconvert_in_store(str(args.store or "")):
                print(f"广告表转换：{_n}")
        except Exception as _e:  # noqa: BLE001
            print(f"⚠ 广告表转换跳过：{_e}")

        # 目录清单 = 本次要算的数据（快照或 inbox），再补上「店铺根目录的 SKU 表」。
        #
        # 为什么要补：订单表里**没有链接 ID**，只有 Seller SKU 和 SKU ID；
        # 链接 ID 要靠 SKU ID 去「商品SKU表」里查。而那张表存在
        # `stores/<店>/商品SKU表/`，不在 inbox 里，以前压根没被扫到，
        # 于是新店铺整列「商品ID / 款式编码」都是空的。
        base_dirs = list(snap_dirs) if snap_dirs else [args.dir]
        _sku_tables = None
        try:
            if args.store:
                from src import paths as _paths
                _cand = _paths.store_root(str(args.store)) / "商品SKU表"
                if _cand.is_dir():
                    _sku_tables = _cand
                    base_dirs = base_dirs + [_cand]
        except Exception:  # noqa: BLE001
            _sku_tables = None

        if snap_dirs or _sku_tables:
            # 多目录合并：跨目录去重后再汇总（SKU 表只贡献映射，不贡献订单）
            # window 告诉加载器这次算哪一段：广告/佣金表是整段汇总的，
            # 同时存在「9月」「9月~10月」两份时要挑最贴合的那份，否则费用串月。
            _win = None
            if args.dfrom and args.dto:
                _win = (args.dfrom.isoformat(), args.dto.isoformat())
            # latest_only 的意思是「只取最新导出的那一份」，那是自动取数的规矩。
            # 手动核算传了 --merge-all，就不能再挑一份了——哪怕这家店有
            # 「商品SKU表」目录（手动核算也会传 --store，好套用这家店的费率）。
            data = load_from_dirs(base_dirs, costs_path, params.usd_to_local, map_path,
                                  order_fields=order_fields, cost_fields=cost_fields,
                                  ads_fields=ads_fields, affiliate_fields=affiliate_fields,
                                  mapping_fields=mapping_fields, window=_win,
                                  latest_only=not (snap_dirs or args.merge_all),
                                  shop_id=str(args.store or ""), declared=_declared,
                                  only=_only or None)
        else:
            if not args.dir.exists():
                print(f"找不到目录：{args.dir}")
                return 2
            # 这一段以前直接 load_from_dir(不带区间、只取最新一份)。两个后果：
            #   · 手动抽屉里攒了 8 月、9 月两份订单时，算 8 月会拿到 9 月那份，
            #     报「区间内没有可核算的订单」；
            #   · 选「全部」时只算最新那一份，旧月份的订单直接消失，
            #     数字偏小还不报错。
            # 手动核算显式带 --merge-all，就把目录里所有导出合并去重再按区间筛；
            # 自动取数不加这个参数，仍然是「只用最新一份」的老规矩。
            _win = None
            if args.dfrom and args.dto:
                _win = (args.dfrom.isoformat(), args.dto.isoformat())
            data = load_from_dirs([args.dir], costs_path, params.usd_to_local, map_path,
                                  order_fields=order_fields, cost_fields=cost_fields,
                                  ads_fields=ads_fields, affiliate_fields=affiliate_fields,
                                  mapping_fields=mapping_fields, window=_win,
                                  latest_only=not args.merge_all,
                                  shop_id=str(args.store or ""), declared=_declared,
                                  only=_only or None)
        source = snap_note or args.dir.name

    if not data.orders:
        print("没有解析出任何订单行，请检查订单文件的表头（需要 Order ID / Seller SKU / Quantity 等列）")
        return 1

    # 数据不全就不出报表。缺一块的报表不会报错，只会把那一块按 0 计，
    # 数字看着正常，拿去对账是错的——这比没有报表更坏。
    miss = _missing_kinds(data)
    if miss and not getattr(args, "allow_partial", False):
        print("数据不全，不出报表：")
        for _m in miss:
            print(f"  · {_m}")
        print("（缺任一类都会让数字偏掉。补齐后再算；确实要凑合看，加 --allow-partial）")
        return 3
    if miss and getattr(args, "allow_partial", False):
        print("⚠ 数据不全，按 --allow-partial 强行出表（数字不可对账）：")
        for _m in miss:
            print(f"  · {_m}")

    res = compute(data, params, switches, args.dfrom, args.dto, args.date_field, fcfg=fcfg)
    if not res.products:
        print("区间内没有可核算的订单，请检查日期范围或数据文件")
        return 1

    # 区间内一单都没有：多半是订单表还是**上一批**的（自动取数没落地，
    # 收件箱里躺着上个月那份）。照算会得到「GMV 0、净利是一堆费用的负数」，
    # 数字没意义却看着像报表，拿去对账就是错的。宁可不出。
    try:
        _gmv0 = float(getattr(res.total, "gmv", 0) or 0)
        _ord0 = float(getattr(res.total, "orders", 0) or 0)
    except Exception:  # noqa: BLE001
        _gmv0 = _ord0 = 0.0
    if _gmv0 <= 0 and _ord0 <= 0:
        print("区间内没有订单（GMV 为 0）：订单表可能还是上一批的，这次不出报表")
        print("（等这次的订单表落地后再算；确实要看，加 --allow-partial）")
        if not getattr(args, "allow_partial", False):
            return 1

    # 输出路径
    period = "全量"
    if args.dfrom or args.dto:
        period = f"{args.dfrom or '起'}~{args.dto or '今'}"
    stamp = f"{args.dfrom or 'all'}_{args.dto or 'all'}".replace("-", "")
    out = args.out or (paths.OUTPUT / f"净利报表_{country}_{stamp}.xlsx")

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
    # 每类数据**实际用了哪一份、盖的哪一段**，写进报表里。
    # 这是对账时唯一能自证的地方：报表数字对不对，先看这一页的区间和文件名。
    try:
        extra.update(prove.summarize(getattr(data, "uses", {}) or {}))
    except Exception:  # noqa: BLE001
        pass
    if getattr(params, "fx_note", ""):
        extra["汇率"] = str(params.fx_note)
    _ac = str(getattr(params, "ads_currency", "") or "").upper()
    if _ac:
        extra["广告账号币种"] = ("自动识别（表里写 LOCAL/空的按美元）"
                                if _ac in ("", "AUTO") else _ac)
    if snap_note:
        extra["数据快照"] = snap_note
    if len(snap_dirs) > 1:
        extra["合并说明"] = (f"合并 {len(snap_dirs)} 份快照；"
                             f"同一订单的同一 SKU 只算一次，重叠部分以取数时间最新的为准")
    _want_formula = bool(getattr(args, "with_formula", False))
    path = build_report(res, res.lines, out, switches, subtitle, extra,
                        with_formula=_want_formula,
                        report_columns=cc.get("report_columns"),
                        total_position=cc.get("report_total_position", "bottom"),
                        report_titles=cc.get("report_titles"))

    # ── 顺手把「带公式版」也生成好，缓存起来 ──
    #
    # 为什么：以前点「导出带公式」是**重新跑一遍完整核算**（大店要等一两分钟），
    # 用户以为是"导出慢"，其实是重算。这里算一次、多写一个 xlsx，
    # 下载时直接拷缓存，秒出。
    #
    # 缓存放在 报表\_公式缓存\<房间>\<店>\ 下（下划线开头，报表列表不显示）。
    _leaf = str(getattr(args, "final_name", "") or "").strip() or path.name
    # 只有输出确实落在「报表\自动|手动\<店>\」下才做缓存。
    # 缓存目录是按输出路径的上级目录名拼的：发布校验把报表写到系统临时目录时，
    # 也会在 _公式缓存\v<版本>\Temp\jingli_formula_xxxx\ 下留一份十几 MB 的副本。
    _cache_ok = True
    try:
        _rel_parts = path.resolve().relative_to(paths.OUTPUT.resolve()).parts
        _cache_ok = len(_rel_parts) >= 3
    except Exception:  # noqa: BLE001
        _cache_ok = False
    # 缓存按版本分目录：`_公式缓存/v0.24.76/<房间>/<店>/<报表>`。
    # 不分版本会出事：0.24.75 把「链接毛利率」合计的 SUMIFS 写坏（单参数），
    # 用户升级后如果只是下载、没有重算，拷到的还是上一版那份坏缓存。
    _cache = (paths.OUTPUT / "_公式缓存" / f"v{paths.app_version() or 'dev'}"
              / path.parent.parent.name / path.parent.name / _leaf)
    if not _want_formula and _cache_ok:
        try:
            # 名字要用**最终**报表名：桥那边是先算到 `.<pid>.calc.xlsx` 再原子改名，
            # 照临时名缓存，下载时就找不到（缓存全叫 `.12345.calc.xlsx`）。
            _cache.parent.mkdir(parents=True, exist_ok=True)
            build_report(res, res.lines, _cache, switches, subtitle, extra,
                         with_formula=True,
                         report_columns=cc.get("report_columns"),
                         total_position=cc.get("report_total_position", "bottom"),
                         report_titles=cc.get("report_titles"))
        except Exception as e:  # noqa: BLE001
            # 缓存失败绝不能影响主报表：主报表已经写好了
            print(f"[warn] 带公式版缓存失败（不影响主报表）：{e}", file=sys.stderr)
    else:
        # 主表本身就是带公式版 —— 直接拷一份到缓存位。
        # 不拷的话，缓存会停在**上一次不带公式那次**生成的内容：
        # 实测 2026-11-04 重出 9 月（带公式）后，缓存还是 14:30 那份，
        # 里面还有「排名」列和改名前的「达人佣金」，用户下载带公式版拿到的是旧的。
        try:
            _cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, _cache)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 刷新公式版缓存失败：{e}", file=sys.stderr)

    t = res.total

    # --json-out 单独给的时候也要产出摘要：写了文件却因为没有 --json 而跳过，
    # 调用方拿到的就是「报表算出来了、摘要不见了」，打包版尤其难查
    # （stdout 是空的，只能靠这个文件）。
    if getattr(args, "json", False) or getattr(args, "json_out", None):
        line = json.dumps({
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
        }, ensure_ascii=False)
        # 直接写 UTF-8 字节，不走 stdout 的文本编码。
        # 打包成 exe 后，中文 Windows 下 stdout 是 GBK，且 PYTHONIOENCODING
        # 不一定生效——那样本地桥按 UTF-8 解析会拿到一堆替换符，
        # 国家名、文件名全变乱码。这里绕开编码层，结果就是确定的。
        # 写文件这条路是给打包版用的：windowed 模式下 sys.stdout 是 None，
        # 下面那段 stdout 写入会直接失败（实测桥收到 0 字节）。
        if getattr(args, "json_out", None):
            try:
                out_p = Path(args.json_out)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(line, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] JSON 摘要写文件失败：{e}", file=sys.stderr)
        try:
            sys.stdout.flush()
            sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
        except (AttributeError, OSError, ValueError):
            try:
                print(line)
            except Exception:  # noqa: BLE001
                pass
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
