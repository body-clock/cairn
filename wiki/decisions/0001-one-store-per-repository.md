---
claim: "Each repository gets its own Cairn store, compose project, port, and volume."
status: verified
owner: body-clock
valid_from: 2026-10-20
valid_to:
sources:
  - "repo://cairn/README.md#L1-10"
---

# One store per repository

The README and `bin/cairn` both make the isolation explicit: `init` writes one
manifest, `up` creates a compose project and volume per repository, and ports are
allocated per store. Stores never share a database merely because they share an
MCP client.

## Consequences

- Work and personal repositories can use different retention and access rules.
- A broken index in one repository cannot corrupt another.
- Cross-repository questions require querying each store, which is intentional.
