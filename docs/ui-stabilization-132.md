# UI stabilization (#132)

## Production evidence before changes

The public production site was inspected with Chromium/Playwright at 1536 and
390 px on 2026-10-25, beginning 07:17 UTC. Home, Performance and Lessons were
loaded independently. All page requests were restricted to GET/HEAD. The
[sanitized trace](ui-132/production-before.json) records API status, response shape,
panel visibility, script initialization, horizontal overflow and page exceptions.
It contains no fixture payloads, credentials or database connection strings.

On both widths, `/api/performance-trends` returned HTTP 200 with valid 60-day
daily data and 7/30-day comparisons. Both comparison periods contained **zero
saved selections and zero verified outcomes**. The Results script loaded, the
panels were visible, no horizontal overflow or page exceptions occurred, and
the chart containers had zero SVG children. The old renderers explicitly
returned before creating an SVG when no outcomes existed. Several independent
home requests returned 503 during the probe; these were genuine partial failures,
not the reason a successful empty trend response omitted its SVG.

[Read-only database counts](ui-132/production-counts.json) established:

* 12,390 matches, including 9,705 finished source fixtures;
* 97 immutable prematch snapshots, containing 191 raw saved selections;
* zero snapshots for finished matches, zero post-kickoff captures, zero snapshots
  before the current Istanbul kickoff date, and zero persisted result decisions;
* first capture 2026-10-24 20:35:46 UTC; last capture 2026-10-25 00:28:46 UTC.

Thus the new database has genuine forward-looking captures but no completed
captured-match cohort yet. Imported historical fixtures are not retrospective
predictions. No capture/settlement code defect was demonstrated by these counts;
there were no eligible finished captured matches to settle at this observation.
The existing settlement sweep selects terminal matches every 60 seconds. Its
heartbeat is not instrumented, so the new diagnostic explicitly reports that
limitation rather than asserting that the worker is healthy. The Render log
search since the new database's 2026-10-24 20:30 UTC creation window found no
`[SETTLEMENT]` messages. Older suspended-database errors were not attributed to
this database.

`/api/history/seasons` returned `enabled:false`: the hybrid-history flag remained
OFF. No Render variables, secrets, service plans or deployment settings changed.
No database rows were created, changed, deleted or backfilled in production.
Open issue title searches for `learning` and `ders` found no separate Lessons
issue. The existing #113 rules are retained: a missing success-rate observation
remains a gap, low sample size does not erase a real measurement, and statistical
evidence does not establish model learning.

## Resulting behavior

Home always draws a bounded 7/30-day count timeline for a valid complete cohort.
An all-zero cohort uses a neutral cyan baseline labeled **0 verified outcomes**,
with no fabricated success percentage, wins/loss dots or fake observations.
When outcomes exist, the real won/lost/settled three-line chart is preserved.
Daily summary text separates saved selections, pending and missing outcome data.
Fixture count is never used to decide whether to draw a result chart.

Performance preserves the compact weekly/daily success-rate line and gaps when
observations exist. An empty cohort gets a separate count timeline, never a
zero-percent success line. Both SVG viewports are 240 px desktop / 200 px mobile.
Truncated evidence does not become a zero total. API failure or malformed data
clears the chart and uses an unavailable state. A subsequent resize cannot
restore the last successful chart after an outage.

Shared schema validation checks bounded arrays, nonnegative integer counts,
won+lost consistency and real success rates before rendering. Timestamp badges
describe the **data check**, not live scores, prediction quality or learned model
improvement. Missing/future timestamps are unverified; stale checks and errors
have distinct text and colors. Existing report-stale metadata remains effective.

All three pages show the same read-only record coverage. Detail text is collapsed
by default to keep the timeline visible on mobile. It explains that today's and
future saved picks are outside completed-day charts. Learning report content
remains tied to its immutable report edition; the separate diagnostic describes
the current database and may therefore have newer counts. The Lessons warming
state now gives a useful explanation and a visible empty view. A sparse ready
report explicitly distinguishes saved picks from verified analytical observations.
No AI generation or model update is initiated by these views.

## Bounded diagnostics

`GET /api/chart-diagnostics` returns global fixture/finished counts and a bounded
sample of up to 5,000 newest snapshots: valid prematch captures, duplicate
selections excluded, unique saved selections, reconciled frozen outcomes,
7/30-day kickoff cohorts, old snapshots, current/future picks, capture timestamps
and last persisted decision. Sensitive provider JSON, identifiers, selections,
names, odds, credentials and job error messages are excluded from the response.

At most one fixture-count SELECT, one LIMITed snapshot/statistics SELECT and one
frozen-decision lookup are used. Existing reconciliation verifies decisions and
does not manufacture wins from a finished score without a persisted ledger.
PostgreSQL responses are cached for 60 seconds with single-flight computation.
The browser also shares one request across all three pages within that TTL.
No full scan is triggered by every chart draw, resize, tab or filter interaction.
When the cap is reached, the response and UI explicitly label a bounded sample;
they do not claim complete historical totals. Fixture counts are global, while
the charts may be filtered; the diagnostic names its unfiltered scope.

The diagnostic is not a durable worker heartbeat or a monitoring system. It does
not repair stale decisions or retroactively create picks. A future eligible
finished snapshot without a decision can be identified from these counts and
existing worker logs without mixing imported match history with forecast history.

## Validation and screenshots

Run with `HYBRID_HISTORY_ENABLED=false`, Telegram/NVIDIA disabled:

```sh
python -m pytest -q tests/test_ui_stabilization.py tests/test_daily_results_chart.py tests/test_performance_trends.py tests/test_learning_browser.py
```

Coverage includes 12,000 real synthetic fixture rows / 97 future captures / zero
results; frozen wins/losses, duplicate exclusions, invalid captures, disputed and
missing decisions; diagnostic truncation and concurrent cache requests; zero
fixtures in a separate diagnostic with real chart observations; 0/1/2/80 settled
observations; API 200 empty, 503 and malformed shapes; Home -> Performance ->
Lessons navigation; unavailable -> resize; 7/30 days; console errors, GET-only
requests, lazy initialization, script loading and 390/1536 px layouts. Existing
graph/learning regressions and JavaScript runtime checks are retained.

Before screenshots are **actual production**, after screenshots are **local
synthetic acceptance fixtures**, not an assertion of deployed behavior. After
captures hide fixed header/navigation overlays only during image capture so a
whole panel can be reviewed. The normal responsive UI remains in the browser tests.

| View | Production before | Local after |
|---|---|---|
| Home, mobile | [before](ui-132/before-home-390.png) | [after](ui-132/after-home-390.png) |
| Home, desktop | [before](ui-132/before-home-1536.png) | [after](ui-132/after-home-1536.png) |
| Performance, mobile | [before](ui-132/before-performance-390.png) | [after](ui-132/after-performance-390.png) |
| Performance, desktop | [before](ui-132/before-performance-1536.png) | [after](ui-132/after-performance-1536.png) |
| Lessons, mobile | [before](ui-132/before-lessons-390.png) | [after](ui-132/after-lessons-390.png) |
| Lessons, desktop | [before](ui-132/before-lessons-1536.png) | [after](ui-132/after-lessons-1536.png) |

This change requires no migration and does not enable hybrid history. Rollback is
the previous application revision; there is no data transformation to undo.
No merge or deployment is performed as part of implementation.
