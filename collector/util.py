"""Shared normalization, redaction, and chunking helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

ERROR_RE = re.compile(
    r"\b(error|exception|traceback|failed|failure|undefined method|nomethoderror|"
    r"argumenterror|sqlite3::|active_record::|errno::|status:\s*[45]\d\d|"
    r"could not|unable to|cannot|can't)\b",
    re.IGNORECASE,
)
COMMAND_RE = re.compile(
    r"^(?:\$ |❯ )?(bin/|docker\s|git\s|curl\s|bundle\s|rails\s|npm\s|npx\s|pnpm\s|"
    r"make\s|ssh\s|kubectl\s|psql\s|sqlite3\s|uv\s|python3?\s|ruby\s|bin/)"
)
DECISION_RE = re.compile(
    r"\b(decided|decision|we will|we'll|chose|chosen|instead|tradeoff|trade-off|"
    r"constraint|must not|never\s|should not|keep\s.{1,40}\bfree|do not|"
    r"the reason|why we|root cause)\b",
    re.IGNORECASE,
)

REDACTIONS = [
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_\-]{30,})\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token|token)\s*[:=]\s*)[\"']?[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in REDACTIONS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def extract_atoms(text: str, limit: int = 10) -> list[str]:
    atoms: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text):
        line = raw.strip()
        if len(line) < 8:
            continue
        if ERROR_RE.search(line) or COMMAND_RE.match(line) or DECISION_RE.search(line):
            key = re.sub(r"\s+", " ", line).lower()
            if key in seen:
                continue
            seen.add(key)
            atoms.append(line[:400])
            if len(atoms) >= limit:
                break
    return atoms


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… [truncated]"


def chunk_text(text: str, max_chars: int = 2400, overlap: int = 200) -> list[dict[str, Any]]:
    """Split on paragraph boundaries while preserving character offsets."""
    if not text.strip():
        return []
    paragraphs = re.split(r"(\n\s*\n)", text)
    chunks: list[dict[str, Any]] = []
    buf = ""
    start = 0
    cursor = 0
    for piece in paragraphs:
        if not piece:
            continue
        if buf and len(buf) + len(piece) > max_chars:
            chunks.append({"text": buf.strip(), "start": start, "end": cursor})
            tail = buf[-overlap:] if overlap else ""
            buf = tail + piece
            start = max(0, cursor - len(tail))
        else:
            if not buf:
                start = cursor
            buf += piece
        cursor += len(piece)
    if buf.strip():
        chunks.append({"text": buf.strip(), "start": start, "end": len(text)})
    return chunks


def chunk_pieces(pieces: list[dict[str, Any]], max_chars: int = 2600) -> list[dict[str, Any]]:
    """Group ordered session pieces into roughly bounded chunks with line anchors."""
    chunks: list[dict[str, Any]] = []
    buf: list[dict[str, Any]] = []
    size = 0
    for piece in pieces:
        text = piece.get("text") or ""
        if not text:
            continue
        if buf and size + len(text) > max_chars:
            chunks.append(_piece_chunk(buf))
            # Keep the last piece as context for the next chunk.
            buf = [buf[-1]] if len(buf[-1]["text"]) < 500 else []
            size = sum(len(p["text"]) for p in buf)
        buf.append(piece)
        size += len(text)
    if buf:
        chunks.append(_piece_chunk(buf))
    return [chunk for chunk in chunks if chunk["text"].strip()]


def _piece_chunk(buf: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": "\n".join(p["text"] for p in buf),
        "start_line": buf[0].get("line"),
        "end_line": buf[-1].get("line"),
        "start_ts": buf[0].get("ts"),
        "end_ts": buf[-1].get("ts"),
        "roles": sorted({p.get("role", "") for p in buf if p.get("role")}),
        "message_ids": [p["message_id"] for p in buf if p.get("message_id")][:20],
        "tool_names": sorted({p["tool"] for p in buf if p.get("tool")})[:20],
    }


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]], limit: int = 200) -> str:
    rows = list(rows)
    shown = rows[:limit]
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in shown:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > len(shown):
        lines.append(f"\n… {len(rows) - len(shown)} more rows omitted.")
    return "\n".join(lines)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")[:120] or "item"


def file_date(path: str) -> str | None:
    try:
        return iso(datetime.fromtimestamp(__import__("os").path.getmtime(path), tz=timezone.utc))
    except OSError:
        return None
