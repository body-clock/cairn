from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile

from collector import exports, git_history, sessions, umami, util
from server import db


class UtilityTest(unittest.TestCase):
    def test_redaction(self):
        text, count = util.redact(
            "Authorization: Bearer abc.def.ghi\npassword=hunter2\ncontact me@example.com\nsk-abcdefghijklmnopqrst"
        )
        self.assertGreaterEqual(count, 3)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("me@example.com", text)
        self.assertNotIn("abcdefghijklmnopqrst", text)

    def test_atoms(self):
        atoms = util.extract_atoms(
            "We decided to keep printing free.\n"
            "bin/rails test test/models/lyric_test.rb\n"
            "ArgumentError: Before process_action callback :require_authentication has not been defined\n"
        )
        joined = "\n".join(atoms)
        self.assertIn("decided", joined)
        self.assertIn("bin/rails test", joined)
        self.assertIn("ArgumentError", joined)

    def test_chunks_preserve_offsets(self):
        text = "\n\n".join(["paragraph " + str(i) + " " + ("x" * 200) for i in range(10)])
        chunks = util.chunk_text(text, max_chars=500, overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["start"], 0)
        self.assertLessEqual(len(chunks[-1]["text"]), 600)


class SessionTest(unittest.TestCase):
    def test_parse_and_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.jsonl")
            rows = [
                {"type": "session", "id": "s1", "timestamp": "2026-10-01T10:00:00Z", "cwd": "/sources/printlyrics", "title": "Pricing decision"},
                {"type": "message", "id": "m1", "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "Why do we keep printing free?"}]}},
                {"type": "message", "id": "m2", "timestamp": "2026-10-01T10:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "We decided to keep printing free because it is the acquisition path. token=abcdef123456"}, {"type": "thinking", "thinking": "private reasoning"}]}},
            ]
            with open(path, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            from pathlib import Path
            docs = sessions.session_documents(Path(path), "printlyrics", "printlyrics", "sessions:test")
            self.assertGreaterEqual(len(docs), 2)
            self.assertTrue(any("Session digest" in d["title"] for d in docs))
            self.assertFalse(any("private reasoning" in d["body"] for d in docs))
            self.assertFalse(any("abcdef123456" in d["body"] for d in docs))


class ExportTest(unittest.TestCase):
    def test_plausible_and_search_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "Plausible export printlyrics.app all .zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("visitors.csv", "date,visitors,pageviews,visits\n2026-09-01,10,20,10\n2026-09-02,5,8,5\n")
                archive.writestr("pages.csv", "name,visitors,pageviews\n/,12,28\n")
                archive.writestr("conversions.csv", "name,unique_conversions,total_conversions\nPrint Page Generated,8,20\n")
            gsc = os.path.join(tmp, "printlyrics.app_DeviceReport_9_21_2026.csv")
            with open(gsc, "w", encoding="utf-8") as handle:
                handle.write("\ufeffDevice,Impressions,Clicks,CTR\nMobile,100,2,2%\n")

            db_path = os.path.join(tmp, "knowledge.db")
            conn = db.connect(db_path)
            db.init_db(conn)
            stats = exports.collect(conn, tmp)
            self.assertEqual(stats["zips"], 1)
            self.assertEqual(stats["csv"], 1)
            self.assertGreaterEqual(stats["documents"], 5)

            results = db.search(conn, "Print Page Generated conversions")
            self.assertTrue(results)
            self.assertTrue(any("conversions.csv" in r["source_uri"] for r in results))
            conn.close()


class UmamiTest(unittest.TestCase):
    def test_snapshot_documents(self):
        snapshot = {
            "exported_at": "2026-10-19T17:35:00Z",
            "website": {"id": "w1", "name": "PrintLyrics", "domain": "printlyrics.app"},
            "range": {"startAt": "2026-09-30T00:00:00Z", "endAt": "2026-10-19T00:00:00Z"},
            "stats": {"current": {"pageviews": 10, "visitors": 5}},
            "metrics": {"path": [{"name": "/", "value": 10}], "event": [{"name": "Print Page Generated", "value": 3}]},
            "eventStats": {"current": {"events": 3}},
            "eventSeries": [{"name": "Print Page Generated", "total": 3, "series": [{"date": "2026-10-01T00:00:00Z", "value": 3}]}],
            "eventProperties": [{"eventName": "Print Page Generated", "propertyName": "entry_method", "dataType": "string", "count": 3}],
            "eventPropertyValues": {"entry_method": [{"value": "print_page", "count": 3}]},
            "funnels": [{"id": "f1", "name": "Sheet to printer", "windowMinutes": 60, "steps": [], "result": [{"step": 1, "value": "Print Page Generated", "visitors": 3}]}],
            "goals": [{"id": "g1", "name": "Print Page Generated"}],
            "segments": [],
            "annotations": [],
        }
        docs = umami.snapshot_documents(snapshot, "snap")
        self.assertTrue(any(d["title"].startswith("Umami overview") for d in docs))
        self.assertTrue(any(d["title"] == "Umami path metrics" for d in docs))
        self.assertTrue(any(d["title"] == "Umami event series: Print Page Generated" for d in docs))
        self.assertTrue(any(d["title"] == "Umami event properties" for d in docs))

    def test_refresh_writes_live_snapshot_and_ingests_two_dirs(self):
        snapshot = {
            "exported_at": "2026-10-19T17:35:00Z",
            "website": {"id": "w1", "name": "PrintLyrics", "domain": "printlyrics.app"},
            "range": {"startAt": "2026-09-30T00:00:00Z", "endAt": "2026-10-19T00:00:00Z"},
            "stats": {"current": {"pageviews": 10}},
            "metrics": {"path": [{"name": "/", "value": 10}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            original = umami.UmamiApi.snapshot
            umami.UmamiApi.snapshot = lambda self, start, end: snapshot
            try:
                path = umami.refresh("key", "https://example.test", "w1", "2026-09-30T00:00:00Z", "2026-10-19T00:00:00Z", tmp)
            finally:
                umami.UmamiApi.snapshot = original
            self.assertEqual(os.path.basename(path), "live.json")
            self.assertTrue(os.path.exists(path))

            db_path = os.path.join(tmp, "knowledge.db")
            conn = db.connect(db_path)
            db.init_db(conn)
            stats = umami.collect_snapshots(conn, [tmp, os.path.join(tmp, "missing")])
            self.assertEqual(stats["files"], 1)
            self.assertGreaterEqual(stats["documents"], 2)
            conn.close()


class GitHistoryTest(unittest.TestCase):
    def test_message_body_is_not_read_as_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(
                    ("git",) + args, cwd=tmp, check=True, capture_output=True, text=True
                )

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test")
            with open(os.path.join(tmp, "one.txt"), "w", encoding="utf-8") as handle:
                handle.write("one\n")
            git("add", "one.txt")
            git("commit", "-q", "-m", "feat: first line", "-m", "second paragraph\n\nthird paragraph")

            db_path = os.path.join(tmp, "knowledge.db")
            conn = db.connect(db_path)
            db.init_db(conn)
            stats = git_history.collect(conn, tmp)
            self.assertEqual(stats["documents"], 1)

            row = conn.execute("SELECT * FROM documents WHERE source_type='git'").fetchone()
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(metadata["files"], ["A one.txt"])
            self.assertEqual(metadata["file_count"], 1)
            self.assertIn("third paragraph", row["body"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
