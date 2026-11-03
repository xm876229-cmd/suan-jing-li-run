"""本地桥服务：接收浏览器扩展抓到的接口记录。

跑起来：
    python -m bridge.server

接口：
    GET  /health     探活
    GET  /endpoints  已识别接口的汇总（按 URL 去重，带次数）
    POST /capture    扩展推送的抓包批次

抓到的原始记录写到 data/capture/capture-YYYYMMDD.jsonl，
去重后的接口清单写到 data/capture/endpoints.json —— 这份清单就是后续自动取数的目标。
"""

from __future__ import annotations

import base64
import json
import tempfile
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from src import paths
from src import safeio  # noqa: E402

ROOT = paths.ROOT
CAPTURE_DIR = paths.CAPTURE

# 没能算成的「待补算」清单：订单还在平台生成时记一条，
# 文件一落地就自动补算——不用人再点一次。
PENDING_RUNS = paths.DATA / "pending_runs.json"
_PENDING_LOCK = threading.Lock()

# 计划任务派下去时，这家店的报表该复制到哪儿（shop_id → 目录）。
# 多条计划各有各的保存位置，不能共用设置里那一个全局目录。
_AUTO_DL: dict[str, str] = {}

# 最近一次启动自检的结果（界面上「调试 → 自检与修复」显示这个）
_SELFCHECK: dict = {}

# 计划任务状态文件是「读-改-写」，后台巡检线程和界面保存会同时碰它。
# 不加锁的话，两边各读一份、各写一遍：后写的把先写的抹掉，
# 表现是「刚跑过的计划又跑了一次」或者「刚加的计划丢了上次时间」。
_SCHED_STATE_LOCK = threading.Lock()


def _pending_load() -> dict:
    try:
        from src import safeio
        got = safeio.read_json(PENDING_RUNS, default={})
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _pending_save(d: dict) -> None:
    try:
        from src import safeio
        safeio.write_json(PENDING_RUNS, d, indent=1)
    except Exception:  # noqa: BLE001
        pass


def _pending_add(key: str, info: dict) -> None:
    with _PENDING_LOCK:
        d = _pending_load()
        d[key] = info
        _pending_save(d)


def _pending_drop(key: str) -> None:
    with _PENDING_LOCK:
        d = _pending_load()
        if d.pop(key, None) is not None:
            _pending_save(d)


def _calc_cmd(shop_id: str, country: str, dfrom: str, dto: str,
              out: Path, with_formula: bool, data_dir: Path | None = None) -> list[str]:
    """拼出核算子进程的命令（和界面点「核算」走的是同一条路）。

    data_dir 只给手动核算用：数据在「店铺/手动导入」，但费率口径还按这家店来，
    所以 --dir 指手动目录、--store 照样传。
    手动目录是「攒着几批表」的，必须带 --merge-all（全部合并去重），
    否则算出来的只是最新那一份——跟「手动核算」页点出来的报表对不上。
    """
    base = [sys.executable, "run"] if paths.is_frozen() else [sys.executable, "-m", "src.cli"]
    cmd = base + ["--dir", str(data_dir or paths.store_dir(shop_id))]
    cmd += ["--store", shop_id] if shop_id else ["--country", country or "马来西亚"]
    cmd += ["--out", str(out), "--json"]
    if data_dir is not None:
        cmd.append("--merge-all")
    if with_formula:
        cmd.append("--with-formula")
    if dfrom:
        cmd += ["--from", dfrom]
    if dto:
        cmd += ["--to", dto]
    return cmd


def _run_calc_cmd(cmd: list[str], timeout: int = 900) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, env=env, timeout=timeout,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                           if os.name == "nt" else 0))
    except subprocess.TimeoutExpired:
        return False, f"核算超过 {timeout} 秒没结束"
    raw = proc.stdout or b""
    text = ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return False, (err or text or "")[-500:]
    return True, text


def _report_out(store_name: str, dfrom: str, dto: str, suffix: str = "",
                shop_id: str = "", unique: bool = True) -> Path:
    """补算（订单晚到）用的报表路径——跟正常核算一样落在「自动」房间里。

    以前这里写的是 `报表/<店名>/`（老布局）。补算一次就会在房间外多出一个
    店名文件夹，报表页上看着是「自动」，磁盘上却不在 `报表/自动` 里，
    迁移也只会在下次启动时跑一遍。
    """
    # 目录名和文件名都要限长：店名是用户起的，200 字符的店名
    # 在 Windows 上直接抛「文件名或扩展名太长」(WinError 206)，
    # 报表出不来，而那家店的其他功能也跟着废。
    safe = paths._safe_folder(store_name or "未命名店铺", "未命名店铺")
    out_dir = (paths.store_reports_dir(shop_id) if shop_id
               else paths.report_room_dir("auto") / safe)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名也要洗一遍：店名是用户在后台起的，可能带 / : * 这类字符，
    # 目录洗了、文件名没洗的话，Windows 上直接抛「文件名语法不正确」。
    base = re.sub(r'[\\/:*?"<>|\r\n\t]', "_",
                  f"{store_name}_{dfrom or '全量'}_{dto or '全量'}{suffix}").strip(" .")
    if not base:
        base = "净利报表"
    # 文件名上限比目录更紧：还要留 ".xlsx" 和 " (12)" 的位置
    if len(base) > 110:
        import hashlib
        h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]
        base = base[:100].rstrip(" .") + "_" + h
    p = out_dir / f"{base}.xlsx"
    if not unique:
        # 补算走「先算临时文件、成功再换名」，跟正常核算一样覆盖同名报表。
        # 原来这里永远不覆盖（自动加 (2)），同一段数据补算两次就多出一份 (2)，
        # 报表页上还分不清哪份是最后那版。
        return p
    i = 2
    while p.exists():
        p = out_dir / f"{base} ({i}).xlsx"
        i += 1
    return p


def _shop_id_by_name(name: str) -> str:
    """按店铺目录名反查 shop_id（报表是按店铺名分目录放的）。"""
    try:
        root = paths.store_root("")
        for d in root.parent.iterdir():
            meta = d / "meta.json"
            if d.is_dir() and meta.is_file() and d.name == name:
                try:
                    return str(json.loads(meta.read_text(encoding="utf-8")).get("shop_id") or "")
                except Exception:  # noqa: BLE001
                    return ""
    except Exception:  # noqa: BLE001
        pass
    return ""


# 订单迟迟不落地就重跑一轮取数。
#
# 6 分钟：正常店 90 秒就出文件，大店 3~5 分钟。超过 6 分钟基本是这一轮废了
# （请求被掐断 / 平台把任务吞了），重跑比干等有效。
# 间隔 8 分钟：重跑要真去平台导（扩展侧 force），太密会把同一账户的导出队列
# 堆满——上午就是这么把 4 家店堵了半小时。
_RETRY_FETCH_AFTER = 6 * 60
_RETRY_FETCH_GAP = 8 * 60


def _orders_fresh(shop_id: str, since: float) -> bool:
    """店里有没有「这次才落地」的订单表。

    为什么不能只看"有没有订单表"：店里往往还留着上一次导的**别的月份**，
    拿它去核算会得到 GMV 0、净利为负的废报表，而且一声不响。
    实测就踩过：9 月的待补，桥看见 10 月那份表就开算，报表 GMV 0、净利 -93212，
    6 秒后 9 月的表才落地——那份废报表当时已经生成了。
    """
    try:
        from src import loader as _ld
        b, _jm, _sk = _ld._scan_folder(paths.store_dir(shop_id))
    except Exception:  # noqa: BLE001
        return True                      # 判不出来就别拦
    files = [p for p, _rows in (b.get("order") or [])]
    if not files:
        return False
    newest = 0.0
    for f in files:
        try:
            newest = max(newest, float(Path(f).stat().st_mtime))
        except OSError:
            continue
    return newest >= since - 60      # 给文件系统时间戳留一分钟余量


def _retry_fetch_if_stuck(key: str, info: dict, now: float) -> None:
    """订单迟迟不落地 → 让扩展重新跑一轮取数。

    为什么要有这一层：这份「待补」是扩展那边记的，正常由它的每分钟闹钟把
    导好的订单收回来。但那条路实测会失灵（后台 fetch 被浏览器按跨域挡掉，
    报 Failed to fetch），一停就是一小时，界面上却一直是「在等订单」。
    这里兜底：等太久就重新派一轮，扩展跑任务时是在**页面里**导的，那条路是通的。
    """
    at = float(info.get("at") or 0)
    if now - at < _RETRY_FETCH_AFTER:
        return
    last = float(info.get("retried_at") or 0)
    if last and now - last < _RETRY_FETCH_GAP:
        return
    shop_id = str(info.get("shop_id") or "")
    dfrom = str(info.get("from") or "")
    dto = str(info.get("to") or "")
    if not (shop_id and dfrom and dto):
        return
    # 已经在队列里、或者正在跑，就别再派一个：不然同一家店会堆出好几轮，
    # 浏览器那边几个任务抢同一个标签页，反而谁也跑不完。
    try:
        with _PENDING_LOCK:
            queued = any(str(j.get("store") or "") == shop_id
                         for j in _PENDING if isinstance(j, dict))
    except Exception:  # noqa: BLE001
        queued = False
    with _PROGRESS_LOCK:
        cur = str(((_PROGRESS.get(shop_id) or {}).get("current")) or "")
    if queued or cur:
        return
    with _PENDING_LOCK:
        d = _pending_load()
        if key not in d:
            return
        d[key]["retried_at"] = now
        _pending_save(d)
    try:
        _enqueue(shop_id, {"startDate": dfrom, "to": dto}, task="all", auto=True,
                 country=str(info.get("country") or ""), force=True)
    except Exception:  # noqa: BLE001
        return
    _append_log(f"[auto] {info.get('store_name') or shop_id} 的订单等太久了"
                f"（{int((now - at) // 60)} 分钟），重新派一轮取数")


def _pending_tick() -> None:
    """后台每分钟看一眼：之前因为「订单没到」没算成的，现在能算了吗。"""
    d = _pending_load()
    if not d:
        return
    now = time.time()
    for key, info in list(d.items()):
        if now - float(info.get("at") or 0) > 3 * 3600:
            _pending_drop(key)
            continue
        shop_id = str(info.get("shop_id") or "")
        # 只认「这次才落地」的订单表：店里留着别的月份不算数，
        # 拿它去算会得到 GMV 0、净利为负的废报表。
        if not _orders_fresh(shop_id, float(info.get("at") or 0)):
            # 订单还没到：要么平台还在生成，要么那条回收路已经废了。
            # 等太久就重新派一轮，别让这份待补无限期躺着。
            _retry_fetch_if_stuck(key, info, now)
            continue
        store_name = str(info.get("store_name") or shop_id)
        dfrom = str(info.get("from") or "")
        dto = str(info.get("to") or "")
        # 同一家店可能同时在「手动点运行」和「补算排队」——两边都调核算子进程，
        # 会各写一份报表、互相抢映射主表。用同一把按店锁串起来，抢不到就下一分钟再说。
        _lk = _run_lock_for(_canon_shop(shop_id))
        if not _lk.acquire(blocking=False):
            continue
        try:
            # 先算临时文件、成功了再换名：跟 /run 同一套规矩。
            # 直接往正式文件里写，算到一半失败会留下半截 xlsx。
            final_out = _report_out(store_name, dfrom, dto, shop_id=shop_id,
                                    unique=False)
            out = final_out.with_name(f".{os.getpid()}.calc.xlsx")
            _append_log(f"[auto] 订单已到，自动补算：{store_name} {dfrom}~{dto}")
            try:
                ok, msg = _run_calc_cmd(_calc_cmd(shop_id, str(info.get("country") or ""),
                                                  dfrom, dto, out,
                                                  bool(info.get("with_formula"))))
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
            if ok and out.exists():
                out = safeio.swap_report(
                    out, final_out,
                    paths.DATA / "回收站" / f"报表_{datetime.now():%Y%m%d_%H%M%S_%f}")
            elif out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
        finally:
            _lk.release()
        _pending_drop(key)
        if ok:
            _append_log(f"[auto] 补算完成：{out}")
        else:
            _append_log(f"[auto] 补算失败：{str(msg)[-300:]}")


def _pending_runner() -> None:
    time.sleep(20)
    while True:
        try:
            _pending_tick()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)
INBOX_DIR = paths.INBOX
MAPPING_DIR = paths.MAPPING
LOG_FILE = paths.CAPTURE / "task.log"
PORT = int(os.environ.get("JINGLI_PORT") or 8765)

TASK_LOG: list[str] = []
_PENDING: list = []          # 待办队列：[{id, task, range, store, country, at}, ...]
_PENDING_LOCK = threading.Lock()

# 任务多久没被领走就作废。
# 手动点的任务 30 分钟：那么久都没开浏览器，多半是忘了，跑起来反而奇怪。
# 计划任务给 12 小时：它的承诺是「浏览器没开就排队，等它打开自动补」，
#   只留 30 分钟的话，早上 9 点派的任务，你中午才开电脑就永远不跑了，
#   界面上还显示「已执行」——这才是最坑的。
_JOB_TTL_MS = 30 * 60 * 1000
_AUTO_JOB_TTL_MS = 12 * 60 * 60 * 1000

# 扩展心跳：shop_id → {at, shop_name, region, ver, running}
# 用来回答界面上那句「这家店的浏览器开着没」。
# 以前只能看 meta.json 的「上次上传时间」，那只说明以前传过，不代表现在还连着。
_ONLINE: dict[str, dict] = {}
_ONLINE_LOCK = threading.Lock()

# 手动「停止」：界面点一下，把这家店（或全部）标成要停；
# 扩展在跑的过程中每隔几秒问一次，看到就收手。
# 存时间戳而不是 True：十分钟后自动过期，免得一次误点把下一次也停了。
_STOP: dict[str, float] = {}
_STOP_LOCK = threading.Lock()
_STOP_TTL = 600.0

# 同一时刻最多几家在取数。
#
# 为什么要有这道闸：7 家店是 7 个浏览器，但同一台机器、同一条网。
# 实测 7 家一起开跑时，订单接口会抛 `Failed to fetch`（抓包里是"发出去了、
# 没有任何响应"，status=None），而且**每轮失败的店不固定**——14:06 是
# Crochet Studio，14:20 换成 Chroma Home，纯属谁抢输了谁失败。
# 平台那边同一账户并发导出也吃不消（一上午堆几十个任务后全部卡在"生成中"）。
# 限制并发比继续加超时有效：一次跑 2 家，其余排队等下一分钟领。
_MAX_ACTIVE_FETCH = 2
_ONLINE_TTL = 150.0          # 超过这个秒数没心跳就算离线（心跳 60 秒一次）

# 进度：shop_id → {task, run_id, steps:{step: {...}}, current, started, updated}
# 扩展每完成一步报一次，桥只做合并与保存（不落盘，重启就没了，够用）。
_PROGRESS: dict[str, dict] = {}
_PROGRESS_LOCK = threading.Lock()

# 一次运行包含哪几步。扩展和界面都用这一份，别各写一遍。
RUN_STEPS = ("sku", "orders", "ads", "affiliate", "calc", "report")
STEP_LABELS = {
    "sku": "商品 SKU 表",
    "orders": "订单表",
    "ads": "广告表",
    "affiliate": "联盟佣金表",
    "calc": "核算",
    "report": "出报表",
}


def _online_now(shop_id: str) -> bool:
    rec = _ONLINE.get((shop_id or "").strip())
    return bool(rec) and (time.time() - float(rec.get("at") or 0)) < _ONLINE_TTL


# 「今天取过什么」的短缓存：卡片墙 3 秒刷一次，每家店每次都去翻 meta.json +
# 台账文件没必要。12 秒的新鲜度对一行提示完全够，取完数之后最多晚一拍显示。
_CAP_TTL = 12.0
_CAP_BRIEF: dict[str, tuple[float, dict]] = {}
_CAP_BRIEF_LOCK = threading.Lock()


def _captured_brief(shop_id: str) -> dict:
    sid = (shop_id or "").strip()
    if not sid:
        return {}
    now = time.time()
    with _CAP_BRIEF_LOCK:
        hit = _CAP_BRIEF.get(sid)
        if hit and now - hit[0] < _CAP_TTL:
            return hit[1]
    try:
        from src import capture as _cap
        data = _cap.today(sid)
    except Exception:  # noqa: BLE001
        data = {}
    with _CAP_BRIEF_LOCK:
        _CAP_BRIEF[sid] = (now, data)
    return data


def _enqueue(store: str, rng: dict, *, task: str = "all", auto: bool = False,
             manual: bool = False, with_formula=None, country: str = "",
             force: bool = False) -> dict:
    """往队列里放一个任务。store 必填——不写 store 就等于"谁都能领"，
    多店同时在线时必然串店，这正是这次要修的东西。

    区间在这里补齐成 startDate/to/endDateExclusive/startMs/endMs 全套：
    界面只传两个日期，而扩展里订单要毫秒、SKU 与联盟要开区间，
    缺字段的结果是「该时段没有订单」这种假结论，最难查。
    """
    import uuid

    from src import auto_run

    job = {
        "id": uuid.uuid4().hex[:12],
        "task": task,
        "range": auto_run.normalize_range(rng or {}),
        "store": (store or "").strip(),
        "country": country or "",
        "auto": bool(auto),
        "manual": bool(manual),
        "at": int(time.time() * 1000),
    }
    if with_formula is not None:
        job["with_formula"] = bool(with_formula)
    if force:
        # 「今天导过也重导」：用户明知道数据变了才会勾这个。
        # 不勾的时候，扩展看到今天的取数台账就会跳过重复的那几步。
        job["force"] = True
    with _PENDING_LOCK:
        # 同一家店在队列里最多留一个待领任务，新的顶掉旧的。
        #
        # 为什么必须去重：界面上「运行」按钮点一下要等几秒才变状态，
        # 用户会以为没点上，连着点几下（实测连点 30 次进了 30 个任务）。
        # 后果是同一家店取数 30 遍、核算 30 遍、出 30 份报表 ——
        # 对平台还可能是异常高频请求。
        # 用「新的顶掉旧的」而不是「直接丢弃」：用户改了区间再点，
        # 应该按最后一次的区间跑，而不是拿到第一下那个。
        if job["store"]:
            keep = []
            replaced = 0
            for old_job in _PENDING:
                if not isinstance(old_job, dict):
                    keep.append(old_job)
                    continue
                if old_job.get("store") != job["store"] or old_job.get("task") != job["task"]:
                    keep.append(old_job)
                    continue
                # 同一家店、同一类任务：要不要顶掉旧的那个，看是谁派的。
                #
                # 手动点「运行」：无条件顶掉。用户连点、或者改了区间重点，
                #   他要的就是「按我最后这次的区间跑」，旧的留着只会跑两遍。
                # 计划任务：只有区间完全一样才顶掉（那就是同一条计划重复触发）。
                #   区间不一样说明是两条计划撞在同一天（比如「每月 1 号导上月」
                #   和「每周一导上周」正好都是今天），两个区间都得跑。
                #   以前一律顶掉，后一条会把前一条悄悄吃掉，
                #   但界面上两条都显示「已执行」——数据就少了一个区间。
                same_range = old_job.get("range") == job["range"]
                if job.get("manual") or same_range:
                    replaced += 1
                    continue
                keep.append(old_job)
            _PENDING[:] = keep
            if replaced:
                job["replaced"] = replaced
        _PENDING.append(job)
    return job


