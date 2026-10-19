"""HTTP server for REST retrieval and MCP Streamable HTTP."""

from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import db, mcp

DB_PATH = os.environ.get("KNOWLEDGE_DB", os.path.join(os.getcwd(), "data", "knowledge.db"))
TOKEN = os.environ.get("KNOWLEDGE_TOKEN", "").strip()
STORE_NAME = os.environ.get("KNOWLEDGE_NAME", "").strip()
MAX_BODY = 2 * 1024 * 1024


def get_conn():
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    return conn


class Handler(BaseHTTPRequestHandler):
    server_version = f"PrintLyricsKnowledge/{mcp.SERVER_VERSION}"

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == TOKEN:
            return True
        if self.headers.get("X-API-Key", "").strip() == TOKEN:
            return True
        return False

    def _send(self, status: int, payload=None, *, headers: dict[str, str] | None = None, raw: bytes | None = None):
        body = raw if raw is not None else (
            b"" if payload is None else json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Session-Id, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._send(status, {"error": message})

    def do_OPTIONS(self):  # noqa: N802
        self._send(204)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            try:
                conn = get_conn()
                count = conn.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0]
                conn.close()
                self._send(200, {"ok": True, "documents": count, "name": STORE_NAME})
            except Exception as exc:
                self._send(503, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if path == "/mcp":
            self._error(405, "MCP uses POST for JSON-RPC; GET/SSE is not enabled")
            return

        if not self._authorized():
            self._error(401, "unauthorized")
            return

        if path == "/":
            self._send(200, {
                "name": "PrintLyrics knowledge base",
                "rest": ["/search?q=...", "/document/<id>", "/status", "/sources", "/health"],
                "mcp": "/mcp (Streamable HTTP, JSON-RPC 2.0)",
                "tools": [tool["name"] for tool in mcp.TOOLS],
            })
            return

        try:
            conn = get_conn()
            if path == "/search":
                q = (query.get("q") or query.get("query") or [""])[0]
                if not q.strip():
                    self._error(400, "q is required")
                    return
                results = db.search(
                    conn,
                    q,
                    scope=(query.get("scope") or [None])[0],
                    source_type=(query.get("source_type") or [None])[0],
                    limit=int((query.get("limit") or ["8"])[0]),
                    since=(query.get("since") or [None])[0],
                    until=(query.get("until") or [None])[0],
                    sensitivity=(query.get("sensitivity") or [None])[0],
                    include_deleted=(query.get("include_deleted") or ["false"])[0].lower() == "true",
                )
                self._send(200, {"query": q, "count": len(results), "results": results})
            elif path.startswith("/document/") or path.startswith("/documents/"):
                doc_id = unquote(path.split("/", 2)[2])
                max_chars = int((query.get("max_chars") or ["20000"])[0])
                document = db.get_document(conn, doc_id, max_chars)
                if document is None:
                    self._error(404, f"record not found: {doc_id}")
                else:
                    self._send(200, document)
            elif path == "/status":
                self._send(200, db.status(conn, DB_PATH))
            elif path == "/sources":
                self._send(200, db.sources(conn))
            else:
                self._error(404, "not found")
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/mcp":
            self._error(404, "not found")
            return
        if not self._authorized():
            self._error(401, "unauthorized")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._error(400, "invalid body size")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid JSON: {exc}")
            return

        session_id = self.headers.get("Mcp-Session-Id") or str(uuid.uuid4())
        headers = {"Mcp-Session-Id": session_id}
        try:
            conn = get_conn()
            response = mcp.handle_payload(conn, DB_PATH, payload)
            conn.close()
        except Exception as exc:
            self._send(500, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }, headers=headers)
            return

        if response is None or response == []:
            self._send(202, None, headers=headers)
        else:
            self._send(200, response, headers=headers)

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/mcp":
            self._send(204)
        else:
            self._error(404, "not found")


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    conn = get_conn()
    db.init_db(conn)
    status = db.status(conn, DB_PATH)
    conn.close()
    print(f"knowledge: db={DB_PATH} documents={status['documents']}", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"knowledge: listening on http://{host}:{port} (mcp: /mcp)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
