# Issue 155 PR2 — prepared code, not a production migration receipt

Base chain: PR154 → PR156 → this PR. The first two remain draft. No draft is
merged or deployed by this task. No production export, backfill, restore,
eviction, old-database resume, database deletion or paid resource is performed.
Tests use synthetic fixtures and mock uploads; PostgreSQL CI uses an isolated
service. None of those fixtures is uploaded to the private archive.

The issue identifies new hot DB `Kuponanaliziivv755` and suspended disk-full old
DB `Kuponanaliziivv7`. The old DB is not contacted by this work. Its 2024+ rows
cannot be claimed recovered until a separately authorized recovery provides a
readable source and actual archive/restore receipts. Do not import them into the
new hot database. The application defaults remain unchanged until rollout flags
are deliberately configured.

## Planner, identity and completeness

The source is a dedicated SELECT-only connection, TLS `verify-full`, repeatable
read/read-only transaction, bounded SQL/lock timeouts. It never uses application
`DATABASE_URL` or runs app migrations against the source. Namespace is a permanent
operator-assigned identifier for the source database. A database/table-OID
fingerprint binds resumable runs to that source. Immutable identity is
`source_namespace:external_match_id`; internal IDs are only meaningful inside a
generation. Hot/cold reconciliation uses provider + external match ID, with hot
data winning. Team and referee identities are remapped; SQL primary keys are
never blindly joined between databases.

Inventory covers matches from 2024 onward plus unknown-kickoff records (reported
as retained). It groups league/season and up to 32 initial matches per partition,
then closes all direct match FKs, board provenance and transitive learning report
source-manifest references. A lesson is never silently omitted to fit a size
limit. Its referenced matches, all settlement revisions and source hashes must
also fit and qualify. Failed groups split down to individual matches; oversized
connected dependency components stay hot with a named blocker. Maximum inventory
is 100,000 matches, 2,048 partitions and 256 closure matches per partition. Hitting
a cap aborts/reports incompleteness rather than truncating coverage.

Each partition retains the complete PR1 row schema: match identity/FT/HT/events,
corners/cards/statistics, bookmaker references and opening/closing/latest market
columns, odds snapshots, first saved predictions, publication/settlement history,
board editions, saved analysis, lessons and report evidence. Source row JSON is
embedded without float reserialization. PostgreSQL restore uses original JSON
with `json_populate_record`, compares the full restored relational row using
JSONB equality (including numeric precision) and exact archived chunk bytes.

Required eligibility is **finished + >=24 hours + known FT/HT + final statistics
+ finalized provider/market evidence + terminal settlements + completed dependent
jobs**. Unknown/live/postponed/incomplete/uncertain records remain hot. Existing
nullable fields remain null; no odds, results, predictions or absence attestations
are manufactured.

### Provider finality contract and current source limitations

Legacy `odds_complete` alone does not prove every FT/HT market and event source
has finalized. The planner requires source-provided `matches.raw.archive_finality`:

- `observed_at`: real observation time after kickoff + 3 hours, not after cutoff.
- `events_final: true`: a real collector/provider finality observation.
- `markets`: all Crown/Bet365/Sbobet × full_time/first_half × 1x2/ah/ou cells.
  Each cell has `status` (`observed` or provider-confirmed `unavailable`) and a
  nonempty `source_reference`. Observed cells contain opening/closing numeric
  home/draw/away, line/home/away or line/over/under as appropriate.

This exporter does **not** create those fields. In particular the legacy typed
market schema does not fully represent HT 1X2/AH; any actual source observations
must be retained in the source raw dossier with provenance, or the records are
reported `unverified_market`, retained and not archived as finalized. Explicit
provider unavailability is preserved in `gaps`, never converted to zero odds.
Collectors need genuine evidence to populate this contract; filling synthetic
`true`/`unavailable` values just to pass the exporter is prohibited.

## Staging, resume and atomic visibility

An immutable run ID creates a private draft Release. Encrypted `plan.enc` fixes
namespace/database fingerprint and eligibility cutoff. Partitions have
content-derived generation IDs. Resume re-reads source rows in one consistent
snapshot, reuses only byte-identical staged assets and rejects any immutable
asset conflict. If mutable source evidence changed, use a new reviewed run ID;
never overwrite the old run. Newly qualifying data after cutoff belongs to the
next run. An interrupted source transaction cannot be resumed as the same
PostgreSQL snapshot; each attempt re-plans fully before root publication.

Per partition: gzip → AES-256-GCM with generation/name AAD → private Release
assets → exact read-after-write → SQLite structural/byte checks → isolated real
PostgreSQL row/FK/count/byte restore → dossier/audit/odds read-through smoke →
encrypted receipt → catalog last → partition publication. PostgreSQL verification
accepts only loopback database `history_verify` or `betapp_test`, creates a unique
schema and rolls back the complete transaction. It never accepts a production
restore URL. Failed checks cannot publish a catalog.

