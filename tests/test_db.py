from __future__ import annotations

import os
import tempfile
import unittest

from server import db, mcp


class KnowledgeDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "knowledge.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)
        self.docs = [
            {
                "id": "doc-pricing",
                "source_type": "doc",
                "source_group": "docs:test",
                "source_key": "PRODUCT.md",
                "source_uri": "repo://printlyrics/PRODUCT.md#L10-20",
                "scope": "printlyrics",
                "title": "PRODUCT.md — Account and Songbook Experiment",
                "summary": "Free accounts keep five songbooks of ten songs.",
                "body": "The proposed $3 monthly, $25 yearly, and $1 24-hour premium access removes both ceilings.",
                "notes": "Decision: keep search and printing free.",
                "event_at": "2026-10-16T15:09:00Z",
                "content_hash": "a",
                "authority": 0.8,
                "metadata": {"path": "PRODUCT.md"},
            },
            {
                "id": "doc-analytics",
                "source_type": "umami",
                "source_group": "umami",
                "source_key": "overview",
                "source_uri": "umami://site/snapshot#overview",
                "scope": "printlyrics/analytics",
                "title": "Umami overview — search funnel",
                "summary": "Song Search Submitted to Song Result Selected dropped 65%.",
                "body": "Search to printer: 895 visitors, 314 selected a result, 232 generated a page, 163 opened print.",
                "notes": "",
                "event_at": "2026-10-19T17:35:00Z",
                "content_hash": "b",
                "authority": 0.9,
                "metadata": {},
            },
            {
                "id": "session-decision",
                "source_type": "session",
                "source_group": "sessions:test",
                "source_key": "session-1",
                "source_uri": "omp-session://session-1#L4-9",
                "scope": "printlyrics",
                "title": "Session: pricing experiment",
                "summary": "We decided to keep the free tier at five songbooks.",
                "body": "We decided to keep the free tier at five songbooks. The constraint is that printing stays free without sign-in.",
                "notes": "Decision: keep printing free.",
                "event_at": "2026-10-15T12:00:00Z",
                "content_hash": "c",
                "authority": 0.45,
                "metadata": {"session_id": "session-1"},
            },
        ]
        db.upsert_documents(self.conn, self.docs)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_search_ranks_and_filters(self):
        results = db.search(self.conn, "free songbooks premium")
        self.assertTrue(results)
        self.assertGreaterEqual(results[0]["score"], results[-1]["score"])
        ids = {r["id"] for r in results}
        self.assertIn("doc-pricing", ids)

        analytics = db.search(self.conn, "search funnel visitors", scope="printlyrics/analytics")
        self.assertEqual([r["id"] for r in analytics], ["doc-analytics"])

        sessions = db.search(self.conn, "printing sign-in", source_type="session")
        self.assertEqual([r["id"] for r in sessions], ["session-decision"])

    def test_read_and_status(self):
        document = db.get_document(self.conn, "doc-pricing")
        self.assertIn("$3 monthly", document["body"])
        self.assertEqual(document["metadata"]["path"], "PRODUCT.md")

        status = db.status(self.conn, self.path)
        self.assertEqual(status["documents"], 3)
        self.assertEqual({row["source_type"] for row in status["by_source"]}, {"doc", "umami", "session"})

    def test_unknown_read(self):
        self.assertIsNone(db.get_document(self.conn, "missing"))

    def test_update_reindexes_and_delete_excludes(self):
        updated = dict(self.docs[0])
        updated["body"] = "The annual plan is now $25 and the monthly plan is $3."
        updated["summary"] = "Annual plan pricing."
        updated["content_hash"] = "updated"
        db.upsert_documents(self.conn, [updated])

        old = db.search(self.conn, "five songbooks")
        self.assertNotIn("doc-pricing", {row["id"] for row in old})
        new = db.search(self.conn, "annual plan")
        self.assertEqual(new[0]["id"], "doc-pricing")

        db.mark_missing(self.conn, "docs:test", set())
        gone = db.search(self.conn, "annual plan")
        self.assertNotIn("doc-pricing", {row["id"] for row in gone})

    def test_match_builder_handles_punctuation(self):
        match = db.build_match('why did we choose "free limits" for PrintLyrics?')
        self.assertIn('"free limits"', match)
        self.assertIn('"printlyrics"*', match)
        self.assertNotIn("?", match)

    def test_mcp_initialize_and_tools(self):
        init = mcp.handle_payload(self.conn, self.path, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}},
        })
        self.assertEqual(init["result"]["serverInfo"]["name"], "cairn")

        listed = mcp.handle_payload(self.conn, self.path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {"knowledge_search", "knowledge_read", "knowledge_status", "knowledge_sources"})

        called = mcp.handle_payload(self.conn, self.path, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "knowledge_search", "arguments": {"query": "free songbooks"}},
        })
        payload = called["result"]["structuredContent"]
        ids = {item["id"] for item in payload["results"]}
        self.assertIn("doc-pricing", ids)
        self.assertGreaterEqual(payload["count"], 1)

        read = mcp.handle_payload(self.conn, self.path, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "knowledge_read", "arguments": {"record_id": "doc-pricing"}},
        })
        self.assertIn("$3 monthly", read["result"]["structuredContent"]["body"])

    def test_mcp_notification_has_no_response(self):
        self.assertIsNone(mcp.handle_payload(self.conn, self.path, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))


class LintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "knowledge.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def wiki(self, doc_id, claim, status, sources, valid_to="", scope="demo"):
        return {
            "id": doc_id,
            "source_type": "wiki",
            "source_group": "wiki:test",
            "source_key": f"wiki/{doc_id}.md",
            "source_uri": f"repo://demo/wiki/{doc_id}.md#L1-8",
            "scope": scope,
            "title": claim,
            "summary": claim,
            "body": claim,
            "event_at": "2026-10-20T00:00:00Z",
            "content_hash": doc_id,
            "authority": 0.9,
            "metadata": {"claim": claim, "status": status, "sources": sources, "valid_to": valid_to},
        }

    def record(self, doc_id, uri, scope="demo"):
        return {
            "id": doc_id,
            "source_type": "doc",
            "source_group": "doc:test",
            "source_key": doc_id,
            "source_uri": uri,
            "scope": scope,
            "title": doc_id,
            "summary": doc_id,
            "body": doc_id,
            "event_at": "2026-10-19T00:00:00Z",
            "content_hash": doc_id,
            "authority": 0.8,
            "metadata": {},
        }

    def test_flags_missing_sources_contested_and_uncited(self):
        db.upsert_documents(self.conn, [
            self.wiki("w1", "Store is isolated", "verified", ["repo://demo/a.md#L1-2"]),
            self.wiki("w2", "Store is shared", "contested", []),
            self.record("a", "repo://demo/a.md#L1-2"),
            self.record("b", "repo://demo/b.md#L1-2"),
        ])
        result = db.lint(self.conn)
        counts = {finding["kind"]: finding["count"] for finding in result["findings"]}
        self.assertEqual(result["wiki_pages"], 2)
        self.assertEqual(result["claims"], 2)
        self.assertEqual(counts["wiki_without_sources"], 1)
        self.assertEqual(counts["contested"], 1)
        self.assertEqual(counts["stale"], 0)
        self.assertEqual(counts["uncited_sources"], 1)
        self.assertFalse(result["clean"])

    def test_clean_when_every_claim_cites_a_source(self):
        db.upsert_documents(self.conn, [
            self.wiki("w1", "Store is isolated", "verified", ["repo://demo/a.md#L1-2"]),
            self.record("a", "repo://demo/a.md#L1-2"),
        ])
        result = db.lint(self.conn)
        self.assertTrue(result["clean"])
        self.assertEqual(result["claims"], 1)


if __name__ == "__main__":
    unittest.main()
