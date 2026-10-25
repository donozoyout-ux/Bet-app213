# Issue 104: measured read-path improvements

Scope note: during implementation, the GitHub issue was rewritten as a historical
match/prediction audit. This report and PR implement the user's explicit UI/API
speed request in this conversation. The revised issue is not closed by this PR.

## Scope and coordination

Base: master `e45307b5aa177352a94f862e37e5e5e5bef3f3ce`. Independent of open
archive PR #101 and capacity PR #103; neither is merged or superseded here.
No deployment, migration, new cache, index, paid resource, historical import,
Telegram/NVIDIA request, or production record deletion is part of this change.

Three targeted areas:

* Upcoming list reuses the window count instead of repeating it. List rows defer
  `Match.raw` (source evidence remains available on match detail). Fallback to the
  nearest fixture day, filters, pagination, sorting and response shape remain.
* Daily coupon validity uses one narrow left join for current match status and
  board expiry. Every request still checks kickoff, rescheduling, quote validity
  and board expiry. No cached selectable coupon can outlive those checks.
* Home's live panel stops requesting unused odds. Match-center league/bookmaker
  controls initialize on navigation rather than home startup. Browser capture
  previously observed two `/api/leagues` calls and an unused `/api/bookmakers`.
  Home startup API requests fall from 13 to 11. For a nonempty live panel its
  odds queries fall from four to zero. Existing six home prediction cards remain
  below the eight-card budget; existing cancellation and expiry behavior remain.

Prediction reads already consume published boards and batch current prices;
they do not need to recompute a model. This sprint deliberately avoids caching
live price eligibility or changing ranking to improve a timing number.

## Live baseline (before deployment)

2026-10-18 19:19 UTC, 21 requests per route (first + 20 warm), concurrency one,
250ms pause, 20s timeout, abort on first HTTP error. 147 GETs, zero errors/504s.
Raw aggregate results: [issue-104-live-baseline.json](issue-104-live-baseline.json).
No response bodies or credentials are retained. Live SQL counts are unavailable
from HTTP; counts below are instrumented test measurements instead.

| Endpoint | First ms | Warm median ms | Warm p95 ms |
|---|---:|---:|---:|
| Matches + odds | 1299.46 | 460.15 | 842.92 |
| Predictions | 1647.63 | 1080.13 | 3033.97 |
| Best predictions | 1386.31 | 1019.75 | 1536.44 |
| Daily coupon | 219.87 | 115.40 | 244.90 |
| Revision | 956.63 | 136.52 | 646.72 |
| Market performance | 3139.90 | 69.10 | 149.07 |
| Match detail | 72.58 | 80.45 | 342.58 |

The first request is **not a verified Render cold start**: server cache and idle
state were unknown, and no restart/cache flush was requested. HTTP totals include
network/Render overhead; they cannot be attributed solely to SQL.

Render 18:20–19:20 UTC: CPU 0.091–0.097 cores, memory 325.36–339.40 MB.
Provider latency series was empty. These are baseline resource observations,
not evidence of post-deployment improvement.

Read-only PostgreSQL at 19:19:19 UTC: 14,796 matches, zero queued/running jobs,
application 696,268,479 bytes, all databases 719,668,812 bytes. Aggregate usage
already exceeds 700,000,000 bytes. No historical collection was started.
An EXPLAIN ANALYZE of the bounded four-match validity join used `matches_pkey`
and `prediction_boards_pkey`, with no temporary spill. Total 14.762ms included
fixture-ID discovery (11,303 older rows filtered by kickoff scan); the actual
application passes known coupon IDs and does not perform that discovery.

## Repeatable local and CI evidence

`tests/test_perceived_performance.py` uses `src/query_profile.py` and cursor
instrumentation, synthetic SQLite and isolated PostgreSQL schemas. It checks
exact SQL budgets, large unused source JSON, upcoming fallback/empty results,
coupon output parity and immediate invalidation after kickoff/price expiry.
The endpoint sample prints first, 20 warm median/p95, query range and errors for
all seven endpoints. Wall-clock values are observations, not CI pass thresholds.
Existing 10,161-match PostgreSQL concurrency, cancellation/cache isolation,
publication/no-model-on-read and mobile browser tests also run in the full suite.

Before changing handlers, the new budget tests failed at **3** queries for both
list and daily coupon. After: **2** each. With list odds: **7 to 6**.

Local Windows/Python 3.12 SQLite ASGI comparison used an isolated export of the
base commit and the same synthetic endpoint sample (21 requests each). The
baseline sample's budget expectations were 7/3, current expectations 6/2.

| Workload | Before median / p95 ms | After median / p95 ms | SQL before → after |
|---|---:|---:|---:|
| Upcoming + odds | 12.07 / 12.66 | 10.10 / 10.57 | 7 → 6 |
| Daily coupon | 8.97 / 9.35 | 7.16 / 8.77 | 3 → 2 |

[Full local samples and asset byte counts](issue-104-local-samples.json).
The workload is small and SQLite does not use PostgreSQL production caches;
these timings are not a promised production speedup. Unchanged best-predictions
p95 varied from 14.38 to 17.22ms, illustrating timing noise. No after-deploy
latency, memory or storage claim is made. No new persisted data/cache is added.

Browser tests cover 320–3840px widths, unchanged layout, coupon navigation,
bounded cards, one league request and lazy bookmaker controls. Browser clock
fixtures returned an empty resource-timing list; static asset byte counts are
recorded rather than inventing download/paint measurements. Mobile scrolling
and card interaction retain existing browser regression coverage; no FPS gain
is claimed without a real-device profile.

Reproduce live baseline only when authorized and service healthy:

```sh
python scripts/benchmark_read_api.py --base-url https://bet-app213.onrender.com --samples 21 --output baseline.json
python -m pytest tests/test_perceived_performance.py -q -rP
python -m pytest -q -rP
```

## Code review record

Author review of the diff checked: count reuse only for the exact nonempty
window; unmodified fallback count for empty windows; raw evidence still in
detail; left join preserves missing-board rejection; expiry checked per read;
no cached odds; lazy controls initialize on match-center navigation; no source
or archived-record write path changed. This is author review, not an independent
reviewer approval. CI evidence is recorded on the PR. Owner approval is required
before merge/deploy; production comparison remains a follow-up after that approval.
