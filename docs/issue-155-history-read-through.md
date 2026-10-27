# Issue 155 / PR1: isolated historical read-through

This is the first, reviewable architecture slice, not a completed production
migration. No production export, catalog, restore or historical coverage is
claimed. PR #154 remains draft and is the base of this stacked change. Its board
backup and disk-full guard remain unchanged. Neither filtered `owned-audit` nor
board-only backup is a complete match archive.

The issue identifies the new free database as `Kuponanaliziivv755`, and the old
`Kuponanaliziivv7` as suspended and disk-full. Those are issue-provided facts;
this PR does not reconnect, resume, replace, delete or migrate either database.
It does not import history into the new database or change Render infrastructure.

## Implemented read contract

Existing historical GET handlers use the same SQLAlchemy models, calculations,
pagination and response schemas through one session dependency. Requests with no
source parameter retain the existing PostgreSQL behavior. An explicit request:

```text
/api/matches/1982/dossier?history_source=archive&history_generation=<reviewed-generation>
```

selects a verified immutable archive scope. Supported routes include match
detail, odds, saved analysis, dossier, histories/H2H/form, prediction audit,
market performance, performance trends, chart diagnostics, season history and
learning reports/lessons/statistics. Metadata routes use the same source.
Writes, scraper controls, live predictions and coupon endpoints cannot select
the archive. Read-only SQLite also rejects SQL writes if a handler regresses.

`X-History-Source`, `X-History-Generation`, `X-History-From`, `X-History-To` and
`X-History-Leagues` describe the coverage of **every** archive response.
History pages embed that coverage too. A mismatched/missing generation returns
409. Disabled, missing, corrupt, oversized or unavailable archives return 503
with a bounded retry instruction, never a fabricated empty result or hot fallback.
The archive can be queried even when the hot database is unavailable.

IDs are namespaced by source and generation: old match 1982 cannot silently be
replaced by a different new-database match 1982. Automatic fallback by integer ID
is deliberately not part of the migration contract. Consumers must propagate the
source/generation on all related requests. The current dashboard still defaults
to hot storage; production UI switching is gated on a verified catalog. There is
no claim that the live dashboard already includes archived history.

The server prediction/history engine can use exactly the same service without
HTTP or provider execution:

```python
from src.archive.history_reader import configured_reader
async with configured_reader().session() as historical_session:
    # Pass this read-only SQLAlchemy session to existing historical queries.
    # Never capture/publish predictions or settle outcomes in this session.
    ...
```

This first version reads ONE explicitly covered, coherent source snapshot. It
does not merge old/new primary keys, partial aggregate pages or lesson totals.
Cross-source H2H, current-match form enrichment and automatic engine routing
require the reviewed identity map and partition planner in PR2. A subset of
history must not be labelled all-2024+ coverage. This PR is not ready to replace
the site's default data source.

## Format and dependency graph

`match-history-v1` stores complete rows with exact application column names and a
schema fingerprint (column types, nullability, primary and foreign keys). Unknown
tables/columns, omitted tables (including empty ones), duplicate identities,
duplicate JSON keys, nonfinite values and incompatible schemas are rejected.
Each chunk's original decrypted bytes are also retained verbatim in the isolated
restore, with read-back equality; provider JSON text is not reconstructed for
backup claims. The typed SQL read model has the application's existing numeric
semantics. Exact source-byte preservation requires PR2 to export source JSON
without lossy float reserialization.

The table allowlist in `src/archive/history_format.py` covers:

- Leagues, seasons, teams, referees, bookmakers and complete match rows (including
  identity, FT/HT, chronology/status observations and raw source fields).
- Match events/statistics (corners/cards and source finality), 1X2, handicap,
  totals, other market odds and odds snapshots. All bookmaker references and
  opening/latest/closing columns are retained, including Crown/Bet365/Sbobet.
- First saved prediction snapshots, every settlement revision, every prediction
  publication, saved match analysis, and referenced immutable board editions.
- Learning report editions, slices, lessons, AI explanations and the snapshot's
  learning head. Provider AI is never executed by this reader.

Foreign-key validation runs after loading the entire scope. Publication board
references (not SQL FKs) and lesson source-manifest settlement revision/digest
references are checked separately. Each saved pick must have a recorded terminal
settlement. Missing provider fields remain null; no odds, picks or results are
inferred. Operational jobs, credentials, anonymous coupon ownership, Telegram
delivery records and mutable active boards are outside this match-history format.
This is not a full operational PostgreSQL backup.

Eligibility requires kickoff at least 24 hours before export, finished status,
known final scores, `odds_complete`, final statistics when present and explicit
settled/provider-final/odds-final/dependencies-complete evidence. Scheduled,
live, postponed, unresolved and unverified records fail closed. The reader
checks the contract; it cannot independently prove a producer did not omit source
rows. PR2 must obtain these attestations from real provider/processing state,
verify counts against one read-only repeatable-read snapshot and retain that
verification receipt. No eviction may rely on an unchecked boolean manifest.

## Publication, bounds and cold startup

Catalog and chunks use gzip + AES-256-GCM with schema, generation and asset name
as authenticated associated data. The encrypted catalog is pinned by asset ID,
byte count and SHA-256 in server configuration. It authenticates all chunk IDs,
names, encrypted/plaintext digests, sizes, row counts, coverage and source ID.

