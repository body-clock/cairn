"""Load the repo-local manifest that drives a per-project knowledge store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

DEFAULT_DOCS = (
    "README.md",
    "AGENTS.md",
    "docs/**/*.md",
    "db/schema.rb",
    "config/routes.rb",
    "config/deploy.yml",
)


DOC_PATTERNS = (
    "README.md",
    "docs/**/*.md",
    "doc/**/*.md",
)

DEFAULT_WIKI = ("wiki/**/*.md",)


@dataclass(frozen=True)
class DocRepo:
    """An additional documentation repository indexed beside the main repo."""

    name: str
    repo: str
    scope: str
    patterns: tuple[str, ...] = DOC_PATTERNS
    host: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    """One repository's indexing contract."""

    name: str
    scope: str
    repo: str
    docs: tuple[str, ...] = DEFAULT_DOCS
    wiki: tuple[str, ...] = DEFAULT_WIKI
    sessions: dict[str, str] | None = None
    doc_repos: tuple[DocRepo, ...] = ()
    app_db: str | None = None
    git: bool = True
    git_limit: int = 800

    @property
    def project(self) -> str:
        return self.name


def _string_list(value: Any, field: str, default: tuple[str, ...] = DEFAULT_DOCS) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def load(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read project config {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} must contain a JSON object")

    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("name must match [a-z0-9][a-z0-9-]* and be at most 63 characters")

    scope = str(data.get("scope") or name).strip()
    if not scope or any(char.isspace() for char in scope):
        raise ValueError("scope must be a non-empty string without whitespace")

    repo = str(data.get("repo") or "/sources/repo").strip()

    sessions: dict[str, str] = {}
    raw_sessions = data.get("sessions") or {}
    if not isinstance(raw_sessions, dict):
        raise ValueError("sessions must be an object mapping session bucket to scope")
    for bucket, bucket_scope in raw_sessions.items():
        bucket = str(bucket).strip()
        bucket_scope = str(bucket_scope).strip()
        if not bucket or not bucket_scope:
            raise ValueError("sessions entries need a bucket and a scope")
        if "/" in bucket or bucket in (".", ".."):
            raise ValueError(f"invalid session bucket: {bucket}")
        sessions[bucket] = bucket_scope

    doc_repos: list[DocRepo] = []
    raw_docs = data.get("doc_repos") or []
    if not isinstance(raw_docs, list):
        raise ValueError("doc_repos must be a list")
    for entry in raw_docs:
        if not isinstance(entry, dict):
            raise ValueError("doc_repos entries must be objects")
        doc_name = str(entry.get("name") or "").strip()
        if not NAME_RE.match(doc_name):
            raise ValueError(f"invalid doc repo name: {doc_name!r}")
        doc_repo = str(entry.get("repo") or "").strip()
        if not doc_repo:
            raise ValueError(f"doc repo {doc_name} needs a repo path")
        doc_scope = str(entry.get("scope") or f"{scope}/docs").strip()
        host = entry.get("host")
        if host is not None and not isinstance(host, str):
            raise ValueError(f"doc repo {doc_name} host must be a string")
        doc_repos.append(
            DocRepo(
                name=doc_name,
                repo=doc_repo,
                scope=doc_scope,
                patterns=_string_list(entry.get("patterns") or list(DOC_PATTERNS), "patterns"),
                host=host,
            )
        )

    app_db = data.get("app_db")
    if app_db is not None and not isinstance(app_db, str):
        raise ValueError("app_db must be a string path or null")

    try:
        git_limit = int(data.get("git_limit", 800))
    except (TypeError, ValueError) as exc:
        raise ValueError("git_limit must be an integer") from exc
    if not 1 <= git_limit <= 20000:
        raise ValueError("git_limit must be between 1 and 20000")

    return ProjectConfig(
        name=name,
        scope=scope,
        repo=repo,
        docs=_string_list(data.get("docs"), "docs"),
        wiki=_string_list(data.get("wiki"), "wiki", DEFAULT_WIKI),
        sessions=sessions,
        doc_repos=tuple(doc_repos),
        app_db=app_db,
        git=bool(data.get("git", True)),
        git_limit=git_limit,
    )
