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

import json
import os
import re
import shutil
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


def _data_root() -> Path:
    """可写数据根（配置 / 数据 / 报表）。

    为什么要跟「程序目录」分开：以前 ROOT = exe 所在目录，
    换个地方启动 exe（安装目录 / release / build/app/jingli）就换一份配置——
    从构建目录启动时读到的是空的 配置\，界面就变成 0 国 0 店。

    查找顺序：
      1. JINGLI_HOME 环境变量：把数据钉死在某处
      2. 从程序目录向上找 `数据目录.txt`（第一行 = 数据根路径）：
         项目里放一个，release/、build/app/ 下启动的 exe 都会指回同一份数据
      3. 兜底：源码 = 项目根；打包 = exe 所在目录（兼容安装版/便携版）
    """
    env = (os.environ.get("JINGLI_HOME") or "").strip()
    if env:
        return Path(env).expanduser().resolve()

    start = app_root()
    p = start
    for _ in range(6):
        f = p / "数据目录.txt"
        try:
            if f.is_file():
                lines = f.read_text(encoding="utf-8-sig").strip().splitlines()
                if lines:
                    t = Path(lines[0].strip().strip('"')).expanduser()
                    if not t.is_absolute():
                        t = p / t
                    t = t.resolve()
                    if t.is_dir():
                        return t
        except OSError:
            pass
        if p.parent == p:
            break
        p = p.parent
    return start


ROOT = app_root()
# 可写数据（配置/数据/报表）的根：默认跟程序目录一致；
# 可用 JINGLI_HOME 或上层「数据目录.txt」固定到一处，让所有实例共用同一份。
DATA_HOME = _data_root()

# 用户要能一眼看出各文件夹是干嘛的，所以对外可见的目录一律中文名。
# 代码包（src / bridge / scripts）保持英文：那是 Python 的包路径，
# 改了名字所有 import 都得跟着改，收益远小于风险。
DIR_DATA = "数据"
DIR_OUTPUT = "报表"
DIR_CONFIG = "配置"
DIR_EXTENSION = "浏览器扩展"


def _pick_dir(cn: str, en: str, base: Path | None = None) -> Path:
    """用中文目录名；发现还是旧的英文目录就顺手改名搬过去。

    这样升级后老用户的数据不会被留在一个没人认得的小角落，
    而新装的用户看到的就是「数据 / 报表 / 配置 / 浏览器扩展」。
    改名失败（文件被占用等）就继续用老目录，绝不因此丢数据。

    base 默认是可写数据根（配置/数据/报表都放这）；
    只读资源（浏览器扩展）传 base=ROOT，跟着程序目录走。
    """
    base = base or DATA_HOME
    new = base / cn
    old = base / en
    if new.is_dir():
        return new
    if old.is_dir():
        try:
            old.rename(new)
            return new
        except OSError as e:
            # 目录里有文件正被别的进程打开（比如桥还开着日志）时改不了名。
            # 不改也不影响使用，只是这个名字先留英文；下次启动会再试一次。
            print(f"[目录] 暂时改不了名（可能有程序占用）：{en} → {cn}（{e}）")
            return old
    return new


# ---- 可写数据 ----

def _merge_into(src: Path, dst: Path) -> None:
    """把 src 里剩下的东西搬进 dst（同名的跳过），搬空了就把 src 删掉。

    只在迁移窗口用：老版本程序还在往英文目录写的时候，别让那批文件丢掉。
    锁着的文件跳过，下次启动再搬。
    """
    try:
        for item in list(src.iterdir()):
            target = dst / item.name
            try:
                if item.is_dir():
                    if target.is_dir():
                        _merge_into(item, target)
                    elif target.exists():
                        continue
                    else:
                        item.rename(target)
                else:
                    if not target.exists():
                        item.rename(target)
                    else:
                        # 同名：留新的，老的加后缀
                        item.rename(target.with_name(
                            f"{target.stem}.old{target.suffix}"))
            except OSError:
                continue
        try:
            src.rmdir()
        except OSError:
            pass
    except OSError:
        pass