def _known_store_ids() -> list[str]:
    """磁盘上已经认出过的店铺目录名，按店名排序（顺序稳定）。"""
    return [it["name"] for it in paths.store_index()]


def _store_dir_names() -> dict[str, str]:
    """shop_id → 目录名。派任务时前端可能传 id 也可能传店名，这里统一一下。

    顺序取自 `paths.store_index()`：驾驶舱卡片墙就是按这个 dict 的顺序排的，
    排好序才不会因为某家店刚跑过一次就跳位置。
    """
    return {it["shop_id"]: it["name"] for it in paths.store_index()}

# 核算锁按店铺分开：两家店各开一个浏览器同时点运行是正常用法，
# 用一把全局锁会让第二家直接报「已有核算任务在进行」——多店铺等于用不了。
# 同一家店仍然互斥（连点两下不该算两遍）。
_RUN_LOCKS: dict[str, threading.Lock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def _canon_shop(key: str) -> str:
    """把「店铺名 / 目录名 / shop_id」统一成 shop_id，给锁和任务用。

    为什么需要：核算入口可能收到三种写法——shop_id、配置里的别名、
    数据目录名。锁按字符串开，同一家店用两种写法就是两把锁，
    等于没锁（两个核算进程各写一份报表、互相抢映射表）。
    """
    # 控制字符（\x00 之类）一律去掉：它会让 os.stat 抛 ValueError
    # （不是 OSError），而下游普遍只 catch OSError，整条请求就变 500。
    k = re.sub(r"[\x00-\x1f]", "", (key or "")).strip()
    if not k:
        return "_all"
    try:
        names = _store_dir_names()          # shop_id → 目录名
    except Exception:  # noqa: BLE001
        return k
    if k in names:
        return k
    for sid, nm in names.items():
        if nm == k:
            return sid
    # 目录里查不到（新店第一次核算，目录还没建），再从 stores.json 的别名反查。
    # 不查这一步的话，界面传「马来一号店」、扩展传 shop_id 会落到两把不同的锁上，
    # 两个核算进程同时跑同一家店 —— 两份报表互相覆盖、还抢映射主表。
    try:
        for sid, nm in paths._load_store_names().items():
            if nm == k:
                return sid
    except Exception:  # noqa: BLE001
        pass
    return k


def _run_lock_for(key: str) -> threading.Lock:
    k = (key or "").strip() or "_all"
    with _RUN_LOCKS_GUARD:
        lk = _RUN_LOCKS.get(k)
        if lk is None:
            lk = threading.Lock()
            _RUN_LOCKS[k] = lk
        return lk



def _mirror_mapping_to_store(store: str, src) -> None:
    """把映射表复制一份到店目录里，给使用者看。

    程序内部用的是全局累积主表（data/mapping/master.csv，所有店共用一份），
    但「每个店有哪些 SKU、哪些链接」是这家店自己的东西，放在店目录下才直观。
    """
    try:
        from pathlib import Path as _P
        sp = _P(str(src))
        if not store or not sp.is_file():
            return
        sub = "链接资料" if "链接资料" in sp.name else "商品SKU表"
        d = paths.store_root(store) / sub
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, d / sp.name)
    except Exception:  # noqa: BLE001
        pass


_LOG_APPENDS = 0
_LOG_MAX_BYTES = 8 * 1024 * 1024


def _append_log(msg: str) -> None:
    global _LOG_APPENDS
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    TASK_LOG.append(line)
    if len(TASK_LOG) > 500:
        del TASK_LOG[:-500]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass

    # 长时间挂着跑（几周不关机）时，日志会一直长。启动时轮转过一次不够，
    # 这里每 200 条看一眼，超过 8MB 就轮转。磁盘被日志占满之后
    # 最典型的症状是「配置突然保存失败」，很难联想到日志。
    _LOG_APPENDS += 1
    if _LOG_APPENDS % 200 == 0:
        try:
            if LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
                old = LOG_FILE.with_name(LOG_FILE.name + ".1")
                if old.exists():
                    old.unlink()
                LOG_FILE.rename(old)
        except OSError:
            pass


