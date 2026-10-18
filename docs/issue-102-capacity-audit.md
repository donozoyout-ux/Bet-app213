# Issue 102 — aggregate hard stop and read-only capacity audit

## Scope and enforcement

This branch is based on master, independently of unmerged archive PR #101 and
private archive PRs #1/#3. No exporter, source-rights gate, live Release, Render
plan, schema migration or production configuration is changed. No source writes,
imports, deletions, merge/deploy or Telegram/NVIDIA calls were performed.

The limit is **700,000,000 bytes across every database in pg_database**, not
700 MiB and not just the application's database. A single materialized aggregate
query requires a non-NULL size for every database. Missing, invalid, failed,
timed-out or older-than-two-second measurements fail closed. No positive result
is cached. A shared nonblocking transaction advisory lock serializes historical
writers until commit/rollback; a competing writer defers and measures again on
retry. The lock is separate from live/API locks.

The guard runs before season/team/fixture creation and before historical odds,
statistics, events, tick-history or NULL-only odds repair writes. Explicit history
job intent survives commits on the same session; a fresh measurement is required
at each storage call and after the manual tool's queue lock. Capacity exceptions
are resumable pauses, not bookmaker errors. Paused jobs are not directly rerun;
counters, completed items, errors and discovery checkpoints remain intact.

Daily update schedules previously bypassed the old job-kind-only guard despite
including prior-season fixtures. They now defer blocked historical rows using a
savepoint and record `deferred_capacity`, while continuing current fixtures.
The existing seven-day live/results correction window stays available, unless
the caller explicitly marks the work as historical. Unknown kickoff dates are
guarded. The dedicated score updater and prediction processing remain available;
old 90-day statistics enrichment defers without writing a source retry failure.
SQLite remains test-only; production Database configuration requires PostgreSQL.

This is an **import admission limit**, not a physical disk quota: concurrent live
processing, other databases, WAL and an already admitted write can still increase
storage. No estimate is misrepresented as a byte reservation or a guarantee that
total storage cannot exceed 700 MB. Existing 650 MB soft admission for supervised
Poland/Denmark repair remains stricter than the hard limit.

## Audited paths

| Entry | Final enforcement |
| --- | --- |
| Manual Europe workflow / bounded history / retries | shared check plus forced-history storage guard |
| Verified odds repair workflow | 650 MB soft gate, shared lock/probe and guarded fill_only |
| CLI backfill/domestic_history/single-match | worker preflight and guarded store_match |
| Startup initial/catalog scheduling | queued execution and guarded discovery/storage |
| Daily update and previous-season schedule discovery | historical row guard even for kind=update |
| Durable stats/market backfills and retries | explicit history session intent and guarded storage |
| Priority recent-statistics retry (up to 90 days) | historical guard, preserve retry state on capacity stop |
| Optional raw odds tick history | always guarded, including recent fixtures |
| Dedicated live scores / near-term odds / predictions | stay operational; not historical import entry points |

Repository search found Goaloo fixture inserts centralized in store_match;
archival odds also have the explicitly guarded bulk fill_only route. Isolated
archive recovery code in unmerged PR #101 is not a production import path.

## Production observations — 18 October 2026

Two read-only aggregate queries bracketed local implementation/test work:

| Metric | Before | After |
| --- | ---: | ---: |
| Application bytes | 688,084,671 | 688,174,783 |
| Aggregate database bytes | 711,485,004 | 711,575,116 |
| Matches | 14,796 | 14,796 |
| Prediction snapshots | 235 | 235 |
| Saved coupons | 238 | 238 |
| Queued/running jobs | 0 | 0 |
| paused_disabled jobs | 20 | 20 |

The paused-job/checkpoint digest was identical:
`31ddf623ac085627b7e5ee0c2af2f09e`. Only SELECT queries were issued by this task.
The live service can allocate pages independently; whole-database byte equality
is not expected. Other databases account for 23,400,333 bytes at these samples.
Aggregate usage is 11,575,116 bytes over the hard stop even though the application
itself is below it. This unmerged PR does not change the currently deployed code;
do not authorize further historical runs while usage remains above the stop.

## Table / index / TOAST breakdown

Values below are allocated bytes from pg_total_relation_size/pg_table_size/
pg_indexes_size, not estimated row JSON length. Table-without-TOAST includes
auxiliary forks; TOAST includes its index. Statistics row/dead-row counts are
estimates and do not establish reclaimable space. Samples are separate reads,
not one frozen cluster snapshot. WAL, replication, temp files and provider
overhead were not measured by these database-size functions.