Limits are 8 MiB plaintext per chunk, 8 chunks, 64 MiB total plaintext, 100,000
rows and a 256 MiB SQLite page budget per scope (checked after each bounded
chunk). These are fail-closed architecture limits, not permission to
truncate 2024+ data. Large board histories or cross-league lesson dependencies
may exceed them. PR2 must design a bounded partition/index planner and dependency
closures; increasing limits until the free instance runs out of memory is not
an acceptable substitute. Free-instance memory/load verification with real
representative exports remains required before enabling production reads.

A cold process checks the repository is private, fetches one pinned catalog and
at most eight chunks (at most ten authenticated requests). Concurrent cold
requests share one load. Verification/restore must finish before the reader is
published; one failed chunk exposes no partial SQL state. Warm reads make zero
GitHub requests. The decrypted cache is temporary and removed on shutdown;
Render restarts rebuild it without a paid persistent disk. Initialization has a
90-second deadline; SQL queries have a 12-second budget. Failures have a
60-second cooldown; GitHub 403/429 response headers extend cooldown up to one
hour. No unauthenticated API quota assumptions or per-row release listing.
Download redirects only permit GitHub's object hosts and never forward tokens.

PR2 publication protocol: upload a new private draft Release with deterministic
chunk names; resume only against the same frozen snapshot; read back/authenticate
every chunk; run isolated restore, source counts and HTTP/query parity; upload
catalog last; verify catalog; publish that new generation. Never overwrite old
assets. Pin all reader settings together in a reviewed deployment. Existing
processes finish on their old immutable generation. There is no mutable `latest`
catalog fetch and no automatic configuration/deployment in this PR.

## Required configuration (not configured by this task)

| Server setting | Requirement |
| --- | --- |
| `HISTORY_ARCHIVE_ENABLED` | Default false; true only after verified production publication |
| `HISTORY_ARCHIVE_REPOSITORY` | Exactly `donozoyout-ux/kuponanalizii-data-archive` |
| `HISTORY_ARCHIVE_TOKEN` | Secret fine-grained token scoped to that private repo, Contents:read |
| `HISTORY_ARCHIVE_KEY` | Secret base64 32-byte AES key; keep separately recoverable, never in Git |
| `HISTORY_ARCHIVE_GENERATION` | Reviewed immutable generation, 1–64 alphanumeric/underscore/hyphen characters |
| `HISTORY_ARCHIVE_CATALOG_ASSET_ID` | Positive numeric Release asset ID |
| `HISTORY_ARCHIVE_CATALOG_BYTES` | Exact encrypted catalog byte count |
| `HISTORY_ARCHIVE_CATALOG_SHA256` | Exact encrypted catalog SHA-256 |

No secret reaches browser JavaScript, query strings, API errors, logs, artifacts
or this repository. These variables configure a reader, not permission to export.
The proposed PR2 protected Actions environment additionally needs a separate
SELECT-only TLS-verified source URL, a separate Contents:write archive token, the
encryption key, storage budget and an explicit export-enabled variable. Its exact
workflow names/permissions will be delivered in PR2; no scheduled export exists
in this PR. Existing `BOARD_BACKUP_*` settings belong only to PR154's board backup.

Before pointing collectors at an empty hot database, keep
`AUTO_BACKFILL_ON_EMPTY=false`; that flag alone does not disable all manual or
external jobs. Keep collector workflows/worker disabled until PR2 enforces the
hot insertion policy and global capacity guard at every writer entry point.
PR2 must reject historical imports while allowing updates to unresolved existing
matches, stop new collection at a conservative high watermark and reserve WAL/
temporary/emergency space. This PR changes no live environment flags.

## Isolated restore runbook

1. Obtain the reviewed production verification receipt and configure the pinned
   generation's settings above in a protected server environment. Never run this
   against an unverified/public archive or expose the decryption key to clients.
2. Run `python -m src.archive.history_cli verify`. It downloads, authenticates,
   validates counts/dependencies, restores in a new temporary directory, compares
   exact bytes and removes the cache. Exit zero prints generation, source,
   coverage and counts; errors are sanitized and exit nonzero.
3. For an isolated retained catalog, run
   `python -m src.archive.history_cli restore --output /new/path/history.sqlite`.
   The destination must not exist. It is a decrypted, sensitive local read model;
   restrict access. Copy SHA-256 is checked. No production URL is accepted, no
   migrations run against PostgreSQL and no data is imported into the hot DB.
4. Start a separate reader with the same pin, request the historical endpoints
   with source/generation, compare results with the original snapshot, including
   pagination, H2H/form, charts, every saved prediction/publication/settlement and
   lesson revision. Validate coverage beyond one fixture before enabling users.
5. Roll back reader configuration to the prior verified pin if validation fails.
   Keep all old assets and keys. Archive copies free **zero** source database
   bytes. Neither verification nor a successful restore authorizes eviction.

## Remaining phases and evidence

PR1 tests use explicitly synthetic, isolated fixtures and mock GitHub transport;
they are not historical betting data and are never uploaded. They test API SQL
parity, ID collisions, reads during hot DB outage, exact restore, read-only
protection, concurrent cache fills, corruption, dependency/count failures,
encryption context, eligibility and rate limiting.

PR2: recover old source connectivity without loss, full inventory, stable identity
mapping, scalable scope index, global capacity/hot-only admission, read-only daily
export, permissions, verified production catalog and site/engine activation.
Do not backfill the new DB with old matches. The suspended old DB is not deleted.

PR3: separately reviewed bounded eviction with all relational and embedded JSON
dependencies, **only after actual production archival/restore evidence and new
explicit user approval**. No eviction, truncate, VACUUM FULL, index drop, trigger
disable, paid service, provider request, merge or deployment is part of PR1.
