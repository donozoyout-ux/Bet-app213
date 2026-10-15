# Automatic prediction settlement — 15 October 2026

## Production evidence (read only)

Read-only Render PostgreSQL inspection at **2026-10-15 11:11:25.830957 UTC**
(14:11 Turkey), joining immutable prediction snapshots to fixtures and final statistics.
Date ranges below refer to fixture kickoff in Europe/Istanbul, not snapshot creation.

| Kickoff date | Saved fixtures | Saved picks | Current outcomes | Final data |
| --- | ---: | ---: | --- | --- |
| 13 October | 3 | 6 | 4 won, 2 lost | FT, HT, corners and yellow cards present |
| 14 October | 3 | 6 | 4 won, 2 lost | FT, HT, corners and yellow cards present |
| 15 October | 5 | 10 | 10 pending | All kickoffs still in the future at inspection |

The selected production records **do not reproduce a stuck historical settlement**.
The 15 October picks must remain pending at this observation time. The six finished
fixtures have no red-card totals, but their twelve saved picks are goal/half-goal
markets and do not require those fields. Missing reds are not zero.

Across all saved fixtures, 61 were finished, 67 scheduled (none overdue at inspection),
and one abandoned. The public `/api/market-performance?limit=100` returned HTTP 200:
276 saved picks, 120 settled (92 won / 28 lost), 155 pending, zero awaiting data and
one void. The summary covers all saved picks; the response item list is paginated.

The reduced, secret-free production extract is committed as
`tests/fixtures/settlement_production_20261015.json`. A deterministic replay verifies
8 won / 4 lost / 10 pending against the recorded observation time and original
market, selection and probability. This is a replay of real observed records,
not a claim that production was modified.

## Confirmed implementation gaps

1. `parse_scoreboard` discarded Goaloo's explicitly labelled `Score 1st Half`.
   Thus fixtures discovered live could finish with FT but missing HT until the
   separate schedule archive refreshed. The actual page
   https://www.goaloo.com/match/live-3073124 was inspected: FT 3–0 and labelled HT
   0–0 are present. Its identity/scoreboard excerpt is a parser regression fixture.
2. Finished fixtures leave the intensive live cycle. Its final-statistics companion
   cannot repair missing HT and selected only the newest missing match per league
   before checking cooldown. A blocked newest fixture could delay older fixtures.
3. A partial final statistics page was retried every six hours indefinitely. Empty
   successful responses had no per-fixture final-data attempt budget.
4. Performance already recalculated results from immutable picks on every fresh
   read; it did not need a new stored outcome or prediction rewrite. The frontend
   merged `pending` and `awaiting_data` counts and displayed no check/source audit.
5. Inconsistent half/full scores incorrectly voided a valid pick. They now await
   corrected data. True pushes and cancelled/abandoned/postponed matches retain
   the existing void handling. Unsupported/malformed selections await data rather
   than crashing the history endpoint or pretending a quarter handicap is binary.

## Repair and operational limits

The existing advisory-lock-owning scraper checks one saved unresolved fixture per
30-second priority interval. A scan visits at most 64 rows, advances an ID cursor,
and wraps, so a failed fixture cannot block another. Only enabled leagues, valid
prematch snapshots and kickoffs at least three hours ago within 30 days qualify.
The source still must explicitly report a final state before settlement; elapsed
time alone never finishes a match.

Each match has **at most six automatic repair attempts**, durably claimed in
`collector_retries` before HTTP. Delays are 5 minutes, 15 minutes, one hour, six
hours and one day; the sixth incomplete check exhausts the budget. Restarts and
partial/missing successful pages do not reset it. A request has an eight-second
budget and holds no database connection. Intensive live polling and recent-stat
polling yield tracked unresolved overdue fixtures to this bounded queue. Once all
saved picks settle, ordinary statistics coverage collection remains available;
the repair budget does not freeze unrelated model input fields. Explicit historical
backfill jobs keep their existing behavior and checkpoints.

Final FT storage survives a missing/changed statistics table. Statistics require
verified identity and an explicit final flag; missing numbers remain NULL. Saved
pick definitions, probabilities, model version and capture time are unchanged.
After six unsuccessful attempts the row stays **Veri Bekleniyor** with the exhausted
state visible. A real final arriving later through another existing collector still
settles it automatically on the next fresh read. No retry/reset admin endpoint,
new scheduler, or production data migration is introduced.

Audit metadata uses the existing match JSON plus existing source timestamps:
last check, source, attempt count, next check and exhausted/completed state.
Legacy rows without a recorded source observation honestly show an absent source;
we do not invent historical observation times. `final_observed_at` is the existing
final score/statistics observation timestamp, not a fabricated per-field timestamp.
The existing PostgreSQL performance read cache expires after 30 seconds; the visible
page also refreshes periodically. There is no persistent outcome cache to rewrite.

## Before/after validation

| Scenario | Before | After |
| --- | --- | --- |
| Verified FT with labelled HT available at source | HT discarded; half pick awaits data | HT stored; second-half pick automatically won with actual total 2 |
| Final page without red-card row | Unknown | Still unknown; explicit Veri Bekleniyor and bounded retry audit |
| Six partial/failing checks and worker restarts | No final-data attempt budget | Exactly six calls; no seventh automatic repair call |
| Invalid HT greater than FT | Void | Veri Bekleniyor until corrected |
| Future kickoff with erroneous finished flag | Could settle on that flag | API remains pending; repair performs no early HTTP |
| Production October 13–15 replay | 8 won / 4 lost / 10 pending | Same outcomes; no production writes or deploy |

Tests cover every requested market individually, genuine zero versus missing and
negative values, provisional versus final data, source identity, malformed selections,
real scoreboard fallback, cooldown fairness, durable exhaustion, automatic settlement
after data arrives and immutable snapshots. The PostgreSQL version runs against an
isolated schema in CI. Chromium verifies populated history, Turkish reasons, check
and source columns, and no document-level horizontal overflow. Node runtime tests
also exercise separate pending/data-waiting counts and exhausted retry display.

Coupon generation, quality thresholds, Telegram scheduling/idempotency and credentials
are unchanged. No Telegram message, merge or Render deployment was performed.
