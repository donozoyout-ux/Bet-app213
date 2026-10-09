# Production API timeout incident — 2026-10-09

## Evidence collected without production writes

Production: `https://bet-app213.onrender.com`, 10,161 matches. Render workspace and
database checks were read-only. No jobs were stopped/reset and no deployment was made.

Sequential HTTPS observations (seconds, single sample each; include network latency):

| Endpoint | Result | Seconds |
|---|---|---:|
| `/health` | 200 | 1.665 |
| `/api/status` | 200 | 1.616 |
| `/api/diagnostics` | 200 | 3.088 |
| `/api/leagues` | 200 | 0.390 |
| `/api/matches?limit=1` | 200 | 0.599 |
| `/api/predictions/best?limit=1` | client timeout | >20.001 |
| `/api/market-performance?limit=1` | 200 | 1.099 |

Real production Chromium: document 200 in 4.121 seconds, no JavaScript exceptions;
the homepage prediction request did not complete before the frontend timeout and
displayed “API kullanılamıyor”. Multiple scraper polling requests occurred during
the 17-second observation. These observations are of the **old deployed code**.

Render logs confirm repeated `diagnostics unavailable` / `TimeoutError` at the
five-second application deadline. PostgreSQL activity included five connections
idle in transactions. A later snapshot showed transactions aged 25.3 and 25.9
seconds, waiting on `ClientRead`, with no blocking PIDs. This confirms clients
holding transactions, not a demonstrated database lock queue. Pool exhaustion was
not directly observed; it is a risk amplified by these long holds.

Read-only `EXPLAIN (ANALYZE, BUFFERS)` samples of the endpoint query shapes:

| Query shape | Execution ms | Plan observation |
|---|---:|---|
| enabled league list | 0.143 | scan of 30 small league rows, eight retained |
| upcoming matches | 3.868 | kickoff index, league PK/memoize |
| league coverage join/group | 7.793 | matches scan, stats PK index-only scan |
| EPL historical model window | 4.071 | kickoff index; 6,622 other rows filtered |
| match count / last scrape | 101.442 | scan of 10,161 rows, timing varied under load |
| representative revision counts/MAX | 373.883 | repeated matches/odds/stats scans |
| performance settlement join | 0.733 | snapshot scan, match/stats PK lookups |

These are component query timings, not full endpoint execution plans or guarantees.
Prediction and best-prediction routes share the same model/history calculation.
No broad index rewrite is justified by these plans. The new composite match index
specifically avoids filtering other leagues from the historical model window.

Render reported a 0.15 CPU quota and 512 MiB memory limit. Memory samples reached
approximately 298 MB; collected samples do not prove OOM. CPU samples are coarse
and do not prove event-loop stalls. The code does confirm that synchronous parsing
and prediction calculations previously shared the HTTP event loop.

## Causes addressed

The confirmed application bottlenecks are compounded work and contention: the
scraper shares HTTP execution and its pool, holds transactions over source fetches,
prediction boards issue per-match reads, diagnostics repeat aggregates, all commits
invalidate the model cache (including job progress), and overlapping frontend
pollers generate avoidable traffic. Increasing the old five-second timeout alone
would not address these causes.

Changes:

- Production scraping runs in a supervised, low-priority Python child on the same
  free web instance. There is no new Render service. Existing PostgreSQL advisory
  locking still permits only one heavy job. SIGTERM cancels work at durable
  boundaries; running items resume after restart. Child exits are retried.
- Separate bounded pools: HTTP five connections, scraper two (lock + writer), no
  overflow. Checkout budget two seconds, connect budget five seconds, statement
  timeout 12 seconds and lock timeout two seconds. Source HTTP waits release the
  session connection. Scraper source concurrency is one in production.
- At 85% container memory use, the scraper waits before starting work or the next
  item. It neither deletes nor fails queued jobs. Cgroup v1/v2 supported.
- Bulk board reads, one-round-trip revision aggregates, deferred raw source JSON,
  and no model invalidation for job counters/snapshot commits. Result-model
  semantics, card NULL/zero rules, observation cutoffs and 0–3 picks are unchanged.
- Per-database bounded caches: summary 20 seconds, prediction/performance 30
  seconds, maximum 128 entries each. One calculation per cache at a time; waiters
  do not acquire DB connections. Failed/cancelled work is not cached. A successful
  snapshot can lag updates by its TTL; expired data is not served on error.
- Request/database time budgets return explicit 504/503 errors with session
  cleanup. Logs identify slow-query duration/operation without SQL parameters.
- Hidden tabs skip polling; the duplicate match-list timer is removed; identical
  in-flight board/match requests are reused; progress polls fast only in its visible
  section. Timeout, unavailable DB, empty fixtures and insufficient history have
  distinct messages. Chromium verifies loading state clears after errors.

## Validation and rollout

The PostgreSQL integration test uses a disposable schema with exactly 10,161
matches across the eight enabled leagues, 3,000 stored statistics, 81 upcoming
matches and an active durable stats-backfill item. It issues eight concurrent HTTP
requests, checks all return 200 inside 12 seconds, verifies no scraper connection
is held during the source wait, and verifies completion/progress persists. The
source wait is controlled for reproducibility; DB queries and application routes
are real. CI prints cold and warm latency measurements with `pytest -q -rP`.

Existing PostgreSQL locking/resume/disabled-league tests, model tests, JavaScript
runtime tests, and Chromium desktop/mobile tests remain required. Local Windows
has no usable Docker daemon; CI supplies PostgreSQL 16 for the full suite.

Deployment requires an explicit later decision; do not merge/deploy this branch
automatically. After review:

1. Keep existing free Render service/database and `DATABASE_URL`,
   `SCRAPER_API_TOKEN`, `APP_ENV=production`, `SCRAPER_WORKER_ENABLED=true` and
   `AUTO_BACKFILL_ON_EMPTY=true`. Keep Python 3.12.8 and one Gunicorn worker with
   the repository start command. No paid worker or database is needed.
2. Use the existing additive initialization migration. The single new index is
   idempotent; its creation is bounded by the DB lock/statement budgets and retries
   on contention. No data migration, job reset, or new backfill enqueue is required.
3. Confirm EPL's completed progress is unchanged and La Liga resumes its saved
   unfinished items. Verify all eight enabled leagues remain enabled.
4. Repeat the production HTTP/Chromium probes during that running job. Observe
   API 504/503 counts, slow-query logs, pool status on errors, child restart logs,
   CPU/memory and backfill progress. Check cache freshness within 20–30 seconds.
5. If rollback is required, restore the previous application release; the added
   index is backward compatible. Durable jobs and stored data remain intact.

Until deployment, production still runs the incident code; local/CI improvements
are not evidence that production has recovered.
