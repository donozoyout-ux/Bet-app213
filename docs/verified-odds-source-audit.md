# Verified odds coverage — production audit (2026-10-10)

This is a source-coverage audit, not a claim that new Goaloo market endpoints exist.

## Confirmed stored odds
Tables: `odds_1x2`, `asian_handicap`, `asian_totals`.
For each Crown, Bet365, Sbobet: 9,894 rows in each table; approximately 7,381–7,408 rows per market/bookmaker have non-NULL latest price.
Current Goaloo odds parser in `src/scrapers/goaloo/odds.py` handles exactly `1x2`, `ah`, `ou`. It does **not** demonstrate collection of half-time/second-half OU, corners, or cards odds.

## Production prediction coverage snapshot
| Market | Recommended picks | Verified matched prices |
| --- | ---: | ---: |
| corners | 52 | 0 |
| second_half_goals | 39 | 0 |
| yellow_cards | 39 | 0 |
| first_half_goals | 24 | 0 |
| goals (full time) | 3 | 0 |
| result 1X2 | 2 | 2 |
| **Total** | **159** | **2** |

Full-time goal candidates are not priced automatically if their selection's exact OU line is absent. One stored `AsianTotals` row per bookmaker and match contains opening/latest/closing *selected* line, not arbitrary full ladders.

`src/analytics/odds_matching.py` correctly refuses to substitute full-time prices for first-/second-half prices. Although `period_odds_rows` is a parameter, matching for half-goal markets is not implemented; this must only be implemented once verified period-specific source records exist.

## Required source verification before implementation
1. Capture genuine Goaloo responses (URL, response type, timestamps, season, bookmaker, match ID). Verify legally accessible half-time/second-half OU, corner and card odds — or explicitly record that the source does not offer them.
2. Document all observed line, period, side, price-stage and source time fields. Preserve the original raw payload and bookmaker source identity.
3. Extend parsing/storage only for proven source fields and use exact `match_id`, period, line, side, bookmaker and observation cutoff; never infer missing odds, never use the full-time OU price for a half.
4. For full-time OU, determine whether full alternate line ladders are available from verified Goaloo history; test exact matching and time cutoff.
5. Distinguish no-source-market, no-verified-line, stale price and unsupported bookmaker in coverage diagnostics.
6. Validate on PostgreSQL and Chromium, then compare priced selections before/after. No deployment without green CI.

## Reproducible coverage SQL
```sql
SELECT rec->>'market' AS market,
 count(*) AS recommendations,
 count(*) FILTER (WHERE rec->'verified_odds'->>'has_odds'='true') AS priced
FROM prediction_boards b
CROSS JOIN LATERAL jsonb_array_elements(b.payload::jsonb->'items') item
CROSS JOIN LATERAL jsonb_array_elements(item->'recommendations') rec
GROUP BY 1 ORDER BY recommendations DESC;
```

Issue: https://github.com/donozoyout-ux/Bet-app213/issues/15
