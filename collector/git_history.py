"""Import commit messages and changed-file lists as decision evidence."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import util


def _log(repo: str, limit: int) -> str:
    result = subprocess.run(
        [
            "git", "-C", repo, "log", "--all", "--reverse", "--date=iso-strict",
            # NUL terminates the body so the message text can contain newlines
            # without being mistaken for the name-status block that follows it.
            "--pretty=format:%x1e%H%x1f%ad%x1f%an%x1f%s%x1f%b%x00",
            "--name-status", "-n", str(limit),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git log failed")
    return result.stdout


def collect(
    conn: sqlite3.Connection,
    repo_root: str,
    repo_name: str = "printlyrics",
    limit: int = 800,
    scope: str = "printlyrics",
) -> dict[str, Any]:
    root = Path(repo_root)
    stats = {"repo": repo_root, "commits": 0, "documents": 0}
    if not (root / ".git").exists():
        stats["error"] = f"not a git repository: {root}"
        return stats
    output = _log(str(root), limit)
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in output.split("\x1e"):
        record = record.strip("\n\r ")
        if not record:
            continue
        header, separator, files_blob = record.partition("\x00")
        if not separator:
            continue
        fields = header.split("\x1f")
        if len(fields) < 5:
            continue
        commit, author_date, author, subject, body = fields[:5]
        files = []
        for line in files_blob.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[1].strip()
            if len(parts) >= 3:
                path = f"{path} -> {parts[2].strip()}"
            files.append(f"{status} {path}")
        message = (subject + "\n\n" + body).strip()
        doc_body = (
            f"# Commit {commit[:12]}\n\n"
            f"- Author: {author}\n"
            f"- Date: {author_date}\n"
            f"- Subject: {subject}\n\n"
            f"## Message\n\n{body.strip() or '(no message body)'}\n\n"
            f"## Files\n\n" + "\n".join(f"- `{line}`" for line in files)
        )
        atom_source = message + "\n" + "\n".join(files)
        atoms = util.extract_atoms(atom_source)
        doc_id = util.stable_id("git", repo_name, commit)
        seen.add(doc_id)
        docs.append({
            "id": doc_id,
            "source_type": "git",
            "source_group": f"git:{repo_root}",
            "source_key": commit,
            "source_uri": f"git://{repo_name}/{commit}",
            "project": repo_name,
            "scope": scope,
            "title": f"{commit[:8]} — {subject}",
            "summary": util.truncate(subject + (f" — {body.strip().splitlines()[0]}" if body.strip() else ""), 320),
            "body": doc_body,
            "notes": "\n".join(atoms),
            "event_at": util.iso(util.parse_iso(author_date)),
            "content_hash": util.content_hash(doc_body),
            "authority": 0.7,
            "sensitivity": "internal",
            "metadata": {
                "commit": commit,
                "author": author,
                "files": files,
                "file_count": len(files),
            },
        })
    from .db_writer import upsert, mark_missing
    upsert(conn, docs)
    mark_missing(conn, f"git:{repo_root}", seen)
    stats["commits"] = len(docs)
    stats["documents"] = len(docs)
    return stats
