"""SQLite storage and BM25 retrieval for the PrintLyrics knowledge base."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or",
    "our", "so", "that", "the", "their", "then", "there", "these", "they",
    "this", "to", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "you", "your",
}


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_group TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_uri TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT 'printlyrics',
            scope TEXT NOT NULL DEFAULT 'printlyrics',
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            event_at TEXT,
            imported_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            authority REAL NOT NULL DEFAULT 0.5,
            sensitivity TEXT NOT NULL DEFAULT 'internal',
            status TEXT NOT NULL DEFAULT 'active',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_documents_source
            ON documents(source_type, source_group);
        CREATE INDEX IF NOT EXISTS idx_documents_scope
            ON documents(scope, status);
        CREATE INDEX IF NOT EXISTS idx_documents_event
            ON documents(event_at);
        CREATE INDEX IF NOT EXISTS idx_documents_key
            ON documents(source_key);

        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            documents_seen INTEGER NOT NULL DEFAULT 0,
            documents_upserted INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            stats_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            title, summary, body, notes,
            content='documents',
            content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, title, summary, body, notes)
            VALUES (new.rowid, new.title, new.summary, new.body, new.notes);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, title, summary, body, notes)
            VALUES ('delete', old.rowid, old.title, old.summary, old.body, old.notes);
        END;

        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, title, summary, body, notes)
            VALUES ('delete', old.rowid, old.title, old.summary, old.body, old.notes);
            INSERT INTO documents_fts(rowid, title, summary, body, notes)
            VALUES (new.rowid, new.title, new.summary, new.body, new.notes);
        END;
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def normalize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults so every collector only supplies the meaningful fields."""
    now = now_iso()
    body = doc.get("body") or ""
    summary = doc.get("summary") or ""
    if not summary:
        summary = re.sub(r"\s+", " ", body).strip()[:280]
    return {
        "id": doc["id"],
        "source_type": doc["source_type"],
        "source_group": doc["source_group"],
        "source_key": doc.get("source_key") or doc["source_uri"],
        "source_uri": doc["source_uri"],
        "project": doc.get("project", "printlyrics"),
        "scope": doc.get("scope", "printlyrics"),
        "title": doc.get("title") or "",
        "summary": summary,
        "body": body,
        "notes": doc.get("notes") or "",
        "event_at": doc.get("event_at"),
        "imported_at": doc.get("imported_at") or now,
        "content_hash": doc["content_hash"],
        "authority": float(doc.get("authority", 0.5)),
        "sensitivity": doc.get("sensitivity", "internal"),
        "status": doc.get("status", "active"),
        "metadata_json": json.dumps(doc.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
    }


UPSERT_SQL = """
INSERT INTO documents (
    id, source_type, source_group, source_key, source_uri, project, scope,
    title, summary, body, notes, event_at, imported_at, content_hash,
    authority, sensitivity, status, metadata_json
) VALUES (
    :id, :source_type, :source_group, :source_key, :source_uri, :project, :scope,
    :title, :summary, :body, :notes, :event_at, :imported_at, :content_hash,
    :authority, :sensitivity, :status, :metadata_json
)
ON CONFLICT(id) DO UPDATE SET
    source_type=excluded.source_type,
    source_group=excluded.source_group,
    source_key=excluded.source_key,
    source_uri=excluded.source_uri,
    project=excluded.project,
    scope=excluded.scope,
    title=excluded.title,
    summary=excluded.summary,
    body=excluded.body,
    notes=excluded.notes,
    event_at=excluded.event_at,
    imported_at=excluded.imported_at,
    content_hash=excluded.content_hash,
    authority=excluded.authority,
    sensitivity=excluded.sensitivity,
    status=excluded.status,
    metadata_json=excluded.metadata_json
"""


def upsert_documents(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]]) -> int:
    rows = [normalize_doc(doc) for doc in docs]
    if not rows:
        return 0
    with conn:
        conn.executemany(UPSERT_SQL, rows)
    return len(rows)


