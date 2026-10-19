"""Read-only web inspector for knowledge stores running on localhost.

Discovers stores by probing a port range, then proxies their REST API:

    GET /api/stores
    GET /api/store/<port>/status
    GET /api/store/<port>/sources
    GET /api/store/<port>/search?q=...&limit=&scope=&source_type=
    GET /api/store/<port>/document/<id>?max_chars=
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
STATE_PATH = Path.home() / ".omp" / "knowledge" / "projects.json"
TOKEN = os.environ.get("KNOWLEDGE_TOKEN", "").strip()
BIND = os.environ.get("KNOWLEDGE_UI_BIND", "127.0.0.1")
PORT = int(os.environ.get("KNOWLEDGE_UI_PORT", "8790"))
RANGE = os.environ.get("KNOWLEDGE_UI_RANGE", "8765-8790")
TIMEOUT = float(os.environ.get("KNOWLEDGE_UI_TIMEOUT", "10"))

_cache: tuple[float, list[dict]] = (0.0, [])


def _ports() -> list[int]:
    start, _, end = RANGE.partition("-")
    try:
        return list(range(int(start), int(end or start) + 1))
    except ValueError:
        return list(range(8765, 8791))


def _state_names() -> dict[int, str]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    names: dict[int, str] = {}
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict) and isinstance(entry.get("port"), int) and entry.get("name"):
                names[entry["port"]] = str(entry["name"])
    return names


def _fetch(port: int, path: str, timeout: float = 2.0) -> dict | None:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _probe(port: int) -> dict | None:
    health = _fetch(port, "/health", timeout=0.25)
    if not health or not health.get("ok"):
        return None
    status = _fetch(port, "/status", timeout=1.5)
    if not status or "documents" not in status:
        return None
    name = health.get("name") or _state_names().get(port) or f"store {port}"
    return {
        "port": port,
        "name": name,
        "documents": status.get("documents"),
        "bytes": status.get("database_bytes"),
        "by_source": status.get("by_source", []),
    }


def stores(force: bool = False) -> list[dict]:
    global _cache
    stamp, cached = _cache
    if not force and time.time() - stamp < 3.0:
        return cached
    with ThreadPoolExecutor(max_workers=12) as pool:
        found = [store for store in pool.map(_probe, _ports()) if store]
    found.sort(key=lambda store: store["port"])
    _cache = (time.time(), found)
    return found


def _upstream(port: int, path: str) -> tuple[int, dict]:
    allowed = {store["port"] for store in stores()}
    if port not in allowed:
        return 404, {"error": f"store on port {port} is not running"}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except (json.JSONDecodeError, OSError):
            return exc.code, {"error": f"upstream returned {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return 502, {"error": f"store on port {port} did not answer: {exc}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "KnowledgeInspector/0.1"

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/":
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "index.html missing"})
            return

        if path == "/api/stores":
            self._send(200, {"stores": stores(force="refresh" in query)})
            return

        if path.startswith("/api/store/"):
            parts = path.split("/")
            if len(parts) < 5:
                self._send(400, {"error": "expected /api/store/<port>/<resource>"})
                return
            try:
                port = int(parts[3])
            except ValueError:
                self._send(400, {"error": "port must be an integer"})
                return
            resource = parts[4]
            if resource == "status":
                status, payload = _upstream(port, "/status")
            elif resource == "sources":
                status, payload = _upstream(port, "/sources")
            elif resource == "search":
                search = urllib.parse.urlencode(
                    {key: query[key][0] for key in ("q", "limit", "scope", "source_type") if key in query}
                )
                status, payload = _upstream(port, f"/search?{search}")
            elif resource == "document" and len(parts) > 5:
                doc_id = unquote(parts[5])
                max_chars = query.get("max_chars", ["20000"])[0]
                status, payload = _upstream(
                    port, f"/document/{urllib.parse.quote(doc_id)}?max_chars={int(max_chars)}"
                )
            else:
                status, payload = 404, {"error": f"unknown resource {resource!r}"}
            self._send(status, payload)
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        self._send(405, {"error": "read-only"})

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"", "text/plain")


def main() -> int:
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"knowledge inspector: http://{BIND}:{PORT} (ports {RANGE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
