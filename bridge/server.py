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

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DIR = ROOT / "data" / "capture"
PORT = 8765


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
                         "captureDir": str(CAPTURE_DIR)})
        elif self.path.startswith("/endpoints"):
            _json(self, rebuild_endpoints())
        else:
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    def do_POST(self):
        if not self.path.startswith("/capture"):
            _json(self, {"ok": False, "error": "unknown endpoint"}, 404)
            return
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
        f = CAPTURE_DIR / f"capture-{datetime.now():%Y%m%d}.jsonl"
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


def main() -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"本地桥已启动：http://127.0.0.1:{PORT}")
    print(f"抓包写入：{CAPTURE_DIR}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