After **all** inventory reconciliation completes, the encrypted root `index.enc`
is published as the atomic reader-visible boundary. Orphaned/staged partitions
are not discovered through release listing. The index reconciles every inventory
ID as covered or retained, and includes named blockers and explicit market gaps.
Zero eligible partitions leave the run draft and return nonzero exit 2; no
verified backup is claimed. Some retained records are normal, but the published
coverage must never be described as complete 2024+ recovery.

Bounds: 8 MiB/chunk, 8 chunks/partition, 64 MiB partition plaintext, 100,000 rows,
256 MiB restored SQLite page budget. Per invocation: 80 new assets, 500 archive
API operations, operator-specified total repository byte budget. Resume with the
same ID after a budget interruption if source content is unchanged. API failures
and rate limiting stop the publisher rather than spinning/retrying mutations.
No delete, overwrite or retention/pruning call is made. Other archive writers
must use the shared concurrency group or be disabled during publication.

## Unified readers, site and model

The pinned root index authenticates the partition descriptors and source identity.
It never uses an unverified `latest` release. At most eight SQL scope caches are
leased serially per process, with 256 MiB aggregate local cache budget. Warm reads
reuse downloaded scopes. Queries may span at most eight partitions/64 MiB
plaintext; over-budget queries return explicit unavailability, not partial form
or silently truncated history. Large queries require narrower explicit scopes.
Temporary caches use the existing free instance; no disk/service plan is added.

`HISTORY_RESOLVER_ENABLED=true` adds verified archive evidence to both model
history (including FT/HT/corners/cards) and site form/H2H, with as-of observation
filters and hot precedence. A live/reopened hot match cannot be replaced by an
old finished archive copy. A missing/invalid enabled archive stops that combined
read/model refresh; it is not silently replaced with insufficient history.

Other existing dossier/statistics/odds/audit/performance/learning routes retain
default hot reads and support explicit `history_source=archive` plus
`history_generation=<partition generation>` via the same SQL handlers. Global
performance/lesson totals are scoped, not naively added across overlapping
report editions. `/api/history/resolve/{provider_match_id}?provider=goaloo`
returns a hot link when available, otherwise an explicitly namespaced archive
link. `/api/history/coverage` exposes verified counts, retained reasons, bounded
scope navigation and admission state without tokens, keys or asset IDs.

The dashboard displays verification/coverage and storage-paused indicators;
scope sections load match links in pages of 20. Match links, form links and
dossier fetches carry the archive generation. The first 100 scopes are displayed
in the compact dashboard indicator; the full bounded index is returned by the
coverage API. Selecting a scope does not mutate source data or generate a past
prediction. No NVIDIA or Telegram request is made by these readers.

## Admission without eviction

`HISTORY_HOT_TIER=true` protects application writer engines. It blocks historical
or unknown-kickoff **new** match inserts, including Core/bulk inserts and raw SQL
inserts. Existing records may still receive legitimate completion updates.
Initial/catalog/history-import jobs are suppressed, existing history jobs are
not run, and schedule discovery counts skipped historical new fixtures under
`not_imported_historical` instead of flooding the hot DB.

The worker checks capacity before collection. Every application data mutation
also checks a short cached PostgreSQL capacity observation. Defaults stop at
700,000,000 summed database bytes and 200,000,000 board relation bytes; operator
overrides may lower but not raise these caps. This is conservative headroom, not
a claim to measure filesystem free bytes/WAL exactly. Failed capacity queries
fail closed. Board editions are additionally limited to 2 MiB/document and one
new edition per league per five minutes; a blocked publication transaction rolls
back its new journal/head changes and leaves existing editions intact. Raw/bulk
board bypasses are rejected. The app returns 503 when admission refuses a write.

These controls never delete existing observations or history. Unsettled/live data
stays in PostgreSQL. With eviction disabled, capacity will eventually require a
reviewed operational decision; archiving alone reclaims zero source bytes.
External admin connections are outside app admission and must not be used to
bypass it. High-watermark recovery does not authorize deletion.

## Exact setup requirements — no values configured by this task

Protected Actions environment: `history-archive`. Repository variable
`HISTORY_EXPORT_ENABLED=true` is required; unset/false skips every run. Schedule
is 03:15 UTC / 06:15 Europe/Istanbul daily, plus manual `run_id` resume. On the
draft branch no schedule is activated. Runtime is capped at 30 minutes.

