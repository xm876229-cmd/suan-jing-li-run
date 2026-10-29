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
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

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
_PENDING: list = []          # 待办队列：[{task, range, store, country, at}, ...]
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


_START_AT = datetime.now()


def bridge_uptime() -> str:
    """桥已经跑了多久，首页显示用。"""
    sec = int((datetime.now() - _START_AT).total_seconds())
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


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
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    (CAPTURE_DIR / "endpoints.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
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
        inbox = d / "inbox"
        files = [f for f in inbox.iterdir() if f.is_file()] if inbox.is_dir() else []
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
        paths.STORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.STORES_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写入 stores.json 失败：{e}"}

    _append_log(f"[store] 店铺改名：{sid} → {hit['name']}")
    return {"ok": True, "shop_id": sid, "name": hit["name"]}


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

    default = ""
    try:
        default = cfg.get("defaults", {}).get("country", "")
    except Exception:
        pass

    return {"ok": True, "stores": stores, "countries": countries, "default": default}


def _make_snapshot(shop_id: str, payload: dict) -> dict:
    """把某家店当前 inbox 里的文件存成一份快照。

    上传一次就是一批数据，所以上传完顺手存一份——
    这样「这次取的是哪批数据」永远能追回来，也随时能回到老数据重算。
    快照失败不影响上传（数据已经落盘了），只记一条日志。
    """
    try:
        from src import snapshots as snaps
        rng = payload.get("range") or {}
        dfrom = str(rng.get("startDate") or rng.get("from") or "")
        dto = str(rng.get("to") or rng.get("endDateExclusive") or "")
        if dto and dfrom:
            # 区间是「结束日期不含」，标签上往前挪一天更符合人的直觉
            try:
                from datetime import datetime, timedelta
                dto = (datetime.strptime(dto, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            except ValueError:
                pass
        name = str(payload.get("shop_name") or "")
        return snaps.create(shop_id, snaps.pick_files(shop_id), dfrom, dto, name)
    except Exception as e:  # noqa: BLE001
        _append_log(f"[upload] 快照入库失败：{e}")
        return {}


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
            # 取队首一个（不是整队掏空）：自动任务在跑的时候，
            # 手动点的那个要能在它后面排着，而不是被清掉。
            with _PENDING_LOCK:
                job = _PENDING.pop(0) if _PENDING else None
            _json(self, {"ok": True, "job": job})
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
            _json(self, settings_api.report_view((q.get("name") or [""])[0]))
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
        if self.path.startswith("/api/mapping"):
            self._post_mapping()
        elif self.path.startswith("/api/stores/rename"):
            self._post_rename_store()
        elif self.path.startswith("/identify"):
            self._post_identify()
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
        elif self.path.startswith("/api/data-import"):
            self._post_data_import()
        elif self.path.startswith("/api/link-info"):
            self._post_link_import()
        elif self.path.startswith("/api/presets/"):
            self._post_presets()
        elif self.path.startswith("/api/costs/import"):
            self._post_costs(save=False)
        elif self.path.startswith("/capture"):
            self._post_capture()
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

    def _post_config(self, preview: bool):
        """参数表试算 / 保存。"""
        from bridge import config_page

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            _json(self, config_page.save_config(
                params, payload.get("formulas") or {},
                by_country=payload.get("by_country"),
                order_fields=payload.get("order_fields"),
                cost_fields=payload.get("cost_fields"),
                exclude_zero_amount=payload.get("exclude_zero_amount")))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_upload(self):
        """扩展把下载到的导出文件回传到这里。

        落盘位置：data/stores/<店铺ID>/inbox/<类型>/，
        类型靠**表头**判断（订单/广告/联盟/映射），不靠文件名——
        文件名会变，认表头才靠谱。

        认不出店铺时不再静默丢进公共目录（旧行为会把多家店的数据混在一起，
        而且不报错），改落 data/stores/_unknown/<时间戳>/ 并回报一条警告。
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

        kind = str(payload.get("dir") or "").lower()
        shop_id = str(payload.get("shop_id") or "").strip()

        safe = re.sub(r'[\\/:*?"<>|]', "_", name)

        # 先落到临时位置，认出是什么表再决定放哪个子目录。
        # （直接按名字猜类型不行：平台导出的文件名千奇百怪，
        #   有时同一类表名字还带不同后缀。）
        tmp_dir = paths.EXPORTS / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"upload-{int(time.time()*1000)}-{safe}"
        try:
            tmp_path.write_bytes(data)
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
        if kind == "mapping" or table_kind == "mapping":
            # 映射表（商品管理 SKU、链接资料）走全局累积主表：
            # 链接改绑后老 SKU 也要能查到归属，所以不能只留最新一份。
            target_dir = MAPPING_DIR
        elif shop_id:
            target_dir = paths.store_kind_dir(shop_id, table_kind or "orders")
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
        out = {"ok": True, "path": str(path), "bytes": len(data),
               "shop_id": shop_id, "store_dir": str(target_dir),
               "kind": table_kind or "unknown"}
        if warning:
            out["warning"] = warning
        # 上传即入库：顺手存一份快照，随时能回到这批数据重算。
        # 区间取扩展带上来的（这一轮取的是什么日期范围），
        # 界面上「用哪次数据」列表就能显示每份快照对应哪个区间。
        _snap = _make_snapshot(shop_id, payload)
        if _snap.get("id"):
            out["snapshot"] = _snap.get("id")
        _json(self, out)

    def _post_presets(self):
        """预设：保存 / 应用 / 删除 / 回滚。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return

        name = str(payload.get("name") or "").strip()
        b64 = payload.get("b64") or ""
        if not name or not b64:
            _json(self, {"ok": False, "error": "缺少文件名或内容"}, 400)
            return
        try:
            data = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"内容解码失败：{e}"}, 400)
            return

        suffix = Path(name).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".csv", ".txt", ".json"}:
            _json(self, {"ok": False, "error":
                         f"不支持的文件类型：{suffix or '（没有扩展名）'}"}, 400)
            return
        if len(data) > 60 * 1024 * 1024:
            _json(self, {"ok": False, "error": "文件超过 60 MB"}, 400)
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
        tmp = tmp_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}{suffix}"
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
            path.write_bytes(data)
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
        # 手动导入的也算一批数据，存一份快照，随时能回到它重算
        _snap = _make_snapshot(shop_id, {"shop_name": shop_id})
        if _snap.get("id"):
            out["snapshot"] = _snap.get("id")
        _json(self, out)

    def _post_link_import(self):
        """导入店铺后台下载的「链接资料」表，读出每条链接的款式编码。

        报表上「款式编码」那一列靠它。
        店铺后台的表没有款式编码列，程序从商家 SKU 推导
        （V27-White-10Pcs → V27），跟你手工维护的那列效果一致。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
        tmp = tmp_dir / f"link-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}{suffix}"
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            _json(self, {"ok": False, "error": "invalid json"}, 400)
            return
        try:
            _json(self, settings_api.save_mapping(payload.get("fields") or []))
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": f"保存失败：{e}"}, 500)

    def _post_rename_store(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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

    def _post_auto_now(self):
        """「现在跑一次」：立刻派一个取数+核算的任务。

        走的是同一条队列，所以扩展在线就马上开始，不在线就排队等着。
        """
        import time as _time
        from datetime import datetime

        from src import auto_run, settings as cfg_settings

        cfg = (cfg_settings.load().get("auto") or {})
        now = datetime.now()
        rng = auto_run.build_range(str(cfg.get("range") or "last_month"), now)
        stores = [str(s) for s in (cfg.get("stores") or []) if str(s).strip()]
        job = {
            "task": "all",
            "range": rng,
            "store": stores[0] if len(stores) == 1 else "",
            "auto": True,
            "manual": True,      # 手动点的：即使自动运行关着也照跑
            "at": int(_time.time() * 1000),
        }
        with _PENDING_LOCK:
            _PENDING.append(job)
        _append_log(f"[自动] 手动触发一次：{auto_run.describe_range(str(cfg.get('range') or 'last_month'), now)}")
        _json(self, {"ok": True, "range": rng,
                     "note": "任务已派发。浏览器里的扩展会在下一次轮询时领走"
                             "（一般几秒内），浏览器关着的话下次打开才会跑。"})

    def _post_settings(self):
        from src import settings as cfg_settings
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
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
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            payload = {}

        try:
            from src import fetcher
            task = str(payload.get("task") or "")
            results = payload.get("results") or []
            rng = payload.get("range") or {}
            store = str(payload.get("store") or "")
            r = fetcher.process_results(task, results, rng, store)
            _json(self, {"ok": r["ok"], "files": r.get("files") or [],
                         "issues": r.get("issues") or []})
        except Exception as e:  # noqa: BLE001
            _json(self, {"ok": False, "error": str(e), "issues": [str(e)]}, 400)

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
            if store:
                for st in store_list().get("stores", []):
                    if st.get("id") == store:
                        country = st.get("country") or country
                        store_name = st.get("name") or ""
                        shop_code = st.get("shop_code") or ""
                        shop_id = st.get("shop_id") or shop_id or store
                        break
            if not store_name:
                store_name = country or "未识别店铺"
            country = country or "马来西亚"

            # 数据来源：有 shop_id 就读它自己的目录，各店数据不混
            data_dir = paths.store_dir(shop_id)
            if not data_dir.is_dir() or not any(data_dir.iterdir()):
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
                        note=str(payload.get("note") or ""))
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

            # 报表放到该店铺自己的子目录，文件名按设置里的模板生成
            cfg_settings.load()
            out_dir = paths.OUTPUT / re.sub(r'[\\/:*?"<>|]', "_", store_name)
            out_dir.mkdir(parents=True, exist_ok=True)

            stg = cfg_settings.load().get("report", {})
            base_name = cfg_settings.render_report_name(
                stg.get("name_template") or "{店铺}_{区间}",
                store=store_name, shop_id=shop_id, shop_code=shop_code,
                country=country, dfrom=dfrom or "", dto=dto or "",
            )
            out = out_dir / f"{base_name}.xlsx"
            if out.exists():
                i = 2
                while out.exists():
                    out = out_dir / f"{base_name} ({i}).xlsx"
                    i += 1

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
                *(["--store", store] if store else ["--country", country]),
                "--out", str(out),
                "--json",
            ]
            if len(data_dirs) > 1:
                cmd += ["--snapshots", ",".join(d.name for d in data_dirs)]
            elif snap_id:
                cmd += ["--snapshot", snap_id]
            # 带不带公式：手动跑用界面上的勾选；自动跑用设置里的开关
            _wf = payload.get("with_formula")
            if _wf is None:
                _auto_cfg = cfg_settings.load().get("auto") or {}
                _wf = _auto_cfg.get("with_formula")
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
                summary["report_path"] = str(out)
                summary["report_name"] = out.name
                summary["store_name"] = store_name
                summary["shop_id"] = shop_id

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
                # 自动运行时用「自动运行」里的目录，手动跑用「报表」页里的目录
                copied = None
                try:
                    _dl = stg.get("download_dir") or ""
                    if payload.get("auto"):
                        _auto_cfg = cfg_settings.load().get("auto") or {}
                        _dl = _auto_cfg.get("download_dir") or _dl
                    copied = cfg_settings.copy_to_download_dir(out, _dl)
                except Exception:  # noqa: BLE001
                    copied = None
                if copied:
                    summary["copied_to"] = str(copied)
                    _append_log(f"[run] 已复制到 {copied}")

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


def _auto_runner() -> None:
    """自动运行：到点往待办队列里塞一个「取数 + 核算」任务。

    扩展会来领这个任务（GET /pending），所以浏览器关着也不影响桥本身。
    跑完的报表照常出在「报表」目录，设定下载目录的话再复制一份过去。
    """
    import time
    from datetime import datetime

    from src import auto_run, settings as cfg_settings

    last_key = ""     # 上次触发用的键（频率+时间+区间），防止同一分钟重复触发

    while True:
        time.sleep(20.0)
        try:
            cfg = (cfg_settings.load().get("auto") or {})
            if not cfg.get("enabled"):
                continue
            now = datetime.now()
            if not auto_run.should_run(cfg, now):
                continue
            key = f"{now:%Y-%m-%d %H:%M}|{cfg.get('freq')}|{cfg.get('time')}|{cfg.get('range')}"
            if key == last_key:
                continue
            last_key = key
            rng = auto_run.build_range(str(cfg.get("range") or "last_month"), now)
            stores = [str(s) for s in (cfg.get("stores") or []) if str(s).strip()]
            job = {
                "task": "all",
                "range": rng,
                "store": stores[0] if len(stores) == 1 else "",
                "auto": True,
                "at": int(now.timestamp() * 1000),
            }
            with _PENDING_LOCK:
                _PENDING.append(job)
            _append_log(f"[自动] {now:%H:%M} 派发任务："
                        f"{auto_run.describe_range(str(cfg.get('range') or 'last_month'), now)}"
                        f"{'　店铺：' + '、'.join(stores) if stores else '　全部店铺'}")
            print(f"[自动] 已派发任务 {rng}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[自动] 检查出错（忽略）：{e}", flush=True)


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
        threading.Thread(target=_auto_runner, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        print(f"自动运行未启用（忽略）：{e}")
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