# 只有「自己人」才给跨域许可：扩展（chrome-extension://）和本机页面。
# 以前是 `*`，等于任何网页都能读到桥的返回、也能发 JSON 过来——
# 打开一个恶意页面就能让本机跑核算、往数据目录传文件。
_ALLOW_ORIGIN = re.compile(r"^(chrome-extension|moz-extension|safari-web-extension)://[\w.-]+$"
                           r"|^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


def _cors(handler: BaseHTTPRequestHandler) -> None:
    origin = (handler.headers.get("Origin") or "").strip()
    # 没有 Origin ＝ 同源页面 / 扩展后台直连（带 host 权限时不走 CORS），不用发头
    if origin and _ALLOW_ORIGIN.match(origin):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    elif origin:
        # 陌生网站：不给许可。浏览器会在预检阶段就拦下，
        # 连 POST 都不会真的发过来（这才是防住 CSRF 的关键）
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


_START_AT = datetime.now()


def bridge_uptime() -> str:
    """桥已经跑了多久，首页显示用。"""
    sec = int((datetime.now() - _START_AT).total_seconds())
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


def _where(exc: BaseException) -> str:
    """异常发生在哪一行（`server.py:1234`）。写进日志，排查时不用再猜。"""
    try:
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            last = tb[-1]
            return f"（{Path(last.filename).name}:{last.lineno}）"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _json(handler: BaseHTTPRequestHandler, obj, code: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _html(handler: BaseHTTPRequestHandler, text: str, code: int = 200) -> None:
    body = text.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # 配置页要读本机数据，禁掉缓存，免得改完还看到旧的
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def normalize(url: str) -> str:
    """把 URL 里的查询参数值抹掉，只留路径和参数名，便于把同一接口聚到一起。"""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    qs = p.query
    if qs:
        names = [kv.split("=")[0] for kv in qs.split("&") if kv]
        qs = "&".join(f"{n}=" for n in names)
    return f"{p.netloc}{p.path}" + (f"?{qs}" if qs else "")


def rebuild_endpoints() -> dict:
    """扫描全部抓包文件，生成去重后的接口清单。"""
    agg: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "methods": Counter(), "statuses": Counter(),
        "content_types": Counter(), "sample_req": None, "last_seen": "",
    })
    for f in sorted(CAPTURE_DIR.glob("capture-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = normalize(rec.get("url", ""))
            if not key:
                continue
            a = agg[key]
            a["count"] += 1
            a["methods"][rec.get("method", "GET")] += 1
            a["statuses"][str(rec.get("status"))] += 1
            a["content_types"][(rec.get("contentType") or "").split(";")[0]] += 1
            if rec.get("reqBody") and not a["sample_req"]:
                a["sample_req"] = rec["reqBody"][:500]
            a["last_seen"] = max(a["last_seen"], rec.get("at", ""))

    out = {}
    for k, a in agg.items():
        out[k] = {
            "count": a["count"],
            "method": a["methods"].most_common(1)[0][0] if a["methods"] else "GET",
            "status": a["statuses"].most_common(1)[0][0] if a["statuses"] else "",
            "contentType": a["content_types"].most_common(1)[0][0] if a["content_types"] else "",
            "sampleRequest": a["sample_req"],
            "lastSeen": a["last_seen"],
        }
    data = {"updatedAt": datetime.now().isoformat(timespec="seconds"),
            "endpointCount": len(out), "endpoints": dict(sorted(out.items()))}
    from src import safeio
    safeio.write_json(CAPTURE_DIR / "endpoints.json", data)
    return data


# 国家代码 → 中文名（自动识别到的店铺要有个看得懂的国家）
REGION_NAMES = {
    "MY": "马来西亚", "PH": "菲律宾", "TH": "泰国", "VN": "越南",
    "SG": "新加坡", "ID": "印尼", "US": "美国", "GB": "英国",
    "DE": "德国", "FR": "法国", "IT": "意大利", "ES": "西班牙",
}


def remember_store(shop_id: str, info: dict) -> None:
    """记下这家店是谁。

    扩展每次上报身份都会调这里，所以 meta.json 里永远是最新的店名与国家。
    第一次见到的店铺会自动登记，不用手工往 stores.json 里填。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return
    meta_file = paths.store_meta_file(sid)
    cur: dict = {}
    try:
        cur = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cur = {}
    region = str(info.get("region") or info.get("country") or "").upper()
    cur.update({
        "shop_id": sid,
        "shop_name": str(info.get("shop_name") or info.get("store_name") or cur.get("shop_name") or ""),
        "shop_code": str(info.get("shop_code") or cur.get("shop_code") or ""),
        "region": region,
        "country": REGION_NAMES.get(region, cur.get("country") or ""),
        "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    try:
        from src import safeio
        safeio.write_json(meta_file, cur)
    except OSError:
        pass
    _retitle_store(meta_file.parent, sid)


def _retitle_store(cur, shop_id: str) -> None:
    """新店第一次被认出来时，把「一串数字」的目录改成店名。

    使用者打开 数据\\stores 应该看到店名。只在目录里除了 meta.json 没别的东西时改，
    已经有数据的店一律不动——正在跑的核算读到一半路径变了，比目录难看严重得多。
    """
    try:
        want = paths.store_pretty_name(shop_id)
    except Exception:  # noqa: BLE001
        return
    safe = re.sub(r'[\\/:*?"<>|]', "_", want or "").strip(" .")
    if not safe or cur.name == safe:
        return
    dst = cur.parent / safe
    if dst.exists():
        return
    try:
        if [p for p in cur.iterdir() if p.name != "meta.json"]:
            return
        cur.rename(dst)
        _append_log(f"[identify] 店铺目录按店名命名：{safe}")
    except OSError:
        pass


def discovered_stores() -> list[dict]:
    """扫 data/stores/*/meta.json，把自动识别到的店铺列出来。"""
    out: list[dict] = []
    root = paths.STORES_ROOT
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        if not meta.is_file():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        inbox = paths.store_inbox(d)
        # 递归数：收件箱下面按类型分了子目录（订单表/广告表/…），
        # 只数直接子文件的话永远是 0，界面上「这家店几个文件」全显示 0，
        # 看着像没取到数据。
        files = [f for f in inbox.rglob("*") if f.is_file()] if inbox.is_dir() else []
        out.append({
            "shop_id": m.get("shop_id") or d.name,
            "shop_name": m.get("shop_name") or d.name,
            "shop_code": m.get("shop_code") or "",
            "region": m.get("region") or "",
            "country": m.get("country") or REGION_NAMES.get(m.get("region") or "", ""),
            "last_seen": m.get("last_seen") or "",
            "inbox_count": len(files),
            "inbox_latest": max((f.stat().st_mtime for f in files), default=0),
        })
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return out


def _rename_store_dirs(shop_id: str, new_name: str) -> list[str]:
    """把店铺的数据目录、以及两个报表房间里的同名目录一起改名。

    为什么必须改目录：驾驶舱/手动核算的卡片标题、报表文件夹、手动数据目录
    都是跟着**店目录名**走的。只改 stores.json 的话，界面还显示旧名字，
    用户会以为改名没生效——实测就是这样。
    """
    moved: list[str] = []
    try:
        old = paths.store_root(shop_id)
    except Exception:  # noqa: BLE001
        return moved
    want = paths.folder_name(new_name)
    if not old.is_dir() or old.name == want:
        return moved
    target = old.parent / want
    if target.exists():
        return [f"数据目录没改：{want} 已存在"]
    try:
        old.rename(target)
        moved.append(f"数据目录 {old.name} → {want}")
    except OSError as e:
        return [f"数据目录改不了名（可能有文件正被占用）：{e}"]
    for room in ("auto", "manual"):
        a = paths.report_room_dir(room) / old.name
        b = paths.report_room_dir(room) / want
        if a.is_dir() and not b.exists():
            try:
                a.rename(b)
                moved.append(f"报表/{paths.REPORT_ROOMS[room]} {old.name} → {want}")
            except OSError:
                pass
    return moved


def reconcile_store_dirs() -> list[str]:
    """启动时对齐一次：stores.json 里改了名、但目录还是旧名的，把目录改过来。

    只认「目标目录不存在」的情况；同名冲突就跳过，绝不覆盖别人的数据。
    """
    out: list[str] = []
    try:
        cfg = json.loads(paths.STORES_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    for s in cfg.get("stores") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        nm = str(s.get("name") or "").strip()
        if not sid or not nm:
            continue
        for m in _rename_store_dirs(sid, nm):
            out.append(f"{nm}：{m}")
    return out


def rename_store(shop_id: str, alias: str) -> dict:
    """给店铺起个自己看得懂的名字。

    shop_id 是一长串数字，认不出来。所以用 shop_id 做键、名字做值，
    存进 config/stores.json —— 那是用户自己的配置文件，
    程序不会覆盖它（自动发现的店铺只在 meta.json 里记原名）。
    """
    sid = (shop_id or "").strip()
    alias = (alias or "").strip()
    if not sid:
        return {"ok": False, "error": "缺少 shop_id"}

    try:
        cfg = json.loads(paths.STORES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {"stores": []}
    stores = cfg.setdefault("stores", [])

    hit = next((x for x in stores if str(x.get("id")) == sid), None)
    if hit is None:
        # 没登记过：从自动发现里补齐国家等信息，再起名
        info = next((d for d in discovered_stores() if d["shop_id"] == sid), {})
        hit = {
            "id": sid,
            "name": alias or info.get("shop_name") or sid,
            "country": info.get("country") or "",
            "shop_code": info.get("shop_code") or "",
            "shop_name": info.get("shop_name") or "",
        }
        stores.append(hit)
    else:
        hit["name"] = alias or hit.get("name") or sid

    try:
        from src import safeio
        safeio.write_json(paths.STORES_FILE, cfg)
    except OSError as e:
        return {"ok": False, "error": f"写入 stores.json 失败：{e}"}

    # 名字改了，目录也得跟着改：卡片标题和报表文件夹都取目录名
    moved = _rename_store_dirs(sid, hit["name"])
    _append_log(f"[store] 店铺改名：{sid} → {hit['name']}"
                + (f"（{'；'.join(moved)}）" if moved else ""))
    return {"ok": True, "shop_id": sid, "name": hit["name"], "moved": moved}


def remove_store(shop_id: str) -> dict:
    """把一家店从列表里移除——目录挪进 `数据/店铺/_已删除/`，不真删。

    为什么不真删：里面是这家店的历史订单/SKU/快照，删了要重新取数，
    而有些数据平台只保留一段时间。挪走能恢复，代价只是占硬盘。
    想彻底清掉，自己把 `_已删除` 那个文件夹删了就行。
    """
    sid = (shop_id or "").strip()
    if not sid:
        return {"ok": False, "error": "缺少店铺 ID"}

    # 1) 摘掉 stores.json 里的登记（手动加过才有）
    removed_cfg = False
    try:
        cfg = json.loads(paths.STORES_FILE.read_text(encoding="utf-8"))
        stores = cfg.get("stores") or []
        keep = [x for x in stores
                if str(x.get("id") or "") != sid
                and str(x.get("zhanfu_browser_id") or "") != sid]
        if len(keep) != len(stores):
            cfg["stores"] = keep
            from src import safeio
            safeio.write_json(paths.STORES_FILE, cfg)
            removed_cfg = True
    except Exception:  # noqa: BLE001
        pass

    # 2) 店目录挪进回收站
    moved_to = ""
    try:
        d = paths.store_root(sid)
        if d.is_dir():
            trash = paths.STORES_ROOT / "_已删除"
            trash.mkdir(parents=True, exist_ok=True)
            dst = trash / f"{d.name}_{datetime.now():%Y%m%d_%H%M%S}"
            shutil.move(str(d), str(dst))
            moved_to = str(dst)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"挪走店目录失败：{e}"}

    if not moved_to and not removed_cfg:
        return {"ok": False, "error": "没找到这家店（可能已经移除）"}

    _append_log(f"[店铺] 已移除 {sid}（目录：{moved_to or '无'}），可在 数据/店铺/_已删除 找回")
    return {"ok": True, "moved_to": moved_to, "removed_config": removed_cfg}


def store_list() -> dict:
    """给扩展用的店铺清单：每家店的国家已经写在配置里，界面上选店即可。

    为什么不让扩展自己读文件：扩展在浏览器沙箱里，读不到本地磁盘。
    走 HTTP 这条路，配置改了刷新面板就生效，不用重装扩展。
    """
    stores: list[dict] = []
    # 这里原来一旦读不到 stores.json 就 return 一个 error + 空列表。
    # 后果：全新安装（还没有 stores.json）、或者用户手动删掉它之后，
    # 界面上「一家店都没有」，还顶一条报错 —— 但磁盘上明明有店目录。
    # 现在缺失/损坏都当「没有手工登记」，继续往下合并自动识别到的店铺。
    try:
        from src import safeio
        cfg = safeio.read_json(paths.STORES_FILE, default={}) or {}
        for s in (cfg.get("stores") or []):
            if not isinstance(s, dict):
                continue
            if not s.get("enabled", True):
                continue
            stores.append({
                "id": s.get("id", ""),
                "name": s.get("name", "") or s.get("id", ""),
                "country": s.get("country", ""),
                "browserId": str(s.get("zhanfu_browser_id", "") or ""),
            })
    except Exception as e:  # noqa: BLE001
        _append_log(f"[店铺] 读 stores.json 失败，按「没有手工登记」继续：{e}")

    countries: list[str] = []
    try:
        rates = json.loads(paths.RATES_FILE.read_text(encoding="utf-8"))
        countries = list(rates.get("countries", {}).keys())
    except Exception:
        pass

    # 自动识别到的店铺（扩展上报过身份的那些）合并进来。
    # 手工配置的优先——店名、国家以 config/stores.json 为准，那是用户自己起的名字。
    seen = {s.get("id") for s in stores}
    for d in discovered_stores():
        if d["shop_id"] in seen:
            continue
        stores.append({
            "id": d["shop_id"],
            "name": d["shop_name"] or d["shop_id"],
            "country": d["country"],
            "browserId": "",
            "shop_id": d["shop_id"],
            "shop_code": d["shop_code"],
            "auto": True,
            "inbox_count": d["inbox_count"],
            "last_seen": d["last_seen"],
        })

    # 「自动」这个标签原来只挂在「没写进 stores.json」的店上，
    # 于是手动登记过的马来一号店看着像"没被识别"。
    # 实际该看的是**有没有识别档案**（meta.json 才是店铺身份的来源）。
    for st in stores:
        try:
            meta = paths.store_root(st.get("id") or "") / "meta.json"
            st["auto"] = meta.is_file()
        except Exception:  # noqa: BLE001
            pass
        # 把「店铺后台里真实的名字」带上：用户可能把目录改成了中文别名
        # （比如 马来一号店），光看别名认不出是哪家店。
        if not st.get("shop_name"):
            try:
                sid = str(st.get("id") or st.get("shop_id") or "")
                m = json.loads((paths.store_root(sid) / "meta.json").read_text(encoding="utf-8"))
                st["shop_name"] = str(m.get("shop_name") or "")
            except Exception:  # noqa: BLE001
                st["shop_name"] = ""

    default = ""
    try:
        default = cfg.get("defaults", {}).get("country", "")
    except Exception:
        pass

    # 站点档案一起发下去：扩展据此推国家、决定某些站点要不要跳过规格接口。
    # 差异写在 config/sites.json 里，加新店/新站点不用改扩展代码。
    return {"ok": True, "stores": stores, "countries": countries,
            "default": default, "sites": _sites_cfg()}


def _sites_cfg() -> dict:
    """读站点档案（配置/sites.json）。读不到就给空表，扩展按默认逻辑走。"""
    try:
        f = paths.CONFIG / "sites.json"
        if f.is_file():
            d = json.loads(f.read_text(encoding="utf-8"))
            return {"sites": d.get("sites") or {}, "defaults": d.get("defaults") or {}}
    except Exception:  # noqa: BLE001
        pass
    return {"sites": {}, "defaults": {}}


def _mark_capture(shop_id: str, table_kind: str, payload: dict, path: Path) -> dict:
    """上传成功后记一笔「这类表今天取过了」。

    记的是「哪家店、哪个区间、哪一类表、文件在哪」，扩展下次跑同店同区间时
    凭它跳过重复导出。认不出店或没有区间就不记——宁可多导一次，
    也不能让别的区间冒用这批数据。
    """
    if not shop_id:
        return {}
    kind = {"order": "orders", "ads": "ads",
            "affiliate": "affiliate", "mapping": "sku"}.get((table_kind or "").lower())
    if not kind:
        return {}
    try:
        from src import auto_run
        from src import capture as _cap

        rng = auto_run.normalize_range(payload.get("range") or {})
        a, b = str(rng.get("startDate") or ""), str(rng.get("to") or "")
        if not a or not b:
            return {}
        if _cap.mark(shop_id, kind, a, b, path,
                     shop_name=str(payload.get("shop_name") or "")):
            return {"kind": kind, "from": a, "to": b,
                    "label": _cap.KIND_CN.get(kind, kind)}
    except Exception as e:  # noqa: BLE001
        # 记账失败不影响上传，也不能让用户看见异常
        _append_log(f"[upload] 取数记账失败：{e}")
    return {}


# 请求体上限。普通接口都是几 KB 的 JSON，8MB 已经非常宽松；
# 上传接口单独放宽（用户在「导入数据」里传整月订单，几十 MB 正常）。
MAX_BODY = 8 * 1024 * 1024

# 二进制上传的上限（订单导出文件，大店 40~80MB 都有）。
# JSON 那条路走不了这么大的：base64 会膨胀 33%，8MB 上限一撞就 413，
# 而 413 之后服务端不读 body，连接被重置 —— 浏览器只看到 `Failed to fetch`，
# 分不清是"网线断了"还是"文件太大"。实测 Chroma Home（5.7 万单 / 40MB）
# 就卡在这里，而 Crochet lover 的 5.5MB 恰好在上限内，所以它一直成功。
MAX_RAW_UPLOAD = 256 * 1024 * 1024
# 上传接口单独放宽。不放到 512MB 是因为整个 body 是一次读进内存的：
# 200MB 的 base64 解码后 150MB，峰值约 350MB，还撑得住；
# 再大就有被系统 OOM 杀掉的风险，而 OOM 表现出来就是「程序突然没了」。
MAX_UPLOAD_BODY = 200 * 1024 * 1024

# 界面上传（导入数据 / 手动核算）单个文件上限 60MB。
# 先看 base64 长度再解码：直接 b64decode 一个 200MB 的串，会先占 150MB 内存，
# 低配机器直接卡死或者被系统杀掉，还查不出原因。
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_B64_CHARS = MAX_FILE_BYTES * 4 // 3 + 8


def _decode_b64(b64) -> tuple[bytes | None, str]:
    """解 base64 并做体积预检。返回 (data, 错误说明)。"""
    if not isinstance(b64, str) or not b64:
        return None, "内容是空的"
    b64 = re.sub(r"\s+", "", b64)      # 有些客户端会按 76 列换行，先去掉空白
    if len(b64) > MAX_B64_CHARS:
        return None, f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MB"
    try:
        # validate=True：默认模式会**忽略**非法字符，返回空字节。
        # 那样传一个乱码串会「上传成功」，落下一个 0 字节的文件，
        # 卡片上显示「未识别」，核算时才报「没有订单表」——白查半天。
        raw = base64.b64decode(b64, validate=True)
    except Exception as e:  # noqa: BLE001
        return None, f"内容解码失败：{e}"
    if not raw:
        return None, "内容是空的（文件可能是 0 字节）"
    return raw, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "JingLiBridge/0.1"

    # 读写都设超时。不设的话，一个只连不发（或声称发 N 兆却只发几个字节）
    # 的连接会把线程永久占住：read 一直等，线程不释放。
    # 单次不会拖垮整个桥（每个连接一个线程），但连几百个就把线程耗光了。
    timeout = 30

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _safe_json(self, obj, code: int = 200) -> bool:
        """尽量把 JSON 发回去；响应已经开始写了就闭嘴。

        为什么需要：兜底的 except 里如果 send_response 又失败（客户端早断了），
        再抛一次异常就变成「异常处理里的异常」，更难查。
        """
        try:
            _json(self, obj, code)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _read_payload(self, limit: int = MAX_BODY):
        """读请求体并解析成 dict。**出错时直接回 400 并返回 None**。

        原来的写法是每个接口各自：
            payload = self._read_payload()
            if payload is None:
                return
        三个问题，全都在实测里复现过：
          · Content-Length 是负数或乱写 → read() 永远等不到，线程卡死
          · 声称 100MB 只发 10 字节 → 同样卡死（没人读就没人释放）
          · body 是数组/字符串/null → 后面 payload.get() 抛异常，
            而那几个接口外层没 try，客户端看到的是「连接被重置」
            （浏览器里就是 Failed to fetch，用户以为程序坏了）
        现在统一在这里挡掉，调用方只需要：
            payload = self._read_payload()
            if payload is None:
                return
        """
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len or 0)
        except (TypeError, ValueError):
            self._safe_json({"ok": False, "error": f"Content-Length 不合法：{raw_len!r}"}, 400)
            return None
        if n < 0:
            self._safe_json({"ok": False, "error": "Content-Length 不能是负数"}, 400)
            return None
        if n > limit:
            mb, lim = n / 1024 / 1024, limit / 1024 / 1024
            self._safe_json({"ok": False,
                             "error": f"请求太大（{mb:.1f} MB，上限 {lim:.0f} MB）"}, 413)
            return None
        if n == 0:
            return {}

        try:
            raw = self.rfile.read(n)
        except Exception as e:  # noqa: BLE001
            self._safe_json({"ok": False, "error": f"读取请求失败：{e}"}, 400)
            return None
        if len(raw) < n:
            self._safe_json({"ok": False,
                             "error": f"请求体只收到 {len(raw)}/{n} 字节（连接被中断？）"}, 400)
            return None

        try:
            obj = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._safe_json({"ok": False,
                             "error": f"JSON 格式错误：第 {e.pos} 个字符附近（{e.msg}）"}, 400)
            return None
        if not isinstance(obj, dict):
            self._safe_json({"ok": False,
                             "error": f"请求体应该是 JSON 对象，收到的是 {type(obj).__name__}"}, 400)
            return None
        return obj

    def do_OPTIONS(self):
        self.send_response(204)
        _cors(self)
        self.end_headers()

    def do_GET(self):
        """所有 GET 的统一入口（兜底见 _do_get 的说明）。"""
        try:
            self._do_get()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _append_log(f"[桥] GET {self.path} 出错{_where(e)}：{type(e).__name__}: {e}")
            self._safe_json({"ok": False, "error": f"服务内部错误：{e}"}, 500)

    def _do_get(self):
        # 这个函数里几个分支各自写了自己的 import，
        # 于是 parse_qs 被当成整个函数的局部名 —— 在前面用就 UnboundLocalError。
        # 统一在入口导一次，谁都能用。
        from urllib.parse import parse_qs, urlparse  # noqa: F401

        if self.path.startswith("/health"):
            # extVersion：磁盘上扩展目录的版本（程序自己读文件）。
            # 面板拿它跟内存里跑的版本对比，就能发现「文件更新了但扩展没重新加载」
            # —— 这种情况界面上版本号是新的，跑的还是旧代码，最难查。
            _ext = ""
            try:
                import json as _j
                _mf = paths.EXTENSION / "manifest.json"
                _ext = str(_j.loads(_mf.read_text(encoding="utf-8")).get("version") or "")
            except Exception:  # noqa: BLE001
                pass
            _json(self, {"ok": True, "service": "jingli-bridge",
                         "captureDir": str(CAPTURE_DIR),
                         "inbox": str(INBOX_DIR),
                         # 桌面窗口用它判断「现在跑着的桥是不是我这个程序的」：
                         # 目录不一样就说明是另一份程序起的，只有那种才要强停。
                         "root": str(ROOT),
                         "pid": os.getpid(),
                         "extVersion": _ext})
        elif self.path.startswith("/api/schedules"):
            self._get_schedules()
        elif self.path.startswith("/api/selfcheck"):
            _json(self, {"ok": True, "last": _SELFCHECK})
        elif self.path.startswith("/endpoints"):
            _json(self, rebuild_endpoints())
        elif self.path.startswith("/log"):
            # 500 行：7 个浏览器时，200 行只够 3-4 分钟，一查问题就被冲掉了
            _json(self, {"ok": True, "lines": TASK_LOG[-500:]})
        elif self.path.startswith("/pending"):
            # 按店铺领任务：扩展轮询时带上自己的 shop_id，只领属于自己的那个。
            #
            # 以前是一个全局队列、谁先来谁拿走。7 个浏览器各装一份扩展时，
            # 给 A 店派的任务会被 B 店的扩展领走 —— 跑出来串店，
            # 而且报表看起来完全正常。现在按 store 字段投递。
            q = parse_qs(urlparse(self.path).query)
            want = (q.get("shop") or [""])[0].strip()

            # 并发闸门：已经有权在跑就别再放新的进来。
            # 只算「十分钟内还有动静」的，避免某家卡死后把整队堵住。
            with _PROGRESS_LOCK:
                _now = time.time()
                _active = [
                    sid_ for sid_, st_ in _PROGRESS.items()
                    if sid_ != want
                    and str((st_ or {}).get("current") or "").strip()
                    and _now - float((st_ or {}).get("updated") or 0) < 600
                ]
            if len(_active) >= _MAX_ACTIVE_FETCH:
                _json(self, {"ok": True, "job": None, "busy": True,
                             "running": len(_active),
                             "queued": len(_PENDING)})
                return

            job = None
            dropped = 0
            with _PENDING_LOCK:
                now_ms = int(time.time() * 1000)
                alive = []
                for j in _PENDING:
                    if not isinstance(j, dict):
                        continue
                    # 还没到它上场的时刻（错峰派单）：先留着，别家先跑
                    if float(j.get("hold_until") or 0) > time.time():
                        alive.append(j)
                        continue
                    ttl = _AUTO_JOB_TTL_MS if j.get("auto") else _JOB_TTL_MS
                    if now_ms - int(j.get("at") or 0) > ttl:
                        dropped += 1
                        continue
                    alive.append(j)
                _PENDING[:] = alive
                for i, j in enumerate(_PENDING):
                    target = str(j.get("store") or "").strip()
                    if target and target != want:
                        continue          # 不是我的，留给它自己的扩展
                    job = _PENDING.pop(i)
                    break
            if dropped:
                _append_log(f"[pending] 清掉 {dropped} 个超时没人领的任务"
                            f"（计划任务 12 小时有效，手动任务 30 分钟）")
            _json(self, {"ok": True, "job": job, "shop": want})
        elif self.path.startswith("/api/run/stop"):
            # 扩展在跑的时候轮询这里：有没有人要求停。
            # 读一次就清掉——只停当前这一轮，不能把下一轮也停了。
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("shop") or [""])[0].strip()
            hit = False
            with _STOP_LOCK:
                now = time.time()
                for k in list(_STOP):
                    if now - _STOP[k] > _STOP_TTL:
                        _STOP.pop(k, None)
                if sid and (sid in _STOP or "*" in _STOP):
                    _STOP.pop(sid, None)
                    hit = True
            _json(self, {"ok": True, "stop": hit})
        elif self.path.startswith("/api/live"):
            # 界面轮询这一个口子就够了：谁在线、跑到哪一步、队列里还压着几个。
            self._get_live()
        elif self.path.startswith("/store_profile"):
            # 读店铺档案：换电脑后扩展从这里直接拿到"学过的"账户/场景，
            # 不用再开页面学一遍。档案跟着数据目录走，拷过去就生效。
            try:
                q = parse_qs(urlparse(self.path).query)
                sid = (q.get("shop_id") or [""])[0].strip()
                f = paths.store_root(sid) / "profile.json"
                prof = {}
                if sid and f.is_file():
                    try:
                        prof = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        prof = {}
                _json(self, {"ok": True, "profile": prof})
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/stores") or self.path.startswith("/api/stores"):
            _json(self, store_list())
        elif self.path == "/" or self.path.startswith("/index"):
            try:
                from bridge import ui
                _html(self, ui.render_html())
            except Exception as e:  # noqa: BLE001
                _html(self, f"<h3>控制台加载失败</h3><pre>{e}</pre>", 500)
        elif self.path.startswith("/api/status"):
            from bridge import ui
            _json(self, ui.status())
        elif self.path.startswith("/api/data-files"):
            from bridge import ui
            _json(self, ui.data_files())
        elif self.path.startswith("/api/snapshots"):
            from bridge import ui
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            _json(self, ui.snapshots((q.get("store") or [""])[0]))
        elif self.path.startswith("/api/presets"):
            from bridge import presets_api
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            if self.path.startswith("/api/presets/backups"):
                _json(self, presets_api.list_backups())
            else:
                _json(self, presets_api.list_presets((q.get("scope") or [""])[0]))
        elif self.path.startswith("/api/reports/zip"):
            # 勾选批量下载：打成一个 zip，zip 内保留店铺子目录
            from urllib.parse import parse_qs, urlparse
            from bridge import settings_api
            q = parse_qs(urlparse(self.path).query)
            names = [n for n in (q.get("names") or [""])[0].split("||") if n.strip()]
            try:
                data, fname = settings_api.reports_zip(names)
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"打包失败：{e}"}, 500)
                return
            if not data:
                _json(self, {"ok": False, "error": "没选中任何报表"}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(fname))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/reports"):
            from bridge import ui
            _json(self, ui.reports())
        elif self.path.startswith("/report?"):
            from bridge import ui
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            f = ui.find_report(name)
            if not f:
                _json(self, {"ok": False, "error": "找不到这个报表"}, 404)
            else:
                data = f.read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Length", str(len(data)))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(f.name)}")
                self.end_headers()
                self.wfile.write(data)
        elif self.path == "/config" or self.path.startswith("/config?"):
            # 旧地址：跳到新界面的「参数配置」页。
            # 界面合并成一个应用后不再维护第二套页面，这里只做跳转，
            # 免得有人收藏了旧链接打不开。
            self.send_response(302)
            self.send_header("Location", "/#/params")
            self.end_headers()
        elif self.path.startswith("/api/mapping"):
            from bridge import settings_api
            _json(self, settings_api.read_mapping())
        elif self.path.startswith("/api/costs/template/save"):
            # 窗口内（WebView2）点下载链接不会弹下载框，所以另给一个
            # 「直接存到报表目录」的接口，前端提示用户文件在哪。
            from bridge import costs_api
            from src import paths as _paths
            from src import settings as _settings
            from pathlib import Path as _P
            try:
                from urllib.parse import parse_qs, urlparse as _up
                _q = parse_qs(_up(self.path).query)
                dir_param = (_q.get("dir") or [""])[0].strip()
                data = costs_api.make_template()
                # 优先用成本表页填的目录；其次设置里配的；都没有就落到程序的「报表」目录。
                td = dir_param or ((_settings.load().get("report") or {}).get("template_dir") or "").strip()
                out_dir = _P(td).expanduser() if td else _paths.OUTPUT
                out = out_dir / "成本表导入模板.xlsx"
                out.parent.mkdir(parents=True, exist_ok=True)
                from src import safeio
                safeio.write_bytes(out, data)
                _json(self, {"ok": True, "path": str(out)})
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"生成模板失败：{e}"}, 500)
        elif self.path.startswith("/api/costs/template"):
            from bridge import costs_api
            try:
                data = costs_api.make_template()
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"生成模板失败：{e}"}, 500)
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote("成本表导入模板.xlsx"))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/bridge/status"):
            from src import paths as _paths
            from pathlib import Path as _P2
            _json(self, {
                "ok": True,
                "port": PORT,
                "root": str(_paths.DATA_HOME),
                "exe_dir": str(_P2(sys.executable).resolve().parent),
            })
        elif self.path.startswith("/api/settings"):
            from src import settings as cfg_settings
            _json(self, {"ok": True, "settings": cfg_settings.load()})
        elif self.path.startswith("/api/rates"):
            from bridge import settings_api
            _json(self, settings_api.read_rates())
        elif self.path.startswith("/api/report-view"):
            from bridge import settings_api
            from urllib.parse import parse_qs, urlparse as _up
            q = parse_qs(_up(self.path).query)
            name = (q.get("name") or [""])[0]
            rel = (q.get("rel") or [""])[0]
            # 前端给的 name 可能就是「自动/店名/文件.xlsx」这种相对路径。
            # 以前只传 name，_find_report 看到带 / 就当成非法名字 → 永远「找不到这个报表」。
            if not rel and ("/" in name or "\\" in name):
                rel = name.replace("\\", "/")
            _json(self, settings_api.report_view(name if not rel else "", rel))
        elif self.path.startswith("/api/trends"):
            from bridge import ui
            _json(self, ui.trends())
        elif self.path.startswith("/api/history"):
            from bridge import settings_api
            _json(self, settings_api.history())
        elif self.path.startswith("/api/debug"):
            from bridge import settings_api
            _json(self, settings_api.debug_info())
        elif self.path.startswith("/api/config"):
            from bridge import config_page
            _json(self, config_page.read_config())
        elif self.path.startswith("/api/order-headers"):
            from bridge import config_page
            _json(self, config_page.order_headers())
        elif self.path.startswith("/api/manual"):
            # 手动核算：按店铺（+临时）分抽屉，跟店铺收件箱完全隔开
            from bridge import manual_api
            # running：哪几家正在算。界面刷新后也要能显示「计算中…」和停止按钮，
            # 不能只靠前端那次点击留下的内存状态。
            _json(self, {"ok": True, "spaces": manual_api.list_spaces(),
                         "running": manual_api.running_keys()})
        elif self.path.startswith("/api/capture"):
            # 「今天这个区间取过没有」——扩展跑之前问一句，取过的就不再导。
            # 参数：shop / from / to，缺区间就直说判断不了（照常全部重取）。
            from src import capture as _cap
            from urllib.parse import parse_qs, urlparse as _up
            q = parse_qs(_up(self.path).query)
            shop = (q.get("shop") or [""])[0].strip()
            sid = _canon_shop(shop) if shop else ""
            st = _cap.status(sid, (q.get("from") or [""])[0],
                            (q.get("to") or [""])[0])
            if sid:
                st["today"] = _cap.today(sid)
            _json(self, st)
        elif self.path.startswith("/api/costs"):
            from bridge import costs_api
            from urllib.parse import parse_qs, urlparse as _up
            q = parse_qs(_up(self.path).query)
            if q.get("check"):
                _json(self, costs_api.check_costs())
            else:
                try:
                    limit = int((q.get("limit") or ["0"])[0])
                except ValueError:
                    limit = 0
                _json(self, costs_api.read_costs(limit))
        else:
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    def do_POST(self):
        """所有 POST 的统一入口。

        以前每个分支各写各的 try，漏一个就变成「连接被重置」：
        浏览器只看到 Failed to fetch，界面上什么提示都没有，日志里也一片空白。
        实测有 7 个接口（自动运行、计划任务 4 个、改店名、参数页、导入数据）
        被一个格式不对的请求就打断了。这里兜一层：任何没被接住的异常
        都变成一条能看懂的 JSON 错误，同时写进日志。
        """
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _append_log(f"[桥] POST {self.path} 出错{_where(e)}：{type(e).__name__}: {e}")
            self._safe_json({"ok": False, "error": f"服务内部错误：{e}"}, 500)

    def _do_post(self):
        if self.path.startswith("/api/reports/delete"):
            # 注意必须在 do_POST 里：页面是 POST 过来的，
            # 之前误放在 do_GET 分支，于是报「unknown endpoint」。
            try:
                payload = self._read_payload()
                if payload is None:
                    return
                names = payload.get("names") or []
                if not isinstance(names, list) or not names:
                    _json(self, {"ok": False, "error": "没选中报表"}, 400)
                    return
                from bridge import ui
                r = ui.delete_reports([str(x) for x in names])
                _append_log(f"[报表] 删除了 {len(r.get('moved') or [])} 份（已移到 _已删除）")
                _json(self, r)
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"删除失败：{e}"}, 500)
        elif self.path.startswith("/api/reports/save"):
            # 「下载」不再靠浏览器跳链接（窗口里不弹下载框，点了没反应），
            # 改成由桥直接存盘，然后告诉界面存到哪了。
            try:
                payload = self._read_payload()
                if payload is None:
                    return
                names = [str(x) for x in (payload.get("names") or []) if str(x).strip()]
                if not names:
                    _json(self, {"ok": False, "error": "没选中报表"}, 400)
                    return
                from bridge import settings_api
                from src import settings as cfg_settings
                stg = cfg_settings.load().get("report", {})
                # 空目录时按「哪个房间」退回各自的默认值：
                # 手动报表不该因为输入框被清空就掉进自动报表的目录里
                _room = "manual" if str(payload.get("room") or "") == "manual" else "auto"
                _fallback = (stg.get("manual_download_dir") if _room == "manual"
                             else stg.get("download_dir")) or ""
                raw_dir = str(payload.get("dir") or _fallback).strip()
                target = Path(raw_dir).expanduser() if raw_dir else Path.home() / "Desktop"
                if not target.is_dir():
                    try:
                        # 用户可能手打一个新路径，先试着建出来，不要默默改存桌面
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        target = Path.home() / "Desktop"
                saved: list[str] = []
                failed: list[str] = []
                tmp_dirs: list[Path] = []
                for rel in names:
                    src = settings_api._find_report("", rel)
                    if src is None or not src.is_file():
                        failed.append(f"{rel}（找不到文件）")
                        continue
                    if payload.get("with_formula"):
                        # 带公式版要按原区间重算一次：文件名里一般带着区间。
                        # 手动核算的「全部」报表没有区间（名字里是 _全部_），
                        # 那就按全量算——但其它认不出区间的（自定义命名模板）
                        # 不能瞎猜成全量，否则算出来的数字跟原报表对不上。
                        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", src.name)
                        if not m and "_全部_" not in src.name:
                            failed.append(f"{rel}（名字里认不出区间）")
                            continue
                        _d1 = m.group(1) if m else ""
                        _d2 = m.group(2) if m else ""
                        # rel 现在是「房间/店名/文件」；老布局是「店名/文件」
                        _parts = str(rel).replace("\\", "/").split("/")
                        if _parts[0] in ("自动", "手动") and len(_parts) > 2:
                            shop = _parts[1]
                        else:
                            shop = _parts[0] if len(_parts) > 1 else ""
                        sid = _shop_id_by_name(shop)
                        # 手动报表要按「手动导入」那份数据重算；拿收件箱的算
                        # 出来的带公式版，数字跟原报表对不上，还没人看得出来
                        _data_dir = None
                        if _parts[0] == "手动" or "手动" in _parts[:-1]:
                            from bridge import manual_api as _mn
                            if _parts[0] == "手动" and len(_parts) > 2:
                                _data_dir = _mn.space_dir(sid) if sid else None
                            elif _parts[0] == "临时手动":
                                _data_dir = _mn.tmp_dir()
                            if _data_dir is None:
                                # 认不出是哪家店时**不能**退回收件箱：
                                # 那样算出来的数字是自动取数那批的，
                                # 跟原报表对不上，还没人看得出来。
                                failed.append(f"{rel}（认不出是哪家店，没法按手动数据重算）")
                                continue
                        # 重算的产物放临时目录：下完就删，别在「报表」页堆一堆 _带公式
                        _tmp = Path(tempfile.mkdtemp(prefix="jingli_dl_"))
                        tmp_dirs.append(_tmp)
                        out = _tmp / f"{Path(rel).stem}_带公式.xlsx"
                        ok, msg = _run_calc_cmd(
                            _calc_cmd(sid, "", _d1, _d2, out, True,
                                      data_dir=_data_dir))
                        if not ok:
                            failed.append(f"{rel}（{str(msg)[-80:]}）")
                            continue
                        src = out
                    dst = target / src.name
                    # 单份存失败不能拖垮整批：目标被 Excel 占着、选的就是报表自己的
                    # 目录（同一个文件）都会抛异常，以前整批就 500 了
                    try:
                        if src.resolve() == dst.resolve():
                            saved.append(str(dst))
                            continue
                        shutil.copy2(src, dst)
                        saved.append(str(dst))
                    except OSError as e:
                        failed.append(f"{rel}（{e}）")
                for _d in tmp_dirs:
                    shutil.rmtree(_d, ignore_errors=True)
                _append_log(f"[报表] 存了 {len(saved)} 份到 {target}")
                _json(self, {"ok": bool(saved), "dir": str(target),
                             "saved": saved, "failed": failed})
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)
        elif self.path.startswith("/hello"):
            self._post_hello()
        elif self.path.startswith("/progress"):
            self._post_progress()
        elif self.path.startswith("/api/run/stop"):
            try:
                p = self._read_payload() or {}
            except Exception:  # noqa: BLE001
                p = {}
            mkey = str(p.get("manual_key") or "").strip()
            if mkey:
                from bridge import manual_api
                okc = manual_api.cancel(mkey)
                _append_log(f"[stop] 手动核算停止：{mkey}（{'已掐掉' if okc else '没在跑'}）")
                _json(self, {"ok": True, "cancelled": okc})
                return
            sid = str(p.get("shop_id") or "").strip()
            keys = ["*"] if (not sid or p.get("all")) else [sid]
            with _STOP_LOCK:
                for k in keys:
                    _STOP[k] = time.time()
            who = "全部店铺" if keys == ["*"] else (p.get("store_name") or sid)
            _append_log(f"[stop] 收到停止请求：{who}")
            _json(self, {"ok": True, "stopping": keys})
        elif self.path.startswith("/api/run-stores"):
            self._post_run_stores()
        elif self.path.startswith("/api/mapping"):
            self._post_mapping()
        elif self.path.startswith("/store_profile"):
            # 店铺档案：扩展第一次跑这家店时，把"这家店的运行时事实"
            # （站点代码、广告账户 ID、导出场景…）写进店里，以后直接用。
            # 存进数据目录而不是配置文件：档案是跟着店走的，换电脑拷数据就行。
            try:
                prof = self._read_payload()
                if prof is None:
                    return
                sid = str(prof.get("shop_id") or "").strip()
                if not sid:
                    _json(self, {"ok": False, "error": "缺少 shop_id"}, 400)
                    return
                d = paths.store_root(sid)
                d.mkdir(parents=True, exist_ok=True)
                # 合并写：每次只更新这次学到的键，别把上次学的冲掉
                old = {}
                f = d / "profile.json"
                if f.is_file():
                    try:
                        old = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        old = {}
                old.update({k: v for k, v in prof.items() if v not in (None, "")})
                old["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                from src import safeio
                safeio.write_json(f, old)
                _json(self, {"ok": True, "path": str(f)})
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"档案保存失败：{e}"}, 500)
        elif self.path.startswith("/api/stores/remove"):
            try:
                payload = self._read_payload()
                if payload is None:
                    return
                sid = str(payload.get("id") or "").strip()
                if not sid:
                    _json(self, {"ok": False, "error": "缺少店铺 ID"}, 400)
                    return
                r = remove_store(sid)
                _json(self, r, 200 if r.get("ok") else 400)
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"移除失败：{e}"}, 500)
        elif self.path.startswith("/api/stores/rename"):
            self._post_rename_store()
        elif self.path.startswith("/identify"):
            self._post_identify()
        elif self.path.startswith("/api/selfcheck"):
            # 手动点「体检一次」：做完整版（含报表体检），慢一点但查得全
            try:
                from src import selfcheck
                rep = selfcheck.run(verbose=True, check_crash=False)
                _SELFCHECK.clear()
                _SELFCHECK.update(rep)
                for a in rep.get("actions") or []:
                    _append_log(f"[自检] {a}")
                _json(self, {"ok": True, "last": _SELFCHECK})
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"自检失败：{e}"}, 500)
        elif self.path.startswith("/api/schedules/preview"):
            self._post_schedules_preview()
        elif self.path.startswith("/api/schedules/save"):
            self._post_schedules_save()
        elif self.path.startswith("/api/schedules/run"):
            self._post_schedules_run()
        elif self.path.startswith("/api/schedules/catchup"):
            self._post_schedules_catchup()
        elif self.path.startswith("/api/settings"):
            self._post_settings()
        elif self.path.startswith("/api/auto-run-now"):
            self._post_auto_now()
        elif self.path.startswith("/api/rates"):
            self._post_rates()
        elif self.path.startswith("/api/config/preview"):
            self._post_config(preview=True)
        elif self.path.startswith("/api/config/save"):
            self._post_config(preview=False)
        elif self.path.startswith("/api/cost-fields"):
            self._post_cost_fields()
        elif self.path.startswith("/api/costs/save"):
            self._post_costs(save=True)
        elif self.path.startswith("/api/data-files/delete"):
            # 导入的文件挪进回收站（不真删），界面上可批量选
            try:
                from bridge import ui
                payload = self._read_payload()
                if payload is None:
                    return
                rels = payload.get("rels")
                if not isinstance(rels, list):
                    _json(self, {"ok": False, "error": "缺少 rels"}, 400)
                    return
                r = ui.delete_data_files([str(x) for x in rels])
                _append_log(f"[导入] 清理 {r.get('moved', 0)} 个文件（移到回收站，可恢复）")
                _json(self, r)
            except Exception as e:  # noqa: BLE001
                _json(self, {"ok": False, "error": f"删除失败：{e}"}, 500)
        elif self.path.startswith("/api/data-import"):
            self._post_data_import()
        elif self.path.startswith("/api/manual/upload"):
            self._post_manual_upload()
        elif self.path.startswith("/api/manual/clear"):
            self._post_manual_clear()
        elif self.path.startswith("/api/manual/delete"):
            self._post_manual_delete()
        elif self.path.startswith("/api/manual/run"):
            self._post_manual_run()
        elif self.path.startswith("/api/link-info"):
            self._post_link_import()
        elif self.path.startswith("/api/presets/"):
            self._post_presets()
        elif self.path.startswith("/api/costs/import"):
            self._post_costs(save=False)
        elif self.path.startswith("/capture"):
            self._post_capture()
        elif self.path.startswith("/upload-raw"):
            self._post_upload_raw()
        elif self.path.startswith("/upload"):
            self._post_upload()
        elif self.path.startswith("/log"):
            self._post_log()
        elif self.path.startswith("/run"):
            self._post_run()
        elif self.path.startswith("/pending"):
            self._post_pending()
        elif self.path.startswith("/spec"):
            self._post_spec()
        elif self.path.startswith("/fetch-result"):
            self._post_fetch_result()
        else:
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    def _post_capture(self):
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        batch = payload.get("batch") or []
        if not isinstance(batch, list):
            _json(self, {"ok": False, "error": "batch must be a list"}, 400)
            return

        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        # 指定 file 就写到单独文件里（广告导出的抓包走这个，
        # 免得跟几十万条日常流量混在一起翻不动）
        name = str(payload.get("file") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+\.jsonl", name):
            f = CAPTURE_DIR / f"capture-{datetime.now():%Y%m%d}.jsonl"
        else:
            f = CAPTURE_DIR / name
        with f.open("a", encoding="utf-8", errors="replace") as fh:
            for rec in batch:
                if not isinstance(rec, dict):
                    continue
                rec["_receivedAt"] = datetime.now().isoformat(timespec="seconds")
                # 页面里的字符串可能被截断出半个代理对（lone surrogate），
                # 直接写会让整个文件出现非法 UTF-8 字节，后续所有读取都报错。
                line = json.dumps(rec, ensure_ascii=False)
                line = line.encode("utf-8", "replace").decode("utf-8", "replace")
                fh.write(line + "\n")
        _json(self, {"ok": True, "saved": len(batch), "file": f.name})

    def _post_config(self, preview: bool):
        """参数表试算 / 保存。"""
        from bridge import config_page

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"invalid payload: {e}"}, 400)
            return

        params = payload.get("params")
        if not isinstance(params, list):
            _json(self, {"ok": False, "error": "params 必须是列表"}, 400)
            return

        if preview:
            _json(self, config_page.preview(
                params, payload.get("formulas"),
                str(payload.get("country") or ""),
                payload.get("by_country"),
                payload.get("order_fields"),
                payload.get("cost_fields"),
                payload.get("exclude_zero_amount")))
            return

        try:
            # 前端发什么，这里就得往下传什么。
            # 之前只传了 6 个字段，报表列 / 合计位置 / 列标题 / 广告表头 / 佣金表头 /
            # 映射表头全被吞掉——界面提示「已保存」，其实一个都没写进去，
            # 刷新后又变回默认（表现就是「勾选保存后又恢复全选」）。
            _json(self, config_page.save_config(
                params, payload.get("formulas") or {},
                by_country=payload.get("by_country"),
                order_fields=payload.get("order_fields"),
                cost_fields=payload.get("cost_fields"),
                exclude_zero_amount=payload.get("exclude_zero_amount"),
                report_columns=payload.get("report_columns"),
                report_column_order=payload.get("report_column_order"),
                report_total_position=payload.get("report_total_position"),
                report_titles=payload.get("report_titles"),
                ads_fields=payload.get("ads_fields"),
                affiliate_fields=payload.get("affiliate_fields"),
                mapping_fields=payload.get("mapping_fields")))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_upload(self):
        """扩展把下载到的导出文件回传到这里（base64 + JSON 的老通道）。

        大文件请走 `/upload-raw`（见那里的说明）。这条留着是因为
        "本地导入"等老调用方还在用，而且它是纯 JSON，好调试。
        """
        try:
            payload = self._read_payload(MAX_UPLOAD_BODY)
            if payload is None:
                return
            name = str(payload.get("name") or "").strip()
            b64 = payload.get("b64") or ""
            if not name or not b64:
                raise ValueError("missing name/b64")
            data = base64.b64decode(b64)
        except Exception as e:
            _json(self, {"ok": False, "error": f"invalid payload: {e}"}, 400)
            return
        self._finish_upload(payload, data, name)

    def _post_upload_raw(self):
        """二进制直传：body 就是文件本身，其余参数在 query 上。

        为什么要有这条：大店订单 40~80MB，base64 后还要再涨 33%。
        老通道上限 8MB，一撞就回 413——而 413 之后服务端不会读走 body，
        连接被直接重置，浏览器那边只看得到 `Failed to fetch`，
        跟"网线断了"一模一样，查一天也查不出是"文件太大"。

        实测对照：Crochet lover 订单 5.5MB（base64 后 7.3MB，压线过）一直成功，
        Chroma Home 5.7 万单 40MB 从来没成功过。
        """
        q = parse_qs(urlparse(self.path).query)
        name = (q.get("name") or [""])[0].strip()
        if not name:
            _json(self, {"ok": False, "error": "missing name"}, 400)
            return
        payload = {
            "name": name,
            "dir": (q.get("dir") or [""])[0],
            "shop_id": (q.get("shop_id") or [""])[0],
            "shop_name": (q.get("shop_name") or [""])[0],
        }
        raw_range = (q.get("range") or [""])[0]
        if raw_range:
            try:
                payload["range"] = json.loads(raw_range)
            except (ValueError, json.JSONDecodeError):
                payload["range"] = None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            _json(self, {"ok": False, "error": "空请求（没有 Content-Length）"}, 400)
            return
        if n > MAX_RAW_UPLOAD:
            _json(self, {"ok": False,
                         "error": f"文件太大（{n / 1048576:.0f} MB，"
                                  f"上限 {MAX_RAW_UPLOAD / 1048576:.0f} MB）"}, 413)
            return
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(1 << 20, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) != n:
            _json(self, {"ok": False, "error": f"上传中断（收到 {len(buf)}/{n} 字节）"}, 400)
            return
        self._finish_upload(payload, bytes(buf), name)

    def _finish_upload(self, payload: dict, data: bytes, name: str):
        """认出这是哪类表，落到对应目录，并记账（两个上传入口共用）。"""
        kind = str(payload.get("dir") or "").lower()
        shop_id = str(payload.get("shop_id") or "").strip()

        safe = re.sub(r'[\\/:*?"<>|]', "_", name)

        # 先落到临时位置，认出是什么表再决定放哪个子目录。
        # （直接按名字猜类型不行：平台导出的文件名千奇百怪，
        #   有时同一类表名字还带不同后缀。）
        tmp_dir = paths.EXPORTS / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # 加纳秒后缀：两家店同时回传时同一毫秒内会撞名，撞了就会互相覆盖
        tmp_path = tmp_dir / f"upload-{time.time_ns()}-{safe}"
        try:
            # 必须带 fsync：只 write 不落盘的话，断电重启后这个临时文件
            # 可能「名字在、内容是空」，被挪进 inbox 就是一张空表——
            # 算出来会说「该时段没有订单」，其实是数据根本没写进去。
            from src import safeio
            safeio.write_bytes(tmp_path, data, backup=False)
        except OSError as e:
            _json(self, {"ok": False, "error": f"写文件失败：{e}"}, 500)
            return

        table_kind = ""
        try:
            from src.loader import classify_rows, read_table
            table_kind = classify_rows(read_table(tmp_path)) or ""
        except Exception:  # noqa: BLE001
            table_kind = ""

        warning = ""
        if kind == "capture":
            # 诊断类文件（导出流程记录）不进 inbox、不参与核算，
            # 单独放 数据/抓包记录/，开发侧直接看
            target_dir = paths.CAPTURE
        elif kind == "mapping" or table_kind == "mapping":
            # 映射表（商品管理 SKU、链接资料）走全局累积主表：
            # 链接改绑后老 SKU 也要能查到归属，所以不能只留最新一份。
            target_dir = MAPPING_DIR
        elif shop_id:
            # 表头认不出来时，用扩展告诉我们的「这张表是从哪个页面导的」兜底。
            # 以前一律落到 orders，结果广告表、佣金表全堆进「订单表」——
            # 核算照样能算（loader 按表头认表），但人打开文件夹是懵的。
            _k = table_kind or (kind if kind in ("orders", "ads", "affiliate") else "orders")
            target_dir = paths.store_kind_dir(shop_id, _k)
            remember_store(shop_id, payload)
        else:
            # 认不出店铺：单独隔一个专区，绝不混进任何一家已知店
            target_dir = paths.unknown_dir() / paths.kind_dirname(table_kind)
            warning = (f"没能识别出店铺，这份「{safe}」已存到 {target_dir}，"
                       f"不会参与核算。请在扩展里重新登录该店铺后台再取一次，"
                       f"或在「店铺」页把文件挪到对应店铺。")

        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / safe
        if path.exists():
            stem, suf = path.stem, path.suffix
            i = 2
            while path.exists():
                path = target_dir / f"{stem} ({i}){suf}"
                i += 1
        try:
            shutil.move(str(tmp_path), str(path))
        except OSError as e:
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)
            return
        who = f"[{shop_id}] " if shop_id else "[未识别店铺] "
        _append_log(f"{who}文件已保存：{path.name}（{len(data)/1024:.0f} KB）")
        if warning:
            _append_log(f"[warn] {warning}")
        # 记账：哪家店、哪个区间、哪一类表，今天已经取到了。
        # 同一天再跑同区间就直接复用这几份，不再去平台重导（见 src/capture.py）。
        _marked = _mark_capture(shop_id, table_kind, payload, path)
        out = {"ok": True, "path": str(path), "bytes": len(data),
               "shop_id": shop_id, "store_dir": str(target_dir),
               "kind": table_kind or "unknown"}
        if warning:
            out["warning"] = warning
        if _marked:
            out["captured"] = _marked
        # 这里不再复制整份快照：每轮取数会传 4 个文件，逐个复制就等于
        # 同一批订单存 4 份（大店 40MB × 4）。核算前（/run）会统一存一份，
        # 那时数据才齐，也才有「这批数据算过一次」的意义。
        if kind == "mapping":
            _mirror_mapping_to_store(shop_id, path)
        _json(self, out)

    def _post_presets(self):
        """预设：保存 / 应用 / 删除 / 回滚。"""
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}

        from bridge import presets_api

        act = ""
        p = self.path
        for name in ("save", "apply", "delete", "update", "rollback"):
            if p.startswith(f"/api/presets/{name}"):
                act = name
                break
        if not act:
            _json(self, {"ok": False, "error": "不认识的操作"}, 400)
            return
        fn = {
            "save": presets_api.save_preset,
            "apply": presets_api.apply_preset,
            "delete": presets_api.delete_preset,
            "update": presets_api.update_preset,
            "rollback": presets_api.rollback,
        }[act]
        try:
            r = fn(payload)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": str(e)}
        if act == "apply" and r.get("ok"):
            _append_log(f"[preset] 已应用预设：{r.get('applied')}")
        _json(self, r)

    def _post_data_import(self):
        """手动导入导出文件——自动取数挂了的时候的兜底通路。

        场景：扩展抓不到接口、平台改版、风控参数失效、店铺登录态掉了……
        这时候你自己进 TikTok 后台把表导出来，在这里上传，
        落进该店铺的 inbox 目录，之后跟自动取到的数据一模一样地参与核算。

        导入时必须能选**国家/店铺**，因为：
          · 费率表（佣金、手续费、服务费…）是按国家分开的
          · 订单表的列名是按国家/语言分开的（马来站导出的表头跟菲律宾不一样）
        选错国家 → 列认不出来、费率套错，数字会全错还不报错。
        """
        try:
            payload = self._read_payload(MAX_UPLOAD_BODY)
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        name = str(payload.get("name") or "").strip()
        b64 = payload.get("b64") or ""
        if not name or not b64:
            _json(self, {"ok": False, "error": "缺少文件名或内容"}, 400)
            return
        data, _err = _decode_b64(b64)
        if data is None:
            _json(self, {"ok": False, "error": _err}, 400)
            return

        suffix = Path(name).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".csv", ".txt", ".json"}:
            _json(self, {"ok": False, "error":
                         f"不支持的文件类型：{suffix or '（没有扩展名）'}"}, 400)
            return

        shop_id = str(payload.get("shop_id") or "").strip()
        country = str(payload.get("country") or "").strip()
        if not country and shop_id:
            # 「跟随店铺」：国家按这家店在配置里的国家来
            for st in store_list().get("stores", []):
                if st.get("id") == shop_id or st.get("shop_id") == shop_id:
                    country = st.get("country") or ""
                    break

        # 按**所选国家**的列标题来认表。
        # 各国站导出的表头不一样：马来站叫「订单金额」，菲律宾站未必这么叫。
        # 用默认别名认表，选错国家就会「没认出来」或者认成别的类型，
        # 而费率也会套错——数字会全错还不报错。
        aliases = None
        if country:
            try:
                from src import formula as _formula
                from src.loader import merged_aliases
                _fcfg = _formula.load_config(paths.CONFIG / "formulas.json")
                _cc = _formula.country_config(_fcfg, country)
                aliases = merged_aliases(_cc.get("order_fields"))
            except Exception:  # noqa: BLE001
                aliases = None

        # 先落到临时位置，靠表头认出这是张什么表，再决定放进哪个目录：
        #   商品映射表 → data/mapping/（跟订单、广告分开）
        #   其余       → data/stores/<店>/inbox/<类型>/
        # 映射表混进 inbox 的话，核算扫目录时会把整张表当成交付文件处理一遍。
        tmp_dir = paths.DATA / "_import_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # 加随机尾巴：两个窗口同时传同名文件时，同一秒+同一 PID 会撞同一个临时文件
        tmp = tmp_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{os.urandom(3).hex()}{suffix}"
        kind = ""
        try:
            tmp.write_bytes(data)
            try:
                from src.loader import classify_rows, read_table
                kind = classify_rows(read_table(tmp), aliases) or ""
            except Exception:
                kind = ""
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        # 用户点「单独导入商品映射表」时会带 kind=mapping：
        # 表头不标准、自动认不出也要按映射表存，否则白传一趟。
        forced = str(payload.get("kind") or "").strip()
        if forced and not kind:
            kind = forced

        if kind == "mapping" or payload.get("kind") == "mapping":
            target_dir = MAPPING_DIR
        elif shop_id:
            target_dir = paths.store_kind_dir(shop_id, kind or "orders")
        else:
            target_dir = paths.unknown_dir() / paths.kind_dirname(kind)
        target_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = re.sub(r'^手动导入-\d{8}-\d{6}-', "", Path(name).stem)
        safe = re.sub(r'[\\/:*?"<>|]', "_", stem) or "data"
        path = target_dir / f"手动导入-{stamp}-{safe}{suffix}"
        i = 2
        while path.exists():
            path = target_dir / f"手动导入-{stamp}-{safe} ({i}){suffix}"
            i += 1
        try:
            safeio.write_bytes(path, data, backup=False)
        except OSError as e:
            _json(self, {"ok": False, "error": f"写文件失败：{e}"}, 500)
            return

        # 回一句「认出来是张什么表」。认不出来就直说，
        # 不然文件躺在那儿，用户还以为传上去就算用上了。
        label = {"order": "订单表", "ads": "广告表", "affiliate": "联盟佣金表",
                 "mapping": "商品映射表（SKU→商品ID）"}.get(kind, "没认出来")
        who = f"[{shop_id}] " if shop_id else ""
        _append_log(f"{who}手动导入：{path.name} → {label}")
        out = {"ok": True, "path": str(path), "name": path.name,
               "kind": kind, "label": label, "bytes": len(data),
               "country": country or "未指定", "store_dir": str(target_dir)}
        if not kind:
            out["warning"] = (f"按{country or '默认'}的列标题没认出这是什么表，"
                              f"文件已存到 {target_dir}，但核算时可能用不上。"
                              f"请确认国家选对了。")
        elif kind == "order" and not shop_id:
            out["warning"] = "没选店铺，这份订单表暂存在未识别专区，选好店铺后需要挪过去才能参与核算。"
        # 同样不在这里存快照：真正开算时（/run）会统一存一份
        _json(self, out)

    def _post_link_import(self):
        """导入店铺后台下载的「链接资料」表，读出每条链接的款式编码。

        报表上「款式编码」那一列靠它。
        店铺后台的表没有款式编码列，程序从商家 SKU 推导
        （V27-White-10Pcs → V27），跟你手工维护的那列效果一致。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            data = base64.b64decode(payload.get("b64") or "")
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"内容解码失败：{e}"}, 400)
            return
        if not data:
            _json(self, {"ok": False, "error": "文件是空的"}, 400)
            return

        name = str(payload.get("name") or "链接资料.xlsx")
        suffix = Path(name).suffix.lower() or ".xlsx"
        tmp_dir = paths.DATA / "_import_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"link-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{os.urandom(3).hex()}{suffix}"
        try:
            tmp.write_bytes(data)
            from src.loader import parse_mapping, read_table
            idx = parse_mapping(read_table(tmp))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"解析失败：{e}"}, 500)
            return
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        if not len(idx):
            _json(self, {"ok": False,
                         "error": "这张表里没找到「商品 ID」和「SKU ID」两列，"
                                  "请确认下载的是店铺后台的「批量编辑 / 商品信息」表。"}, 400)
            return

        # 存进映射目录：核算时会被自动合并进映射主表
        import csv as _csv
        MAPPING_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = MAPPING_DIR / f"链接资料-{stamp}.csv"
        sku_of_item: dict[int, str] = {}
        for sku, items in idx.by_seller_sku.items():
            for it in items:
                sku_of_item.setdefault(id(it), sku)
        with out_path.open("w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["sku_id", "product_id", "seller_sku",
                                              "product_name", "style_code"])
            w.writeheader()
            for sid, item in idx.by_sku_id.items():
                w.writerow({"sku_id": sid, "product_id": item["product_id"],
                            "seller_sku": sku_of_item.get(id(item), ""),
                            "product_name": item.get("product_name", ""),
                            "style_code": item.get("style_code", "")})

        n_sku = len(idx.by_sku_id)
        n_link = len(idx.by_product_styles)
        n_multi = sum(1 for v in idx.by_product_styles.values() if len(v) > 1)
        _append_log(f"[link] 导入链接资料：{n_sku} 个 SKU / {n_link} 条链接"
                    f"（{n_multi} 条含多个款式编码）")
        _json(self, {"ok": True, "path": str(out_path), "name": out_path.name,
                     "sku_count": n_sku, "link_count": n_link, "multi_count": n_multi,
                     "samples": [{"link": pid, "styles": idx.styles_of(pid)}
                                 for pid in list(idx.by_product_styles)[:8]]})

    def _post_mapping(self):
        from bridge import settings_api
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            _json(self, settings_api.save_mapping(payload.get("fields") or []))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_rename_store(self):
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        _json(self, rename_store(str(payload.get("shop_id") or ""),
                                 str(payload.get("name") or "")))

    def _post_identify(self):
        """扩展上报「我现在登的是哪家店」。

        扩展在 seller 域页面里调 /api/v1/affiliate/account/info_v2，
        把 shop_id / shop_name / region 带过来。桥据此：
          · 记住这家店（以后不用手工登记）
          · 后续上传的数据落到 data/stores/<shop_id>/inbox
        多店铺就靠这个分开，不同店的数据永远不会混在一个文件夹里。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        shop_id = str(payload.get("shop_id") or payload.get("shopId") or "").strip()
        if not shop_id:
            _json(self, {"ok": False, "error": "缺少 shop_id"}, 400)
            return

        remember_store(shop_id, payload)
        d = discovered_stores()
        mine = next((x for x in d if x["shop_id"] == shop_id), {})
        _append_log(f"[identify] 店铺 {mine.get('shop_name') or shop_id}"
                    f"（{mine.get('country') or mine.get('region') or '?'}）")
        _json(self, {
            "ok": True,
            "shop_id": shop_id,
            "shop_name": mine.get("shop_name") or "",
            "country": mine.get("country") or "",
            "store_dir": str(paths.store_dir(shop_id)),
        })

    def _post_hello(self):
        """扩展心跳：报上「我是哪家店、什么版本、在不在跑」。

        为什么非要有它：界面上得能回答「这家店的浏览器开着没」。
        以前只有 meta.json 里的「上次上传时间」，那只说明以前传过数据，
        不代表现在还连着 —— 于是点了运行一直排队，人以为程序卡了。
        """
        try:
            p = self._read_payload()
            if p is None:
                return
        except Exception:  # noqa: BLE001
            _json(self, {"ok": False}, 400)
            return
        sid = str(p.get("shop_id") or "").strip()
        if sid:
            with _ONLINE_LOCK:
                _ONLINE[sid] = {
                    "at": time.time(),
                    "shop_name": str(p.get("shop_name") or ""),
                    "region": str(p.get("region") or ""),
                    "ver": str(p.get("ver") or ""),
                    "running": p.get("running") or None,
                }
        _json(self, {"ok": True, "steps": list(RUN_STEPS)})

    def _post_progress(self):
        """跑一步报一步。桥只做合并，不落盘（重启就没了，够界面看）。

        请求体：
          {shop_id, task, run_id, step, state, detail, elapsed, error}
          state: running / done / fail / skip；step 传 "__finish__" 表示整轮结束
        """
        try:
            p = self._read_payload()
            if p is None:
                return
        except Exception:  # noqa: BLE001
            _json(self, {"ok": False}, 400)
            return
        sid = str(p.get("shop_id") or "").strip()
        if not sid:
            _json(self, {"ok": False, "error": "缺少 shop_id"}, 400)
            return

        step = str(p.get("step") or "").strip()
        state = str(p.get("state") or "").strip()
        run_id = str(p.get("run_id") or "").strip()
        now = time.time()

        with _PROGRESS_LOCK:
            rec = _PROGRESS.get(sid) or {}
            if run_id and rec.get("run_id") and rec.get("run_id") != run_id:
                rec = {}                       # 新一轮，别把上一轮的进度混进来
            if not rec:
                rec = {"run_id": run_id, "steps": {}, "started": now,
                       "task": str(p.get("task") or "")}
            if run_id:
                rec["run_id"] = run_id
            rec["updated"] = now
            if step == "__finish__":
                rec["finished"] = now
                rec["current"] = ""
                rec["ok"] = state != "fail"
            else:
                one = rec["steps"].setdefault(step, {})
                one["state"] = state or one.get("state") or "running"
                for k in ("detail", "error"):
                    if p.get(k):
                        one[k] = str(p.get(k))
                if p.get("elapsed") is not None:
                    try:
                        one["elapsed"] = round(float(p.get("elapsed")), 1)
                    except (TypeError, ValueError):
                        pass
                one["at"] = now
                if state == "running":
                    rec["current"] = step
                elif rec.get("current") == step:
                    rec["current"] = ""
            _PROGRESS[sid] = rec

        if step and step != "__finish__":
            _append_log(f"[进度] {sid} {STEP_LABELS.get(step, step)} → {state}"
                        + (f"（{p.get('detail')}）" if p.get("detail") else ""))
        _json(self, {"ok": True})

    # ---------------- 手动核算（按店铺分抽屉，不碰收件箱） ----------------

    def _post_manual_upload(self):
        from bridge import manual_api

        try:
            payload = self._read_payload(MAX_UPLOAD_BODY)
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        key = str(payload.get("key") or "").strip()
        name = str(payload.get("name") or "").strip()
        b64 = payload.get("b64") or ""
        if not key or not name or not b64:
            _json(self, {"ok": False, "error": "缺少店铺、文件名或内容"}, 400)
            return
        data, _err = _decode_b64(b64)
        if data is None:
            _json(self, {"ok": False, "error": _err}, 400)
            return
        r = manual_api.save_file(key, name, data, str(payload.get("country") or ""))
        if r.get("ok"):
            _append_log(f"[手动核算] {key} ← {r.get('name')}（{r.get('label')}）")
        _json(self, r)

    def _post_manual_delete(self):
        from bridge import manual_api

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        key = str(payload.get("key") or "").strip()
        fname = str(payload.get("file") or "").strip()
        if not key or not fname:
            _json(self, {"ok": False, "error": "缺少店铺或文件名"}, 400)
            return
        r = manual_api.delete_file(key, fname)
        if r.get("ok"):
            _append_log(f"[手动核算] 删除 {key}/{fname} → 回收站")
        _json(self, r)

    def _post_manual_clear(self):
        from bridge import manual_api

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        key = str(payload.get("key") or "").strip()
        if not key:
            _json(self, {"ok": False, "error": "缺少店铺"}, 400)
            return
        r = manual_api.clear_space(key)
        if r.get("ok"):
            _append_log(f"[手动核算] 清空 {key} 的手动数据：{r.get('moved', 0)} 个文件 → 回收站")
        _json(self, r)

    def _post_manual_run(self):
        from bridge import manual_api

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        key = str(payload.get("key") or "").strip()
        if not key:
            _json(self, {"ok": False, "error": "缺少店铺"}, 400)
            return
        # 同一家店只许算一个：连点两次、或开着两个窗口同时点，
        # 两个子进程会往同一个文件名里写，报表有可能互相覆盖、数字还对不上。
        # 用跟自动核算同一套按店锁（key 加 manual_ 前缀，两条线互不阻塞）。
        _lk = _run_lock_for("manual_" + _canon_shop(key))
        if not _lk.acquire(blocking=False):
            _json(self, {"ok": False, "error": "这家店正在计算中，等它算完再点"})
            return
        try:
            _append_log(f"[手动核算] 开始计算 {key} "
                        f"{payload.get('from') or '全部'}~{payload.get('to') or '全部'}")
            r = manual_api.run(key,
                               country=str(payload.get("country") or ""),
                               dfrom=str(payload.get("from") or ""),
                               dto=str(payload.get("to") or ""),
                               with_formula=bool(payload.get("with_formula")))
        except Exception as e:  # noqa: BLE001
            # 未预料的异常也要给人话，不要把 500 甩到界面上
            r = {"ok": False, "error": f"计算失败：{e}"}
        finally:
            _lk.release()
        if r.get("ok"):
            _append_log(f"[手动核算] 完成：GMV {r.get('gmv')} 净利 {r.get('net_local')}"
                        f" → {r.get('report_name')}")
        else:
            _append_log(f"[手动核算] 失败：{str(r.get('error') or '')[:120]}")
        _json(self, r)

    def _post_run_stores(self):
        """界面点「运行」：给勾选的每家店各派一个任务。

        按店派是关键：每家店一个 job，store 字段填死，
        只有那家店的扩展能领走。以前是全局一个队列，谁先轮询谁跑。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except Exception:  # noqa: BLE001
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        raw = payload.get("stores") or []
        if isinstance(raw, str):
            raw = [raw]
        wanted = [str(x).strip() for x in raw if str(x).strip()]

        # 支持传「店名」也支持传 shop_id —— 前端两个都可能给
        _names = _store_dir_names()                 # shop_id → 目录名
        id_by_name = {v: k for k, v in _names.items()}   # 目录名 → shop_id
        ids: list[str] = []
        ignored: list[str] = []
        # 只认真正存在的店：磁盘上有目录的，或者 stores.json 里登记过的
        # （新店第一次运行前目录可能还没建）。不认的一律丢掉——
        # 否则一个写错的店名会变成一条谁也领不走的任务，在队列里躺 30 分钟，
        # 界面上却显示「已派发」。
        # 认的是 shop_id（不是目录名）：以前这里写成 set(id_by_name)，
        # 那是**店名**的集合，于是每个 shop_id 都对不上、全被当成「不认识的店」，
        # 界面上点「运行」只回一句「已派发」，实际一家都没派 —— 0.24.16 起的回归。
        try:
            _known = set(_names) | set(paths._load_store_names())
        except Exception:  # noqa: BLE001
            _known = set(_names)
        for x in wanted:
            sid = id_by_name.get(x, x)
            if sid in _known:
                ids.append(sid)
            else:
                ignored.append(x)
        if not ids and not ignored:
            ids = list(_names.keys())            # 空 = 全部已知店铺
        if ignored:
            _append_log("[运行] 跳过不认识的店铺：" + "、".join(ignored[:6]))
        if wanted and not ids:
            # 点了名却一个都没认出来：别回「已派发」，那是骗人
            _json(self, {"ok": False, "ignored": ignored,
                         "error": "这些店铺没认出来：" + "、".join(ignored[:6])}, 400)
            return

        rng = payload.get("range") or {}
        if not isinstance(rng, dict):
            # 传个字符串/数字进来时，补出来的区间会是空的，任务派下去也跑不对。
            # 宁可当场说清楚，也不要让它变成一个「已派发但永远算不出」的任务。
            _json(self, {"ok": False, "error": "range 格式不对，应该是 {startDate, to}"}, 400)
            return
        if rng.get("kind"):
            from src import auto_run
            rng = auto_run.build_range(auto_run.quick_rule(str(rng.get("kind"))), datetime.now())

        sent, offline, jobs = [], [], []
        _names = _store_dir_names()          # 循环里别反复扫店铺目录
        for sid in ids:
            job = _enqueue(sid, rng, auto=bool(payload.get("auto")),
                           manual=not bool(payload.get("auto")),
                           with_formula=payload.get("with_formula"),
                           country=str(payload.get("country") or ""),
                           force=bool(payload.get("force")))
            # 错峰：7 家同时开跑会互相挤（抓包里能看到请求发出去、没有任何响应）。
            # 第 i 家延后 i*12 秒才允许被领走，前面几家先跑起来。
            try:
                with _PENDING_LOCK:
                    for _j in _PENDING:
                        if isinstance(_j, dict) and _j.get("id") == job.get("id"):
                            _j["hold_until"] = time.time() + len(jobs) * 12
                            break
            except Exception:  # noqa: BLE001
                pass
            jobs.append(job["id"])
            is_on = _online_now(sid)
            (sent if is_on else offline).append(sid)
            _append_log(f"[运行] 派给 {_names.get(sid, sid)}"
                        f"{'（在线）' if is_on else '（浏览器没开，排队等 30 分钟）'}")

        # 说清楚这次会不会真去平台导：今天同区间已经取全的店，会直接复用，
        # 几十秒就出结果——不然用户会以为「怎么这么快，是不是没跑」。
        _reuse_names: list[str] = []
        if not payload.get("force"):
            try:
                from src import capture as _cap
                _a = str((rng or {}).get("startDate") or "")
                _b = str((rng or {}).get("to") or "")
                for sid in ids:
                    if len(_cap.status(sid, _a, _b).get("reuse") or []) >= 4:
                        _reuse_names.append(_names.get(sid, sid))
            except Exception:  # noqa: BLE001
                _reuse_names = []

        _base_note = ("已派发。在线的店几秒内开始；离线的店等浏览器打开后自动开跑。"
                      if offline else "已派发，在线的店几秒内开始。")
        if _reuse_names:
            _base_note += "（" + "、".join(_reuse_names) + " 今天已取过这批数据，会直接复用；要重导勾「强制重新取数」）"

        _json(self, {
            "ok": True, "count": len(ids), "jobs": jobs,
            "range": rng, "sent": sent, "offline": offline,
            "reuse": _reuse_names, "ignored": ignored,
            "note": _base_note,
        })

    def _get_live(self):
        """各店的在线状态 + 进度 + 队列。卡片墙每 3 秒拉一次。"""
        now = time.time()
        names = _store_dir_names()
        with _PROGRESS_LOCK:
            prog = json.loads(json.dumps(_PROGRESS))
        with _ONLINE_LOCK:
            online = json.loads(json.dumps(_ONLINE))
        with _PENDING_LOCK:
            queue = [{"store": j.get("store"), "at": j.get("at"),
                      "store_name": names.get(str(j.get("store") or ""), "")}
                     for j in _PENDING if isinstance(j, dict)]

        out: dict[str, dict] = {}
        for sid, nm in names.items():
            rec = online.get(sid) or {}
            age = now - float(rec.get("at") or 0) if rec else 1e9
            out[sid] = {
                "shop_id": sid,
                "store_name": nm,
                # 扩展心跳时报上来的「店铺后台里真实的名字」。
                # 用户可能把目录改成中文别名，卡片上两个都显示，才认得出是哪家。
                "shop_name": rec.get("shop_name") or "",
                "region": rec.get("region") or "",
                "online": bool(rec) and age < _ONLINE_TTL,
                # 这个进程里到底**收到过**它的心跳没有。
                # 程序刚启动时内存表是空的，这时「没在线」和「浏览器关了」
                # 是两件事：前者过一分钟自己就变了，后者得人去开浏览器。
                # 混在一起说「浏览器没开」，用户会白折腾一圈。
                "heard": bool(rec),
                "last_beat": int(rec.get("at") or 0),
                "ver": rec.get("ver") or "",
                "running": rec.get("running") or None,
                "progress": prog.get(sid) or None,
                "queued": sum(1 for q in queue if q.get("store") == sid),
                # 已经点了「停止」、扩展还没收手的这段：界面显示「正在停…」
                "stopping": (sid in _STOP or "*" in _STOP),
                # 今天这家店已经取到过什么（卡片上那行「今日 09:12 已取数」）。
                # 有了它，用户点运行前就知道这次会不会真去平台导。
                "captured": _captured_brief(sid),
            }
        _json(self, {"ok": True, "now": int(now * 1000),
                     "steps": list(RUN_STEPS), "labels": STEP_LABELS,
                     "stores": out, "queue": queue, "order": list(names)})

    def _get_schedules(self):
        """计划任务列表 + 每条的下次/上次/漏跑。"""
        from src import auto_run

        now = datetime.now()
        state = auto_run.load_state()
        names = _store_dir_names()
        store_meta: dict[str, str] = {}
        for sid in names:
            try:
                m = json.loads((paths.store_root(sid) / "meta.json").read_text(encoding="utf-8"))
                store_meta[sid] = str(m.get("shop_name") or "")
            except Exception:  # noqa: BLE001
                store_meta[sid] = ""

        out, missed = [], []
        for r in _rules_now():
            rid = r["id"]
            st = state.get(rid) or {}
            nxt = auto_run.next_due(r, now)
            item = dict(r)
            item.update({
                "next": nxt.strftime("%m-%d %H:%M") if nxt else "",
                "next_full": nxt.strftime("%Y-%m-%d %H:%M") if nxt else "",
                "last": st.get("last") or "",
                "last_at": st.get("at") or "",
                "last_range": st.get("range") or "",
                "last_stores": st.get("stores"),
                "missed_at": st.get("missed_at") or "",
                "range_label": auto_run.range_label(r, now),
                "range_preview": auto_run.describe_range(r, now),
                "when_label": auto_run.describe_rule(r),
                "store_names": [names.get(s, s) for s in (r.get("stores") or [])],
            })
            if st.get("missed_at") and r.get("enabled"):
                missed.append({"id": rid, "name": r.get("name"), "at": st["missed_at"]})
            out.append(item)

        stores = [{"id": sid, "name": nm, "shop_name": store_meta.get(sid) or ""}
                  for sid, nm in names.items()]
        _json(self, {"ok": True, "rules": out, "stores": stores, "missed": missed,
                     "now": now.strftime("%Y-%m-%d %H:%M:%S")})

    def _post_schedules_preview(self):
        """编辑计划时算一下「这条会跑哪一段」，不用先保存。

        前端做同样的日期推演会跟后端两套逻辑，迟早对不上——
        干脆一律问后端，界面上显示的就是真会跑的东西。
        """
        from src import auto_run

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        rule = auto_run.normalize_rule(payload.get("rule") or payload or {})
        now = datetime.now()
        nxt = auto_run.next_due(rule, now)
        _json(self, {
            "ok": True,
            "when_label": auto_run.describe_rule(rule),
            "range_label": auto_run.range_label(rule, now),
            "range_preview": auto_run.describe_range(rule, now),
            "next": nxt.strftime("%Y-%m-%d %H:%M") if nxt else "",
            "name": rule.get("name") or "",
        })

    def _post_schedules_save(self):
        """整份计划列表保存（前端改完一次性提交）。"""
        from src import auto_run, settings as cfg_settings

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list):
            _json(self, {"ok": False, "error": "缺少 rules"}, 400)
            return

        with _SCHED_STATE_LOCK:
            state = auto_run.load_state()
            clean = []
            now = datetime.now()
            keep_keys = ("id", "name", "enabled", "freq", "time", "day", "weekday",
                         "period", "offset", "cut", "cut_day", "stores",
                         "download_dir", "with_formula")
            for i, raw in enumerate(raw_rules):
                if not isinstance(raw, dict):
                    continue
                # 只存规则本身：界面上那些 next / range_label 是算出来的，
                # 存进配置只会越积越大，还会跟真正重算出来的值打架
                r = auto_run.normalize_rule({k: raw.get(k) for k in keep_keys if k in raw})
                if not r.get("id"):
                    r["id"] = f"r{int(now.timestamp())}_{i + 1}"
                clean.append(r)
                # 新规则先记一个「刚处理过」，免得刚建就被判成漏跑
                if r["id"] not in state:
                    due = auto_run.last_due(r, now)
                    state[r["id"]] = {"mark": (due or now).strftime("%Y-%m-%d %H:%M"),
                                      "seeded": True}

            # 删掉的规则把状态一起清掉，省得以后同 id 复活带出旧记录
            keep = {r["id"] for r in clean}
            for rid in list(state):
                if rid not in keep:
                    state.pop(rid, None)
            auto_run.save_state(state)

        try:
            cfg_settings.save({"schedules": clean})
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)
            return
        _append_log(f"[计划] 已保存 {len(clean)} 条计划")
        self._get_schedules()

    def _post_schedules_run(self):
        """立即按某条计划跑一次（不动它的定时记录）。"""
        from src import auto_run

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        rid = str(payload.get("id") or "").strip()

        rules = [r for r in _rules_now() if not rid or r["id"] == rid]
        if not rules:
            _json(self, {"ok": False, "error": "没找到这条计划"}, 404)
            return

        with _SCHED_STATE_LOCK:
            done = []
            state = auto_run.load_state()
            for r in rules:
                res = _dispatch_rule(r, datetime.now())
                # 只记「显示用的上次」，不动 mark：手动跑一次不等于这次定时已经跑过，
                # 到点该跑还得跑。
                st = dict(state.get(r["id"]) or {})
                st["last"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st["range"] = res["range_label"]
                st["stores"] = len(res["ids"])
                state[r["id"]] = st
                done.append({"id": r["id"], "name": r.get("name"),
                             "stores": len(res["ids"]), "range": res["range_label"]})
            auto_run.save_state(state)
        _json(self, {"ok": True, "ran": done,
                     "note": f"已派发 {sum(d['stores'] for d in done)} 个任务"})

    def _post_schedules_catchup(self):
        """补跑漏掉的计划。

        用「当时该跑的那一刻」算区间，不是用今天算——
        11-01 09:00 漏的上月报表，补跑时还得是上月，不能变成这个月。
        """
        from src import auto_run

        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        want = payload.get("ids")
        want = [str(x) for x in want] if isinstance(want, list) and want else None

        with _SCHED_STATE_LOCK:
            state = auto_run.load_state()
            done, skipped = [], []
            for r in _rules_now():
                rid = r["id"]
                st = dict(state.get(rid) or {})
                at = _parse_stamp(st.get("missed_at"))
                if not at:
                    continue
                if want and rid not in want:
                    continue
                if not r.get("enabled"):
                    skipped.append({"id": rid, "name": r.get("name"), "why": "计划已关闭"})
                    continue
                res = _dispatch_rule(r, at)
                st.pop("missed_at", None)
                st.pop("missed_name", None)
                st["mark"] = at.strftime("%Y-%m-%d %H:%M")
                st["last"] = at.strftime("%Y-%m-%d %H:%M")
                st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st["range"] = res["range_label"]
                st["stores"] = len(res["ids"])
                state[rid] = st
                done.append({"id": rid, "name": r.get("name"),
                             "range": res["range_label"], "stores": len(res["ids"])})
            auto_run.save_state(state)

        if not done and not skipped:
            _json(self, {"ok": True, "ran": [], "note": "没有需要补跑的计划"})
            return
        _append_log("[计划] 补跑：" + "、".join(d["name"] or d["id"] for d in done) if done else "")
        _json(self, {"ok": True, "ran": done, "skipped": skipped,
                     "note": f"已补跑 {len(done)} 条" if done else "没有可补的计划"})

    def _post_auto_now(self):
        """「现在跑一次」：立刻派一个取数+核算的任务。

        走的是同一条队列，所以扩展在线就马上开始，不在线就排队等着。
        """
        import time as _time
        from datetime import datetime

        from src import auto_run, settings as cfg_settings

        now = datetime.now()

        # 新版本的计划任务和旧版本的「自动运行」是两套配置，同时生效会重复跑。
        # 有启用的计划就按第一条跑（等价于在计划页点「立即跑」），
        # 没有才退回旧配置。
        _rules = [r for r in _rules_now() if r.get("enabled")]
        if _rules:
            res = _dispatch_rule(_rules[0], now)
            _json(self, {"ok": True, "range": res["range"], "count": len(res["ids"]),
                         "offline": [n for s2, n in _store_dir_names().items()
                                     if s2 in res["ids"] and not _online_now(s2)],
                         "note": f"已按计划「{_rules[0].get('name')}」"
                                 f"派给 {len(res['ids'])} 家店（{res['range_label']}）"})
            return

        cfg = (cfg_settings.load().get("auto") or {})
        rng = auto_run.build_range(auto_run.quick_rule(str(cfg.get("range") or "last_month")), now)
        stores = [str(s) for s in (cfg.get("stores") or []) if str(s).strip()]
        # 一家店一个任务、store 写死：以前只派一个、store 留空，
        # 谁先轮询谁拿走 —— 多店同时在线必然串店。
        ids = stores or list(_store_dir_names().keys())
        jobs, offline = [], []
        for sid in ids:
            jobs.append(_enqueue(sid, rng, auto=True, manual=True))
            if not _online_now(sid):
                offline.append(_store_dir_names().get(sid, sid))
        _append_log(f"[自动] 手动触发一次："
                    f"{auto_run.describe_range(str(cfg.get('range') or 'last_month'), now)}"
                    f"（{len(jobs)} 家店）")
        _json(self, {"ok": True, "range": rng, "count": len(jobs),
                     "offline": offline,
                     "note": ("已给 %d 家店各派了一个任务。在线的几秒内开始"
                              % len(jobs))
                             + ("；这些店的浏览器没开，等打开后自动开跑："
                                + "、".join(offline) if offline else "")})

    def _post_settings(self):
        from src import settings as cfg_settings
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            _json(self, cfg_settings.save(payload))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_rates(self):
        from bridge import settings_api
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            _json(self, settings_api.save_rates(payload))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_cost_fields(self):
        """成本表「哪一列是编码 / 哪一列是成本」的标题设置。"""
        from bridge import config_page
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            _json(self, config_page.save_cost_fields(
                payload.get("code") or [], payload.get("cost") or []))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_costs(self, save: bool):
        """成本表：整表保存（save=True）或导入 Excel 合并（save=False）。

        成本变动不频繁，所以不做成「每次改一行就落盘」，
        统一走一次保存：界面里改完点保存，或者丢一份 Excel 进来让它自己合并。
        """
        from bridge import costs_api
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            if save:
                _json(self, costs_api.save_costs(payload.get("items") or []))
            else:
                _json(self, costs_api.import_costs(
                    str(payload.get("name") or ""),
                    payload.get("b64") or "",
                    str(payload.get("mode") or "merge"),
                ))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"处理失败：{e}"}, 500)

    def _post_spec(self):
        """给扩展发取数规格。

        扩展不再写死任何接口，它只照着这里的规格发请求。
        规格在 config/api_specs.json 里，变量（{{startDate}} 等）在这里就填好，
        扩展那边连替换都不用做——它只负责「把请求发出去、把响应带回来」。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}

        try:
            from src import fetcher
            task = str(payload.get("task") or "")
            specs = fetcher.load_specs()
            rng = payload.get("range") or {}
            store = str(payload.get("store") or "")
            if task:
                spec = fetcher.get_task_spec(task, specs)
            else:
                spec = {"tasks": specs.get("tasks", {})}
            vars_ = fetcher.make_vars(rng, store)
            # 把步骤里的占位符填好再发出去
            if task:
                spec = dict(spec)
                spec["steps"] = [fetcher.fill_vars(s, vars_) for s in (spec.get("steps") or [])]
            _json(self, {"ok": True, "task": task, "spec": spec})
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": str(e)}, 400)

    def _post_fetch_result(self):
        """扩展把原始响应交回来，按规格解析成文件落进 inbox。

        解析、映射、汇总、出文件全在 Python 侧（src/fetcher.py），
        所以调这些逻辑不用刷新扩展。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}

        try:
            from src import fetcher
            task = str(payload.get("task") or "")
            results = payload.get("results") or []
            rng = payload.get("range") or {}
            store = str(payload.get("store") or "")

            # 产出落到哪很关键：广告/佣金必须进「这家店的 inbox/ads」，
            # 核算才读得到。以前一律写进 data/exports/_inbox，
            # 结果文件生成了、报表里广告花费却一直是 0。
            out_dir = None
            if task in ("ads", "affiliate"):
                if store:
                    out_dir = paths.store_kind_dir(store, task)
            elif task in ("sku", "link"):
                out_dir = paths.MAPPING_DIR
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)

            r = fetcher.process_results(task, results, rng, store, out_dir=out_dir)
            # SKU / 链接资料是这家店自己的表，复制一份到店目录下给使用者看
            if task in ("sku", "link"):
                for f in (r.get("files") or []):
                    _mirror_mapping_to_store(store, f)
            _json(self, {"ok": r["ok"], "files": r.get("files") or [],
                         "issues": r.get("issues") or []})
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": str(e), "issues": [str(e)]}, 400)

    def _post_log(self):
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            payload = {}
        msg = str(payload.get("msg") or "")
        if msg:
            _append_log(msg)
            print(msg, flush=True)
        _json(self, {"ok": True})

    def _post_run(self):
        """跑一次核算：取 _inbox 里的导出文件 → 算净利 → 出 Excel。

        扩展点一下「立即运行」时会调这里。核算在本机以子进程执行，
        不占扩展，也不受浏览器生命周期影响。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        _lk = None
        _tmp_out: Path | None = None      # 重算时的临时产物，失败要删掉
        try:
            from src import settings as cfg_settings

            country = str(payload.get("country") or "")
            store = str(payload.get("store") or "").strip()
            shop_id = str(payload.get("shop_id") or store or "").strip()
            dfrom = payload.get("from") or None
            dto = payload.get("to") or None

            # 店铺信息：名字给人看，shop_id 给程序用。
            # 名字优先用用户在 stores.json 里起的别名，没有就用平台原名。
            store_name = ""
            shop_code = ""
            _stores = store_list().get("stores", [])
            if store:
                for st in _stores:
                    if st.get("id") == store:
                        country = st.get("country") or country
                        store_name = st.get("name") or ""
                        shop_code = st.get("shop_code") or ""
                        shop_id = st.get("shop_id") or shop_id or store
                        break

            # 以「浏览器实际登录的那家」为准。
            # 两个浏览器各登一家时，弹窗里的店铺可能还停在上一次选的那家，
            # 照它算就会把 A 店的名字贴到 B 店的数据上，报表看着正常、数字是别家的。
            if shop_id and store != shop_id:
                _alias = next((st.get("name") for st in _stores
                               if st.get("id") == shop_id), "")
                _meta: dict = {}
                try:
                    _mf = paths.store_meta_file(shop_id)
                    if _mf.is_file():
                        _meta = json.loads(_mf.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    _meta = {}
                _real = str(_alias or _meta.get("shop_name") or "").strip()
                if _real and _real != store_name:
                    if store_name:
                        _append_log(f"[run] 弹窗选的是「{store_name}」，浏览器登的是「{_real}」，"
                                    f"以登录的店铺为准")
                    store_name = _real
                if _meta.get("country"):
                    country = str(_meta.get("country") or country)
                shop_code = str(_meta.get("shop_code") or shop_code)

            # 一家店一把锁：两家店可以同时算，同一家店不能重复算
            _lk = _run_lock_for(_canon_shop(shop_id or store))
            _got_lock = _lk.acquire(blocking=False)
            if not _got_lock:
                _lk = None
                _json(self, {"ok": False,
                             "error": f"店铺「{store_name or shop_id or '全部'}」正在核算中，请稍候"})
                return

            if not store_name:
                store_name = country or "未识别店铺"
            country = country or "马来西亚"

            # 数据来源：有 shop_id 就读它自己的目录，各店数据不混
            data_dir = paths.store_dir(shop_id)
            # 要看「有没有真的文件」：目录结构是程序自己建的（订单表/广告表…），
            # 只看有没有子目录的话，一家还没取过数的店也会被当成有数据，
            # 然后一路跑到 loader 报 FileNotFoundError，用户只看到一串堆栈。
            _has_data = False
            if data_dir.is_dir():
                try:
                    _has_data = any(p.is_file() for p in data_dir.rglob("*"))
                except OSError:
                    _has_data = False
            if not _has_data:
                if shop_id:
                    # 选了店铺却没有数据：直说，别悄悄拿别的数据算。
                    # 旧行为是退回公共目录——多店铺时会把别家的数据算进来，
                    # 报表却顶着这家店的名字，事后根本查不出来。
                    _json(self, {"ok": False,
                                 "error": f"店铺「{store_name}」还没有取到数据。"
                                          f"先在扩展里取一次，或用「手动导入」把文件传进来。"})
                    return
                # 没指定店铺（全部店铺模式）：退回公共目录
                data_dir = INBOX_DIR
                # 公共目录空、但「未识别专区」有数据时要说清楚，
                # 不然用户只看到一句「没识别出订单表」，根本不知道文件去哪了
                try:
                    _unk = ([p for p in paths.UNKNOWN_ROOT.rglob("*") if p.is_file()]
                            if paths.UNKNOWN_ROOT.is_dir() else [])
                except OSError:
                    _unk = []
                if _unk and not any(data_dir.iterdir()):
                    _json(self, {"ok": False,
                                 "error": f"有 {len(_unk)} 个文件没能识别出店铺，"
                                          f"存在「未识别店铺」专区里，不会参与核算。"
                                          f"请在扩展里重新登录店铺后台再取一次，"
                                          f"或在核算页选好店铺后重新导入。"})
                    return

            # 快照：没指定就先把当前 inbox 存成一份再算。
            # inbox 是实时的，下一次取数就把它冲掉了；存一份才能回头重算、
            # 也才能说清「这份报表到底是哪批数据算出来的」。
            #
            # 可以选**多份**一起算：勾哪几份就合并哪几份，
            # 跨快照重复的订单只算一次（重叠部分以取数时间最新的为准）。
            from src import snapshots
            _snaps = [str(s).strip() for s in (payload.get("snapshots") or []) if str(s).strip()]
            if not _snaps:
                _one = str(payload.get("snapshot") or "").strip()
                _snaps = [_one] if _one else []

            snap_id = ""
            data_dirs: list[Path] = []
            if _snaps:
                for _s in _snaps:
                    _sd = snapshots.resolve(shop_id, _s)
                    if _sd is None:
                        _json(self, {"ok": False, "error": f"找不到快照：{_s}"})
                        return
                    data_dirs.append(_sd)
                snap_id = _snaps[0] if len(_snaps) == 1 else ""
            else:
                try:
                    _snap = snapshots.create(
                        shop_id, snapshots.pick_files(shop_id),
                        dfrom or "", dto or "", store_name,
                        note=str(payload.get("note") or ""),
                        # 重算（replace）时数据没变，别每次都复制一整份订单表
                        reuse_any_age=bool(payload.get("replace")))
                    if _snap.get("ok"):
                        snap_id = _snap.get("id") or ""
                        _sd = snapshots.resolve(shop_id, snap_id)
                        if _sd:
                            data_dirs = [_sd]
                except Exception as e:  # noqa: BLE001
                    # 快照失败不该拖垮核算：退回实时目录照算
                    _append_log(f"[run] 快照入库失败，改用实时数据：{e}")
                    snap_id = ""

            if not data_dirs:
                data_dirs = [data_dir]

            # 预检订单表：没有订单就没有核算的必要。
            # 以前让它一路跑到 loader 抛 FileNotFoundError，子进程还会卡住不退，
            # 界面只能干等 15 分钟——不如在这里直接说清。
            try:
                from src import loader as _ld
                _has_order = _ld.has_orders_table(data_dir)
            except Exception:  # noqa: BLE001
                _has_order = True
            if not _has_order:
                # 大店订单在平台要生成好几分钟，取数那边常常是"已排队、还没落地"。
                # 记一条待补算：文件一落地后台自己算，不用人再点第二次。
                try:
                    _pending_add(str(shop_id or store_name), {
                        "shop_id": shop_id, "store_name": store_name,
                        "country": country, "from": dfrom or "", "to": dto or "",
                        "with_formula": bool(payload.get("with_formula")),
                        "at": time.time(),
                    })
                except Exception:  # noqa: BLE001
                    pass
                _json(self, {"ok": False, "pending": True,
                             "error": f"店铺「{store_name}」的订单还在平台生成，还没落到本地。"
                                      f"已记下这次核算——订单一落地程序会自动补算，不用再点。"
                                      f"报表会出现在「报表」页。"})
                return

            # 报表放到「自动」房间里该店铺自己的子目录，文件名按设置里的模板生成。
            # 自动 / 手动分两个房间（报表\自动、报表\手动），各自下面再按店铺。
            cfg_settings.load()
            out_dir = (paths.store_reports_dir(shop_id) if shop_id else
                       paths.report_room_dir("auto") / re.sub(r'[\\/:*?"<>|]', "_", store_name))
            out_dir.mkdir(parents=True, exist_ok=True)

            stg = cfg_settings.load().get("report", {})
            base_name = cfg_settings.render_report_name(
                stg.get("name_template") or "{店铺}_{区间}",
                store=store_name, shop_id=shop_id, shop_code=shop_code,
                country=country, dfrom=dfrom or "", dto=dto or "",
            )
            final_out = out_dir / f"{base_name}.xlsx"
            # 重算（改了费率/成本后马上重来）默认覆盖同名报表。
            # 以前是「同名不覆盖、自动加 (2)」——取数一天只做一次，
            # 重算却可能做十次，两天下来「报表」页就堆满 (2)(3)(4)，
            # 还分不清哪份是最后那版。
            #
            # 关键：**先算到临时文件，算成功了才换名**。
            # 早期版本在开算之前就把旧报表挪进回收站——算失败（表头不对、
            # 文件被 Excel 占着）时报表页上就凭空少一份，数字还是旧的那版也没了。
            _replace = bool(payload.get("replace"))
            # **一律先算到临时文件，成功了再原子换名**（见 safeio.swap_report）。
            #
            # 以前只有「重算 + 已有同名报表」才走临时文件，其余情况直接往最终文件名
            # 里写：算到一半失败（磁盘满、文件正被 Excel 占着）就在报表目录里留下
            # 一份半截 xlsx，报表页会把它当成一份真报表列出来，点开报「文件损坏」，
            # 而上一版好报表已经被覆盖掉了。
            #
            # 临时名要短：店名 + 区间 + _手动 已经很长，再加前缀容易撞
            # Windows 260 字符路径上限（报 WinError 206，算完拿不到文件）
            out = out_dir / f".{os.getpid()}.calc.xlsx"
            _tmp_out = out

            # 打包后 sys.executable 是 exe 本身，`-m src.cli` 不再有效，
            # 改成 exe 自己的 run 子命令。两种模式参数完全一致。
            if paths.is_frozen():
                base = [sys.executable, "run"]
            else:
                base = [sys.executable, "-m", "src.cli"]
            cmd = base + [
                "--dir", str(data_dirs[0]),
                # 有店铺就只传店铺：国家由 config/stores.json 决定，
                # 两处都传容易打架（--country 会覆盖 --store）
                # 优先传 shop_id：快照是按店铺目录存的，只传国家时
                # 子进程不知道去哪找快照，会报「找不到快照（店铺 未指定）」。
                *(["--store", shop_id or store] if (shop_id or store)
                  else ["--country", country]),
                "--out", str(out),
                "--json",
            ]
            if len(data_dirs) > 1:
                cmd += ["--snapshots", ",".join(d.name for d in data_dirs)]
            elif snap_id:
                cmd += ["--snapshot", snap_id]
            # 带不带公式：手动跑用界面上的勾选；计划任务带自己的开关
            _wf = payload.get("with_formula")
            if _wf is None:
                # 计划里没指定 = 跟随「设置 → 报表」，不再去读旧 auto 的开关
                _wf = stg.get("with_formula")
            if _wf:
                cmd += ["--with-formula"]
            if dfrom:
                cmd += ["--from", str(dfrom)]
            if dto:
                cmd += ["--to", str(dto)]

            _append_log(f"[run] 开始核算 {country} {dfrom or '全量'}~{dto or '全量'}")
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, env=env,
                # 不用 text=True：打包后的子进程在无控制台时按系统编码（GBK）输出，
                # 统一按 utf-8 解会把中文日志全变成乱码，出错了也读不出原因。
                timeout=300,
                # 不弹黑窗口：桥在后台跑，每核算一次冒一个窗口很吓人
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                               if os.name == "nt" else 0),
            )

            def _dec(raw: bytes) -> str:
                for enc in ("utf-8", "gbk"):
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode("utf-8", "replace")

            stdout = _dec(proc.stdout or b"")
            stderr = _dec(proc.stderr or b"")

            summary = None
            for line in reversed(stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        summary = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    break

            if proc.returncode == 0 and summary:
                if out.exists():
                    out = safeio.swap_report(
                        out, final_out,
                        paths.DATA / "回收站" / f"报表_{datetime.now():%Y%m%d_%H%M%S_%f}")
                    _tmp_out = None
                # 子进程返回的 file 是它自己写的那个路径（现在是临时名
                # `.<pid>.calc.xlsx`）。换名成功之后必须改回来，否则界面和日志里
                # 显示的是「.15144.calc.xlsx」——一个已经被改掉、根本不存在的文件名，
                # 卡片上的报表名点进去也就找不到东西。
                summary["file"] = str(out)
                # 检查用到的导出文件有没有太旧的：广告任务可能还没下来，
                # 这时会静默沿用上一期文件，数字看着正常但其实是错的。
                stale = []
                # 文件在哪家店的目录里就在哪查：多店铺时不能统一看公共 inbox，
                # 否则要么漏报，要么把别家店旧文件的账算到这家头上
                _src_dir = paths.store_dir(shop_id) if shop_id else INBOX_DIR
                for label, fname in (summary.get("sources") or {}).items():
                    f = _src_dir / fname
                    try:
                        age_h = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
                    except OSError:
                        continue
                    if age_h > 24:
                        stale.append(f"{label}：{fname}（{age_h:.0f} 小时前）")
                if stale:
                    summary["stale"] = stale
                    _append_log("[run] 注意：有数据文件超过 24 小时未更新 → " + "；".join(stale))

                # 哪一类表这一轮压根没有：不能悄悄按 0 算。
                # 广告缺席时净利会明显偏高，报表看着漂亮、实际是错的方向。
                missing = []
                for label, cn in (("广告", "广告表"), ("联盟佣金", "联盟佣金表")):
                    d = _src_dir / cn
                    try:
                        has = d.is_dir() and any(p.is_file() for p in d.rglob("*"))
                    except OSError:
                        has = False
                    if not has:
                        missing.append(label)
                if missing:
                    summary["missing"] = missing
                    _append_log(f"[run] 注意：本轮没有{'、'.join(missing)}数据，"
                                f"这部分按 0 计，净利偏高，别直接拿去对账")
                _append_log(
                    f"[run] 完成：GMV {summary.get('gmv')} 净利 {summary.get('net_local')} "
                    f"（人民币 {summary.get('net_cny')}）"
                )
                summary["report_path"] = str(out)
                summary["report_name"] = out.name
                summary["store_name"] = store_name
                summary["shop_id"] = shop_id
                if _replace:
                    summary["replaced"] = True
                # 这一轮哪几类表沿用了当天早些时候取的那批（没有重新去平台导）。
                # 不上报表，但界面和日志要看得见——不然「怎么秒出结果」会让人怀疑。
                _rv = payload.get("reused")
                _reused = ([str(x) for x in _rv if str(x).strip()][:8]
                           if isinstance(_rv, list) else [])
                if _reused:
                    summary["reused"] = _reused
                    _append_log("[run] 复用当天已取的数据：" + "、".join(_reused))

                # 记一笔历史，界面画走势用
                try:
                    from bridge import settings_api
                    settings_api.append_history({
                        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "store": store_name,
                        "shop_id": shop_id,
                        "from": dfrom or "",
                        "to": dto or "",
                        "gmv": summary.get("gmv"),
                        "cost": summary.get("cost"),
                        "fees": summary.get("fees"),
                        "net": summary.get("net_local"),
                        "net_cny": summary.get("net_cny"),
                        "report": out.name,
                    })
                except Exception:  # noqa: BLE001
                    pass

                # 自动复制到用户指定的位置（比如 D:////净利报表）
                # 计划任务派下来的用那条计划自己的目录；手动跑用「报表」页里的目录。
                copied = None
                try:
                    _dl = stg.get("download_dir") or ""
                    if payload.get("auto"):
                        # 计划任务派下来的：以那条计划为准（空 = 只留程序目录）。
                        # 只有「没有登记过」时才退回旧配置，免得旧 auto 的目录
                        # 被新计划悄悄继承，报表跑进一个你早就不用的文件夹。
                        _sid = _canon_shop(shop_id or store)
                        if _sid in _AUTO_DL:
                            _dl = _AUTO_DL[_sid]
                        else:
                            _dl = (cfg_settings.load().get("auto") or {}).get("download_dir") or _dl
                    copied = cfg_settings.copy_to_download_dir(out, _dl)
                except Exception:  # noqa: BLE001
                    copied = None
                if copied:
                    summary["copied_to"] = str(copied)
                    _append_log(f"[run] 已复制到 {copied}")

                _json(self, {"ok": True, **summary})
            else:
                tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-12:])
                # 堆栈对人没用。认得出的情况直接说他能听懂的那句。
                if "没识别出订单表" in tail or "没有可读的数据目录" in tail:
                    brief = "没有识别出订单表：先在扩展里取一次订单；如果该时段本来就没订单，换日期再算"
                elif "没有解析出任何订单行" in tail:
                    brief = "订单表里没有可用的行（表头不对，或这个区间确实没有订单）"
                elif "区间内没有可核算的订单" in tail:
                    brief = "这个日期区间内没有订单，换个区间再试"
                elif "没有解析出任何商品" in tail:
                    brief = "没有解析出商品：检查商品 SKU 表是否取到"
                elif "读不了「" in tail:
                    brief = ("有表格读不出来：多半是那个文件正在 Excel/WPS 里开着。"
                             "关掉它再点一次运行")
                else:
                    brief = tail or "核算失败"
                _append_log(f"[run] 失败：{brief.splitlines()[-1] if brief else 'unknown'}")
                _json(self, {"ok": False, "error": brief, "returncode": proc.returncode})
        except subprocess.TimeoutExpired:
            _append_log("[run] 失败：核算超时（5 分钟）")
            _json(self, {"ok": False, "error": "核算超过 5 分钟没跑完，已中断。"
                                             "多半是订单表太大或某个文件有问题，可以换成按店铺单独算。"})
        except Exception as e:  # noqa: BLE001
            _append_log(f"[run] 异常：{e}")
            _json(self, {"ok": False, "error": str(e)})
        finally:
            if _tmp_out is not None:
                try:
                    _tmp_out.unlink(missing_ok=True)
                except OSError:
                    pass
            if _lk is not None:
                _lk.release()


    def _post_pending(self):
        """面板把任务放这里，扩展定时来取。

        为什么需要这条：面板 → 后台的 sendMessage 偶尔会断（上下文失效、
        后台刚被唤醒等），断了任务就发不出去。桥是普通 HTTP，
        两个方向都能通，作为第三条通道。
        """
        try:
            payload = self._read_payload()
            if payload is None:
                return
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        with _PENDING_LOCK:
            # 追加到队尾，不清空。自动任务排着队的时候你手动点一次，
            # 两个都该跑，而不是后者把前者顶掉。
            _PENDING.append(payload or {})
        _append_log(f"[pending] 入队：{payload.get('task')} "
                    f"{payload.get('range', {}).get('startDate', '')}"
                    f"（队列 {len(_PENDING)} 个）")
        _json(self, {"ok": True, "queued": len(_PENDING)})


# ---------------- 热重载 ----------------
# 改完代码不重启桥，是最容易浪费时间的坑：桥跑着旧代码，
# 扩展把数据按新协议发过来，老桥不认，参数被静默丢掉，
# 表现就是「文件没落到我说的目录」「参数没生效」，极难排查。
#
# 这里让桥自己盯着自己的源码：变了就原地重启。
# 用 os.execv 而不是 spawn，是为了让新进程接管同一个端口，
# 中间不留「端口被占用」的窗口。

WATCH_FILES = [
    Path(__file__).resolve(),
    ROOT / "src" / "cli.py",
    ROOT / "src" / "calc.py",
    ROOT / "src" / "loader.py",
    ROOT / "src" / "report.py",
    ROOT / "src" / "paths.py",
    ROOT / "src" / "fx.py",
    ROOT / "src" / "formula.py",
    # 界面文件也必须监听：它是一份内嵌的 HTML/JS 字符串，
    # 不重启就一直用旧的那份。漏掉这里的表现最坑——
    # 加了开关、改了文案，刷新页面却什么都没变，
    # 会让人以为代码没保存或者改错了地方。
    ROOT / "bridge" / "ui.py",
    ROOT / "bridge" / "config_page.py",
    ROOT / "bridge" / "costs_api.py",
    ROOT / "bridge" / "settings_api.py",
    # 新加的模块也要盯：漏了它们，改了代码桥不提示重启，
    # 「改了没反应」会白白查半天
    ROOT / "bridge" / "manual_api.py",
    ROOT / "src" / "capture.py",
    ROOT / "src" / "safeio.py",
]

def _code_stamp() -> tuple:
    out = []
    for f in WATCH_FILES:
        try:
            st = f.stat()
            out.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((f.name, 0, 0))
    return tuple(out)


def _watch_code(srv) -> None:
    """后台盯着源码，变了就在日志里提示。

    这里**不自动重启**。试过 in-process 的 os.execv 自重启，三种方式都会出事：
      · execv 后新进程起不来（端口/句柄竞争），桥直接没了
      · 连续改几次文件会连锁重启，最后把桥重启到死
      · 桥是后台常驻进程，重启后不再挂在任何任务管理器下，日志也跟着丢
    宁可让人手动重启一次，也不要桥在干活的时候凭空消失。
    重启命令：python -m src.app serve
    """
    last = _code_stamp()

    def loop() -> None:
        import time
        nonlocal last
        started = time.time()
        while True:
            time.sleep(2.0)
            try:
                if time.time() - started < 6:
                    continue
                now = _code_stamp()
                if now != last:
                    changed = [a[0] for a, b in zip(now, last) if a[1:] != b[1:]]
                    print(
                        f"[提示] 检测到源码变化：{changed}。"
                        f"当前桥仍跑着旧代码，改动要生效请重启：python -m src.app serve",
                        flush=True,
                    )
                    last = now
            except Exception as e:  # noqa: BLE001
                print(f"[热重载] 检查出错（忽略）：{e}", flush=True)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


class _Tee:
    """同时写到控制台和日志文件。

    为什么需要：桥是后台常驻进程，热重载后就不再挂在任何任务管理器下，
    一旦它悄悄死了，日志也跟着没了——上次就是这样，桥死在半路，
    扩展的抓包请求全部发不出去，排查时连个错误线索都没有。
    """

    def __init__(self, a, b):
        self.a = a
        self.b = b

    def write(self, s):
        for x in (self.a, self.b):
            try:
                x.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for x in (self.a, self.b):
            try:
                x.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self.a.isatty()
        except Exception:
            return False


def _setup_log_file():
    """把 stdout/stderr 接到 data/capture/bridge_run.log。"""
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        f = (CAPTURE_DIR / "bridge_run.log").open("a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, f)
        sys.stderr = _Tee(sys.stderr, f)
    except Exception as e:  # noqa: BLE001
        print(f"日志文件不可用（忽略）：{e}")


def _parse_stamp(s: str):
    """把状态文件里的 "2026-11-02 09:00" 解析成 datetime。"""
    raw = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _dispatch_rule(rule: dict, when: datetime | None = None) -> dict:
    """按一条计划派任务（一家店一个）。

    when 是「这次该跑的时刻」——补跑错过的计划时传当时的时刻，
    否则会拿今天的日期去算上月区间，跑出来跟计划里写的不是一回事。
    """
    from src import auto_run

    now = datetime.now()
    at = when or now
    rule = auto_run.normalize_rule(rule)
    rng = auto_run.build_range(rule, at)

    stores = [str(s) for s in (rule.get("stores") or []) if str(s).strip()]
    names = _store_dir_names()
    if stores:
        alias = {v: k for k, v in names.items()}     # 允许写店名
        ids = [alias.get(s, s) for s in stores]
    else:
        ids = list(names.keys())

    dl = str(rule.get("download_dir") or "")
    jobs = []
    for sid in ids:
        # 核算那一步的报表复制目录：按店记着，_post_run 里取。
        # 不走设置里的全局 auto.download_dir，多条计划各有各的去处。
        # 空值也要写：否则上一条计划留下的目录会被下一条「继承」，
        # 报表莫名其妙跑到别的文件夹里。
        _AUTO_DL[sid] = dl
        job = _enqueue(sid, rng, auto=True, with_formula=rule.get("with_formula"))
        jobs.append(job["id"])

    shown = "、".join(names.get(i, i) for i in ids[:8])
    _append_log(
        f"[计划] {rule.get('name') or auto_run.describe_rule(rule)} 派发 "
        f"{auto_run.describe_range(rule, at)} → {len(ids)} 家店"
        + (f"（{shown}{'…' if len(ids) > 8 else ''}）" if ids else "（没有店铺）")
    )
    return {"range": rng, "ids": ids, "jobs": jobs, "range_label": auto_run.describe_range(rule, at)}


def _rules_now() -> list[dict]:
    """当前所有计划（已规整）。迁移来的旧配置也算一条。"""
    from src import auto_run, settings as cfg_settings
    try:
        cfg = cfg_settings.load()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i, raw in enumerate(cfg.get("schedules") or []):
        r = auto_run.normalize_rule(raw)
        if not r.get("id"):
            r["id"] = f"r{i + 1}"
        out.append(r)
    return out


def _schedule_runner() -> None:
    """计划任务：每 20 秒看一次有没有到点。

    触发容差 10 分钟：桥或电脑刚好在触发那一刻没开（开机慢、重启），
    10 分钟内起来就直接补跑，不用人来点。
    超过 10 分钟算「漏跑」，只记账、不自动跑——上一周的计划
    不该在今天突然自己跑起来，得让人确认。
    """
    import time as _t

    from src import auto_run

    TOLERANCE = 600.0
    time.sleep(15.0)          # 启动后先让桥把别的活干完

    while True:
        try:
            rules = _rules_now()
            if rules:
                now = datetime.now()
                _SCHED_STATE_LOCK.acquire()
                state = auto_run.load_state()
                changed = False
                for r in rules:
                    rid = r["id"]
                    st = dict(state.get(rid) or {})
                    due = auto_run.last_due(r, now)
                    if not due:
                        continue

                    # mark = 「已经处理到哪一刻」的水位；last = 真的跑过的那次。
                    # 两者分开：新建计划时先把水位推到最近一次到期，
                    # 不然刚建就判成漏跑；而界面上「上次」也不该显示一个
                    # 它还没出生时的日期。
                    if not st.get("mark"):
                        st["mark"] = due.strftime("%Y-%m-%d %H:%M")
                        st["seeded"] = True
                        state[rid] = st
                        changed = True
                        continue

                    mark = _parse_stamp(st.get("mark"))
                    if mark and mark >= due:
                        continue
                    if not r.get("enabled"):
                        continue

                    late = (now - due).total_seconds()
                    if late < 0:
                        continue
                    if late > TOLERANCE:
                        # 记成漏跑，等界面上一键补
                        if not st.get("missed_at"):
                            st["missed_at"] = due.strftime("%Y-%m-%d %H:%M")
                            st["missed_name"] = r.get("name") or ""
                            state[rid] = st
                            changed = True
                            _append_log(f"[计划] 漏跑：{r.get('name')}（应在 "
                                        f"{st['missed_at']} 跑，点「补跑」可补上）")
                        continue

                    try:
                        res = _dispatch_rule(r, due)
                    except Exception as e:  # noqa: BLE001
                        _append_log(f"[计划] 派发失败：{r.get('name')}（{e}）")
                        continue
                    state[rid] = {
                        "mark": due.strftime("%Y-%m-%d %H:%M"),
                        "last": due.strftime("%Y-%m-%d %H:%M"),
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "range": res["range_label"],
                        "stores": len(res["ids"]),
                    }
                    changed = True
                    print(f"[计划] {r.get('name')} 已派发 {res['range_label']}", flush=True)

                if changed:
                    auto_run.save_state(state)
                _SCHED_STATE_LOCK.release()
        except Exception as e:  # noqa: BLE001
            try:
                _SCHED_STATE_LOCK.release()
            except RuntimeError:
                pass
            print(f"[计划] 检查出错（忽略）：{e}", flush=True)
        _t.sleep(20.0)


def _sync_extension_copies() -> None:
    """把最新的扩展同步到「浏览器实际加载的那一份」。

    为什么会不同步：开发时改的是 `程序目录\\浏览器扩展`，
    而浏览器里加载的通常是 `release\\净利助手\\浏览器扩展`（当初装的时候点的那份）。
    两份不同步时，改完代码、点刷新、版本号还是旧的——查半天以为是缓存。
    这里在启动时对一次版本号，不一致就从程序目录覆盖过去。
    打包运行没有 release 目录，这段自然不做事。
    """
    try:
        src = paths.EXTENSION
        dst = ROOT / "release" / "净利助手" / "浏览器扩展"
        if not dst.is_dir() or dst.resolve() == src.resolve():
            return
        v1 = json.loads((src / "manifest.json").read_text(encoding="utf-8")).get("version")
        v2 = json.loads((dst / "manifest.json").read_text(encoding="utf-8")).get("version")
        if v1 == v2:
            return
        for f in src.iterdir():
            if f.is_file():
                shutil.copy2(f, dst / f.name)
        print(f"[扩展] 已同步到浏览器加载目录：{v2} → {v1}", flush=True)
        _append_log(f"[扩展] 浏览器加载目录里的扩展已更新：{v2} → {v1}（刷新扩展生效）")
    except Exception as e:  # noqa: BLE001
        print(f"[扩展] 同步跳过：{e}", flush=True)


def main() -> int:
    # 自检要放在接日志之前：它会把过大的日志改名轮转，
    # 先打开的话文件被占用，Windows 上改名会失败。
    try:
        from src import selfcheck
        _SELFCHECK.update(selfcheck.run())
    except Exception as e:  # noqa: BLE001
        _SELFCHECK.update({"ok": False, "error": f"自检跳过：{e}", "actions": []})

    _setup_log_file()
    _sync_extension_copies()
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    # 老报表搬进「自动」房间（报表\<店名>\ → 报表\自动\<店名>\）。
    # 放在启动时做：只搬一次，之后目录结构就稳定了；搬不动的（Excel 开着）下次再试。
    try:
        _moved = paths.migrate_report_dirs()
        if _moved:
            _append_log(f"[报表] 老报表搬进「自动」房间：{len(_moved)} 份")
            for _m in _moved[:5]:
                print(f"[报表] {_m}")
    except Exception as e:  # noqa: BLE001
        print(f"[报表] 目录迁移跳过：{e}")

    # 改过名但目录还是旧名的，启动时对齐（界面上的卡片标题认目录名）
    try:
        _renamed = reconcile_store_dirs()
        for _r in _renamed:
            _append_log(f"[store] 目录对齐：{_r}")
            print(f"[store] 目录对齐：{_r}")
    except Exception as e:  # noqa: BLE001
        print(f"[store] 目录对齐跳过：{e}")

    if _SELFCHECK.get("crashed"):
        # 界面上已经有一条横幅说这件事了，这里只写日志，不重复塞进 actions
        print("[自检] 上次没有正常退出（断电/强杀），已做完整检查")
        _append_log("[自检] 上次没有正常退出（断电/强杀），已做完整检查")
    for _a in _SELFCHECK.get("actions") or []:
        print(f"[自检] {_a}")
        _append_log(f"[自检] {_a}")
    try:
        from src import selfcheck
        selfcheck.mark_running()
    except Exception:  # noqa: BLE001
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"本地桥已启动：http://127.0.0.1:{PORT}")
    print(f"抓包写入：{CAPTURE_DIR}")
    print("按 Ctrl+C 停止")
    try:
        _watch_code(srv)
    except Exception as e:  # noqa: BLE001
        print(f"热重载未启用（忽略）：{e}")
    try:
        threading.Thread(target=_schedule_runner, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        print(f"计划任务未启用（忽略）：{e}")
    try:
        threading.Thread(target=_pending_runner, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        print(f"待补算看守未启用（忽略）：{e}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
        try:
            from src import selfcheck
            selfcheck.clear_running()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






# test0

# test1

# test2
