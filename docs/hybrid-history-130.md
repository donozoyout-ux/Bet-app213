# Hybrid history and active calendar window (#130)

## Storage contract

`HYBRID_HISTORY_ENABLED=false` is the default. Enabling it changes the prematch
board, quotes, manual coupon validation, capture selection and revision manifest
to **today plus the next three Europe/Istanbul calendar dates**. The upper bound
is exclusive midnight after the fourth date. The rolling 72-hour priority subset
is distinct from that calendar window. API board metadata exposes both bounds;
the dashboard labels the calendar window explicitly.

This is a selection window, **not a retention policy**. No Match, odds, snapshot,
settlement, coupon or audit rows are deleted, migrated out, or retrospectively
predicted. Daily fixture/result pages and the bounded settlement sweep keep
their existing historical behavior. Historical model inputs still cover 2024+
within the model's existing three-year lookback, never only the active days.

There are two separate caches:

* A private process-local history cache holds at most eight league envelopes,
  each at most 2,000 finished observations, for ten minutes. No raw JSON or odds
  is loaded into this cache, and no fixture row is written to disk. Per-match
  form, league baseline, venue, season and H2H calculations use this replacement
  history, never concatenate it with the same database rows. Every use reapplies
  created/updated/observed cutoffs and the three-hour completion buffer.
* `data/aggregates/season-2024-2025.csv`, `season-2025-2026.csv` and
  `season-2026-2027.csv` contain one allowlisted totals row each. They describe
  source/finished/scored coverage and home/away goal totals across all represented
  competitions. They contain no match, team, referee, bookmaker or league
  identifiers, fixture dates, odds or provider payloads. They are coverage
  summaries, **not a substitute training set**: totals cannot reconstruct form,
  H2H or observation-safe history. Team-level evidence stays private in memory
  and recoverable PostgreSQL storage.

Season membership follows the database Season label, not an inferred July date.
Accepted labels are YYYY-YYYY, YYYY/YYYY and YYYY-YY for these three seasons.
Calendar-year competitions with other labels are excluded from these files;
their historical matches remain available to the model from PostgreSQL.

## Revisions and failure behavior

History uses a per-league count/max-update fingerprint of finished matches and
statistics plus a small team-name fingerprint. ORM team-name commits invalidate process entries immediately. History/statistics
commits change the per-league fingerprint checked on next use; rollbacks never
publish writer state.
Other processes recheck the fingerprint before reusing history. All application
corrections must retain normal `updated_at` behavior. Arbitrary SQL that backdates
timestamps or rewrites team names without changing the lightweight fingerprint
can remain cached until the ten-minute TTL; purge/restart caches after such an
administrative repair. Live quote updates alone do not transfer finished history.

The cache loads a ten-minute future eligibility envelope. Each read applies its
actual cutoff, so a result becoming three hours old enters naturally without
waiting for an invalidation. Envelopes over 2,000 rows are rejected; the original
SQL selection is used with a 2,000-row cap for both score and statistics history.
This is a deliberate bounded-sample limit; it does not remove underlying rows.
The goal model already used that cap. The flag also caps the previously unbounded
statistics-history transfer. Large-league statistics may therefore use a smaller
sample when the flag is enabled; compare them in the staged pilot.

Each CSV is self-contained: schema version, UTC generation and source-update
times, opaque database scope, content SHA-256 and coverage counts. The hash
excludes generation time, so unchanged source content has the same version.
The fixed field order, season sort and newline convention are deterministic.
Publish writes and fsyncs a temporary file before replacing the destination.
Readers accept at most 8 KiB and one row with the exact allowlisted schema;
wrong versions, checksums, database scopes, malformed counts, future generation
times and files older than ten minutes fall back to SQL.

`GET /api/history/seasons` reads fresh files locally with zero database queries
after dependency acquisition; fallback is one GROUP BY returning at most three
rows. No HTTP request fetches GitHub or writes archive files. The existing scraper
priority cycle refreshes CSVs no more than once per ten minutes when enabled.
Generation time can advance while the content hash stays unchanged. The worker's
existing stable board-publication logic avoids new editions when evidence is
unchanged; validity/freshness checks still run before renewing a publication.

