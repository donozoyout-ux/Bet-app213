# Corner/card production pipeline audit — 2026-10-09

Read-only checks against `https://bet-app213.onrender.com` and its Render PostgreSQL database. No production jobs, database writes, merge or deployment were performed during this audit.

## Measured production state

There are 10,161 stored matches, including 8,051 in the eight enabled leagues. The disabled leagues' data remains stored. The worker is enabled but idle; the enabled leagues have eight finished/partial **odds backfill** jobs and no statistics backfill jobs.

| League | Finished matches | Statistics rows | Final corners | Complete final cards | Corner coverage | Card coverage |
|---|---:|---:|---:|---:|---:|---:|
| Premier League | 810 | 0 | 0 | 0 | 0% | 0% |
| La Liga | 829 | 0 | 0 | 0 | 0% | 0% |
| Serie A | 810 | 0 | 0 | 0 | 0% | 0% |
| Bundesliga | 648 | 0 | 0 | 0 | 0% | 0% |
| Ligue 1 | 657 | 0 | 0 | 0 | 0% | 0% |
| Süper Lig | 702 | 0 | 0 | 0 | 0% | 0% |
| Eredivisie | 681 | 0 | 0 | 0 | 0% | 0% |
| Primeira Liga | 674 | 23 | 23 | 7 | 3.41% | 1.04% |

Coverage denominator is finished matches, not scheduled fixtures. Complete cards require both teams' yellow **and** red totals. Unknown red totals are not zero.

## Root causes and changes

1. Historical fixtures were not covered by a dedicated statistics job. An odds backfill is not evidence of statistics coverage: completed odds fixtures are skipped by later odds discovery. The existing statistics rollout was explicit-only, and could return an existing odds job in place of a stats job or an old completed stats job indefinitely.
   - The worker now reconciles missing statistics every 60 seconds when PostgreSQL and `AUTO_BACKFILL_ON_EMPTY=true` are enabled. Stats jobs are independent of odds jobs and survive restarts. Explicit authenticated rollout remains available.
   - Only enabled leagues in the eight-league allowlist are queued, in the requested order. Recent finished fixtures are collected first within each league. The existing global PostgreSQL worker advisory lock keeps heavy execution serial.
   - Active stats jobs are reused. Failed items resume without rerunning completed items. Automatic failure retries wait one day. Previously observed incomplete/unavailable records retry after seven days. Complete final records are preserved and skipped. New missing fixtures can create a new job after an earlier job completed.
   - Disabled queued/running jobs are excluded from selection. Disabling a league pauses an executing job at the next round/item boundary.
2. An odds-fetch failure previously bypassed statistics entirely; a later history-fetch failure could roll back previously stored odds/statistics.
   - Odds, statistics and bookmaker histories now commit their valid observations independently. Missing/malformed individual markets do not discard other valid markets. Result fields already known are preserved when absent in a later source response. Statistics-only jobs never depend on bookmaker coverage.
3. Empty filtered prediction lists hid the distinction between insufficient data, quality rejection, ranking and user filters.
   - Both prediction list APIs now include `availability`, with per-market model sample counts, required samples, qualification and selection states. Match statistics include `market_availability`; candidate rejection reasons are exposed.
   - Diagnostics provide `stats_coverage_by_league`, `active_stats_backfill` and `queued_stats_backfills`. The dashboard displays per-league coverage and explains empty market lists. Performance responses include coverage and explicitly distinguish no saved prematch picks.

## Real stored examples and recommendation limits

Primeira Liga internal match **7793**, Goaloo **3023476**, Viseu–Vitoria Guimaraes, September 12:

- Stored final corners: **4–8**.
- Stored yellows: **4–3**; explicit reds: **0–1**; combined cards: **8**.
- Re-fetching `https://www.goaloo.com/match/live-3023476` returned the same counts.
- EPL source match `2590898` also returns real corners **7–8** and yellows **2–3**, but no red totals. Its corners are usable; its combined card total is unknown.

The production prediction scan evaluated **75 upcoming matches**. No strong corner/card recommendations qualify with the current stored coverage. For the next Primeira Liga match (internal **7809**), the corner model sees league=23, home=2, away=3, home venue=1, away venue=1. The card model sees league=7, home=0, away=0, both venues=0. The other seven leagues have no count history at all.

Model minimums remain league=20, each team=5, each venue=3. Strong recommendation gates remain league=50, each team=8, each venue=5, recent completeness≥60%, stability≥50%, probability≥60%, reliability≥70%, score≥74%. Selection keeps 0–3 strongest recommendations and one per correlation group. There is no market quota. Tests demonstrate both corner and card picks reaching both prediction APIs when these conditions hold; synthetic test inputs are not claimed as production recommendations.

All eight leagues still need statistics backfill. Even after full collection, combined card recommendations may remain unavailable where Goaloo omits explicit red totals. Do not fabricate those values or weaken the agreed card definition. Saved performance starts with actual published prematch picks; collecting old results must not invent old predictions or win rates.

## Rollout after code review

This branch requires deployment before the production behavior changes. Keep `DATABASE_URL`, `SCRAPER_WORKER_ENABLED=true` and `AUTO_BACKFILL_ON_EMPTY=true`. No new tables, migrations or environment variables are required. The existing authenticated `POST /api/scraper/stats-backfill-enabled` can explicitly enqueue/resume the rollout if automatic backfill is disabled; it uses a manually supplied Bearer token, never an HTML-embedded secret.

Watch `/api/diagnostics` for the active job and queued leagues, then inspect sample/rejection reasons from `/api/predictions?market=corners`, `/api/predictions?market=cards`, and `/api/matches/{id}/statistics`. Keep polling/backfills rate-limited. PostgreSQL integration tests use the isolated CI database, never production.
