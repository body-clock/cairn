"""Import project documentation, schema, and configuration notes."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import util

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _git_date(repo: str, relpath: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%cI", "--", relpath],
            capture_output=True, text=True, timeout=10,
        )
        value = result.stdout.strip()
        return util.iso(util.parse_iso(value)) if value else None
    except (OSError, subprocess.SubprocessError):
        return None


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _sections(text: str) -> list[tuple[str, str, int]]:
    """Return (heading, section text, absolute character offset)."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("", text, 0)]
    sections: list[tuple[str, str, int]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group(2).strip()
        sections.append((heading, text[start:end], start))
    return sections


def document_for_file(path: Path, repo_root: str, relpath: str, repo_name: str, scope: str, project: str) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if len(raw) > 2_000_000:
        raw = raw[:2_000_000]
    text, redactions = util.redact(raw)
    event_at = _git_date(repo_root, relpath) or util.file_date(str(path))
    source_group = f"docs:{repo_root}"
    docs: list[dict[str, Any]] = []
    used_ids: dict[str, int] = {}
    for heading, section, section_offset in _sections(text):
        for chunk_index, chunk in enumerate(util.chunk_text(section, max_chars=3000, overlap=300)):
            body = (f"# {relpath}" + (f" — {heading}" if heading else "") + "\n\n" + chunk["text"]).strip()
            chunk_start = text.count("\n", 0, section_offset + chunk["start"]) + 1
            chunk_end = text.count("\n", 0, section_offset + chunk["end"]) + 1
            atoms = util.extract_atoms(chunk["text"])
            # A file may repeat a heading; keep the first id stable and
            # disambiguate later sections instead of overwriting them.
            base_id = util.stable_id("doc", repo_name, relpath, heading, str(chunk_index))
            occurrence = used_ids.get(base_id, 0)
            used_ids[base_id] = occurrence + 1
            doc_id = base_id if occurrence == 0 else util.stable_id(
                "doc", repo_name, relpath, heading, str(chunk_index), f"occurrence-{occurrence}"
            )
            docs.append({
                "id": doc_id,
                "source_type": "doc",
                "source_group": source_group,
                "source_key": relpath,
                "source_uri": f"repo://{repo_name}/{relpath}#L{chunk_start}-{chunk_end}",
                "project": project,
                "scope": scope,
                "title": f"{relpath} — {heading}" if heading else relpath,
                "summary": util.truncate(chunk["text"].strip(), 320),
                "body": body,
                "notes": "\n".join(atoms),
                "event_at": event_at,
                "content_hash": util.content_hash(body),
                "authority": 0.8,
                "sensitivity": "internal",
                "metadata": {
                    "repo": repo_name,
                    "path": relpath,
                    "heading": heading,
                    "line_range": [chunk_start, chunk_end],
                    "redactions": redactions,
                },
            })
    return docs


def collect_repo(
    conn: sqlite3.Connection,
    repo_root: str,
    repo_name: str,
    scope: str,
    project: str,
    patterns: list[str],
) -> dict[str, Any]:
    root = Path(repo_root)
    stats = {"repo": repo_root, "files": 0, "documents": 0}
    if not root.exists():
        stats["error"] = f"repo missing: {root}"
        return stats
    seen: set[str] = set()
    source_group = f"docs:{repo_root}"
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            relpath = str(path.relative_to(root))
            docs = document_for_file(path, repo_root, relpath, repo_name, scope, project)
            if not docs:
                continue
            from .db_writer import upsert
            upsert(conn, docs)
            seen.update(d["id"] for d in docs)
            stats["files"] += 1
            stats["documents"] += len(docs)
    from .db_writer import mark_missing
    mark_missing(conn, source_group, seen)
    return stats


PRINTLYRICS_PATTERNS = [
    "README.md",
    "PRODUCT.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "docs/**/*.md",
    "db/schema.rb",
    "Gemfile",
    ".ruby-version",
    "config/deploy.yml",
    "config/routes.rb",
]


def collect(conn: sqlite3.Connection, printlyrics_repo: str, ideation_repo: str | None, analytics_repo: str | None) -> dict[str, Any]:
    results = [
        collect_repo(conn, printlyrics_repo, "printlyrics", "printlyrics", "printlyrics", PRINTLYRICS_PATTERNS),
    ]
    if ideation_repo:
        results.append(collect_repo(
            conn, ideation_repo, "printlyrics-ideation", "printlyrics/ideation", "printlyrics-ideation",
            ["gap-shortlist.md", "docs/**/*.md", "README.md"],
        ))
    if analytics_repo:
        results.append(collect_repo(
            conn, analytics_repo, "printlyrics-analytics", "printlyrics/analytics", "printlyrics-analytics",
            ["PLAN.md", "README.md", "docs/**/*.md"],
        ))
    return {"repos": results}
