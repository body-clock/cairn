"""Import local omp agent sessions as searchable, source-linked records."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import util

KNOWN_PROJECTS = {
    "-code-p-printlyrics": ("printlyrics", "printlyrics"),
    "-code-p-ideation": ("printlyrics/ideation", "printlyrics-ideation"),
    "-code-p-analytics-connector": ("printlyrics/analytics", "printlyrics-analytics"),
}

TEXT_LIMIT = 4000
TOOL_LIMIT = 3000


def _tool_summary(name: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return f"[tool call: {name}]"
    keys = ("command", "path", "file_path", "query", "pattern", "url", "i", "content")
    parts = []
    for key in keys:
        value = arguments.get(key)
        if value:
            parts.append(f"{key}: {util.truncate(str(value), 600)}")
    label = arguments.get("i")
    prefix = f"[tool call: {name}]"
    if label:
        prefix += f" {label}"
    return prefix + ("\n" + "\n".join(parts) if parts else "")


def _content_pieces(message: dict[str, Any], ts: str | None) -> list[dict[str, Any]]:
    role = message.get("role") or "unknown"
    content = message.get("content")
    pieces: list[dict[str, Any]] = []
    if isinstance(content, str):
        pieces.append({"role": role, "text": content, "ts": ts})
        return pieces
    if not isinstance(content, list):
        return pieces
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = part.get("text") or ""
            if text.strip():
                pieces.append({"role": role, "text": text, "ts": ts})
        elif kind == "toolCall" and role == "assistant":
            name = part.get("name") or "tool"
            text = _tool_summary(name, part.get("arguments"))
            pieces.append({"role": role, "text": text, "ts": ts, "tool": name, "kind": "tool_call"})
        elif kind == "toolResult":
            text = part.get("text") or ""
            if text.strip():
                pieces.append({
                    "role": "toolResult",
                    "text": "[tool result]\n" + util.truncate(text, TOOL_LIMIT),
                    "ts": ts,
                    "kind": "tool_result",
                })
    return pieces


def parse_session(path: Path) -> dict[str, Any] | None:
    title = path.stem
    session_id = None
    session_ts = None
    cwd = None
    pieces: list[dict[str, Any]] = []
    redactions = 0
    skipped_thinking = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = entry.get("type")
            if kind in ("session", "title"):
                title = entry.get("title") or title
                session_id = entry.get("id") or session_id
                session_ts = entry.get("timestamp") or session_ts
                cwd = entry.get("cwd") or cwd
                continue
            if kind == "title_change":
                title = entry.get("title") or title
                continue
            if kind != "message":
                continue
            message = entry.get("message") or {}
            ts = entry.get("timestamp") or message.get("timestamp") or session_ts
            redacted_pieces = []
            for piece in _content_pieces(message, ts):
                text, n = util.redact(piece["text"])
                redactions += n
                redacted_pieces.append({**piece, "text": util.truncate(text, TEXT_LIMIT)})
            if message.get("role") == "assistant":
                for part in message.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "thinking":
                        skipped_thinking += 1
            for piece in redacted_pieces:
                piece["line"] = line_no
                piece["message_id"] = entry.get("id")
                pieces.append(piece)

    if not pieces:
        return None
    return {
        "session_id": session_id or path.stem,
        "title": title,
        "session_ts": session_ts,
        "cwd": cwd,
        "pieces": pieces,
        "redactions": redactions,
        "skipped_thinking": skipped_thinking,
    }


def session_documents(path: Path, scope: str, project: str, source_group: str) -> list[dict[str, Any]]:
    parsed = parse_session(path)
    if parsed is None:
        return []
    session_id = parsed["session_id"]
    title = parsed["title"]
    base_uri = f"omp-session://{session_id}"
    docs: list[dict[str, Any]] = []

    # Format pieces with line numbers so a chunk anchor is directly actionable.
    formatted: list[dict[str, Any]] = []
    for piece in parsed["pieces"]:
        role = piece.get("role") or "unknown"
        text = f"[L{piece['line']}] {piece.get('ts') or ''} {role}: {piece['text']}".strip()
        formatted.append({**piece, "text": text})

    chunks = util.chunk_pieces(formatted)
    for index, chunk in enumerate(chunks):
        body = chunk["text"]
        atoms = util.extract_atoms(body)
        start_line = chunk.get("start_line")
        end_line = chunk.get("end_line")
        anchor = f"#L{start_line}-{end_line}" if start_line else f"#chunk-{index}"
        docs.append({
            "id": util.stable_id("session", session_id, str(index)),
            "source_type": "session",
            "source_group": source_group,
            "source_key": session_id,
            "source_uri": base_uri + anchor,
            "project": project,
            "scope": scope,
            "title": f"{title} — lines {start_line}-{end_line}" if start_line else title,
            "summary": util.truncate(body.split("\n", 1)[0], 300),
            "body": body,
            "notes": "\n".join(atoms),
            "event_at": chunk.get("start_ts") or parsed["session_ts"],
            "content_hash": util.content_hash(body),
            "authority": 0.45,
            "sensitivity": "internal",
            "metadata": {
                "session_id": session_id,
                "cwd": parsed["cwd"],
                "line_range": [start_line, end_line],
                "roles": chunk.get("roles", []),
                "message_ids": chunk.get("message_ids", []),
                "tool_names": chunk.get("tool_names", []),
                "redactions": parsed["redactions"],
                "skipped_thinking_parts": parsed["skipped_thinking"],
                "anchor": anchor,
            },
        })

    # A session-level digest makes "what was this session about?" work even when
    # the relevant passage is split across many chunks.
    prompts = [p["text"] for p in parsed["pieces"] if p.get("role") == "user"]
    assistant = [p["text"] for p in parsed["pieces"] if p.get("role") == "assistant" and p.get("kind") != "tool_call"]
    atoms = util.extract_atoms("\n".join(p["text"] for p in parsed["pieces"]), limit=20)
    digest_body = (
        f"# Session: {title}\n\n"
        f"- Session ID: {session_id}\n"
        f"- cwd: {parsed['cwd']}\n"
        f"- Started: {parsed['session_ts']}\n"
        f"- Messages: {len(parsed['pieces'])}\n\n"
        "## User prompts\n\n" + "\n\n".join(util.truncate(p, 700) for p in prompts[:12]) + "\n\n"
        "## Final assistant text\n\n" + "\n\n".join(util.truncate(a, 1200) for a in assistant[-3:]) + "\n\n"
        "## Extracted decisions, errors, and commands\n\n" + "\n".join(f"- {a}" for a in atoms)
    )
    docs.append({
        "id": util.stable_id("session-digest", session_id),
        "source_type": "session",
        "source_group": source_group,
        "source_key": session_id,
        "source_uri": base_uri,
        "project": project,
        "scope": scope,
        "title": f"Session digest: {title}",
        "summary": util.truncate(f"{title} — {prompts[0] if prompts else ''}", 300),
        "body": digest_body,
        "notes": "\n".join(atoms),
        "event_at": parsed["session_ts"],
        "content_hash": util.content_hash(digest_body),
        "authority": 0.6,
        "sensitivity": "internal",
        "metadata": {
            "session_id": session_id,
            "cwd": parsed["cwd"],
            "prompts": len(prompts),
            "chunks": len(chunks),
            "redactions": parsed["redactions"],
            "skipped_thinking_parts": parsed["skipped_thinking"],
        },
    })
    return docs


def collect(
    conn: sqlite3.Connection,
    sessions_root: str,
    projects: list[str],
    known_projects: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    root = Path(sessions_root)
    known = known_projects if known_projects is not None else KNOWN_PROJECTS
    stats = {"files": 0, "documents": 0, "redactions": 0, "projects": []}
    if not root.exists():
        stats["error"] = f"sessions root missing: {root}"
        return stats
    for dirname in sorted(os.listdir(root)):
        mapping = known.get(dirname)
        if mapping is None:
            continue
        scope, project = mapping
        if projects and project not in projects and scope not in projects:
            continue
        directory = root / dirname
        if not directory.is_dir():
            continue
        stats["projects"].append(dirname)
        source_group = f"sessions:{dirname}"
        seen: set[str] = set()
        for path in sorted(directory.glob("*.jsonl")):
            docs = session_documents(path, scope, project, source_group)
            if not docs:
                continue
            util_rows = [d for d in docs if "redactions" in d["metadata"]]
            if util_rows:
                stats["redactions"] += max(d["metadata"].get("redactions", 0) for d in util_rows)
            from .db_writer import upsert  # local import avoids a cycle
            upsert(conn, docs)
            seen.update(d["id"] for d in docs)
            stats["files"] += 1
            stats["documents"] += len(docs)
        from .db_writer import mark_missing
        mark_missing(conn, source_group, seen)
    return stats
