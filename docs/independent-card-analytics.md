# Independent card analysis

The previous `cards` model correctly rejected histories with unknown reds, but
it was the only card prediction model. Yellow history therefore could not
produce a yellow-only selection. The dialog also hid descriptive averages when
venue samples did not pass model requirements.

## Market definitions

| API market | Basis | Complete match sample requires |
| --- | --- | --- |
| `yellow_cards` | `yellow_only` | Both teams' verified yellow totals |
| `red_cards` | `red_only` | Both teams' verified red totals |
| `cards` (existing) | `yellow_plus_red` | All four yellow/red totals |

Unknown values remain NULL. A team's descriptive average can use its known
total even when the opponent's total is missing; total-match rates require
both teams. Every average includes its own denominator. Last 5/10 windows
refer to actual recent matches, including missing observations, rather than
silently replacing missing matches with older complete records. Venue splits
cover the retained three-year history; season summaries use the fixture season.
Only final statistics known at the analysis cutoff enter the history.

All categories reuse the same fetched history; no migration or additional
history query is needed. Existing scraper processes, pools, caches, live
refreshes, enabled leagues, job records and checkpoints are unchanged.

## Predictions and publication

The count model still requires league/team/venue samples of 20/5/3 and abstains
on a zero league baseline. Red-card lines are 0.5, 1.5 and 2.5; yellow lines
are 2.5 through 6.5. Model probabilities are separate from recommendations.
Recommendation thresholds remain probability 0.60, reliability 0.70, score
0.74, team samples 8, venue samples 5, league samples 50, completeness 0.60,
and stability 0.50. All card types share one correlation group. Each match
still gets zero to three strong recommendations, at most one card selection.

New snapshots use `quality-poisson-v2-cards`. Existing snapshots remain
immutable; the old `cards` IDs, labels and settlement definitions are unchanged.
Yellow/red selections have distinct market IDs and settlement field sets.
The prediction, best-prediction and performance endpoints accept the new IDs.
The existing performance `card_basis` describes the legacy combined market;
`card_market_definitions` explicitly identifies all three markets.

## Dashboard and validation

Open any upcoming fixture in Match Center, then Kart. Sarı Kart, Kırmızı Kart
and Sarı + Kırmızı each have a keyboard-accessible subtab. Descriptive averages,
both venue splits, counts and missing-data explanations remain visible even
when no card recommendation qualifies. Recommendation boards remain restricted
to selected strong picks.

`tests/test_independent_cards.py` covers NULL/zero separation, independent
models, unchanged quality gates, settlement, immutable PostgreSQL snapshots,
API filters, and Chromium interaction/layout. PostgreSQL tests use an isolated
temporary schema via `TEST_POSTGRES_URL`. The browser uses stored synthetic
test history through real FastAPI endpoints; optional `CARD_SCREENSHOT_DIR`
captures desktop/mobile screenshots, not production observations.

After an approved deployment, verify a known yellow-history fixture through
`/api/matches/{id}/statistics`: yellow sample counts should remain nonzero when
red/combined samples are zero. Check `/api/predictions?market=yellow_cards`
for qualified selections or explicit availability reasons. No backfill reset,
data rewrite or deployment is part of this change.