# 数据根下面这些子目录，一律用中文名。
# 老版本是英文的，升级时自动改名搬过来；改不了（文件被占用）就继续用英文名，
# 下次启动再试——绝不为了好看去动数据。
SUB_DIRS: dict[str, str] = {
    "抓包记录": "capture",
    "导出": "exports",
    "成本": "costs",
    "链接映射": "mapping",
    "日志": "logs",
    "店铺": "stores",
}

# 这几层是「目录套目录」，也得跟着改：导出/_inbox、店铺/<店>/inbox
NESTED_DIRS: dict[str, str] = {
    "收件箱": "_inbox",
}

STORE_INBOX = ("收件箱", "inbox")          # 每家店自己的收件箱


def _cn_sub(parent: Path, cn: str, en: str) -> Path:
    """数据下面的子目录：用中文名，老版本是英文的就改名迁过来。

    改名只在**新名字不存在**时做，失败就继续用老名字，绝不丢数据。
    """
    new = parent / cn
    old = parent / en
    if new.is_dir():
        # 改名过程中「新旧两个都在」是可能的：老版本还在跑、又往英文目录写了东西。
        # 把英文目录里剩下的搬进中文目录，避免那批数据变成「看不见」。
        if old.is_dir():
            _merge_into(old, new)
        return new
    if old.is_dir():
        try:
            old.rename(new)
            print(f"[目录] 已改名为中文：{en} → {cn}")
            return new
        except OSError as e:
            print(f"[目录] 暂时改不了名（可能有程序占用）：{en} → {cn}（{e}）")
            return old
    return new


def migrate_data_dirs(root: Path | str, *, verbose: bool = False) -> list[str]:
    """把某个数据根下面的英文子目录全部改成中文名。

    为什么要单独做成函数：目录改名原来只发生在「程序 import paths 的那一刻」，
    而且只对**当前这个数据根**生效。于是出现过一个很迷惑的现象——
    程序里已经是「数据\\店铺」，但 release 目录里躺着的那份还是 stores，
    用户打开文件夹一看：「怎么还是英文？」

    现在打包、同步、启动都调这一个函数，谁也不用等谁。
    返回动过的目录（相对路径 + 改了什么），调用方可以打进日志。

    注意：**只改名，不删除**。两边都有内容就合并，同名的留新的、旧的加 .old。
    """
    root = Path(root)
    moved: list[str] = []
    if not root.is_dir():
        return moved

    for cn, en in SUB_DIRS.items():
        new, old = root / cn, root / en
        if new.is_dir():
            if old.is_dir():
                _merge_into(old, new)
                moved.append(f"{en} → {cn}（合并了剩余文件）")
            continue
        if old.is_dir():
            try:
                old.rename(new)
                moved.append(f"{en} → {cn}")
            except OSError as e:
                if verbose:
                    print(f"[目录] 改不了名（可能被占用）：{en} → {cn}（{e}）")
                continue

    # 导出/_inbox → 导出/收件箱
    exports = root / "导出"
    if exports.is_dir():
        for cn, en in NESTED_DIRS.items():
            new, old = exports / cn, exports / en
            if new.is_dir():
                if old.is_dir():
                    _merge_into(old, new)
                continue
            if old.is_dir():
                try:
                    old.rename(new)
                    moved.append(f"导出/{en} → 导出/{cn}")
                except OSError:
                    continue

    # 店铺/<每家店>/inbox → 店铺/<店>/收件箱
    stores = root / "店铺"
    if stores.is_dir():
        cn, en = STORE_INBOX
        for shop in stores.iterdir():
            try:
                if not shop.is_dir():
                    continue
            except OSError:
                continue
            new, old = shop / cn, shop / en
            if new.is_dir():
                if old.is_dir():
                    _merge_into(old, new)
                continue
            if old.is_dir():
                try:
                    old.rename(new)
                    moved.append(f"店铺/{shop.name}/{en} → {cn}")
                except OSError:
                    continue

    for m in moved:
        print(f"[目录] {root.name}: {m}" if verbose else f"[目录] 已改名 {m}")
    return moved


DATA = Path(os.environ.get("JINGLI_DATA") or _pick_dir(DIR_DATA, "data")).resolve()
CAPTURE = _cn_sub(DATA, "抓包记录", "capture")
EXPORTS = _cn_sub(DATA, "导出", "exports")
INBOX = _cn_sub(EXPORTS, "收件箱", "_inbox")
COSTS = _cn_sub(DATA, "成本", "costs")
MAPPING = _cn_sub(DATA, "链接映射", "mapping")
LOGS = _cn_sub(DATA, "日志", "logs")

