# Private archive phase 2 — no source deletion

Destination: **donozoyout-ux/ma-larii-archive** (private). Application source:
**donozoyout-ux/Bet-app213** (public). Never put exported data in public commits,
Actions artifacts, caches or Releases. Neither workflow publishes an archive.

## Actual phase-1 evidence

The private repository already exists. Poland 2025–2026 read-only pilot
[39114835293](https://github.com/donozoyout-ux/ma-larii-archive/actions/runs/39114835293)
succeeded: 306 fixtures, 2,754 bookmaker/market rows, gzip SHA-256
`915a6b70c52deeaf4d1b6ce2010d5e8cea1c9b825d9a94a24e820d2d9571611f`.
That version-1 run proved **SQLite logical parity only**, not PostgreSQL restore.
Release publishing was skipped; no permanent archive asset was created.
Draft PR #99 is the prototype; phase 2 supersedes its exporter.

## Version-2 safety contract

- One PostgreSQL REPEATABLE READ READ ONLY transaction; server read-only check,
  5-second statement / 1-second lock / 15-second idle-transaction timeouts and
  120-second total snapshot lifetime. No source schema initialization or writes.
- Keyset pages of 50, at most 100; bulk related reads and bounded ORM identity
  map. Writer/reader enforce 5,000 fixtures, 2 MiB per record, 128 MiB inflated
  payload and a conservative **64 MiB application asset limit**.
- Canonical gzip JSONL, no embedded filename/mtime; SHA-256 over compressed and
  canonical uncompressed bytes. Deterministic for the same unchanged snapshot.
  Checksums detect alteration against a trusted manifest; they are not signatures.
- League, season, teams, optional referee, match identity and scores, all match
  statistic columns, three bookmaker identities and FT 1X2/AH/OU opening,
  closing and latest observations with complete raw metadata and timestamps.
  NULL is preserved, never replaced by a default or inferred price.
- Manifest and record schema, linked identities, unique/sorted match IDs,
  unique bookmaker/market pairs, numeric values, hashes and counts validated.
  Partial exports never publish a valid archive/manifest pair. Existing evidence
  cannot be overwritten.
- SQLite remains a separately labelled logical check. Real PostgreSQL restore
  uses only loopback `betapp_test` / `betapp_archive_restore`, a random new schema
  and real model tables/foreign keys. Re-exported **every-value** digest and
  counts must match. The entire target transaction, including DDL, rolls back.
  No DROP/DELETE/TRUNCATE or production-target restore is needed.

CI supplies PostgreSQL 16 and seeded synthetic data. Tests prove exact roundtrip,
JSON nulls, zero handicap, metadata, repeatable snapshot under a concurrent test
writer, SELECT-only export verbs, reproducible bytes, failure cleanup, corrupt
manifests/counts, linked-identity conflicts and rejection of live restore targets.
Synthetic test data never enters Render.

## Read-only Denmark pilot

Private workflow `.github/workflows/archive-pilot.yml` must pin the **full tested
exporter commit SHA**, validate actual repository privacy via GitHub API, run
SQLite and genuine disposable PostgreSQL restores, and log only counts/checksums
and proof flags. It must be manually dispatched with league 7, season 2025–2026,
`publish_release=false`. Phase-2 results will be recorded only after execution.

A dedicated SELECT-only source credential is recommended; do not create users,
change Render configuration or grants in this task. The current credential's
scope must be reported honestly; server read-only transactions are mandatory
regardless of role privileges. No Telegram/NVIDIA calls, scraper scheduling,
prediction/coupon reads or mutations are part of the exporter.

## Publishing remains disabled

`publish_release=true` fails before export. Job token is contents:read, there is
no upload action, no Release upload command, no public artifact and no persisted
export on the ephemeral runner. Private access alone grants no redistribution
rights. Source rights/licensing and user approval remain unestablished.

A future **separate reviewed uploader** requires documented source permission,
explicit user clearance, freshly checked exact private destination, narrowly
scoped write token, independently trusted manifest and PostgreSQL proof, asset
size preflight, immutable unique tags, no clobber/replace, and a manual retention
policy. No automatic source pruning or archive deletion. Check the current
GitHub limits before that uploader is enabled; 64 MiB here is an application cap.

## Disaster recovery limits

This is a partition archive and relational parity test, not a full native backup,
PITR system or production recovery drill. Events, odds tick snapshots, other
market tables, users, prediction/coupon history, jobs and service configuration
are excluded. Local internal IDs are remapped via checked external identities.
Production restore, retention decisions and independent offsite recovery copies
require separately reviewed procedures. Historical observations never satisfy
900-second live-quote freshness requirements. Imports keep the existing 650 MB
soft / 700 MB hard safety guards; this task performs no imports or source pruning.
