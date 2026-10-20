"""Collect local PrintLyrics evidence into the shared SQLite knowledge base."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
from typing import Any, Callable

from server import db

from . import app_db, docs, exports, git_history, project_config, sessions, umami, util

STOP = False


def _on_signal(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _count_documents(stats: dict[str, Any]) -> int:
    if "documents" in stats:
        return int(stats.get("documents") or 0)
    repos = stats.get("repos") or []
    return sum(int(repo.get("documents") or 0) for repo in repos)


def _run_source(conn: sqlite3.Connection, source: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = util.iso_now()
    cursor = conn.execute(
        "INSERT INTO import_runs(source, started_at, status) VALUES (?, ?, 'running')",
        (source, started),
    )
    run_id = cursor.lastrowid
    conn.commit()
    try:
        stats = fn()
        status = "success"
    except Exception as exc:
        stats = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-4000:]}
        status = "failed"
    finished = util.iso_now()
    conn.execute(
        """
        UPDATE import_runs
           SET finished_at=?, status=?, documents_seen=?, documents_upserted=?, error=?, stats_json=?
         WHERE id=?
        """,
        (
            finished,
            status,
            _count_documents(stats),
            _count_documents(stats),
            stats.get("error"),
            json.dumps(stats, ensure_ascii=False, sort_keys=True),
            run_id,
        ),
    )
    conn.commit()
    print(f"collector: {source}: {status} {json.dumps(stats, ensure_ascii=False)}", flush=True)
    return stats


def _run_configured(conn: sqlite3.Connection, config: project_config.ProjectConfig) -> dict[str, Any]:
    """Collect one repository described by a repo-local manifest."""
    summary: dict[str, Any] = {}
    if config.git:
        summary["git"] = _run_source(
            conn,
            "git",
            lambda: git_history.collect(
                conn, config.repo, config.name, limit=config.git_limit, scope=config.scope
            ),
        )
    summary["docs"] = _run_source(
        conn,
        "docs",
        lambda: docs.collect_repo(
            conn, config.repo, config.name, config.scope, config.name, list(config.docs)
        ),
    )
    if config.wiki:
        summary["wiki"] = _run_source(
            conn,
            "wiki",
            lambda: docs.collect_repo(
                conn,
                config.repo,
                config.name,
                config.scope,
                config.name,
                list(config.wiki),
                source_type="wiki",
            ),
        )
    for extra in config.doc_repos:
        summary[f"docs:{extra.name}"] = _run_source(
            conn,
            f"docs:{extra.name}",
            lambda extra=extra: docs.collect_repo(
                conn, extra.repo, extra.name, extra.scope, extra.name, list(extra.patterns)
            ),
        )
    if config.sessions:
        known = {
            bucket: (scope, config.name) for bucket, scope in config.sessions.items()
        }
        sessions_root = _env("SESSIONS_ROOT", "/sources/sessions")
        summary["sessions"] = _run_source(
            conn,
            "sessions",
            lambda: sessions.collect(conn, sessions_root, [], known_projects=known),
        )
    if config.app_db:
        summary["app_db"] = _run_source(
            conn,
            "app_db",
            lambda: app_db.collect(
                conn, config.app_db, project=config.name, scope=config.scope
            ),
        )
    return summary


def run_once(db_path: str) -> dict[str, Any]:
    conn = db.connect(db_path)
    db.init_db(conn)
    summary: dict[str, Any] = {}
    config_path = _env("PROJECT_CONFIG")
    if config_path:
        config = project_config.load(config_path)
        summary = _run_configured(conn, config)
        summary["corpus"] = db.status(conn, db_path)
        conn.close()
        return summary

    printlyrics_repo = _env("PRINTLYRICS_REPO", "/sources/printlyrics")
    ideation_repo = _env("IDEATION_REPO", "/sources/ideation/lyric-sync")
    analytics_repo = _env("ANALYTICS_REPO", "/sources/analytics-connector")
    sessions_root = _env("SESSIONS_ROOT", "/sources/sessions")
    exports_dir = _env("EXPORTS_DIR", "/sources/exports")
    umami_dir = _env("UMAMI_SNAPSHOT_DIR", "/sources/umami")
    umami_live_dir = _env("UMAMI_LIVE_DIR", "/data/umami-live")
    app_db_path = _env("APP_DB", os.path.join(printlyrics_repo, "storage", "development.sqlite3"))
    projects = [p.strip() for p in (_env("SOURCE_PROJECTS", "") or "").split(",") if p.strip()]

    # Refresh from the Umami API when a key is supplied. Snapshot ingestion below
    # still runs, so a failed refresh cannot empty the corpus.
    api_key = _env("UMAMI_API_KEY")
    if api_key:
        try:
            start_at = _env("UMAMI_START") or "2026-09-30T04:05:18.000Z"
            end_at = _env("UMAMI_END") or util.iso_now()
            path = umami.refresh(
                api_key,
                _env("UMAMI_URL", "https://analytics.printlyrics.app"),
                _env("UMAMI_WEBSITE_ID", "ea5c0e32-fcc8-4081-814e-085e7dfefa20"),
                start_at,
                end_at,
                umami_live_dir,
            )
            summary["umami_refresh"] = {"path": path}
        except Exception as exc:
            summary["umami_refresh"] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"collector: umami refresh failed: {exc}", file=sys.stderr, flush=True)

    summary["sessions"] = _run_source(
        conn, "sessions", lambda: sessions.collect(conn, sessions_root, projects)
    )
    summary["git"] = _run_source(
        conn, "git", lambda: git_history.collect(conn, printlyrics_repo)
    )
    summary["docs"] = _run_source(
        conn, "docs", lambda: docs.collect(conn, printlyrics_repo, ideation_repo, analytics_repo)
    )
    summary["exports"] = _run_source(
        conn, "exports", lambda: exports.collect(conn, exports_dir)
    )
    summary["umami"] = _run_source(
        conn, "umami", lambda: umami.collect_snapshots(conn, [umami_dir, umami_live_dir])
    )
    summary["app_db"] = _run_source(
        conn, "app_db", lambda: app_db.collect(conn, app_db_path)
    )

    summary["corpus"] = db.status(conn, db_path)
    conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="collect once and exit")
    parser.add_argument("--loop", action="store_true", help="collect on an interval")
    parser.add_argument("--interval", type=int, default=None, help="seconds between runs in loop mode")
    parser.add_argument("--db", default=_env("KNOWLEDGE_DB", os.path.join(os.getcwd(), "data", "knowledge.db")))
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    interval = args.interval or int(_env("COLLECT_INTERVAL_SECONDS", "900"))
    if args.loop and not args.once:
        print(f"collector: loop mode, interval={interval}s", flush=True)
        while not STOP:
            try:
                run_once(args.db)
            except Exception:
                traceback.print_exc()
            for _ in range(interval):
                if STOP:
                    break
                time.sleep(1)
        print("collector: stopped", flush=True)
        return 0

    summary = run_once(args.db)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if all(
        not (isinstance(value, dict) and value.get("error"))
        for key, value in summary.items()
        if key not in ("corpus", "umami_refresh")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