# 多店铺：一家店一个目录，互不干扰
#   数据/店铺/<店名>/订单表/     这家店的原始导出（按类型分子目录）
#   数据/店铺/<店名>/meta.json   店名、国家、最后活动时间
#   数据/店铺/未识别店铺/<时间>/  认不出店铺时的落点（不混进任何已知店）
STORES_ROOT = _cn_sub(DATA, "店铺", "stores")
UNKNOWN_ROOT = STORES_ROOT / "未识别店铺"

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
EXTENSION = _pick_dir(DIR_EXTENSION, "extension", base=ROOT)
RATES_FILE = CONFIG / "rates.json"
STORES_FILE = CONFIG / "stores.json"


# 单个目录名最长多少。
# Windows 单个路径段上限 255，但**整条路径**只有 260：
#   C://Users//xxx//Desktop//公司资料//净利助手//数据//店铺//<店名>//收件箱//订单表//<文件名>
# 本身就已经一百多个字符了，店名再长一点就爆。
# 60 个字符足够认出是哪家店；超出的截断并加短哈希，
# 免得两个长名字被截成同一个目录。
FOLDER_NAME_MAX = 60


def _safe_folder(name: str, fallback: str) -> str:
    """把名字整理成能当文件夹名的。

    文件系统不允许 \\ / : * ? " < > | 这些字符，中文没问题。
    太长的要截断（见 FOLDER_NAME_MAX 的说明）。
    """
    s = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()).strip(" .")
    if not s:
        return fallback
    if len(s) > FOLDER_NAME_MAX:
        import hashlib
        h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
        keep = FOLDER_NAME_MAX - len(h) - 1
        s = s[:keep].rstrip(" .") + "_" + h
    return s or fallback


_STORE_NAMES: dict[str, str] = {}
_STORE_NAMES_MTIME: float = -1.0


def _load_store_names() -> dict[str, str]:
    """店 id → 店名。

    目录用**店名**而不是一串数字：使用者打开文件夹就能看懂是哪家店。
    店名可以在界面上改，所以同时以 meta.json 里的 shop_id 为准做一次核对——
    改了名也不会认错家（旧目录靠 meta.json 找回）。

    缓存按 stores.json 的修改时间失效。以前是「读到一次就永久记住」，
    于是在界面里改完店名，显示的还是旧名，得重启程序才生效——
    用户会以为没保存成功，反复点。
    """
    global _STORE_NAMES, _STORE_NAMES_MTIME
    try:
        mtime = STORES_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _STORE_NAMES and mtime == _STORE_NAMES_MTIME:
        return _STORE_NAMES

    names: dict[str, str] = {}
    try:
        if STORES_FILE.is_file():
            from src import safeio
            data = safeio.read_json(STORES_FILE, default={}) or {}
            for s in (data.get("stores") or []):
                if not isinstance(s, dict):
                    continue
                sid = str(s.get("id") or s.get("shop_id") or "")
                if sid:
                    names[sid] = str(s.get("name") or sid)
    except Exception:  # noqa: BLE001
        pass
    _STORE_NAMES = names
    _STORE_NAMES_MTIME = mtime
    return _STORE_NAMES


def folder_name(name: str, fallback: str = "未识别店铺") -> str:
    """把用户起的名字变成合法目录名（去掉非法字符、限长、留个兜底）。

    改名要用它算目标目录，保证跟「新建店铺时按名字建目录」是同一套规则，
    否则改完名下次启动又会被认成另一个目录。
    """
    return _safe_folder(name, fallback)