On an immutable filesystem, publishing fails with a sanitized warning and SQL
fallback remains available. A Render restart can lose **all** local files and
memory without losing source observations or audit records. Cache warmup is
bounded; persistence does not depend on a writable checkout or a mounted disk.
Do not treat these recomputable caches as backups.

## Build, stage and roll back

1. Deploy the reviewed code with `HYBRID_HISTORY_ENABLED=false`. No DDL or data
   migration is required. Do not change DATABASE_URL or existing archive secrets.
2. In an isolated staging environment, enable `HYBRID_HISTORY_ENABLED=true`.
   Set `SEASON_AGGREGATE_DIR` to a writable cache directory if one is available.
   Optionally run `python -m src.analytics.season_archive build`; this command
   performs SELECTs and local file writes only, without schema initialization.
3. Compare model outputs and query-profile stages against the disabled path.
   Check midnight/rescheduling boundaries, source coverage, delayed correction
   visibility, old settlement recovery and immutable captures. Restart the process
   and remove staging cache files to verify recovery. No production switches or
   deployments are performed by this change.
4. Roll back by setting the flag false and restarting the affected processes.
   Existing tables and snapshots need no restoration. Flagged board model versions
   differ from the old versions, so the worker warms compatible publications.
   Cache files may be retained or removed; disabled runtime does not read them.

Generated CSVs are ignored by Git. Do not automate daily commits, publish raw
Goaloo records, or use a GitHub repository as the operational database. Publication
rights for any exported aggregate still need to match the storage destination's
policy. Sensitive owned audit records continue to use the existing #126 encrypted
private release archive; this change neither weakens its rights/configuration
checks nor expands its payload. That audit subset is not a complete backup of
licensed source fixtures. Any future source-data retention migration requires a
separate approved storage/rights plan and a verified full recovery exercise.

## Diagnosis and validation evidence

Read-only production diagnosis on 2026-10-24 confirmed the web service's
`/api/status` reported connected PostgreSQL and active collection. The new Render
PostgreSQL primary was available; SELECTs confirmed application connections,
1,940 matches and 20 immutable forecast snapshots at the observation instant.
Counts were increasing during collection. No credentials were retrieved or
changed, and neither suspended legacy database was resumed.

The existing scheduling profile is: daily enqueue workflow at 04:15 UTC;
priority tick every five seconds, upcoming quote checks at five-second scheduling
intervals with per-source cooldown, live polling at least 60 seconds, recent
statistics checks every 15 seconds, settlement sweep every 60 seconds, and one
league board checkpoint every ten seconds (subject to deadlines/backoff).
HTTP already reads persisted projections and short-lived response caches. The
additional repeated large transfers addressed here were separate score and
statistics history queries at board model recalculation.

`pg_stat_statements` was not installed, so per-statement live byte attribution
was unavailable. A read-only pg_stat_database sample showed 1,050,530 tuples
returned, 227,186 fetched, 1,524 commits and 2,465 rollbacks since the available
statistics baseline; these counters include unrelated work and are not an egress
bill or a before/after production benchmark. Rollbacks include reader cleanup.

Reproducible **synthetic** benchmark:

```sh
python -m pytest -q -rP tests/test_hybrid_history.py::test_history_wire_volume_benchmark
```

With 75 synthetic observations across the three seasons and three model rebuilds
one minute apart, the disabled path transfers six history rowsets (three score,
three statistics); the enabled path transfers one shared envelope, plus three
small fingerprint rows. Large history transfers fall from 6 to 1 (83.3%). No
claim is made that total production SQL count, bytes or cost falls by 83.3%.
Repeated CSV reads return zero SQL; missing CSV returns one aggregated query.
Tests compare model evidence after removing measured execution durations.

The dedicated suite also covers season boundaries/coverage, Istanbul midnight,
the rolling completion buffer and 72-hour advance, delayed corrections,
live-to-finished changes, bounded fallback, rollback isolation, empty DB,
missing/corrupt/wrong-scope/oversized/stale/future files, repeat generation,
read-only publication failure, ephemeral restarts, unchanged immutable picks and
settlement of a result older than 72 hours. Tests disable Telegram and NVIDIA.

Browser QA: [calendar window, empty test database](hybrid-history-130/empty-calendar-window.png).
The screenshot contains no fabricated fixtures or provider records.
