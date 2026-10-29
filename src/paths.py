"""统一路径解析：源码运行与打包运行都能正确工作。

为什么需要这个模块：
    原来每个文件都写 `ROOT = Path(__file__).resolve().parents[1]`。
    这在源码模式下没问题，但 PyInstaller 打包后：
      · 代码被塞进 exe，`__file__` 指向的还是源码路径（甚至不存在）
      · 资源（config、extension 模板）和可写数据（data、output）必须在真实目录里
    所以凡是需要"程序目录"的地方，都改从这里取。

两种模式：
    源码   python -m src.cli …        ROOT = 项目根目录
    打包   净利助手.exe run …          ROOT = exe 所在目录

数据目录可以用环境变量挪走（多台机器共享一个数据盘时有用）：
    JINGLI_DATA=C:\\jingli-data
    JINGLI_OUTPUT=D:\\净利报表
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序目录：源码模式是项目根，打包模式是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()

# 用户要能一眼看出各文件夹是干嘛的，所以对外可见的目录一律中文名。
# 代码包（src / bridge / scripts）保持英文：那是 Python 的包路径，
# 改了名字所有 import 都得跟着改，收益远小于风险。
DIR_DATA = "数据"
DIR_OUTPUT = "报表"
DIR_CONFIG = "配置"
DIR_EXTENSION = "浏览器扩展"


def _pick_dir(cn: str, en: str) -> Path:
    """用中文目录名；发现还是旧的英文目录就顺手改名搬过去。

    这样升级后老用户的数据不会被留在一个没人认得的小角落，
    而新装的用户看到的就是「数据 / 报表 / 配置 / 浏览器扩展」。
    改名失败（文件被占用等）就继续用老目录，绝不因此丢数据。
    """
    new = ROOT / cn
    old = ROOT / en
    if new.is_dir():
        return new
    if old.is_dir():
        try:
            old.rename(new)
            return new
        except OSError:
            return old
    return new


# ---- 可写数据 ----
DATA = Path(os.environ.get("JINGLI_DATA") or _pick_dir(DIR_DATA, "data")).resolve()
CAPTURE = DATA / "capture"
EXPORTS = DATA / "exports"
INBOX = EXPORTS / "_inbox"
COSTS = DATA / "costs"
MAPPING = DATA / "mapping"
LOGS = DATA / "logs"

# 多店铺：一家店一个目录，互不干扰
#   data/stores/<shop_id>/inbox/    这家店的原始导出（按类型分子目录）
#   data/stores/<shop_id>/meta.json 店名、国家、最后活动时间
#   data/stores/_unknown/<时间戳>/   认不出店铺时的落点（不混进任何已知店）
STORES_ROOT = DATA / "stores"
UNKNOWN_ROOT = STORES_ROOT / "_unknown"

# 店里 inbox 下按数据类型分子目录：文件多起来才不乱
KIND_DIRS = ("orders", "ads", "affiliate", "mapping")

# 表头识别出来的类型名 → 目录名。两套名字要对上，
# 否则会冒出 order/ 和 orders/ 两种目录，看起来像两批数据。
KIND_BY_TABLE = {"order": "orders", "ads": "ads",
                  "affiliate": "affiliate", "mapping": "mapping"}

# ---- 产出 ----
OUTPUT = Path(os.environ.get("JINGLI_OUTPUT") or _pick_dir(DIR_OUTPUT, "output")).resolve()

# ---- 只读资源 ----
CONFIG = _pick_dir(DIR_CONFIG, "config")
EXTENSION = _pick_dir(DIR_EXTENSION, "extension")
RATES_FILE = CONFIG / "rates.json"
STORES_FILE = CONFIG / "stores.json"


def store_dir(shop_id: str) -> Path:
    """某家店的数据目录。shop_id 为空时退回旧的公共目录。"""
    sid = (shop_id or "").strip()
    if not sid:
        return INBOX
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return STORES_ROOT / (safe or "unknown") / "inbox"


def kind_dirname(table_kind: str) -> str:
    """把「表头认出来的类型」换算成目录名（order → orders）。"""
    k = (table_kind or "").strip().lower()
    if k in KIND_BY_TABLE:
        return KIND_BY_TABLE[k]
    return k if k in KIND_DIRS else "orders"


def store_kind_dir(shop_id: str, kind: str) -> Path:
    """某家店 inbox 下的分类子目录。

    分类型存放是为了「一眼看出这家店的数据齐不齐」：
    缺哪一类，目录就是空的，不用去翻文件名猜。
    """
    return store_dir(shop_id) / kind_dirname(kind)


def store_reports_dir(shop_id: str) -> Path:
    """某家店的报表目录。"""
    sid = (shop_id or "").strip()
    if not sid:
        return OUTPUT
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return STORES_ROOT / (safe or "unknown") / "reports"


def unknown_dir(stamp: str = "") -> Path:
    """认不出店铺时的落点：data/stores/_unknown/<时间戳>/。

    单独隔出来是有原因的：以前识别失败会静默落公共目录，
    多店铺一起跑时数据会混在一起，谁也说不清哪份数据属于哪家店。
    """
    s = (stamp or "").strip() or datetime.now().strftime("%Y%m%d-%H%M%S")
    return UNKNOWN_ROOT / s


def store_meta_file(shop_id: str) -> Path:
    return store_dir(shop_id).parent / "meta.json"


def ensure_dirs() -> None:
    """创建所有需要的目录。打包后首次运行必须调用一次。"""
    for d in (DATA, CAPTURE, EXPORTS, INBOX, COSTS, MAPPING, LOGS, OUTPUT,
              STORES_ROOT, UNKNOWN_ROOT):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 只读目录（比如装在 Program Files 又没权限）不该让整个程序崩掉，
            # 后续写入失败时会给出更具体的错误
            pass


def describe() -> dict[str, str]:
    """诊断用：把关键路径一次性列出来。"""
    return {
        "运行模式": "打包 exe" if is_frozen() else "源码",
        "程序目录": str(ROOT),
        "数据目录": str(DATA),
        "产出目录": str(OUTPUT),
        "配置目录": str(CONFIG),
        "扩展目录": str(EXTENSION),
    }
