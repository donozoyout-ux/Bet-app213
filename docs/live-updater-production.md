# Live status and recent statistics repair

## Confirmed incident

Read-only production inspection on 2026-10-09 confirmed BetApp match 5959,
Goaloo 3026745 (Galatasaray–Kasimpasa), kickoff 17:00 UTC, was still `scheduled`
with NULL scores and last scrape `2026-10-08T18:59:16.666011Z`.

The verified Goaloo match page identified competition 30, home team 516 and away
team 4469. Its JavaScript polls `/Ajax/SoccerAjax/?type=11&id=3026745` and reads
`Data.state` and `Data.html`. Both the page and that endpoint reported state 1 and
score 0–0 during inspection (17:39 UTC). The trimmed captured fixture is committed
under `tests/fixtures/live_3026745.*`. Goaloo's separate `MatchState: 0` response
field is **not** the displayed match state; its own frontend uses `Data.state`.

The source reported “Ongoing”, not a numeric minute. The new collector deliberately
returns `live_minute: null` in that case. It accepts explicit reported minutes such
as `67` or `45+2`, but never derives one from kickoff, phase timestamps or events.

## Exact causes

1. `enqueue_all_updates()` reused any queued/running job for a league, without a
   `kind='update'` condition. An active `stats_backfill` was therefore returned as
   if it were a fresh league update. Explicit league updates were also blocked.
2. A worker executed an entire historical job before selecting another job. Merely
   assigning queued updates priority zero could not interrupt the running job.
3. There was no independent recurring score/status collector. Statistics and odds
   scraping did not refresh the fixture's state. The live API correctly filters
   stored live states, so a stale scheduled record was absent from it.

## Implementation

The new priority updater is independent of the job queue and runs in the existing
scraper child process. The single PostgreSQL worker advisory lock still serializes
source work. It checks for due fixtures before jobs, between discovery rounds and
between durable historical items. No new service or paid infrastructure is added.

- Source-reported live and near-kickoff fixtures are eligible every 60 seconds.
  Today's scheduled fixtures farther than 30 minutes from kickoff refresh every
  ten minutes; yesterday's unresolved fixtures are also considered. Kickoff only
  selects candidates, never sets a live state. Finished fixtures leave intensive
  score polling.
- The match, competition and both team IDs must match the source page before its
  score endpoint is accepted. Unknown schemas/states are rejected. Scores commit
  separately from statistics. Missing red cards remain NULL.
- Live-cycle work has a 25-second outer budget, eight-second source fetch budgets
  and bounded batches ordered by oldest check. Cadence is best effort under source
  outages, heavy-item completion and free-container memory pressure; no exact
  60-second SLA is claimed. Persistent check timestamps survive restarts.
- Recent completed fixtures from the last 90 days rotate across **all enabled
  leagues**, ahead of old history. One recent item is attempted every 15 seconds
  at worker boundaries. Final partial statistics have a six-hour retry cooldown;
  complete observations are retained. Full historical queues are not replaced.
- Explicit update jobs now coexist with stats backfills and deduplicate against
  other updates. Historical processing yields at an item boundary when an update
  is queued, leaving completed items and counters untouched. Update jobs are
  selected before the retained running historical job.
- An older scheduled archive row cannot overwrite a verified live score/status;
  a finished record cannot regress to live. No recommendations are fabricated.
- Additive nullable columns: `live_minute`, `live_checked_at`,
  `status_observed_at`. Existing match IDs, jobs, items and statistics remain intact.
- Diagnostics exposes the latest stats-backfill counters for each enabled league;
  the existing coverage table shows those actual counters alongside corner/card
  coverage. Match rows/detail show a minute only when one was reported.

This branch also includes the prior tested API-timeout changes because latest
master did not contain them: child-process isolation, bounded web/scraper pools,
short-lived read caches, batched prediction queries, safe transaction boundaries,
memory backpressure and reduced frontend polling. See `production-api-timeouts.md`.

## Validation

Regression tests cover the captured Galatasaray fixture, scheduled → live →
finished persistence, no kickoff-derived state/minute, identity validation,
independent partial statistics, disabled leagues, all-eight-league rotation,
backfill checkpoint preservation, unsuppressed/deduplicated update jobs and
concurrent HTTP responsiveness during source waits. PostgreSQL CI runs both the
live-update concurrency test and the inherited 10,161-match workload test.
Chromium verifies actual per-league counters, desktop/mobile layout and error
states. Source-transition variants are explicitly synthetic tests; the captured
0–0/Ongoing fixture is the real observation.

## Production verification plan — no automatic deployment

1. Review the branch and CI. Keep the existing free Render/PostgreSQL services,
   Python 3.12.8, one Gunicorn worker, `APP_ENV=production`,
   `SCRAPER_WORKER_ENABLED=true`, and the existing database/token variables.
2. After a separately authorized deployment, allow additive initialization to
   complete. Do not reset jobs or enqueue a replacement historical backfill.
3. Confirm `[LIVE]` logs and `status_observed_at` updates. Check match 5959 against
   Goaloo's **then-current** status/score; if it has finished, it should be in
   history, not the live list. NULL minute is correct if Goaloo reports no minute.
4. During a running stats backfill, observe at least three priority cycles. Verify
   actual live fixtures update near the target cadence, finished fixtures stop
   intensive polling, and historical item counters continue increasing.
5. Submit one authorized league update during backfill and confirm its job kind
   is `update`, its ID differs from the stats job, and historical progress resumes.
6. Verify recent corner/card coverage grows across all eight enabled leagues, with
   missing red cards still NULL and insufficient models explicitly unavailable.
   Confirm completed EPL checkpoints remain unchanged and La Liga resumes saved
   items. Monitor API latency and memory while source activity continues.

No production writes, job resets, merge or deployment were performed for this fix.
