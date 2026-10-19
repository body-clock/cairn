"""Minimal MCP Streamable HTTP server logic (JSON-RPC 2.0, tools only)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "printlyrics-knowledge"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
This server is the PrintLyrics knowledge base. Before answering questions about
PrintLyrics decisions, product history, analytics, prior investigations, or
implementation constraints, call knowledge_search with a focused query and then
knowledge_read on the strongest results. Cite source_uri and event_at. Treat
generated summaries as navigation aids, not as evidence. For analytics
questions, pass scope "printlyrics/analytics" or source_type "umami",
"plausible_export", or "search_console_export" so measurement records are not
buried under code-session matches. If the corpus does not contain the answer,
say so instead of guessing.
"""

TOOLS = [
    {
        "name": "knowledge_search",
        "description": (
            "Search the PrintLyrics knowledge base using SQLite FTS5/BM25 with "
            "recency and authority weighting. Returns ranked excerpts, source "
            "URIs, event dates, and record IDs. Use this before answering "
            "questions about PrintLyrics history, decisions, code, analytics, "
            "prior investigations, or constraints. For analytics questions, "
            "filter with scope=printlyrics/analytics or a measurement source_type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query or keywords."},
                "scope": {
                    "type": "string",
                    "description": "Optional scope prefix, e.g. printlyrics, printlyrics/analytics, printlyrics/ideation.",
                },
                "source_type": {
                    "type": "string",
                    "description": "Optional source filter: session, git, doc, plausible_export, search_console_export, umami, app_db.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
                "since": {"type": "string", "description": "Optional inclusive ISO date lower bound on event_at."},
                "until": {"type": "string", "description": "Optional inclusive ISO date upper bound on event_at."},
                "sensitivity": {
                    "type": "string",
                    "description": "Optional filter: public, internal, or personal. Customer feedback notes are marked personal.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "knowledge_read",
        "description": (
            "Read the full text and metadata of one knowledge-base record by ID. "
            "Call this after knowledge_search when an excerpt looks relevant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 100000, "default": 20000},
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "knowledge_status",
        "description": (
            "Report corpus coverage: active documents by source and scope, event "
            "date ranges, database size, and recent import runs. Use this when an "
            "answer depends on whether the corpus is complete or fresh."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "knowledge_sources",
        "description": "List indexed source types and scopes with document counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _call_tool(conn: sqlite3.Connection, db_path: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "knowledge_search":
        query = (arguments.get("query") or "").strip()
        if not query:
            return _text_result({"error": "query is required"}, is_error=True)
        results = db.search(
            conn,
            query,
            scope=arguments.get("scope"),
            source_type=arguments.get("source_type"),
            limit=int(arguments.get("limit") or 8),
            since=arguments.get("since"),
            until=arguments.get("until"),
            sensitivity=arguments.get("sensitivity"),
        )
        return _text_result({
            "query": query,
            "count": len(results),
            "results": results,
            "hint": "Call knowledge_read with a result id to read the full record before making claims.",
        })

    if name == "knowledge_read":
        record_id = arguments.get("record_id") or arguments.get("id")
        if not record_id:
            return _text_result({"error": "record_id is required"}, is_error=True)
        document = db.get_document(conn, record_id, int(arguments.get("max_chars") or 20000))
        if document is None:
            return _text_result({"error": f"record not found: {record_id}"}, is_error=True)
        return _text_result(document)

    if name == "knowledge_status":
        return _text_result(db.status(conn, db_path))

    if name == "knowledge_sources":
        return _text_result(db.sources(conn))

    return _text_result({"error": f"unknown tool: {name}"}, is_error=True)


def handle_message(conn: sqlite3.Connection, db_path: str, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    is_notification = "id" not in message

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "arguments must be an object"},
            }
        try:
            result = _call_tool(conn, db_path, name, arguments)
        except Exception as exc:  # keep the MCP session alive on tool failure
            result = _text_result({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}

    if is_notification:
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def handle_payload(conn: sqlite3.Connection, db_path: str, payload: Any) -> Any:
    if isinstance(payload, list):
        responses = [handle_message(conn, db_path, item) for item in payload]
        return [response for response in responses if response is not None]
    if isinstance(payload, dict):
        return handle_message(conn, db_path, payload)
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "invalid request"},
    }
