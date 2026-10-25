# Settlement lifecycle and connection capacity (#134)

## Read-only production evidence

On 2026-10-25, after the reported 08:11 UTC incident, Render logs confirmed four
simultaneous SQLAlchemy pool timeouts on `/api/leagues`, `/api/bookmakers`,
`/api/coupons/daily` and `/api/dashboard/revision`:

```text
08:11:50.450 /api/leagues            TimeoutError pool_size=5 checked_out=5 idle_in_pool=0
08:11:50.451 /api/bookmakers         TimeoutError pool_size=5 checked_out=5 idle_in_pool=0
08:11:50.451 /api/coupons/daily      TimeoutError pool_size=5 checked_out=5 idle_in_pool=0
08:11:50.452 /api/dashboard/revision TimeoutError pool_size=5 checked_out=5 idle_in_pool=0
```

A later pg_stat_activity sample saw five idle `betapp-web` connections and three
`betapp-scraper` connections (two idle, one briefly idle in transaction). Idle
server connections are not proof of leaked or currently checked-out application
connections. No single long-running production query was identified by this
sample. The existing web pool allowed five concurrent general handlers; slow
queries, response cleanup and concurrent cache misses could consume every slot.
The new admission regression isolates this capacity failure rather than claiming
that a specific production query alone caused the incident.

The current primary still had 12,390 source matches, 97 immutable prematch
snapshots, zero invalid post-kickoff captures, **zero terminal captured matches**,
zero decisions and no latest settlement revision/time. The earliest captured
kickoff was 2026-10-25 11:15 UTC. At that observation, there was no finished
captured match to settle. These counts do not justify inventing historical
predictions or treating imported results as settled forecast outcomes.

Only read-only Render queries/logs and local/CI test writes were used. The paid
database, credentials, plans and Render configuration were not changed.
`HYBRID_HISTORY_ENABLED` remains false. No Telegram or NVIDIA sends are made.

## Web connection protection

The physical web pool stays at **five connections**, zero overflow. An ASGI
admission layer divides its existing capacity: three general API operations and
two lightweight metadata operations. Heavy callers wait before checking out a
connection. Metadata includes league/bookmaker catalogs, status, persisted daily
coupon reads, publication revision and runtime health. Requests exceeding the
two-second admission budget receive 503 `api_busy` and `Retry-After: 1`; this is
distinct from a database failure and does not mark the database unready.

