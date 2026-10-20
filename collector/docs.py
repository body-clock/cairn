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


def _scalar(value: str) -> Any:
    value = value.strip().strip("'\"")
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Parse a leading YAML-ish frontmatter block without a YAML dependency.

    Supports `key: value`, inline lists (`key: [a, b]`), and block lists
    (`key:` followed by `- item`). Returns (fields, body, line_offset).
    """
    if not text.startswith("---"):
        return {}, text, 0
    lines = text.splitlines(keepends=True)
    if lines[0].strip() not in ("---", "+++"):
        return {}, text, 0
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text, 0
    fields: dict[str, Any] = {}
    key: str | None = None
    for raw in lines[1:end]:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key is not None:
            current = fields.get(key)
            if not isinstance(current, list):
                current = []
                fields[key] = current
            current.append(_scalar(line.lstrip()[2:]))
            continue
        if ":" in line:
            name, value = line.split(":", 1)
            key = name.strip()
            value = value.strip()
            if not value:
                fields[key] = []
            elif value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                fields[key] = [_scalar(part) for part in inner.split(",") if part.strip()] if inner else []
            else:
                fields[key] = _scalar(value)
    return fields, "".join(lines[end + 1:]), end + 1


def document_for_file(
    path: Path,
    repo_root: str,
    relpath: str,
    repo_name: str,
    scope: str,
    project: str,
    source_type: str = "doc",
) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if len(raw) > 2_000_000:
        raw = raw[:2_000_000]
    text, redactions = util.redact(raw)
    frontmatter, text, line_offset = _frontmatter(text)
    event_at = _git_date(repo_root, relpath) or util.file_date(str(path))
    source_group = f"docs:{repo_root}"
    docs: list[dict[str, Any]] = []
    used_ids: dict[str, int] = {}
    claim = str(frontmatter.get("claim") or "").strip()
    for heading, section, section_offset in _sections(text):
        for chunk_index, chunk in enumerate(util.chunk_text(section, max_chars=3000, overlap=300)):
            body = (f"# {relpath}" + (f" — {heading}" if heading else "") + "\n\n" + chunk["text"]).strip()
            chunk_start = text.count("\n", 0, section_offset + chunk["start"]) + 1 + line_offset
            chunk_end = text.count("\n", 0, section_offset + chunk["end"]) + 1 + line_offset
            atoms = util.extract_atoms(chunk["text"])
            # A file may repeat a heading; keep the first id stable and
            # disambiguate later sections instead of overwriting them.
            base_id = util.stable_id(source_type, repo_name, relpath, heading, str(chunk_index))
            occurrence = used_ids.get(base_id, 0)
            used_ids[base_id] = occurrence + 1
            doc_id = base_id if occurrence == 0 else util.stable_id(
                source_type, repo_name, relpath, heading, str(chunk_index), f"occurrence-{occurrence}"
            )
            summary = claim or chunk["text"].strip()
            docs.append({
                "id": doc_id,
                "source_type": source_type,
                "source_group": source_group,
                "source_key": relpath,
                "source_uri": f"repo://{repo_name}/{relpath}#L{chunk_start}-{chunk_end}",
                "project": project,
                "scope": scope,
                "title": f"{relpath} — {heading}" if heading else relpath,
                "summary": util.truncate(summary, 320),
                "body": body,
                "notes": "\n".join(atoms),
                "event_at": event_at,
                "content_hash": util.content_hash(body),
                "authority": 0.9 if source_type == "wiki" else 0.8,
                "sensitivity": "internal",
                "metadata": {
                    "repo": repo_name,
                    "path": relpath,
                    "heading": heading,
                    "line_range": [chunk_start, chunk_end],
                    "redactions": redactions,
                    "frontmatter": frontmatter,
                    "claim": claim or None,
                    "status": frontmatter.get("status"),
                    "sources": frontmatter.get("sources") or [],
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
    source_type: str = "doc",
) -> dict[str, Any]:
    root = Path(repo_root)
    stats = {"repo": repo_root, "files": 0, "documents": 0, "source_type": source_type}
    if not root.exists():
        stats["error"] = f"repo missing: {root}"
        return stats
    seen: set[str] = set()
    source_group = f"{source_type}:{repo_root}"
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            relpath = str(path.relative_to(root))
            docs = document_for_file(
                path, repo_root, relpath, repo_name, scope, project, source_type=source_type
            )
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
