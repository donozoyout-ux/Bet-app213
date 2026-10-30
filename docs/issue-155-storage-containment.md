# P0 storage containment — 30 October 2026

## Evidence and cause

Read-only Render query at 15:36:24 UTC against **Kuponanaliziivv755**:
802,936,511 database bytes; 543,367,168 board relation bytes; 4,601 editions;
last edition 15:20:41 UTC; 12,390 matches, 9,745 older than four days.
These are database/relation sizes, not filesystem free space. An additional
five-row JSON size breakdown request timed out; no result is claimed or retry run.
The earlier incident report in #155 recorded 1,336 editions/24h and 144 MiB raw
JSON. The user-disabled worker has not produced a newer edition in this sample.

`board_cache.refresh_one` selects work on expiry (10 minute TTL, renewal two
minutes early), global input revision or missing projection. `publish_board`
hashes the complete stable document, including repeated items, coupon candidates
and engine evidence. Every changed digest inserts that entire document again;
there is no production cap on master. Each individual observation may be real,
but full-document append frequency is not a sustainable retention policy.
PR #157's controls were unmerged, opt-in, cached per process for five seconds,
and its five-minute league cooldown did not provide a total daily budget.
This patch does not assert that clock-only differences explain every production
edition; it covers that reproducible source of redundant versions separately.

## Change and bounds

- Build on #157/#156/#154; include merged master #160 schedule guards. #159 CSS
  is inspected and left on its separate branch. No remote PR is merged/deployed.
- Admission is mandatory when APP_ENV=production or RENDER=true; the old
  HISTORY_HOT_TIER=false flag cannot disable it. Development can opt in.
- Stop application DML at 700,000,000 aggregate logical database bytes; stop
  board insert/collector at 200,000,000 board relation bytes. Operators may lower
  caps, never raise them past these ceilings. Reserve headroom for WAL/overhead;
  these observations are not a guarantee of physical free disk.
- Use a transaction advisory try-lock shared across application processes and
  recheck sizes on every write, eliminating the five-second concurrent burst.
  Competing writers fail closed with storage_writer_busy (HTTP 503); no waiting
  third pool connection and no infinite lock queue. External administrative
  connections not using this application are outside this boundary.
- Maximum 128 KiB per new edition, 15-minute minimum interval per league,
  64 new editions across all leagues per rolling 24h, at most 8 MiB reserved raw
  board JSON/day. Actual PostgreSQL/TOAST/index/WAL overhead differs. The rolling
  byte reservation conservatively charges 128 KiB per prior edition, so lowering
  limits or restarting cannot reset it. Count existing rows without detoasting
  historical JSON. Backdated/future board inserts and bulk/raw bypasses reject.
- At a budget breach, preserve the old publication; do not silently substitute,
  delete or renew an unvalidated selection. Boards can become visibly stale.
- Clock-only cutoff changes use the original immutable edition; actual evidence,
  probabilities, selections or model changes still need a distinct publication.
  Legacy edition IDs and payload bytes are never rewritten. Regression covers
  clock-only renewal and a real sample-size change.
- Explicit board refresh gets capacity preflight before model work. Production
  defaults activate #157's historical match INSERT/backfill prohibitions as well.
  Current existing-match updates remain possible only below the DB watermark.
- COPY/TRUNCATE and raw/CTE match or board insertion bypasses are rejected through
  this application's SQL path. No production DDL or destructive command is run.

## Archive preparation

The #157 encrypted private Release protocol, checksum/count/FK validation,
transactionally isolated PostgreSQL restore, exact-byte roundtrip and read-through
smoke tests remain intact. Daily export stays at 03:15 UTC / 06:15 Turkey,
requires HISTORY_EXPORT_ENABLED=true and protected environment `history-archive`.
The target remains donozoyout-ux/kuponanalizii-data-archive.

Manual workflow now defaults to `plan_only=true`, which can run without enabling
scheduled exports. It requires only the read-only TLS source credentials and
namespace; it does not instantiate the GitHub store or need archive write/key
credentials. CLI equivalent:

```
python -m src.archive.history_export --plan-only
```

It reports `planned_not_archived`, complete eligible/retained counts and named
blockers. This is not restore proof, never an eviction authorization. A successful
plan may contain zero eligible rows; export still exits non-success for zero
verified partitions. Full secret lists and protocol are in
`issue-155-pr2-rollout.md` and `issue-155-history-read-through.md`.

**Remaining data prerequisite:** existing collectors do not populate the full
`matches.raw.archive_finality` contract. Real provider finality/HT market evidence
must be supplied from an authorized, verifiable source; absent values stay named
blockers. Do not populate invented attestations, mark missing prices unavailable
without source evidence, or weaken eligibility to make a run green. No actual
production archive upload/restore has occurred in this task.

## Production procedure (after explicit merge/deploy approval)

1. Keep SCRAPER_WORKER_ENABLED=false and AUTO_BACKFILL_ON_EMPTY=false. Keep
   GOALOO_DAILY_UPDATE_ENABLED and TELEGRAM_COUPON_WAKE_ENABLED disabled. Do not
   enable NVIDIA or send real Telegram requests. Old suspended DB stays untouched.
2. Review this stacked PR and dependencies and CI. Deploy only after approval.
   Ensure APP_ENV=production (Render also supplies RENDER=true). No new paid
   resources or plan changes are required. At today's size admission will block;
   this patch cannot safely free already occupied space under the no-delete rule.
3. Inspect `/api/history/coverage` storage status: a bounded read-only capacity
   observation is cached for 30 seconds, including while workers are paused. Do not test by inserting production records.
   A green /health alone is not DB capacity proof. Keep workers paused.
4. Configure a least-privilege readable source and protected archive environment
   as documented in PR2. Run plan-only, resolve genuine missing provenance, then
   explicitly run the bounded exporter with plan_only=false and a stable run ID.
5. Require real encrypted assets + published root pin + PostgreSQL restore and
   read-through receipts. Until then do not claim historical migration complete.
   Export reclaims **zero** source bytes. Do not import history into the new hot DB.
6. No database deletion/eviction is implemented or authorized here, even after
   verification. Future recovery/capacity work requires a separate review and
   explicit approval. Do not re-enable collectors just because this CI is green.

Rollback: keep worker/backfill and both schedule flags off, stop the export flag,
restore previous reader pins if needed. Do not use HISTORY_HOT_TIER=false as a
production escape hatch, raise limits, erase editions or rewrite first predictions.
Reverting code also removes safeguards and must not be paired with worker restart.
Render reports this free DB expires 26 November 2026; export/containment does not
change that expiry or establish a permanent hot-store lifecycle.
