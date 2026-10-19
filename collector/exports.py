"""Import Plausible ZIP exports and Search Console CSV exports."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from . import util


def _read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    text = text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _zip_date(path: Path) -> str:
    return util.iso(util.parse_iso(util.file_date(str(path)))) or util.iso_now()


def _plausible_member_doc(zip_name: str, member: str, headers: list[str], rows: list[list[str]], event_at: str) -> dict[str, Any]:
    table = util.markdown_table(headers, rows, limit=500)
    title = f"Plausible export {zip_name} — {member}"
    body = f"# {title}\n\nExport date: {event_at}\n\n{table}"
    return {
        "id": util.stable_id("plausible", zip_name, member),
        "source_type": "plausible_export",
        "source_group": "exports",
        "source_key": f"{zip_name}/{member}",
        "source_uri": f"plausible://{zip_name}#{member}",
        "project": "printlyrics",
        "scope": "printlyrics/analytics",
        "title": title,
        "summary": util.truncate(f"{member}: " + ", ".join(headers), 300),
        "body": body,
        "notes": "",
        "event_at": event_at,
        "content_hash": util.content_hash(body),
        "authority": 0.9,
        "sensitivity": "internal",
        "metadata": {"export": zip_name, "member": member, "rows": len(rows), "columns": headers},
    }


def _plausible_summary(zip_name: str, members: dict[str, tuple[list[str], list[list[str]]]], event_at: str) -> dict[str, Any]:
    lines = [f"# Plausible export: {zip_name}", "", f"Export date: {event_at}", ""]
    visitors = members.get("visitors.csv")
    if visitors:
        headers, rows = visitors
        if "date" in headers:
            dates = [r[headers.index("date")] for r in rows if r and r[0]]
            numeric = {h: 0.0 for h in headers if h != "date"}
            for row in rows:
                for h in numeric:
                    try:
                        numeric[h] += float(row[headers.index(h)] or 0)
                    except (ValueError, IndexError):
                        pass
            lines.append("## Range and totals")
            lines.append("")
            lines.append(f"- Date range: {min(dates) if dates else 'unknown'} to {max(dates) if dates else 'unknown'}")
            for key, value in numeric.items():
                lines.append(f"- {key}: {value:g}")
            lines.append("")
    conversions = members.get("conversions.csv")
    if conversions:
        headers, rows = conversions
        lines.append("## Conversions")
        lines.append("")
        lines.append(util.markdown_table(headers, rows[:50]))
        lines.append("")
    pages = members.get("pages.csv")
    if pages:
        headers, rows = pages
        lines.append("## Top pages")
        lines.append("")
        lines.append(util.markdown_table(headers, rows[:20]))
        lines.append("")
    lines.append("## Files in export")
    lines.append("")
    for name, (headers, rows) in sorted(members.items()):
        lines.append(f"- `{name}` — {len(rows)} rows")

    body = "\n".join(lines)
    return {
        "id": util.stable_id("plausible-summary", zip_name),
        "source_type": "plausible_export",
        "source_group": "exports",
        "source_key": zip_name,
        "source_uri": f"plausible://{zip_name}",
        "project": "printlyrics",
        "scope": "printlyrics/analytics",
        "title": f"Plausible export summary: {zip_name}",
        "summary": util.truncate("Plausible export with " + ", ".join(sorted(members)) , 320),
        "body": body,
        "notes": "",
        "event_at": event_at,
        "content_hash": util.content_hash(body),
        "authority": 0.95,
        "sensitivity": "internal",
        "metadata": {"export": zip_name, "files": sorted(members)},
    }


def _gsc_doc(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    headers, rows = _read_csv(text)
    if not headers:
        return None
    table = util.markdown_table(headers, rows, limit=1000)
    title = f"Search Console export: {path.name}"
    body = f"# {title}\n\n{table}"
    return {
        "id": util.stable_id("gsc", path.name),
        "source_type": "search_console_export",
        "source_group": "exports",
        "source_key": path.name,
        "source_uri": f"gsc-export://{path.name}",
        "project": "printlyrics",
        "scope": "printlyrics/analytics",
        "title": title,
        "summary": util.truncate(f"{path.name}: " + ", ".join(headers), 300),
        "body": body,
        "notes": "",
        "event_at": util.file_date(str(path)),
        "content_hash": util.content_hash(body),
        "authority": 0.85,
        "sensitivity": "internal",
        "metadata": {"file": path.name, "rows": len(rows), "columns": headers},
    }


def collect(conn: sqlite3.Connection, exports_dir: str) -> dict[str, Any]:
    root = Path(exports_dir)
    stats = {"zips": 0, "csv": 0, "documents": 0}
    if not root.exists():
        stats["error"] = f"exports dir missing: {root}"
        return stats
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for path in sorted(root.glob("*.zip")):
        if "plausible" not in path.name.lower():
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                members: dict[str, tuple[list[str], list[list[str]]]] = {}
                for name in archive.namelist():
                    if not name.lower().endswith(".csv"):
                        continue
                    raw = archive.read(name).decode("utf-8", errors="replace")
                    headers, rows = _read_csv(raw)
                    if headers:
                        members[name] = (headers, rows)
        except (OSError, zipfile.BadZipFile) as exc:
            stats.setdefault("errors", []).append(f"{path.name}: {exc}")
            continue
        if not members:
            continue
        event_at = _zip_date(path)
        summary = _plausible_summary(path.name, members, event_at)
        docs.append(summary)
        seen.add(summary["id"])
        for name, (headers, rows) in members.items():
            doc = _plausible_member_doc(path.name, name, headers, rows, event_at)
            docs.append(doc)
            seen.add(doc["id"])
        stats["zips"] += 1

    for path in sorted(root.glob("*.csv")):
        if not path.name.lower().startswith("printlyrics.app_"):
            continue
        doc = _gsc_doc(path)
        if doc:
            docs.append(doc)
            seen.add(doc["id"])
            stats["csv"] += 1

    from .db_writer import upsert, mark_missing
    upsert(conn, docs)
    mark_missing(conn, "exports", seen)
    stats["documents"] = len(docs)
    return stats
