# Issue 95: supervised Polish and Danish archive repair

The existing importer skips fixtures already discovered by daily updates. As a
result, a league can have many final scores but little three-book archived odds.
This repair fills only missing opening/closing fields on existing finished
Polish (Goaloo 6) and Danish (7) fixtures dated from 2024. It does not discover or
create matches, enable leagues, update latest quotes, rewrite statistics or
scores, or publish predictions/coupons.

## Read-only baseline, 2026-10-17 UTC

Exact counts: 14,796 matches, 197 prediction snapshots, 102 saved coupons.
Application database: 494,687,935 bytes; aggregate database allocation:
518,088,268 bytes. These SQL sizes exclude WAL and filesystem overhead.
Twenty `paused_disabled` jobs and their checkpoints are retained.

At 19:02 UTC six queued/running statistics jobs were present: Belgium 2020
(256/510), Scotland 2021 (24/384), Albania 2042 (17/207, running), Albania 2043
(2/229), Poland 2044 (13/366), Denmark 2046 (12/225). Progress between inspections
confirms the worker is alive. Archive repair uses the independent supervised
import lock and does not take the worker's scheduler lock or enqueue more jobs.

Actual field audit, ignoring the cached `odds_complete` flag:

| Goaloo | Season | Fixtures | Finished | All three books, opening/closing 1X2+AH+OU |
|---|---|---:|---:|---:|
| 6 | 2023-2024 (2024 dates) | 25 | 25 | 25 |
| 6 | 2025-2026 | 306 | 306 | 0 |
| 6 | 2026-2027 | 306 | 92 | 26 |
| 7 | 2023-2024 (2024 dates) | 25 | 25 | 25 |
| 7 | 2025-2026 | 193 | 193 | 0 |
| 7 | 2026-2027 | 132 | 61 | 25 |

Thus 51/637 Polish fixtures (51/423 finished), 50/350 Danish fixtures
(50/279 finished) initially satisfy the full 54-field test. Fixture discovery
is not verified bookmaker coverage. Existing statistics workers may improve
these counts independently during the repair.

`pg_stat_user_tables` estimates: matches 1,034 dead tuples; 1X2 2,120; AH 2,048;
OU 2,580; job items 482. Table/index allocations respectively: matches
6,070,272/2,449,408; 1X2 15,769,600/2,596,864; AH 15,728,640/2,605,056;
OU 15,802,368/2,637,824; job items 1,769,472/1,720,320 bytes.
Dead-tuple estimates are not a measurement of exact recoverable bloat. No
VACUUM FULL, reindex, deletion, schema initialization or storage resize is run.

## Safety and operation

Manual `Supervised Europe history batch`: select `leagues=6,7`, `limit=25`,
`fill_existing_odds=true`, optionally `batches=1..4`. Every batch considers at
most 25 existing matches per league. Never combine this mode with activation
or Albania pilot flags. The existing `europe_bounded` jobs are reused and stay
`manual_paused`; admission proof remains in their discovery metadata.

Source season/league identity, fixture ID, both teams, kickoff and known FT/HT
scores must agree before quotes are used. Network calls occur outside database
transactions. PostgreSQL atomic conditional upserts preserve every non-NULL
price, latest quote and existing observation timestamp. AH/OU stage fills must
agree with the stored line and side prices; a missing line attached to old
prices is never guessed. Zero is a valid handicap line.

Source omissions are recorded as terminal `source_unavailable`; conflicting
fixtures/price stages as `source_conflict`. Transport/schema failures receive
at most three attempts, retain their error evidence, and three consecutive
failures stop the run. Terminal gaps are excluded from automatic retries;
deliberate source re-review is required. Counters are recomputed from durable
items, so retries cannot inflate completed work. Each match commits its
checkpoint; re-dispatch resumes without duplicate odds or matches.

Capacity is checked before each write; aggregate database allocation >=650 MB
pauses this repair early, while the shared 700 MB hard guard remains. Live
health is checked between matches, and prediction/coupon/live read APIs before
and after each league. API errors or five-second timeouts stop further work.
Existing statistics job priorities, coupon thresholds and quote freshness
requirements are unchanged. Workflows clear Telegram/NVIDIA credentials.

API timing samples are empirical observations, not an uptime guarantee.
Production completion evidence is appended after supervised execution.