def mark_missing(conn: sqlite3.Connection, source_group: str, seen_ids: set[str]) -> int:
    """Mark records removed from a fully scanned source as deleted."""
    with conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen_ids (id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _seen_ids")
        conn.executemany("INSERT OR IGNORE INTO _seen_ids(id) VALUES (?)", [(i,) for i in seen_ids])
        cur = conn.execute(
            """
            UPDATE documents
               SET status='deleted'
             WHERE source_group=?
               AND status='active'
               AND id NOT IN (SELECT id FROM _seen_ids)
            """,
            (source_group,),
        )
        conn.execute("DELETE FROM _seen_ids")
        return cur.rowcount


def build_match(query: str) -> str:
    """Turn a natural-language question into a tolerant FTS5 OR query."""
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    tokens = re.findall(r"[\w][\w.+/:@#-]*", rest.lower())
    parts: list[str] = []
    for phrase in phrases:
        phrase = phrase.replace('"', " ")
        if phrase.strip():
            parts.append(f'"{phrase.strip()}"')
    for token in tokens:
        token = token.strip("._-/")
        if len(token) < 2 or token in STOPWORDS:
            continue
        token = token.replace('"', "")
        parts.append(f'"{token}"*')
    seen: set[str] = set()
    unique = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return " OR ".join(unique)


SOURCE_WEIGHTS = {
    "umami": 1.35,
    "plausible_export": 1.30,
    "search_console_export": 1.25,
    "doc": 1.15,
    "app_db": 0.90,
    "git": 1.00,
    "session": 0.85,
}


def _recency_factor(event_at: str | None, now: datetime) -> float:
    parsed = parse_dt(event_at)
    if parsed is None:
        return 1.0
    age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
    return 1.0 + 0.25 * math.exp(-age_days / 180.0)


def _public_row(row: sqlite3.Row, snippet: str, score: float) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        pass
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "snippet": snippet,
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "project": row["project"],
        "scope": row["scope"],
        "event_at": row["event_at"],
        "authority": round(row["authority"], 3),
        "sensitivity": row["sensitivity"],
        "score": round(score, 4),
        "metadata": metadata,
    }


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: str | None = None,
    source_type: str | None = None,
    limit: int = 8,
    since: str | None = None,
    until: str | None = None,
    sensitivity: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    match = build_match(query)
    if not match:
        return []
    limit = max(1, min(int(limit), 50))

    clauses = ["documents_fts MATCH ?"]
    params: list[Any] = [match]
    if not include_deleted:
        clauses.append("d.status='active'")
    if scope:
        clauses.append("d.scope LIKE ?")
        params.append(f"{scope}%")
    if source_type:
        clauses.append("d.source_type=?")
        params.append(source_type)
    if since:
        clauses.append("d.event_at >= ?")
        params.append(since)
    if until:
        clauses.append("d.event_at <= ?")
        params.append(until)
    if sensitivity:
        clauses.append("d.sensitivity=?")
        params.append(sensitivity)

    sql = f"""
        SELECT d.id, d.title, d.summary, d.body, d.source_type, d.source_uri,
               d.project, d.scope, d.event_at, d.authority, d.sensitivity,
               d.metadata_json,
               -bm25(documents_fts, 8.0, 3.0, 1.0, 2.0) AS relevance,
               snippet(documents_fts, 2, '[[', ']]', '…', 40) AS snippet
          FROM documents_fts
          JOIN documents d ON d.rowid = documents_fts.rowid
         WHERE {' AND '.join(clauses)}
         ORDER BY relevance DESC
         LIMIT ?
    """
    params.append(max(limit * 12, 60))
    rows = conn.execute(sql, params).fetchall()

    now = datetime.now(timezone.utc)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        relevance = float(row["relevance"] or 0.0)
        score = (
            relevance
            * _recency_factor(row["event_at"], now)
            * (1.0 + 0.5 * float(row["authority"]))
            * SOURCE_WEIGHTS.get(row["source_type"], 1.0)
        )
        snippet = row["snippet"] or ""
        if not snippet.strip():
            snippet = (row["summary"] or row["body"] or "")[:400]
        ranked.append({"_score": score, "row": row, "snippet": snippet})
    ranked.sort(key=lambda item: item["_score"], reverse=True)
    return [
        _public_row(item["row"], item["snippet"], item["_score"])
        for item in ranked[:limit]
    ]


def get_document(conn: sqlite3.Connection, doc_id: str, max_chars: int = 20000) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        return None
    body = row["body"] or ""
    truncated = False
    if max_chars and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        pass
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "body": body,
        "truncated": truncated,
        "notes": row["notes"],
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "source_key": row["source_key"],
        "project": row["project"],
        "scope": row["scope"],
        "event_at": row["event_at"],
        "imported_at": row["imported_at"],
        "authority": row["authority"],
        "sensitivity": row["sensitivity"],
        "status": row["status"],
        "metadata": metadata,
    }


def status(conn: sqlite3.Connection, db_path: str | None = None) -> dict[str, Any]:
    by_source = [
        dict(row)
        for row in conn.execute(
            """
            SELECT source_type, COUNT(*) AS documents,
                   SUM(LENGTH(body)) AS characters,
                   MIN(event_at) AS earliest_event,
                   MAX(event_at) AS latest_event,
                   MAX(imported_at) AS last_import
              FROM documents
             WHERE status='active'
             GROUP BY source_type
             ORDER BY documents DESC
            """
        )
    ]
    by_scope = [
        dict(row)
        for row in conn.execute(
            """
            SELECT scope, COUNT(*) AS documents, MAX(event_at) AS latest_event
              FROM documents
             WHERE status='active'
             GROUP BY scope
             ORDER BY documents DESC
            """
        )
    ]
    runs = [dict(row) for row in conn.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 12")]
    for run in runs:
        try:
            run["stats"] = json.loads(run.pop("stats_json") or "{}")
        except json.JSONDecodeError:
            run["stats"] = {}
    total = conn.execute("SELECT COUNT(*) FROM documents WHERE status='active'").fetchone()[0]
    deleted = conn.execute("SELECT COUNT(*) FROM documents WHERE status='deleted'").fetchone()[0]
    return {
        "database": db_path,
        "database_bytes": os.path.getsize(db_path) if db_path and os.path.exists(db_path) else None,
        "documents": total,
        "deleted_documents": deleted,
        "by_source": by_source,
        "by_scope": by_scope,
        "recent_imports": runs,
    }


def sources(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "source_types": [dict(row) for row in conn.execute(
            "SELECT source_type, scope, COUNT(*) AS documents FROM documents WHERE status='active' GROUP BY source_type, scope ORDER BY source_type"
        )],
        "scopes": [row[0] for row in conn.execute(
            "SELECT DISTINCT scope FROM documents WHERE status='active' ORDER BY scope"
        )],
    }


def rebuild_fts(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