def store_pretty_name(shop_id: str) -> str:
    """这家店对外该叫什么：stores.json 里的别名 → 平台店名 → 店铺 ID。

    别名优先，是因为用户会在程序里把店改成自己认得的名（「马来一号店」），
    平台原名（「Party Pop Hub」）反而认不出来。

    注意跟 `store_display_name()` 的区别：那个是**目录名**（报表分文件夹用），
    这个是**给人看的名字**（新建目录、提示语用）。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return "未识别店铺"
    name = _load_store_names().get(sid, "")
    if name:
        return name
    try:
        meta = store_root(sid) / "meta.json"
        if meta.is_file():
            m = json.loads(meta.read_text(encoding="utf-8"))
            n = str(m.get("shop_name") or "").strip()
            if n:
                return n
    except Exception:  # noqa: BLE001
        pass
    return sid


def store_root(shop_id: str) -> Path:
    """某家店的根目录（里面有 inbox 订单表 等分类目录）。

    优先按 meta.json 里的 shop_id 找已有目录——目录用店名命名，
    而店名会被改；meta 里的 id 才是稳定身份，改名也不怕认错家。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return UNKNOWN_ROOT
    try:
        if STORES_ROOT.is_dir():
            for d in STORES_ROOT.iterdir():
                if not d.is_dir():
                    continue
                meta = d / "meta.json"
                if not meta.is_file():
                    continue
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if str(m.get("shop_id") or "") == sid:
                    return d
    except OSError:
        pass
    name = _load_store_names().get(sid, sid)
    return STORES_ROOT / _safe_folder(name, "未识别店铺")


def store_inbox(store_root_dir: Path | str) -> Path:
    """某家「店目录」下面的收件箱。

    为什么单拿出来：改中文名之后，还有好几处代码在写 `d / "inbox"`。
    它们不报错、只是永远扫不到东西 —— 表现是「店铺文件数是 0」
    「参数页读不到数据」「映射表补不齐」，全都看不出原因。
    这里统一走一遍改名逻辑，中文名/英文旧名都能拿到。
    """
    return _cn_sub(Path(store_root_dir), *STORE_INBOX)


def store_dir(shop_id: str) -> Path:
    """某家店的数据目录（收件箱）。shop_id 为空时退回公共目录。"""
    sid = (shop_id or "").strip()
    if not sid:
        return INBOX
    return _cn_sub(store_root(sid), "收件箱", "inbox")


# 分类子目录用中文：使用者打开文件夹一眼就知道哪张表是什么
KIND_DIR_CN = {
    "orders": "订单表",
    "ads": "广告表",
    "affiliate": "联盟佣金表",
    "mapping": "商品映射",
}


def kind_dirname(table_kind: str) -> str:
    """把「表头认出来的类型」换算成目录名（order → 订单表）。"""
    k = (table_kind or "").strip().lower()
    if k in KIND_BY_TABLE:
        k = KIND_BY_TABLE[k]
    else:
        k = k if k in KIND_DIRS else "orders"
    return KIND_DIR_CN.get(k, k)


def store_kind_dir(shop_id: str, kind: str) -> Path:
    """某家店 inbox 下的分类子目录。

    分类型存放是为了「一眼看出这家店的数据齐不齐」：
    缺哪一类，目录就是空的，不用去翻文件名猜。
    """
    return store_dir(shop_id) / kind_dirname(kind)


