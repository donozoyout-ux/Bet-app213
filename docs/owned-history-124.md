# Daily owned-history audit on Neon

Neon remains the live PostgreSQL database. This implementation never initializes or migrates it, deletes live records, starts a backfill, sends Telegram, calls NVIDIA, or deploys anything. Exports remain disabled until an operator completes the checklist below. No real production export or live coverage measurement was performed while preparing this change.

## Reconciliation of the two proposals

Reviewed [application PR #120](https://github.com/donozoyout-ux/Bet-app213/pull/120) and [private archive PR #6](https://github.com/donozoyout-ux/ma-larii-archive/pull/6), including their review comments. Neither is merged or enabled by this change.

* Archive #6 discovers columns dynamically and recursively removes suspicious key names. Innocuous keys and free-text values can still carry provider data or secrets. Do not enable it. Its Fernet format is not imported by this exporter.
* Application #120 provides the stronger explicit typed projection, AES-256-GCM authenticated manifest, exclusive file creation and isolated catalog restore. Those four standalone modules are adapted here; its outage UI, database adapter changes and weekly exporter are deliberately not brought into the live Neon application.
* **One export path:** `python -m src.archive.cli daily`, in this application repository, targeting encrypted Releases in the verified private archive repository. Do not also enable the legacy weekly workflows (`OWNED_HISTORY_BACKUP_ENABLED` or #120's earlier workflow). This path uses `OWNED_AUDIT_ENABLED` and schema/tag namespace v2. v1 snapshots remain separately recoverable using their original reader and retained key; they are never overwritten, relabeled or automatically removed.

## Exact recoverable subset

| Source | Allowed audit fields | Explicit exclusions |
|---|---|---|
| prediction_snapshots | Internal match reference, prematch capture time, SHA256 model-version digest, pick index, enumerated market/selection, finite probability | Labels, teams, fixtures, prices, free text and all other pick keys |
| prediction_settlements | ID, internal match/pick reference, revision, won/lost/void, recorded time | Scores, actual values, evidence, actor, reason and source hashes |
| saved_coupons | UUID, revision, created time; counts in authenticated manifest | Owner/root/fingerprint, selections, payload, price, delivery information |
| learning_reports | ID, created time, SHA256 method digest; counts in authenticated manifest | Source manifest, summary, narratives, slices, lessons, prompts and AI responses |

Only forecasts captured before kickoff are eligible; settlements must refer to eligible picks. Unknown fields/types, duplicate identities/revisions, orphan decisions, invalid enums and non-finite probabilities fail validation or are excluded at projection as appropriate. Coupon and learning summaries are intentionally minimal audit metadata; they cannot recreate full coupon contents or learning narratives.

The restore target is a **new isolated SQLite audit catalog**, with no foreign keys into missing provider matches and no executable application state. It preserves every exported field and validates record-by-record equality. **This is not a full operational database backup**, cannot reconstruct matches, prices, live coupons or learning narratives, cannot resume scraping or deliveries, and must never hydrate production tables. Native Neon recovery remains a separate operational responsibility.

## Daily reports and limits

Daily export at 03:35 UTC (06:35 Europe/Istanbul), once enabled. Day identity and tags use UTC. Each run collects all four owned tables plus coverage in one repeatable-read, server-enforced read-only transaction. A dedicated connection uses libpq/psycopg, `sslmode=verify-full`, preserves optional `channel_binding=require`, disables pooling, and bounds connect, statement and lock timeouts. Elevated role memberships, inherited relation ownership, database/schema creation and table/column write grants are rejected. It never uses the live `DATABASE_URL` fallback.

`coverage.json` is a small Actions artifact (30-day retention) and a job summary; the same report is authenticated inside the encrypted snapshot. It contains numeric enabled league codes, fixture count and newest fixture **date**, odds completeness counts, finished matches with final six-field corner/yellow/red-card completeness, missing counts and queued/running/failed item backlog. Expected league codes absent or disabled in Neon are listed separately. No match/team identifiers, fixture rows, score, raw match/odds data, names, error messages or credentials appear in reports. Empty leagues remain visible; missing source/schema data causes a failed run, never a zero-filled success.

Odds completeness means the existing `matches.odds_complete` flag, not every possible market or price freshness. Statistics completeness means `is_final`, `collection_status=available`, and non-null home/away corners, yellow cards and red cards. Missing statistics use finished fixtures as denominator. Backlog counts items, including failed items, across jobs; it does not claim distinct missing fixtures or enqueue retries. The existing serial/resumable Neon worker and its locking/deduplication remain unchanged. Use this report to prioritize that queue; do not launch another bulk collector.

Storage uses `pg_database_size(current_database())`, operator-configured quota bytes, previous authenticated day's size and growth. The first run has unknown trend. This is a database-size proxy: other Neon branches, history, project overhead, compute usage and actual provider billing/quota are not measured by this query. Confirm the current project allowance in Neon before setting `NEON_QUOTA_BYTES`; no plan or billing changes are made. A warning starts at 80% of the configured quota.

Caps: 50,000 source rows per owned table (overflow fails, never truncates silently), 32 MiB canonical plaintext, 64 MiB envelope/download bound, 500 report leagues, 1,000 total Release inventory entries, caller-configured total Release asset storage budget. Budget includes unrelated Release assets, and must fit the new snapshot **before** retention. Quota/rate-limit/access errors fail closed. GET API calls have three bounded attempts; mutations reconcile on rerun rather than blindly retrying POSTs.

One immutable tag per UTC day: `owned-audit-v2-YYYY-MM-DD`. A same-day retry downloads, checks SHA256, decrypts, validates and restores the existing snapshot; it does not re-read the source or create duplicate releases. Unique tags plus Actions concurrency serialize normal runs; a racing create conflicts safely. An empty draft from a crash may receive the first asset; an uploaded draft is finalized only after authenticated download and restore. Unexpected/multiple assets fail closed and need operator inspection. Subsequent same-day source changes are captured the following day; this is a daily checkpoint, not continuous replication.

Retention runs only after a newer snapshot has been verified. It deletes Releases in this exact v2 namespace older than `ARCHIVE_KEEP_DAYS` (7–3650), and only after decrypting and validating the old asset. It never deletes drafts, legacy Releases, unrelated assets, Git tags, or Neon data. Authentication failure stops cleanup. Retained Git tags are tiny metadata. Preserve older encryption keys independently; before rotating, plan separate recovery/retention of releases encrypted with older keys, since this CLI accepts one key per invocation and will fail closed on an old key. Losing the key makes recovery impossible.

Missing calendar days since the last authenticated Release produce a warning. An independent 12:35 UTC watchdog fails if the daily workflow has no verifiable scheduled/manual success within 36 hours, including when the daily schedule did not run. Enable GitHub Actions failure notifications for maintainers; there are no Telegram alerts and no new issue/comment messages. GitHub schedule availability and notification delivery are external dependencies; no workflow can detect an outage that prevents all Actions from running. Check the first run manually and monitor the watchdog itself.

## Activation checklist (operator, not performed by this PR)

1. Keep both earlier weekly exporters and all Goaloo/raw CSV archive workflows disabled. Review rights/privacy for exactly the table above; record the approval as `APP-124` (the syntax alone is not legal approval). Verify the destination `donozoyout-ux/ma-larii-archive` is still private and that its existing branches/workflows do not export licensed data.
2. Provision a dedicated Neon SELECT-only role, without membership in writer/admin/owner roles, database/schema CREATE or any table/column write grants. Restrict SELECT to the columns below; avoid granting generic access to raw provider tables. Supply a reader URL with `sslmode=verify-full`, CA validation and optional `channel_binding=require`. Verify it independently; never use the live owner URL. No SQL below is run automatically.

   ```sql
   GRANT USAGE ON SCHEMA public TO owned_audit_reader;
   GRANT SELECT (match_id,captured_at,model_version,picks) ON prediction_snapshots TO owned_audit_reader;
   GRANT SELECT (id,match_id,pick_index,revision,outcome,recorded_at) ON prediction_settlements TO owned_audit_reader;
   GRANT SELECT (id,revision,created_at) ON saved_coupons TO owned_audit_reader;
   GRANT SELECT (id,created_at,method_version) ON learning_reports TO owned_audit_reader;
   GRANT SELECT (id,league_id,kickoff_at,status,odds_complete) ON matches TO owned_audit_reader;
   GRANT SELECT (id,external_id,enabled) ON leagues TO owned_audit_reader;
   GRANT SELECT (match_id,is_final,collection_status,home_corners,away_corners,home_yellow_cards,away_yellow_cards,home_red_cards,away_red_cards) ON match_statistics TO owned_audit_reader;
   GRANT SELECT (id,league_id) ON scraper_jobs TO owned_audit_reader;
   GRANT SELECT (job_id,status) ON scraper_job_items TO owned_audit_reader;
   ```

3. Generate a random 32-byte AES key encoded as URL-safe base64 **offline**; store its recovery copy separately in a secret manager. Never put keys, tokens, DSNs, exports or restored catalogs into Git/chat/logs. Create a fine-grained token scoped only to contents-write on the private archive repository. The source workflow's built-in token has `contents: read`; it cannot write Releases. The watchdog has only contents/actions read.
4. Create protected GitHub environment **owned-audit**, restrict it to the default branch and require maintainer review. Store secrets `ARCHIVE_DATABASE_URL`, `ARCHIVE_ENCRYPTION_KEY`, `ARCHIVE_GITHUB_TOKEN` there. Store environment vars `ARCHIVE_REPOSITORY`, `ARCHIVE_RIGHTS_REFERENCE`, `ARCHIVE_BUDGET_BYTES`, `ARCHIVE_KEEP_DAYS`, `NEON_QUOTA_BYTES`, `ARCHIVE_EXPECTED_LEAGUES`. Set limits deliberately; no paid quota is inferred. Expected leagues are reviewed comma-separated numeric codes (e.g. the current established production scope, excluding staged leagues until admitted).
5. After the configuration/source/rights/target checks, explicitly set repository variable **OWNED_AUDIT_ENABLED=true**. Until then both daily jobs are skipped, with no secrets supplied. This PR does not set variables/secrets or activate workflows. Scheduled execution uses the merged default branch; merging is outside this task.
6. Run one protected manual workflow, inspect the non-provider aggregate artifact, Release checksum and encrypted asset. Download through a trusted operator channel; with the key in environment run `python -m src.archive.cli restore --input /secure/owned-audit.enc.json --destination /secure/new-audit.sqlite`. The destination must not exist. Compare catalog counts/records with the permitted projection. Do not point this process at live tables or enable deliveries. Re-run the same day to verify no second Release appears.
7. Review the next day's size trend, missing league/data counts and watchdog status. Validate retention in a synthetic private test repository first. Keep exports disabled if the first restore, read-only credential validation or key retention fails.

## Validation

`python -m pytest -q tests/test_owned_archive.py` exercises synthetic zero/nonzero catalogs, encrypted restore, manifest/ciphertext corruption, disguised secrets, explicit query columns, missing configuration/TLS, same-day retries, crash drafts, readback verification, public targets, 403/422/budget/outage failures, retention boundaries, missing leagues and run-watchdog failures. CI also supplies isolated PostgreSQL for actual read-only transaction and column-write privilege rejection, alongside the repository's complete existing test suite. No test uses production credentials or sends real Telegram/NVIDIA requests.
