# PrintLyrics reference deployment

`compose.yaml` is the original single-project deployment behind
`printlyrics-knowledge`. It predates the generic `kn` stack and indexes more
sources than a repository store:

| Source | Scope | Contents |
|---|---|---|
| `session` | `printlyrics`, `printlyrics/ideation`, `printlyrics/analytics` | omp transcripts, chunked with line anchors |
| `git` | `printlyrics` | Commit messages and changed files |
| `doc` | `printlyrics` + related scopes | README, PRODUCT, AGENTS, CHANGELOG, plans, schema, routes, deploy config |
| `plausible_export` | `printlyrics/analytics` | CSVs inside Plausible ZIPs plus a totals summary |
| `search_console_export` | `printlyrics/analytics` | Country, device, keyword, page, performance CSVs |
| `umami` | `printlyrics/analytics` | Stats, traffic, dimensions, events, funnels, goals, segments |
| `app_db` | `printlyrics` | Development schema and row counts only |

## Start

```sh
cd /path/to/kn
docker compose up -d --build
docker compose run --rm collector --once
```

The server listens on `127.0.0.1:8765`; the collector refreshes every 15 minutes.

```sh
curl -s http://127.0.0.1:8765/health
curl -s 'http://127.0.0.1:8765/search?q=free%20songbooks&limit=5' | jq
curl -s http://127.0.0.1:8765/status | jq
```

MCP endpoint: `http://127.0.0.1:8765/mcp`.

The collector accepts only mapped omp project directories (`KNOWN_PROJECTS` in
`collector/sessions.py`); it does not walk every session on the machine.

## Umami refresh

The checked-in snapshot lives in `data/umami/` (gitignored). Export a read-only
key in the shell that starts the collector:

```sh
export UMAMI_API_KEY='...'
docker compose up -d --force-recreate collector
```

A failed refresh is recorded in the snapshot's `errors` array; the existing
snapshot still ingests. A successful refresh writes `live.json` to the
`umami-live` volume.

## Add a source

1. Add a module under `collector/` returning document dictionaries.
2. Give each record a stable `id`, `source_type`, `source_group`, `source_uri`,
   `content_hash`, and `event_at`.
3. Call `upsert` and `mark_missing` from `collector.db_writer`.
4. Register the source in `collector/collect.py`.
5. Add an acceptance query to `tests/`.

Keep generated summaries separate from original text: `body` is the evidence;
`summary` and `notes` are retrieval aids.
