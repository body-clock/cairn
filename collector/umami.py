"""Ingest Umami snapshots and optionally refresh them through the Umami API."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import util

METRIC_TYPES = [
    "path", "entry", "exit", "title", "query", "referrer", "channel",
    "country", "region", "city", "browser", "os", "device", "language",
    "screen", "hostname", "event", "tag", "utmSource", "utmMedium",
    "utmCampaign", "utmContent", "utmTerm",
]


def _range(snapshot: dict[str, Any]) -> tuple[str | None, str | None]:
    rng = snapshot.get("range") or {}
    return rng.get("startAt"), rng.get("endAt")


def _doc(
    snapshot: dict[str, Any],
    file_stem: str,
    section: str,
    title: str,
    body: str,
    *,
    summary: str | None = None,
    sensitivity: str = "internal",
) -> dict[str, Any]:
    website = snapshot.get("website") or {}
    website_id = website.get("id") or "unknown"
    start_at, end_at = _range(snapshot)
    return {
        "id": util.stable_id("umami", file_stem, section),
        "source_type": "umami",
        "source_group": "umami",
        "source_key": f"{website_id}/{section}",
        "source_uri": f"umami://{website_id}/{file_stem}#{section}",
        "project": "printlyrics",
        "scope": "printlyrics/analytics",
        "title": title,
        "summary": summary or util.truncate(body, 320),
        "body": body,
        "notes": "",
        "event_at": end_at or snapshot.get("exported_at"),
        "content_hash": util.content_hash(body),
        "authority": 0.9,
        "sensitivity": sensitivity,
        "metadata": {
            "website": website,
            "range": {"startAt": start_at, "endAt": end_at},
            "exported_at": snapshot.get("exported_at"),
            "section": section,
        },
    }


def _overview(snapshot: dict[str, Any], file_stem: str) -> dict[str, Any]:
    website = snapshot.get("website") or {}
    start_at, end_at = _range(snapshot)
    stats = snapshot.get("stats") or {}
    current = stats.get("current") or stats
    previous = stats.get("previous") or {}
    lines = [
        f"# Umami overview — {website.get('name', 'PrintLyrics')}",
        "",
        f"- Domain: {website.get('domain', 'printlyrics.app')}",
        f"- Website ID: {website.get('id', '')}",
        f"- Range: {start_at} to {end_at}",
        f"- Exported: {snapshot.get('exported_at')}",
        "",
        "## Headline totals",
        "",
        util.markdown_table(
            ["Metric", "Current", "Previous"],
            [[key, current.get(key, ""), previous.get(key, "")] for key in (
                "pageviews", "visitors", "visits", "bounces", "bounceRate",
                "averageVisitDurationSeconds", "viewsPerVisit",
            )],
        ),
        "",
    ]
    event_stats = snapshot.get("eventStats") or {}
    if event_stats:
        ev = event_stats.get("current") or event_stats
        lines += ["## Event totals", "", util.markdown_table(
            ["Metric", "Value"], [[k, v] for k, v in ev.items()]
        ), ""]
    session_stats = snapshot.get("sessionStats") or {}
    if session_stats:
        lines += ["## Session totals", "", util.markdown_table(
            ["Metric", "Value"], [[k, v] for k, v in session_stats.items() if k not in ("websiteId", "range")]
        ), ""]
    funnels = snapshot.get("funnels") or []
    if funnels:
        lines += ["## Funnels", ""]
        for funnel in funnels:
            lines.append(f"### {funnel.get('name')} ({funnel.get('windowMinutes', '?')} min window)")
            steps = funnel.get("result") or []
            if steps:
                lines.append(util.markdown_table(
                    ["Step", "Event", "Visitors", "Dropoff rate", "Remaining rate"],
                    [[s.get("step"), s.get("value"), s.get("visitors"), s.get("dropoffRate"), s.get("remainingRate")] for s in steps],
                ))
            else:
                lines.append("No result captured.")
            lines.append("")
    for key, label in (
        ("path", "Top pages"),
        ("entry", "Entry pages"),
        ("referrer", "Referrers"),
        ("channel", "Channels"),
        ("event", "Events"),
        ("device", "Devices"),
        ("country", "Countries"),
    ):
        values = (snapshot.get("metrics") or {}).get(key) or []
        if values:
            lines += [f"## {label}", "", util.markdown_table(["Name", "Value"], [[v.get("name"), v.get("value")] for v in values[:20]]), ""]
    body = "\n".join(lines)
    return _doc(snapshot, file_stem, "overview", f"Umami overview — {start_at} to {end_at}", body)


def _metrics_docs(snapshot: dict[str, Any], file_stem: str) -> list[dict[str, Any]]:
    docs = []
    for metric_type, values in (snapshot.get("metrics") or {}).items():
        if not values:
            continue
        body = (
            f"# Umami {metric_type} metrics\n\n"
            f"Range: {(snapshot.get('range') or {}).get('startAt')} to {(snapshot.get('range') or {}).get('endAt')}\n\n"
            + util.markdown_table(["Name", "Value"], [[v.get("name"), v.get("value")] for v in values], limit=500)
        )
        docs.append(_doc(snapshot, file_stem, f"metric-{metric_type}", f"Umami {metric_type} metrics", body))
    return docs


def _event_docs(snapshot: dict[str, Any], file_stem: str) -> list[dict[str, Any]]:
    docs = []
    for event in snapshot.get("eventSeries") or []:
        name = event.get("name") or "event"
        series = event.get("series") or []
        body = (
            f"# Umami event series: {name}\n\nTotal: {event.get('total')}\n\n"
            + util.markdown_table(["Date", "Value"], [[p.get("date"), p.get("value")] for p in series], limit=500)
        )
        docs.append(_doc(snapshot, file_stem, f"series-{util.slug(name)}", f"Umami event series: {name}", body))
    return docs


def _event_properties_doc(snapshot: dict[str, Any], file_stem: str) -> dict[str, Any] | None:
    names = snapshot.get("eventProperties") or []
    values = snapshot.get("eventPropertyValues") or {}
    if not names and not values:
        return None
    lines = ["# Umami event properties", ""]
    if names:
        lines += ["## Property names", "", util.markdown_table(
            ["Event", "Property", "Type", "Count"],
            [[p.get("eventName"), p.get("propertyName"), p.get("dataType"), p.get("count")] for p in names],
        ), ""]
    for name, rows in values.items():
        lines += [f"## Values: {name}", "", util.markdown_table(["Value", "Count"], [[r.get("value"), r.get("count")] for r in rows]), ""]
    body = "\n".join(lines)
    return _doc(snapshot, file_stem, "event-properties", "Umami event properties", body, sensitivity="personal")


def _governance_doc(snapshot: dict[str, Any], file_stem: str) -> dict[str, Any] | None:
    parts = []
    for key, label in (("goals", "Goals"), ("segments", "Segments"), ("annotations", "Annotations"), ("performance", "Core Web Vitals")):
        value = snapshot.get(key)
        if not value:
            continue
        parts += [f"## {label}", "", "```json", util.json_text(value), "```", ""]
    if not parts:
        return None
    body = "# Umami definitions and annotations\n\n" + "\n".join(parts)
    return _doc(snapshot, file_stem, "governance", "Umami goals, segments, annotations, and Web Vitals", body)


def snapshot_documents(snapshot: dict[str, Any], file_stem: str) -> list[dict[str, Any]]:
    docs = [_overview(snapshot, file_stem)]
    docs += _metrics_docs(snapshot, file_stem)
    docs += _event_docs(snapshot, file_stem)
    for builder in (_event_properties_doc, _governance_doc):
        doc = builder(snapshot, file_stem)
        if doc:
            docs.append(doc)
    return docs


def collect_snapshots(conn: sqlite3.Connection, snapshot_dir: str | list[str]) -> dict[str, Any]:
    roots = [Path(item) for item in (snapshot_dir if isinstance(snapshot_dir, list) else [snapshot_dir])]
    stats = {"files": 0, "documents": 0, "dirs": [str(root) for root in roots]}
    if not any(root.exists() for root in roots):
        stats["error"] = f"snapshot dirs missing: {', '.join(str(root) for root in roots)}"
        return stats
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            stats.setdefault("errors", []).append(f"missing snapshot dir: {root}")
            continue
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                stats.setdefault("errors", []).append(f"{path.name}: {exc}")
                continue
            snapshots = payload if isinstance(payload, list) else [payload]
            for index, snapshot in enumerate(snapshots):
                stem = path.stem if len(snapshots) == 1 else f"{path.stem}-{index}"
                for doc in snapshot_documents(snapshot, stem):
                    docs.append(doc)
                    seen.add(doc["id"])
            stats["files"] += 1
    from .db_writer import upsert, mark_missing
    upsert(conn, docs)
    mark_missing(conn, "umami", seen)
    stats["documents"] = len(docs)
    return stats


class UmamiApi:
    def __init__(self, base_url: str, api_key: str, website_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.website_id = website_id

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api/websites/{self.website_id}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def snapshot(self, start_at: str, end_at: str) -> dict[str, Any]:
        start_ms = int(util.parse_iso(start_at).timestamp() * 1000)
        end_ms = int(util.parse_iso(end_at).timestamp() * 1000)
        base = {"startAt": start_ms, "endAt": end_ms}
        snapshot: dict[str, Any] = {
            "exported_at": util.iso_now(),
            "website": {"id": self.website_id, "name": "PrintLyrics", "domain": "printlyrics.app"},
            "range": {"startAt": start_at, "endAt": end_at},
            "errors": [],
        }
        for key, path, params in (
            ("stats", "stats", base),
            ("traffic", "pageviews", {**base, "unit": "day"}),
            ("eventStats", "events/stats", base),
            ("eventSeries", "events/series", {**base, "unit": "day"}),
            ("annotations", "annotations", base),
            ("goals", "goals", None),
            ("segments", "segments", None),
            ("performance", "performance", base),
            ("sessionStats", "sessions/stats", base),
        ):
            try:
                snapshot[key] = self.get(path, params)
            except Exception as exc:
                snapshot["errors"].append(f"{key}: {type(exc).__name__}: {exc}")
        metrics: dict[str, Any] = {}
        for metric_type in METRIC_TYPES:
            try:
                rows = self.get("metrics", {**base, "type": metric_type, "limit": 500})
                if rows:
                    metrics[metric_type] = rows
            except Exception as exc:
                snapshot["errors"].append(f"metrics/{metric_type}: {type(exc).__name__}: {exc}")
        snapshot["metrics"] = metrics
        try:
            snapshot["eventProperties"] = self.get("event-properties", base)
        except Exception as exc:
            snapshot["errors"].append(f"eventProperties: {type(exc).__name__}: {exc}")
        try:
            funnels = self.get("funnels", None)
            for funnel in funnels if isinstance(funnels, list) else []:
                try:
                    funnel["result"] = self.get(f"funnels/{funnel['id']}", base)
                except Exception as exc:
                    snapshot["errors"].append(f"funnel {funnel.get('id')}: {exc}")
            snapshot["funnels"] = funnels
        except Exception as exc:
            snapshot["errors"].append(f"funnels: {type(exc).__name__}: {exc}")
        return snapshot


def refresh(api_key: str, base_url: str, website_id: str, start_at: str, end_at: str, snapshot_dir: str) -> str:
    api = UmamiApi(base_url, api_key, website_id)
    snapshot = api.snapshot(start_at, end_at)
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "live.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
