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
import os
import re
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from src import paths  # noqa: E402

ROOT = paths.ROOT
CAPTURE_DIR = paths.CAPTURE
INBOX_DIR = paths.INBOX
MAPPING_DIR = paths.MAPPING
LOG_FILE = paths.CAPTURE / "task.log"
PORT = int(os.environ.get("JINGLI_PORT") or 8765)

TASK_LOG: list[str] = []
_RUN_LOCK = threading.Lock()
_PENDING: dict = {}
_PENDING_LOCK = threading.Lock()


def _append_log(msg: str) -> None:
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


def _cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _json(handler: BaseHTTPRequestHandler, obj, code: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _cors(handler)
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
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    (CAPTURE_DIR / "endpoints.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def store_list() -> dict:
    """给扩展用的店铺清单：每家店的国家已经写在配置里，界面上选店即可。

    为什么不让扩展自己读文件：扩展在浏览器沙箱里，读不到本地磁盘。
    走 HTTP 这条路，配置改了刷新面板就生效，不用重装扩展。
    """
    stores: list[dict] = []
    try:
        cfg = json.loads(paths.STORES_FILE.read_text(encoding="utf-8"))
        for s in cfg.get("stores", []):
            if not s.get("enabled", True):
                continue
            stores.append({
                "id": s.get("id", ""),
                "name": s.get("name", "") or s.get("id", ""),
                "country": s.get("country", ""),
                "browserId": str(s.get("zhanfu_browser_id", "") or ""),
            })
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取 stores.json 失败：{e}", "stores": [], "countries": []}

    countries: list[str] = []
    try:
        rates = json.loads(paths.RATES_FILE.read_text(encoding="utf-8"))
        countries = list(rates.get("countries", {}).keys())
    except Exception:
        pass

    default = ""
    try:
        default = cfg.get("defaults", {}).get("country", "")
    except Exception:
        pass

    return {"ok": True, "stores": stores, "countries": countries, "default": default}


class Handler(BaseHTTPRequestHandler):
    server_version = "JingLiBridge/0.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        _cors(self)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            _json(self, {"ok": True, "service": "jingli-bridge",
                         "captureDir": str(CAPTURE_DIR),
                         "inbox": str(INBOX_DIR)})
        elif self.path.startswith("/endpoints"):
            _json(self, rebuild_endpoints())
        elif self.path.startswith("/log"):
            _json(self, {"ok": True, "lines": TASK_LOG[-200:]})
        elif self.path.startswith("/pending"):
            with _PENDING_LOCK:
                job = dict(_PENDING) if _PENDING else None
                _PENDING.clear()
            _json(self, {"ok": True, "job": job})
        elif self.path.startswith("/stores"):
            _json(self, store_list())
        else:
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    def do_POST(self):
        if self.path.startswith("/capture"):
            self._post_capture()
        elif self.path.startswith("/upload"):
            self._post_upload()
        elif self.path.startswith("/log"):
            self._post_log()
        elif self.path.startswith("/run"):
            self._post_run()
        elif self.path.startswith("/pending"):
            self._post_pending()
        else:
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    def _post_capture(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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

    def _post_upload(self):
        """扩展把下载到的导出文件回传到这里。

        默认落进 inbox 目录；带 dir="mapping" 的落到 data/mapping/。
        映射类文件（商品管理 SKU、链接资料）要和订单、广告、佣金分开放，
        否则核算时会被当成交付文件一起扫。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            name = str(payload.get("name") or "").strip()
            b64 = payload.get("b64") or ""
            if not name or not b64:
                raise ValueError("missing name/b64")
            data = base64.b64decode(b64)
        except Exception as e:
            _json(self, {"ok": False, "error": f"invalid payload: {e}"}, 400)
            return

        target_dir = MAPPING_DIR if str(payload.get("dir") or "").lower() == "mapping" else INBOX_DIR
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / safe
        if path.exists():
            stem, suf = path.stem, path.suffix
            i = 2
            while path.exists():
                path = target_dir / f"{stem} ({i}){suf}"
                i += 1
        path.write_bytes(data)
        _append_log(f"文件已保存：{path.name}（{len(data)/1024:.0f} KB）")
        _json(self, {"ok": True, "path": str(path), "bytes": len(data)})

    def _post_log(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        if not _RUN_LOCK.acquire(blocking=False):
            _json(self, {"ok": False, "error": "已有核算任务在进行，请稍候"})
            return

        try:
            country = str(payload.get("country") or "")
            store = str(payload.get("store") or "").strip()
            dfrom = payload.get("from") or None
            dto = payload.get("to") or None

            # 给了店铺就以店铺配置为准：国家、费率、报表命名全跟着它走，
            # 避免面板上填的国家和配置里的对不上。
            if store:
                for s in store_list().get("stores", []):
                    if s.get("id") == store:
                        country = s.get("country") or country
                        break
            country = country or "马来西亚"

            stamp = f"{dfrom or 'all'}_{dto or 'all'}".replace("-", "")
            out = paths.OUTPUT / f"净利报表_{country}_{stamp}.xlsx"

            # 打包后 sys.executable 是 exe 本身，`-m src.cli` 不再有效，
            # 改成 exe 自己的 run 子命令。两种模式参数完全一致。
            if paths.is_frozen():
                base = [sys.executable, "run"]
            else:
                base = [sys.executable, "-m", "src.cli"]
            cmd = base + [
                "--dir", str(INBOX_DIR),
                # 有店铺就只传店铺：国家由 config/stores.json 决定，
                # 两处都传容易打架（--country 会覆盖 --store）
                *(["--store", store] if store else ["--country", country]),
                "--out", str(out),
                "--json",
            ]
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
                text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""

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
                # 检查用到的导出文件有没有太旧的：广告任务可能还没下来，
                # 这时会静默沿用上一期文件，数字看着正常但其实是错的。
                stale = []
                for label, fname in (summary.get("sources") or {}).items():
                    f = INBOX_DIR / fname
                    try:
                        age_h = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
                    except OSError:
                        continue
                    if age_h > 24:
                        stale.append(f"{label}：{fname}（{age_h:.0f} 小时前）")
                if stale:
                    summary["stale"] = stale
                    _append_log("[run] 注意：有数据文件超过 24 小时未更新 → " + "；".join(stale))
                _append_log(
                    f"[run] 完成：GMV {summary.get('gmv')} 净利 {summary.get('net_local')} "
                    f"（人民币 {summary.get('net_cny')}）"
                )
                _json(self, {"ok": True, **summary})
            else:
                tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-12:])
                _append_log(f"[run] 失败：{tail.splitlines()[-1] if tail else 'unknown'}")
                _json(self, {"ok": False, "error": tail or "核算失败", "returncode": proc.returncode})
        except subprocess.TimeoutExpired:
            _append_log("[run] 失败：核算超时")
            _json(self, {"ok": False, "error": "核算超过 15 分钟未完成"})
        except Exception as e:  # noqa: BLE001
            _append_log(f"[run] 异常：{e}")
            _json(self, {"ok": False, "error": str(e)})
        finally:
            _RUN_LOCK.release()


    def _post_pending(self):
        """面板把任务放这里，扩展定时来取。

        为什么需要这条：面板 → 后台的 sendMessage 偶尔会断（上下文失效、
        后台刚被唤醒等），断了任务就发不出去。桥是普通 HTTP，
        两个方向都能通，作为第三条通道。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        with _PENDING_LOCK:
            _PENDING.clear()
            _PENDING.update(payload or {})
        _append_log(f"[pending] 收到任务：{payload.get('task')} {payload.get('range', {}).get('startDate', '')}")
        _json(self, {"ok": True})


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


def main() -> int:
    _setup_log_file()
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"本地桥已启动：http://127.0.0.1:{PORT}")
    print(f"抓包写入：{CAPTURE_DIR}")
    print("按 Ctrl+C 停止")
    try:
        _watch_code(srv)
    except Exception as e:  # noqa: BLE001
        print(f"热重载未启用（忽略）：{e}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






# test0

# test1

# test2