| Relation | Total bytes | Table without TOAST | Ordinary indexes | TOAST incl. index |
| --- | ---: | ---: | ---: | ---: |
| board_editions | 429,858,816 | 606,208 | 532,480 | 428,720,128 |
| match_events | 98,336,768 | 61,669,376 | 36,659,200 | 8,192 |
| match_statistics | 22,511,616 | 22,110,208 | 393,216 | 8,192 |
| asian_totals | 20,422,656 | 17,612,800 | 2,801,664 | 8,192 |
| asian_handicap | 20,316,160 | 17,530,880 | 2,777,088 | 8,192 |
| odds_1x2 | 20,094,976 | 17,342,464 | 2,744,320 | 8,192 |
| market_odds | 16,596,992 | 13,672,448 | 2,916,352 | 8,192 |
| learning_slices | 11,116,544 | 10,469,376 | 638,976 | 8,192 |
| matches | 8,601,600 | 6,144,000 | 2,449,408 | 8,192 |
| learning_lessons | 7,675,904 | 7,053,312 | 614,400 | 8,192 |

Largest ordinary index is the match_events unique (match_id, source_key)
constraint at 27,820,032 bytes; its recorded idx_scan=0 does **not** make it
disposable. It enforces event identity. The events primary key uses 6,144,000
bytes, match-id lookup index 2,695,168 bytes. None was changed.

## Retention/recovery options — estimates, not authorization

| Option | Measured candidate / estimated effect | Consequence and uncertainty |
| --- | --- | --- |
| Keep all records, stop historical intake | 0 immediate bytes reclaimed | Safest current state; live publication still grows. |
| 7-day or 30-day board history retention | 0 eligible rows at audit | No immediate relief; not a solution to this incident. |
| Review non-head board editions older than 1 day | 2,267 rows, 249,903,148 stored payload bytes | Candidate payload size only; not proof records are unreferenced/disposable or physical reclaim. These publications are excluded from the phase-2 match archive. |
| Exact redundant-payload deduplication preserving version references | 0 proven recoverable bytes so far; 429.86 MB total relation upper bound | Needs byte-equality audit and separately reviewed schema/application migration; hash resemblance is insufficient. |
| Vacuum/bloat investigation | 0 proven physical bytes recoverable | match_events reports ~29,455 dead rows, but row-count statistics cannot determine reclaimable bytes. Ordinary vacuum usually enables reuse rather than shrinking files. No vacuum/reindex/rewrite performed. |

No record is marked deletable by this report. Any 1-day policy would remove
immutable publication history and may break external/version references even
when a row is not the current board head. GitHub archiving by itself changes
**zero PostgreSQL bytes**, and current match archives omit board editions/events.

## Zero-data-loss recovery runbook

1. Keep history intake stopped; preserve all queued/paused checkpoints. Confirm
   aggregate size and API health before considering any controlled retry. This
   PR requires review and a separately authorized deployment before it protects
   production. Do not switch tiers or loosen the threshold.
2. Obtain owner approval for a backup scope covering all affected tables, indexes,
   identities, publication references, jobs, predictions and coupons. A match-only
   archive is insufficient. Source rights remain uncleared; do not enable private
   Releases based on this report.
3. Create and verify independently controlled durable backups in separate failure
   domains under a separately approved procedure. Existing private GitHub work is
   not a second independent backup, and its pilots were ephemeral. Do not provision
   a paid/new provider automatically. Retain independently trusted manifests,
   checksums, encryption/key custody and an access recovery plan.
4. Restore into isolated PostgreSQL, comparing exact values/NULLs, counts, primary
   and foreign identities, quote metadata and publication/version references.
   Demonstrate recovery without source/GitHub credentials. Record duration and
   every exclusion. Keep the source as the only-authoritative copy until this passes.
5. Only then prepare a separate owner-approved migration/retention proposal with
   exact selection predicates, dependency checks, estimated bytes and rollback.
   Plan temporary space and locking for any physical rewrite; ordinary deletion
   would not guarantee immediate disk reduction. No operation may remove the only
   verified copy of a record. Do not treat a non-head edition as safe to delete.
6. After independently authorized recovery, remeasure aggregate size and verify
   API/live/prediction behavior. Resume an existing checkpoint in a small batch
   only below the applicable gates (650 MB for supervised odds repair); retain
   per-write fresh admission. This task executes none of these future mutations.