| Actions setting | Kind / least privilege |
| --- | --- |
| `HISTORY_SOURCE_DATABASE_URL` | Secret, approved readable source only, dedicated SELECT-only role, TLS verify-full |
| `HISTORY_EXPORT_TOKEN` | Secret fine-grained Contents:write only on private `donozoyout-ux/kuponanalizii-data-archive` |
| `HISTORY_ARCHIVE_KEY` | Secret base64 32-byte AES key, separately recoverable |
| `HISTORY_SOURCE_NAMESPACE` | Variable, permanent 1–64 character source identity; never reuse for a different database |
| `HISTORY_ARCHIVE_BUDGET_BYTES` | Variable, explicitly reviewed total byte budget; mandatory positive integer |
| `HISTORY_EXPORT_ENABLED` | Repository variable, default disabled |

The source reader needs CONNECT and schema USAGE plus SELECT on exactly the
tables listed in `history_format.TABLES`, `scraper_job_items`, `scraper_jobs`,
and `learning_ai_tasks`; no ownership, INSERT/UPDATE/DELETE/TRUNCATE, schema CREATE
or elevated role membership. `validate_reader` checks inherited permissions too.
Provision this via the owner's protected administration channel; do not place
passwords in SQL files, commits or chat. Do not weaken shared database grants to
make a failed check pass without separate review.

The workflow's local PostgreSQL verifier password is a disposable CI-only value.
Application DATABASE_URL and NVIDIA/Telegram credentials are explicitly empty in
the job. Its standard GITHUB_TOKEN has only Contents:read and cannot write the
private destination. No decrypted artifact upload occurs; logs contain only
sanitized status/counts/catalog pins. Retain encrypted assets and keys together
with the reviewed receipt references.

| Render/server setting | Requirement |
| --- | --- |
| `HISTORY_INDEX_ENABLED` | true only after actual production publication verification |
| `HISTORY_INDEX_RUN_ID` | Reviewed immutable run ID |
| `HISTORY_INDEX_ASSET_ID` | Published root index asset ID |
| `HISTORY_INDEX_BYTES` | Exact encrypted index byte count |
| `HISTORY_INDEX_SHA256` | Exact encrypted index SHA-256 |
| `HISTORY_ARCHIVE_KEY` | Same protected decryption key |
| `HISTORY_ARCHIVE_TOKEN` | Separate secret Contents:read only on the private archive |
| `HISTORY_RESOLVER_ENABLED` | Opt-in combined model/site history after smoke/load checks |
| `HISTORY_HOT_TIER` | true before enabling collectors on the new hot DB |
| `AUTO_BACKFILL_ON_EMPTY` | false; keep historical workflows disabled |
| `HISTORY_DB_STOP_BYTES` | Optional lower threshold, never above 700,000,000 |
| `HISTORY_BOARD_STOP_BYTES` | Optional lower threshold, never above 200,000,000 |

PR1 single-catalog settings remain supported when the index is disabled. Update
all pins/key/namespace together in a reviewed deployment. Never expose these
secrets to browser scripts; public generation IDs are not credentials.

## Rollout / restore / rollback gate

1. Review PR chain and CI; keep drafts unmerged until design approval. Keep
   collectors/history imports disabled on the new DB while preparing controls.
2. Obtain an approved readable source, create the least-privilege reader and
   configure protected Actions settings. Do not resume/access the old suspended
   DB or copy it into the new DB as an implicit part of this setup.
3. Manually run a bounded export with a unique run ID. On interruption use the
   same ID only for unchanged source content; on immutable conflict create a new
   run after review. Preserve all incomplete assets, never overwrite them.
4. Require actual encrypted assets, receipts containing PostgreSQL restore,
   read-through and exact-byte checks, reconciled coverage/retained counts and
   a published root pin. A green synthetic CI run is not this production proof.
5. Pin one partition temporarily with PR1 reader settings and run
   `python -m src.archive.history_cli verify` / `restore --output <new file>`.
   For PostgreSQL re-verification use the same exporter verifier in an isolated
   loopback `history_verify` DB; source credentials are never a restore target.
6. Pin the root index in a separate reviewed deployment, check coverage API and
   dossier/HT/odds/audit/lesson/performance scopes, identity collisions, as-of
   filtering, cold latency/memory and warm no-download behavior. Enable combined
   resolver only when those checks pass on representative real data.
7. Enable hot-tier admission before collectors. Verify history-import rejection,
   capacity reporting and board limits, then enable only recent-data collection.
   Observe retained/incomplete growth; do not evade limits to keep jobs green.
8. Enable daily schedule last. Do not auto-promote reader pins merely because a
   job ran; review the coverage and verification evidence of each new root.

Rollback: disable `HISTORY_EXPORT_ENABLED` to stop new runs; disable combined
resolver/index or restore the previous complete reader pin. Keep hot-tier
admission and historical backfill disabled. Stop collectors if capacity is
blocked. Keep source DBs, all archive generations, keys, first predictions,
settlements and lessons. There is no data reversal or eviction in this PR.

PR3 remains separately gated by actual verified production recovery copies and
explicit user approval of bounded dependency-safe eviction. No deletion plan is
automatically executed or inferred from this PR's acceptance.
