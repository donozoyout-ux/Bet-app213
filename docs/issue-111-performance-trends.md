# Issue #111 — real kickoff performance cohorts

## Read-only production observation (2026-10-19, Europe/Istanbul)
Six GET pages of the existing public market-performance API returned a consistent total of 505 saved picks. This was sequential, not an atomic PostgreSQL snapshot. The new analytics were replayed offline on the deployed API outcomes. Aggregates only are in `issue-111-production-observation.json`; no raw personal/source records were published.

As of 2026-10-19, excluding that day:
- Recent 7 days: 110/147 = 74.83%, 76 distinct decided matches, Brier 0.1958, **28 disputed** selections.
- Previous nonoverlapping 7 days: 78/98 = 79.59%, 50 matches, Brier 0.1607.
- Raw change: -4.76 percentage points, not evidence of deterioration or learning: incomplete disputed evidence, selection mix and correlation matter.
- Recent 30 days: 188/245 = 76.73%, 126 matches; previous 30 days have **zero decided records**. There is no valid 30-day improvement comparison.
- Four recorded version labels: quality-poisson-v1, quality-poisson-v2-cards, quality-poisson-v3-half-goals, quality-poisson-v3-specialist-adapters-v1. Labels alone do not attest weight/calibration updates. The screen explicitly says model change is not verified.

This prediction record begins in October 2026, not 2024. Historical fixture availability is not a prediction track record. No ROI or profit trend is calculated. Production data may change after observation; delayed results revise their original kickoff cohort, not their settlement-date cohort.

## Method and bounds
`GET /api/performance-trends` uses 60 kickoff days before explicit `as_of` in Europe/Istanbul. Today is excluded by default. 7 vs previous 7 and 30 vs previous 30 never overlap. Its date control is independent of the older table's capture-date filters; league, market, exact selection and confidence filters are shared. Disabled leagues remain auditable in this endpoint. Only recorded, strictly prematch snapshots are evaluated; repeated market/selection pairs are excluded consistently.

At least 30 decided predictions, 30 distinct matches **and 30 effective match clusters** are required per period. Counts of pending/missing/disputed/void are separate; unresolved evidence blocks a credible comparison. Wilson intervals are supplied, and the screen uses conservative match-cluster Hoeffding 95% bounds with effective sample n²/sum(cluster size²), allowing arbitrary dependence within one match. This does not eliminate repeated-team dependence. A separated interval is only a descriptive signal, never proof of sustained improvement or learning. Probability-valid cohorts alone enter mean probability, observed calibration and Brier; the same valid denominator is shown.

Daily insufficient/incomplete cohorts have null chart values and gaps, never invented zeroes. Breakdown counts by league, market and version reveal mix changes; these are unadjusted comparisons, not a causal model evaluation. No separate automated learning or model promotion occurs.

At most 5001 snapshot/match rows are fetched over a bounded kickoff range; only 5000 are evaluated. A cap warning suppresses comparison eligibility and all chart points. Two batched SELECTs, no N+1, deferred raw payloads, shared 30-second performance cache. No migrations, writes, queue changes, backfills, Telegram/NVIDIA, new services, merge or deploy.

## Verification
Synthetic tests cover upward/downward/flat cohorts, 29/30 boundaries, clustering, missing/disputed/void, invalid probabilities, midnight conversion, delayed settlement, date/selection filters, immutable history regression, query count/cache and mobile/desktop rendering. Screenshots under `issue-111-screenshots` are local synthetic ASGI fixtures, not production screen claims.

Run `python -m pytest tests/test_performance_trends.py tests/test_performance_samples.py tests/test_performance_status.py tests/test_market_terminal.py -q` and `node tests/market_runtime.cjs`. Set `UI_SCREENSHOT_DIR=docs/issue-111-screenshots` while running `tests/test_performance_trends.py` to reproduce images. PostgreSQL-enabled full CI must pass; PR remains for review without merge/deploy.

Production observation limitation: the deployed public market-performance API scopes enabled leagues and does not expose decision timestamps. The new endpoint also retains disabled-league history and counts decisions recorded after the kickoff calendar day, with a separate timestamp-available denominator. Production replay cannot validate those extra records/timestamp counts. This calendar-day indicator is descriptive and does not assert an erroneous delay.
