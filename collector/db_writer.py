"""Thin write-side wrapper around the shared database module."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from server import db


def upsert(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]]) -> int:
    return db.upsert_documents(conn, docs)


def mark_missing(conn: sqlite3.Connection, source_group: str, seen_ids: set[str]) -> int:
    return db.mark_missing(conn, source_group, seen_ids)
