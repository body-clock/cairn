from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from collector import collect as collect_mod
from collector import docs as docs_collector
from collector import project_config
from server import db


class ProjectConfigTest(unittest.TestCase):
    def write(self, directory: str, payload: dict) -> str:
        path = os.path.join(directory, "project.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, {"name": "fair-station"})
            config = project_config.load(path)
            self.assertEqual(config.scope, "fair-station")
            self.assertEqual(config.repo, "/sources/repo")
            self.assertEqual(config.docs, project_config.DEFAULT_DOCS)
            self.assertEqual(config.sessions, {})
            self.assertTrue(config.git)

    def test_rejects_invalid_name_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, {"name": "Fair Station"})
            with self.assertRaises(ValueError):
                project_config.load(path)
            path = self.write(tmp, {"name": "fair-station", "scope": "two words"})
            with self.assertRaises(ValueError):
                project_config.load(path)
            path = self.write(tmp, {"name": "fair-station", "sessions": {"../etc": "x"}})
            with self.assertRaises(ValueError):
                project_config.load(path)

    def test_frontmatter_is_metadata_not_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "claim.md")
            path.write_text(
                "---\n"
                'claim: "The store is isolated"\n'
                "status: verified\n"
                "owner: demo\n"
                "sources:\n"
                '  - "repo://demo/a.md#L1-2"\n'
                "---\n"
                "# Claim\n\nEach repository gets its own store.\n",
                encoding="utf-8",
            )
            documents = docs_collector.document_for_file(
                path, tmp, "claim.md", "demo", "demo", "demo", source_type="wiki"
            )
            metadata = documents[0]["metadata"]
            self.assertEqual(metadata["frontmatter"]["claim"], "The store is isolated")
            self.assertEqual(metadata["status"], "verified")
            self.assertEqual(metadata["sources"], ["repo://demo/a.md#L1-2"])
            self.assertNotIn("claim:", documents[0]["body"])
            self.assertGreater(documents[0]["metadata"]["line_range"][0], 6)
            self.assertEqual(documents[0]["source_type"], "wiki")
            self.assertEqual(documents[0]["authority"], 0.9)

    def test_repeated_heading_keeps_both_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "notes.md")
            path.write_text(
                "# Notes\n\n## Setup\n\nfirst section\n\n## Setup\n\nsecond section\n",
                encoding="utf-8",
            )
            documents = docs_collector.document_for_file(
                path, tmp, "notes.md", "demo", "demo", "demo"
            )
            self.assertEqual(len({document["id"] for document in documents}), len(documents))
            bodies = " ".join(document["body"] for document in documents)
            self.assertIn("first section", bodies)
            self.assertIn("second section", bodies)

    def test_configured_collection_scopes_git_docs_and_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()

            def git(*args):
                subprocess.run(("git",) + args, cwd=repo, check=True, capture_output=True, text=True)

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test")
            (repo / "README.md").write_text("# Demo\n\nChosen SQLite for the bootstrap.\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-q", "-m", "feat: choose sqlite", "-m", "SQLite keeps the bootstrap self-contained.")

            sessions_root = Path(tmp, "sessions")
            bucket = sessions_root / "-code-demo"
            bucket.mkdir(parents=True)
            (bucket / "2026-10-19T00-00-00-000Z_demo.jsonl").write_text(
                "\n".join([
                    json.dumps({"type": "title", "title": "Demo session"}),
                    json.dumps({
                        "type": "session",
                        "version": 3,
                        "id": "demo",
                        "timestamp": "2026-10-19T00:00:00Z",
                        "cwd": str(repo),
                    }),
                    json.dumps({
                        "type": "message",
                        "id": "m1",
                        "parentId": None,
                        "timestamp": "2026-10-19T00:00:01Z",
                        "message": {"role": "user", "content": "why did we choose sqlite"},
                    }),
                ]) + "\n",
                encoding="utf-8",
            )

            wiki = Path(repo, "wiki")
            wiki.mkdir()
            (wiki / "claim.md").write_text(
                "---\nclaim: \"Chosen SQLite\"\nstatus: verified\nsources:\n"
                '  - "repo://demo/README.md#L1-4"\n---\n# Claim\n\nSQLite.\n',
                encoding="utf-8",
            )

            ops_repo = Path(tmp, "opscode")
            (ops_repo / "doc").mkdir(parents=True)
            (ops_repo / "doc" / "opensearch.md").write_text(
                "# OpenSearch\n\nProduction logs live in OpenSearch.\n", encoding="utf-8"
            )

            config_path = self.write(tmp, {
                "name": "demo",
                "scope": "demo",
                "repo": str(repo),
                "docs": ["README.md"],
                "wiki": ["wiki/**/*.md"],
                "doc_repos": [{
                    "name": "opsdoc",
                    "repo": str(ops_repo),
                    "scope": "demo/ops",
                    "patterns": ["doc/**/*.md"],
                }],
                "sessions": {"-code-demo": "demo"},
            })
            db_path = os.path.join(tmp, "knowledge.db")
            previous = {key: os.environ.get(key) for key in ("PROJECT_CONFIG", "SESSIONS_ROOT")}
            os.environ["PROJECT_CONFIG"] = config_path
            os.environ["SESSIONS_ROOT"] = str(sessions_root)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    summary = collect_mod.run_once(db_path)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.assertEqual(summary["git"]["documents"], 1)
            self.assertGreaterEqual(summary["docs"]["documents"], 1)
            self.assertGreaterEqual(summary["wiki"]["documents"], 1)
            self.assertEqual(summary["docs:opsdoc"]["documents"], 1)
            self.assertGreaterEqual(summary["sessions"]["documents"], 1)

            conn = db.connect(db_path)
            rows = conn.execute(
                "SELECT source_type, source_uri, scope FROM documents ORDER BY source_type"
            ).fetchall()
            self.assertEqual({row["scope"] for row in rows}, {"demo", "demo/ops"})
            self.assertTrue(any("opensearch.md" in row["source_uri"] for row in rows))
            self.assertTrue(any(row["source_uri"].startswith("git://demo/") for row in rows))
            self.assertTrue(any(row["source_uri"].startswith("omp-session://") for row in rows))
            conn.close()


if __name__ == "__main__":
    unittest.main()
