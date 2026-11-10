"""把所有「版本」同步到一致。

为什么要这个脚本
----------------
同一个程序在机器上有多份，改一处不同步，就会出现「明明改了、刷新了、还是旧的」：

    项目目录\\浏览器扩展                         ← 开发时改的这份（源头）
    release\\净利助手\\浏览器扩展                 ← 免安装版，浏览器里加载的常常是这份
    build\\app\\jingli\\浏览器扩展                ← PyInstaller 中间产物
    %LOCALAPPDATA%\\JingLiHelper\\extension      ← 安装版

配置（各国费率、计算公式、商品成本）也一样：删了要能自动补回，就必须同步进
各版本的 `_internal\\默认配置`、`_internal\\默认数据`。

用法
----
    python -m scripts.sync_all             # 只同步（扩展 / 配置 / 文档），秒级
    python -m scripts.sync_all --build     # 先重打 exe 与安装包，再同步（几分钟）
    python -m scripts.sync_all --check     # 只看差异，不动文件

原则：**只覆盖、不删除**。任何一份目标目录里多出来的东西都不动，
免得把运行中的数据（数据/报表/快照）当成旧文件清掉。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT_SRC = ROOT / "浏览器扩展"

# 配置 / 成本表的「正本」在哪：
# 0.24.44 起数据跟着程序目录走，用户在界面里改的费率、公式、成本表
# 都写在 release\净利助手\ 下；项目根那份是迁移前的残留。
# 这里以前直接指向项目根 —— 于是每次「同步」都把几个月前的旧配置塞进
# 各版本的 _internal\默认配置：用户重装后恢复出来的是旧公式，还查不出来。
_LIVE = ROOT / "release" / "净利助手"
CONFIG_SRC = (_LIVE / "配置"
              if (_LIVE / "配置" / "formulas.json").is_file()
              else ROOT / "配置")
# 数据子目录 0.24.4 起改中文名；这里写死英文的话会**静默空跑**——
# 每次同步都说成功，其实 `_internal\默认数据` 一直是老内容。
COST_SRC = ((_LIVE / "数据" / "成本" / "master.csv")
            if (_LIVE / "数据" / "成本" / "master.csv").is_file()
            else ROOT / "数据" / "成本" / "master.csv")
DOCS = ("CHANGELOG.md", "README.md")

# 各版本的落点。不存在就跳过（换台机器、只装了其中一种都正常）。
def targets() -> list[dict]:
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    installed = home / "JingLiHelper"
    out = [
        {"name": "免安装版", "dir": ROOT / "release" / "净利助手", "ext": "浏览器扩展"},
        {"name": "构建中间产物", "dir": ROOT / "build" / "app" / "jingli", "ext": "浏览器扩展"},
        {"name": "安装版", "dir": installed, "ext": "extension"},
    ]
    # 安装版也有中文目录名的老布局，两种都认
    if (installed / "浏览器扩展").is_dir():
        out[2]["ext"] = "浏览器扩展"
    return [t for t in out if Path(t["dir"]).is_dir()]


def _manifest_version(p: Path) -> str:
    try:
        return str(json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _copy_tree_files(src: Path, dst: Path) -> list[str]:
    """只覆盖同名文件，不删目标里多出来的东西。返回相对路径列表。"""
    changed: list[str] = []
    if not src.is_dir():
        return changed
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        # 备份/临时文件不进「默认配置」：程序读坏配置时会回退到 .bak，
        # 而默认配置里的 .bak 是同步那一刻的旧值，恢复出来反而更错。
        if f.name.endswith((".bak", ".part", ".tmp")) or f.name.startswith("."):
            continue
        rel = f.relative_to(src)
        t = dst / rel
        try:
            if t.is_file() and t.stat().st_size == f.stat().st_size \
                    and int(t.stat().st_mtime) >= int(f.stat().st_mtime):
                continue
        except OSError:
            pass
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        changed.append(str(rel))
    return changed


def check() -> int:
    src_v = _manifest_version(EXT_SRC)
    print(f"源扩展版本：{src_v}")
    print(f"配置源：{CONFIG_SRC}")
    print(f"成本源：{COST_SRC}")
    bad = 0
    for t in targets():
        d = Path(t["dir"])
        ext = d / t["ext"]
        v = _manifest_version(ext)
        flag = "一致" if v == src_v else "不一致"
        if v != src_v:
            bad += 1
        exe = next(iter(d.glob("*.exe")), None)
        stamp = ""
        if exe:
            import datetime as _dt
            stamp = _dt.datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {t['name']:<10} 扩展 {v or '无':<8} [{flag}]　exe {stamp or '无'}")
    return bad



# ---------------- 各版本「正在用」的配置：只并口径，不整份覆盖 ----------------
#
# 每个版本都有自己的 `配置\formulas.json`（跟着自己的程序目录走，绝不共用）。
# 但新版加了订单类别（售前取消 / 售后已发货 / 售后未发货），如果只更新
# `_internal\默认配置`，用户不重装就永远用不到新口径——三个版本算出来的数
# 还不一样。这里把「口径」这一小块并进各自的活配置：
#   · kinds 名称表：以正本为准
#   · 每个参数的 kinds：**只加不减**（正本有的类别补进去，用户自己勾的不动）
# 其余字段（费率、公式、成本、报表列…）一个字都不碰。
_RULE_PARAM_IDS = ("cost", "order_count", "items", "dispatch")


def _merge_rule_kinds(src_cfg: dict, dst_cfg: dict) -> int:
    n = 0
    s_kinds = src_cfg.get("kinds") or {}
    d_kinds = dst_cfg.setdefault("kinds", {})
    for k, v in s_kinds.items():
        if d_kinds.get(k) != v:
            d_kinds[k] = v
            n += 1
    src_params = {str(it.get("id")): it for it in (src_cfg.get("params") or [])
                  if isinstance(it, dict)}
    for it in dst_cfg.get("params") or []:
        if not isinstance(it, dict):
            continue
        sp = src_params.get(str(it.get("id")))
        if not sp:
            continue
        want = list(sp.get("kinds") or [])
        if not want:
            continue
        have = list(it.get("kinds") or [])
        add = [k for k in want if k not in have]
        if add:
            it["kinds"] = have + add
            n += len(add)
    return n


# 报表新增的列：同步进各版本「正在用」的列清单。
# 只加不删，位置固定在「订单数」后面；用户自己排的顺序不动。
_NEW_COLS = ("pre_cancel_orders", "post_cancel_orders", "post_cancel_share",
             "all_cancel_share", "post_cancel_total_share")


def _merge_report_cols(src_cfg: dict, dst_cfg: dict) -> int:
    src_cols = src_cfg.get("report_columns")
    if not isinstance(src_cols, list):
        return 0
    n = 0
    for key in ("report_columns", "report_column_order"):
        dst = dst_cfg.get(key)
        if not isinstance(dst, list):
            continue
        anchor = "orders"
        for k in _NEW_COLS:
            if k in src_cols and k not in dst:
                i = dst.index(anchor) + 1 if anchor in dst else len(dst)
                dst.insert(i, k)
                n += 1
    return n


def _merge_order_fields(src_cfg: dict, dst_cfg: dict) -> int:
    """把新增的订单表字段（物流单号/物流商/发货时间…）补进各版本配置。

    字段清单以**代码里的默认值**为准，不读正本配置：正本自己也是用户可编辑的，
    万一哪次没同步上，就等于谁都拿不到（历史踩过：物流三列只在代码里，
    界面看不见、平台改名就静默读空）。

    只补**缺的键**，已有的标题候选一个字都不动——各版本可能自己加过候选，
    粗暴覆盖会让用户的改动消失。新字段追加在末尾，界面按这个顺序展示。
    """
    try:
        from src.formula import DEFAULT_ORDER_FIELDS as src_of
    except Exception:  # noqa: BLE001
        src_of = src_cfg.get("order_fields")
    if not isinstance(src_of, dict):
        return 0
    dst_of = dst_cfg.setdefault("order_fields", {})
    if not isinstance(dst_of, dict):
        return 0
    n = 0
    for k, v in src_of.items():
        if k in dst_of:
            continue
        dst_of[k] = v
        n += 1
    return n



def _strip_schedules(path: Path) -> bool:
    """把「默认配置」里的计划任务清空。

    计划是这台机器自己的安排（几点跑、跑哪几家店），不该当产品默认值发出去。
    2026-11-04 装机时就把开发机的测试计划带进了安装版，店主从没设过，
    却每天 15:04 自动出 8 月报表。这里只清 schedules，其它设置一律不动。
    """
    try:
        import json as _json
        d = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(d, dict) or not d.get("schedules"):
        return False
    d["schedules"] = []
    try:
        from src import safeio
        safeio.write_json(path, d, indent=1)
    except Exception:  # noqa: BLE001
        try:
            path.write_text(_json.dumps(d, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        except OSError:
            return False
    return True


def sync_live_rules() -> int:
    """把正本里的订单类别并进每个版本自己那份活配置。"""
    src = CONFIG_SRC / "formulas.json"
    if not src.is_file():
        return 0
    try:
        src_cfg = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    total = 0
    for t in targets():
        f = Path(t["dir"]) / "配置" / "formulas.json"
        if not f.is_file():
            continue
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"  {t['name']:<10} 配置读不出来，跳过（{f}）")
            continue
        n = _merge_rule_kinds(src_cfg, cfg)
        n += _merge_report_cols(src_cfg, cfg)
        n += _merge_order_fields(src_cfg, cfg)
        if not n:
            continue
        try:
            from src import safeio
            safeio.write_json(f, cfg, indent=1)
        except Exception:  # noqa: BLE001
            f.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
        print(f"  {t['name']:<10} 配置 → 口径同步 {n} 项（{f}）")
        total += n
    return total


def sync(build: bool) -> int:
    # 打包前先查一遍脚本符号：界面/扩展里「只有调用、没有定义」的函数
    # 不会让语法检查报错，但运行时会静默失败（v0.24.10 的 fileToB64 就是这样）。
    # 放在 build 之前：坏了就别打包，省得又发一版带病的出去。
    try:
        from scripts import check_ext
        if check_ext.main() != 0:
            print("\n脚本符号检查没通过，先修完再打包。")
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"（脚本符号检查跳过：{e}）")

    if build:
        print("重打包（exe + 安装包 + 免安装 zip）…")
        r = subprocess.run([sys.executable, "-m", "scripts.build_app"], cwd=str(ROOT))
        if r.returncode != 0:
            print("打包失败，后面的同步照做（至少扩展和配置能先对齐）")

    src_v = _manifest_version(EXT_SRC)
    print(f"源扩展版本：{src_v}")
    for t in targets():
        d = Path(t["dir"])
        ext = d / t["ext"]
        changed = _copy_tree_files(EXT_SRC, ext)
        print(f"  {t['name']:<10} 扩展 → {_manifest_version(ext) or '无'}"
              f"（更新 {len(changed)} 个文件）")

        # 默认配置 / 默认成本：程序启动时会拿它们补回被删掉的配置
        for sub, src in (("_internal/默认配置", CONFIG_SRC),
                         ("_internal/默认数据/costs", COST_SRC.parent)):
            dst = d / sub
            if not (d / "_internal").is_dir() and sub.startswith("_internal"):
                continue
            n = _copy_tree_files(src, dst)
            if n:
                print(f"             {sub}：更新 {len(n)} 个")
            if sub.endswith("默认配置"):
                # 默认配置里不能带这台机器的计划（见 _strip_schedules 注释）
                try:
                    if _strip_schedules(dst / "settings.json"):
                        print(f"             {sub}：清掉随包的计划（计划只属于本机）")
                except Exception:  # noqa: BLE001
                    pass
            # _copy_tree_files 只覆盖不删除，所以历史上混进「默认配置」的 .bak
            # 会一直躺着。程序读坏配置时会回退到 .bak，那是打包当天的旧值，
            # 比没有更糟——这里主动清掉。
            if sub.endswith("默认配置") and dst.is_dir():
                stale = 0
                for b in (list(dst.glob("*.bak")) + list(dst.glob("*.bak_*"))
                          + list(dst.glob("*.tmp"))):
                    try:
                        b.unlink()
                        stale += 1
                    except OSError:
                        pass
                if stale:
                    print(f"             {sub}：清掉 {stale} 个过期的 .bak")

        # 数据子目录改名（英文 → 中文）
        # 为什么在这里做：改名原本只在「程序启动」时发生，而 release / 安装版
        # 可能一直没被新版打开过，于是用户打开文件夹一看还是 stores / capture。
        # 同步时顺手迁一遍，谁也不用等谁。
        try:
            from src import paths as _paths
            for sub in ("数据", "data"):
                moved = _paths.migrate_data_dirs(d / sub)
                if moved:
                    print(f"             数据目录改中文：{len(moved)} 处")
        except Exception as e:  # noqa: BLE001
            print(f"             数据目录改名跳过：{e}")

        # 文档
        for name in DOCS:
            f = ROOT / name
            if f.is_file():
                try:
                    shutil.copy2(f, d / name)
                except OSError:
                    pass

    # 安装版平时跑的就是 exe / 顶层源码：只同步扩展不改程序，
    # 会出现「界面是新的、算出来的数是旧的」。这一步不能省。
    try:
        from scripts import sync_installed
        sync_installed.sync(verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"  安装版程序同步失败（忽略）：{e}")

    # 店铺改名：每个版本改自己的 数据/店铺 目录（不共享数据），
    # 只把「shop_id → 名字」对齐。用户在某个版本界面里改了名，
    # 订单类别（口径）并进每个版本自己那份活配置。
    # 放在收尾前：它要读正本、写各版本，跟扩展/默认配置互不影响。
    try:
        print("\n订单类别（口径）同步：")
        _n = sync_live_rules()
        if not _n:
            print("  各版本已经一致，无需改动")
    except Exception as e:  # noqa: BLE001
        print(f"  口径同步失败（忽略）：{e}")

    # 另外两个版本如果不同步，就会出现「界面显示新名、文件夹还是旧名」。
    # 放在最后一步：前面同步配置时可能已经把 stores.json 覆盖成本机这份了。
    try:
        from scripts import sync_store_names
        print("\n店铺名对齐：")
        sync_store_names.main([])
    except Exception as e:  # noqa: BLE001
        print(f"  店铺名对齐失败（忽略）：{e}")

    print("\n完成。浏览器里还要点一次扩展的「刷新」才会用上新代码。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步扩展/配置/文档到所有版本")
    ap.add_argument("--build", action="store_true", help="先重打 exe 与安装包")
    ap.add_argument("--check", action="store_true", help="只看差异")
    a = ap.parse_args(argv)
    if a.check:
        return 1 if check() else 0
    return sync(a.build)


if __name__ == "__main__":
    raise SystemExit(main())