Database dependencies use `scope='function'`: cleanup occurs before response
delivery. Admission is also released before response send, so slow clients do not
retain the SQL connection or the metadata gate after serialization. FastAPI's
[dependency-scope behavior](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/#early-exit-and-scope)
and [release notes](https://fastapi.tiangolo.com/release-notes/#01211) underpin the
minimum supported FastAPI version, now 0.121.1 (including the parameterless
function-scope fix). JSON API responses do not stream ORM data after cleanup.

League and bookmaker DTO catalogs have a 60-second single-flight cache. Cold
metadata reads remain covered by reserved admission. Transaction commits now
release the half-time/corner availability query before each Goaloo request in
market collection; valid half-time evidence is committed before corner retries.
Other existing source reads/commits and advisory-lock ownership are preserved.

This protection cannot manufacture capacity if the database itself is down or
connections are monopolized outside admitted HTTP work. General overload is
bounded explicitly, not solved by increasing server resources. Existing query
and request deadlines remain in place.

## Automatic settlement

The scraper runner starts an independent settlement task, no longer behind the
network-driven priority cycle. Its one-connection pool is split out of the former
two-connection learning allocation: web 5 + scraper 2 + learning 1 + settlement 1
= **9**, the same configured baseline maximum as before (5 + 2 + 2). Optional AI
uses one connection instead of two. There is no new Render service or paid plan.

The task checks every five seconds; a durable checkpoint schedules successful
sweeps 60 seconds apart. A sweep is limited to 100 captured terminal matches,
with a 20-second task deadline and a PostgreSQL transaction advisory lock. The
lock prevents duplicate checkpoint writers across processes. Retries use 10,
20, 40, then 60 seconds when a failure checkpoint can be persisted. A database
outage may also prevent checkpoint persistence; sanitized logs state that fact
and the in-process loop retries without advancing the cursor.

The cursor, successful observation, counts and first decisions commit together.
Failure/cancellation rolls the transaction back; restart resumes the last committed
cursor. A completed pass wraps to zero and revisits lower-ID matches that finish
later. Fully recorded snapshots are excluded from background candidates; missing
statistics stay eligible for a later pass. Stored live/statistics hooks still
settle immediately when they can; the independent sweep covers missed hooks and
bulk updates without being tied to provider HTTP latency.

Batch settlement fetches evidence and existing decisions in two bounded SELECTs,
defers raw source JSON, then inserts first terminal records in groups of 128 with
the existing unique conflict guard. It does not query decisions once per match.
Every result requires an immutable capture strictly before kickoff. It reads no
post-kickoff prices and creates no snapshots. Missing final statistics yield
awaiting-data in readers and no invented terminal loss. Frozen decisions are
idempotent under concurrency. Corrections remain explicit append-only revisions
through the existing trusted correction function; the worker does not silently
rewrite prior decisions. Reconciliation exposes disputes until corrected.

## Monitoring and additive migration v1

`Database.pool_health()` exposes bounded counters: checkout count, current and
maximum checked-out connections, longest hold, slow-hold count, pool/admission
timeouts and longest admission wait. Checkin/invalidation/cancellation releases
measurement state. Slow-hold logs include only a fixed role, duration and count;
no SQL parameters, DSN, credentials or exception text. The scraper's intentionally
long advisory-lock lease can contribute to its hold metric; inspect web and
settlement roles independently.

`GET /api/runtime-health` returns current-process pool metrics and a bounded
settlement checkpoint probe, even when the probe is unavailable. `/health`
remains process liveness. Chart diagnostics share the durable worker status,
adding one point lookup to their existing 60-second cached counts. A successful
probe is not a claim that future provider calls will succeed.

The additive schema change is `worker_checkpoints` v1. Normal idempotent
`Base.metadata.create_all` startup creates it with no alteration or deletion of
Match, odds, captures, decisions or audit tables. It stores attempted/last-success/
next-attempt timestamps, cursor, examined/written counts, failure count and sanitized
error class. Missing checkpoint is `warming`; an old or future successful timestamp
is not `ok`; repeated failures are `retrying`. A successful checkpoint older than
three intervals becomes `stale`.

Deploy only after review. Ensure normal initialization creates the new table
before checking runtime health, then inspect the first successful sweep and
connection-role metrics. Rollback to the old application leaves this additive
table harmlessly present. No destructive rollback or source-data restoration is
required. Do not remove captures or decisions to make counters appear healthy.

## Reproduction and validation

```sh
python -m pytest -q -rP tests/test_settlement_pool.py
```

With an isolated `TEST_POSTGRES_URL`, the contention regression first disables
admission in the test harness, holds five real SQL sessions and reproduces 503
for all four affected metadata endpoints. It then enables admission with the
same five-connection pool, holds the three general sessions and verifies all
four metadata requests return 200 in under 1.5 seconds. It prints actual before/
after status arrays and elapsed milliseconds for the CI/PR benchmark. This is
an isolated concurrency reproduction, not a production load test or egress claim.

The first isolated CI run on `7c54a436` measured 2,005.7 ms with four 503s
versus 20.8 ms with four 200s, retaining pool size five. Timing varies by runner;
this supports the controlled connection-capacity fix, not a production latency
guarantee. The full regression run also identified two outdated runner test
constructors, which were updated to verify the additional role and cleanup.

The same test blocks five response consumers after response start and verifies
zero checked-out SQL connections; it also runs five concurrent settlement sweeps
and verifies one successful checkpoint and one original decision per eligible
saved pick. It is part of the first PostgreSQL CI admission stage so failures
surface before the full regression package.

Other tests cover scheduled -> live -> finished, pending statistics, immutable
captures, independent sweep repair, cursor wrap, engine restart, append-only
correction, retry/backoff and failure rollback, returned-connection cancellation
metrics, unchanged total connection budget, and zero checkouts during both source
HTTP fetches. Home trends, Performance, diagnostics and Lessons agree on the
reconciled outcomes; a Chromium test shows the home timeline move from zero to
two real worker-written outcomes via GET refresh only. No UI mutation produces
the decisions.

Local Docker was unavailable; PostgreSQL-specific cases run against CI's isolated
database. SQLite lifecycle, browser and existing regression checks run locally.
Actual CI timing evidence and final totals are recorded in the pull request.

Chart comparisons continue to exclude the current Istanbul kickoff day. A
current-day settlement can appear in the ledger, diagnostics and Lessons before
entering the next day's completed-day chart. Lifecycle tests align the as-of
window before comparing chart counts; they do not backdate snapshots or odds.