def store_index() -> list[dict]:
    """所有认出来的店铺，按店名排好序（稳定顺序）。

    为什么要统一排序：
        驾驶舱和手动核算都是「一店一张卡」。以前两个页面各自用
        `iterdir()` 的原始顺序，还按「有没有数据」往前挪，
        结果在手动核算里导完一批表，卡片就跳到第一位——人刚记住的位置没了。
        现在两边都用这一个函数，顺序只跟店名有关，跟数据、时间、导入次数都无关。

    返回：[{shop_id, dir, name, meta}]，name 是目录名（也是报表文件夹名）。
    """
    out: list[dict] = []
    try:
        dirs = [d for d in STORES_ROOT.iterdir() if d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        meta_file = d / "meta.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        sid = str((meta or {}).get("shop_id") or "").strip()
        if not sid:
            continue
        out.append({"shop_id": sid, "dir": d, "name": d.name, "meta": meta})
    # casefold 让大小写不影响顺序；再用原名兜底，保证同一次运行里绝对稳定
    out.sort(key=lambda x: (x["name"].casefold(), x["name"], x["shop_id"]))
    return out


# 报表分两个房间：报表/自动/<店名>/ 和 报表/手动/<店名>/
# 自动 = 驾驶舱取数 + 卡片「重算」出来的；手动 = 手动核算那批表算出来的。
REPORT_ROOMS = {"auto": "自动", "manual": "手动"}


def store_display_name(shop_id: str) -> str:
    """报表分目录用的店名 = 店目录名。

    为什么不直接用 stores.json 的别名：别名可能改过，而店目录是
    `store_root()` 靠 meta.json 认出来的那个真实目录——报表分目录、
    以及「按目录名反查 shop_id」都以它为准，用别名会把同一家店拆成两个文件夹。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return "未识别店铺"
    d = store_root(sid)
    return _safe_folder(d.name or sid, "未识别店铺")


def report_room_dir(room: str = "auto") -> Path:
    """报表根下的房间目录：自动 / 手动。"""
    return OUTPUT / REPORT_ROOMS.get(room, REPORT_ROOMS["auto"])


def store_reports_dir(shop_id: str, room: str = "auto") -> Path:
    r"""某家店在某个房间里的报表目录：报表\自动\<店名>\ / 报表\手动\<店名>\。

    不放在店数据目录里——报表是给人看的结果，数据是给程序用的原料，
    混在一起的话「数据」文件夹里就会塞满 xlsx。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return report_room_dir(room)
    return report_room_dir(room) / store_display_name(sid)


def migrate_report_dirs(root: Path | str | None = None) -> list[str]:
    r"""把老的报表布局搬进「自动 / 手动」两个房间。

    老布局：报表\<店名>\x.xlsx（自动）、报表\<店名>\手动\x.xlsx（手动）
    新布局：报表\自动\<店名>\x.xlsx、报表\手动\<店名>\x.xlsx

    只搬 xlsx，不删任何东西；目标已存在同名文件就跳过（留旧的不动）。
    文件被 Excel 开着搬不动时跳过，下次启动再试。
    """
    base = Path(root) if root else OUTPUT
    moved: list[str] = []
    rooms = set(REPORT_ROOMS.values())
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return moved
    # 老的「手动临时 / 手动批次」目录：它们本来就属于手动房间，
    # 别当成店名搬进「自动」里（曾经会搬成 自动/手动核算/…，看着莫名其妙）
    manual_root = base / REPORT_ROOMS["manual"]
    for legacy in ("临时手动", "手动核算"):
        src_dir = base / legacy
        if not src_dir.is_dir():
            continue
        for f in list(src_dir.rglob("*.xlsx")):
            try:
                if legacy == "临时手动":
                    dst = manual_root / "临时" / f.name
                else:
                    rel_parent = f.parent.relative_to(src_dir)
                    dst = manual_root / rel_parent / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    continue
                f.replace(dst)
                moved.append(f"{legacy}/{f.name} → 手动/{dst.parent.name}/")
            except OSError:
                continue
        for sub in sorted(src_dir.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    pass
        try:
            src_dir.rmdir()
        except OSError:
            pass

    for d in dirs:
        if d.name in rooms or d.name.startswith("_") or d.name in ("临时手动", "手动核算"):
            continue
        auto_dst = base / REPORT_ROOMS["auto"] / d.name
        manual_src = d / "手动"
        # ① 这家店目录下直接放着的 xlsx = 自动报表
        for f in list(d.glob("*.xlsx")):
            try:
                auto_dst.mkdir(parents=True, exist_ok=True)
                dst = auto_dst / f.name
                if dst.exists():
                    continue
                f.replace(dst)
                moved.append(f"{d.name}/{f.name} → 自动/{d.name}/")
            except OSError:
                continue
        # ② 店目录下的「手动」子目录 → 手动/<店名>/
        if manual_src.is_dir():
            manual_dst = base / REPORT_ROOMS["manual"] / d.name
            for f in list(manual_src.glob("*.xlsx")):
                try:
                    manual_dst.mkdir(parents=True, exist_ok=True)
                    dst = manual_dst / f.name
                    if dst.exists():
                        continue
                    f.replace(dst)
                    moved.append(f"{d.name}/手动/{f.name} → 手动/{d.name}/")
                except OSError:
                    continue
            try:
                manual_src.rmdir()      # 空了就删掉；不空说明还有别的文件，留着
            except OSError:
                pass
        # ③ 整店目录空了就收掉，免得 报表/ 里还挂着一堆空文件夹
        try:
            d.rmdir()
        except OSError:
            pass
    return moved


def unknown_dir(stamp: str = "") -> Path:
    """认不出店铺时的落点：数据/stores/未识别店铺/<时间戳>/。

    单独隔出来是有原因的：以前识别失败会静默落公共目录，
    多店铺一起跑时数据会混在一起，谁也说不清哪份数据属于哪家店。
    """
    s = (stamp or "").strip() or datetime.now().strftime("%Y%m%d-%H%M%S")
    return store_root("") / s


def store_meta_file(shop_id: str) -> Path:
    return store_root(shop_id) / "meta.json"


def _bundle_dir() -> Path:
    """随包携带的「默认配置 / 默认数据」目录。

    打包后放在 `_internal/默认配置`、`_internal/默认数据`；
    源码运行时退回项目根的 `配置`、`数据`，方便调试。
    """
    root = app_root()
    for cand in (root / "_internal", root):
        if (cand / "默认配置").is_dir() or (cand / "默认数据").is_dir():
            return cand
    return root


def restore_defaults() -> list[str]:
    """把「缺了的」默认配置 / 成本表补回来。

    为什么需要：用户升级、清理目录、或误删时，配置（各国佣金、费率、
    计算公式）和商品成本表会一起没了，程序还照跑，算出来的净利全是错的
    ——比直接报错更危险。所以在每次启动时做一次自愈：
      · `配置/*.json`                 ← `_internal/默认配置/*.json`
      · `数据/costs/master.csv`       ← `_internal/默认数据/costs/master.csv`
    只补「不存在」的，已存在的一律不动（用户改过的费率/成本不能被覆盖）。
    """
    fixed: list[str] = []
    bundle = _bundle_dir()

    cfg_src = bundle / "默认配置"
    if cfg_src.is_dir():
        CONFIG.mkdir(parents=True, exist_ok=True)
        for f in cfg_src.iterdir():
            if not f.is_file():
                continue
            dst = CONFIG / f.name
            if dst.exists():
                continue
            try:
                shutil.copy2(f, dst)
                fixed.append(f"配置/{f.name}")
            except OSError:
                pass

    data_src = bundle / "默认数据"
    if data_src.is_dir():
        # 随包带的是 costs/master.csv（英文，包内结构不对外），
        # 落到用户目录时用中文的「成本/master.csv」
        src = data_src / "costs" / "master.csv"
        dst = COSTS / "master.csv"
        if src.is_file() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                fixed.append(f"数据/{COSTS.name}/master.csv")
            except OSError:
                pass
    return fixed


def ensure_dirs() -> None:
    """创建所有需要的目录。打包后首次运行必须调用一次。

    每家店的目录（含分类子目录）也一并建出来——使用者把整个文件夹
    删了也不怕，下次启动会重新建好。
    """
    for d in (DATA, CAPTURE, EXPORTS, INBOX, COSTS, MAPPING, LOGS, OUTPUT,
              STORES_ROOT, UNKNOWN_ROOT):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 只读目录（比如装在 Program Files 又没权限）不该让整个程序崩掉，
            # 后续写入失败时会给出更具体的错误
            pass
    # 配置/成本表缺了就补回来（升级或误删后不影响算钱）
    try:
        restore_defaults()
    except Exception:  # noqa: BLE001
        pass

    for sid in list(_load_store_names()):
        try:
            store_root(sid).mkdir(parents=True, exist_ok=True)
            store_dir(sid).mkdir(parents=True, exist_ok=True)
            for cn in KIND_DIR_CN.values():
                (store_dir(sid) / cn).mkdir(parents=True, exist_ok=True)
            (store_root(sid) / "快照").mkdir(parents=True, exist_ok=True)
            # 给使用者看的两份表：商品SKU表（SKU→商品ID）、链接资料（链接→款式编码）
            for sub in ("商品SKU表", "链接资料"):
                (store_root(sid) / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def describe() -> dict[str, str]:
    """诊断用：把关键路径一次性列出来。"""
    return {
        "运行模式": "打包 exe" if is_frozen() else "源码",
        "程序目录": str(ROOT),
        "数据根": str(DATA_HOME),
        "数据目录": str(DATA),
        "产出目录": str(OUTPUT),
        "配置目录": str(CONFIG),
        "扩展目录": str(EXTENSION),
    }
