"""Summarize the local PrintLyrics development database without copying content."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from . import util


def _open_read_only(path: Path) -> sqlite3.Connection:
    """Open without writing; WAL databases need immutable when no -shm exists."""
    last: Exception | None = None
    for query in ("?mode=ro", "?mode=ro&immutable=1"):
        app = sqlite3.connect(f"file:{path}{query}", uri=True, timeout=10)
        app.row_factory = sqlite3.Row
        try:
            app.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return app
        except sqlite3.OperationalError as exc:
            last = exc
            app.close()
    raise last or sqlite3.OperationalError(f"cannot open {path}")


def collect(
    conn: sqlite3.Connection,
    db_path: str,
    project: str = "printlyrics",
    scope: str = "printlyrics",
) -> dict[str, Any]:
    path = Path(db_path)
    stats = {"path": db_path, "documents": 0}
    if not path.exists():
        stats["error"] = f"database missing: {path}"
        return stats
    app = _open_read_only(path)
    try:
        tables = [row[0] for row in app.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        title = (
            "PrintLyrics development database schema and row counts"
            if project == "printlyrics"
            else f"{project} development database schema and row counts"
        )
        source_uri = (
            "sqlite://printlyrics/storage/development.sqlite3#schema"
            if project == "printlyrics"
            else f"sqlite://{project}/app.db#schema"
        )
        heading = "PrintLyrics development database" if project == "printlyrics" else f"{project} development database"
        lines = [f"# {heading}", "", f"Path: {path}", "", "## Tables", ""]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            try:
                count = app.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            except sqlite3.Error:
                count = "unknown"
            lines.append(f"- `{table}` — {count} rows")
        lines += ["", "## Schema", ""]
        for row in app.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ):
            lines += [f"### {row['name']}", "", "```sql", row["sql"] or "", "```", ""]
        body = "\n".join(lines)
        doc = {
            "id": util.stable_id("app-db", str(path)),
            "source_type": "app_db",
            "source_group": "app-db",
            "source_key": str(path),
            "source_uri": source_uri,
            "project": project,
            "scope": scope,
            "title": title,
            "summary": "Local SQLite schema and row counts (no lyric or account content).",
            "body": body,
            "notes": "",
            "event_at": util.file_date(str(path)),
            "content_hash": util.content_hash(body),
            "authority": 0.75,
            "sensitivity": "internal",
            "metadata": {"tables": tables},
        }
        from .db_writer import upsert
        upsert(conn, [doc])
        stats["documents"] = 1
    finally:
        app.close()
    return stats
