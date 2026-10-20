# CAIRN.md — wiki conventions

This repository keeps a small, agent-maintained wiki under `wiki/`. Cairn indexes
it as `source_type=wiki`; `cairn lint` checks it against the evidence store.

## Layers

1. **Sources** — raw material: commits, docs, sessions, issue text. Never edit a
   source to make a claim true.
2. **Wiki** — `wiki/**/*.md`, written and maintained by the agent. Pages may be
   rewritten, split, or retired.
3. **Store** — Cairn's SQLite index. It is derived; the markdown is the durable
   artifact.

## Page format

Every claim page starts with frontmatter:

```yaml
---
claim: "One sentence that is either true or false."
status: verified | contested | stale | retired
owner: github-handle-or-name
valid_from: 2026-10-20
valid_to:
sources:
  - "repo://project/path/to/file.md#L10-20"
  - "git://project/<full-commit-sha>"
---
```

Rules:

- `sources` must contain at least one exact `source_uri` from `cairn ask` or
  `knowledge_search`. Relative links are not evidence.
- `status: contested` when sources disagree. Do not silently pick a winner.
- `valid_to` only when the claim is time-bounded (prices, providers, quotas).
- `wiki/index.md` and `wiki/log.md` are structural and need no frontmatter.

## Operations

- **Ingest** — read the new source, update affected claim pages, add new ones,
  update `wiki/index.md`, append to `wiki/log.md`.
- **Query** — search the store first, then read the cited page. File useful
  answers back as claim pages.
- **Lint** — run `cairn lint`; fix `wiki_without_sources`, `contested`, and
  `stale` before adding pages. `uncited_sources` is a backlog, not an error.

## Rules

1. Cite, do not paraphrase from memory.
2. When new evidence contradicts an old claim, mark it `contested` and keep both
   sources.
3. Never let a generated page overwrite a source record.
4. Prefer one claim per page; if a page needs two claims, split it.
5. Keep the log append-only with the prefix `## [YYYY-MM-DD] ingest | Title`.
