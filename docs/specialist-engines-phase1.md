# Phase 1 specialist engines

## Production quote audit

Read-only `/api/odds-coverage`, 12 October 2026 16:54:47 Europe/Istanbul,
reported 102 selected strong predictions and 5 exact fresh prices. All 10
daily selections were rejected for price. The historical issue description's
2 matched prices is not the same observation: this audit found 5.

The recorded response contained:

- corners: 42 selections; 38 no source rows, 4 line mismatches;
- first_half_goals: 14 selections; 11 line mismatches, 3 matched fresh prices;
- goals: 3 selections; 3 line mismatches;
- result: 2 selections; 2 matched fresh prices;
- second_half_goals: 31 selections; source market unavailable;
- yellow_cards: 10 selections; source market unavailable.

The board was fresh, generated at 16:46:52 and expiring at 16:56:52 Istanbul,
version `quality-poisson-v3-exact-priced-two-picks-v4`. An expired board was
therefore not the cause at this observation. This endpoint does not disclose
model sample counts or production calibration history. No such counts are
inferred from selection counts. No production log or database credentials were
available for independently verifying the corners/Telegram failures mentioned
in the issue. No Telegram messages were sent.

Exact line/period/bookmaker requirements must remain strict. Adding BTTS or
double-chance engines cannot create source prices for these markets. They stay
unpriced with `source_market_unavailable` until a verified adapter exists.

## Independently reproduced quote defect

When field metadata exists but a particular price observation is absent,
the old reader borrowed `updated_at`, potentially labeling an old price fresh
after another field changed. Missing field line metadata similarly borrowed
the row's latest line. The reader now treats these as unknown/mismatched.
SQL projections preserve the distinction between absent metadata on legacy
rows and explicitly present empty dictionaries. Legacy rows retain the prior
fallback; partially observed rows cannot acquire fabricated field provenance.
`tests/test_quote_metadata.py` exercises the complete quote path.

This is a separate commit. It does not loosen any price eligibility checks.
It may reduce apparent freshness where provenance was incomplete.

## Architecture and compatibility

`PrematchContext.load` adapts the existing model once per match, after the
existing SQL observation cutoff. Kickoff and observation times are aware;
target results and results within the three-hour completion buffer are excluded.
The immutable context records match/team identity and a shared model result.
Historical SQL also excludes rows created, updated or scraped after cutoff.

`MarketEngine` and `EngineRegistry` expose `result_1x2`, `total_goals`,
`double_chance` and `both_teams_to_score`. The last two derive from the same
Poisson score model, not a product of independent market probabilities. Total
goals uses explicit 1.5, 2.5 and 3.5 lines and full-time period. Existing legacy
candidate IDs, ranking, additional-statistics engines and API fields remain.
Each registry result contains evidence, sample size, model/adapter version,
calibration status, errors, duration and priced/fresh/strong coverage.

The registry describes future specialists separately, including the difference
between Asian settlement and Iddaa three-way handicap. Planned specialists do
not invent model predictions; existing additional-statistics behavior remains.

Set `DISABLED_MARKET_ENGINES=double_chance,both_teams_to_score`, for example,
and restart the application/workers. Disabled adapters cannot contribute
candidates. Flag configuration enters the board version, forcing regeneration;
the process model cache also includes flags. Failures in one adapter produce
an unavailable result and do not prevent the other adapters running.

`/api/matches/{id}/statistics` includes engine reports, context and registry
metadata. `/api/odds-coverage` includes `engine_analysis` for all evaluated
eligible board matches, including those without a strong selection. It
revalidates exact quotes against the current database without rerunning models.
Match-detail prediction tabs separate priced/unpriced options and identify
strong versus below-threshold options and unavailable engines.

## Baseline and evaluation limits

Identical stored fixture inputs produce the same existing Poisson probabilities
before and after adapter introduction (`test_four_adapters_match_shared_distribution`).
No model retraining or claimed probability uplift is introduced. The quote
regression uses the same price, row timestamp and empty field metadata before
and after; the price remains visible but becomes unknown/ineligible.

The existing venue model has no verified calibration artifact. Reports therefore
say `unvalidated`, never “calibrated”. `evaluate_binary` measures Brier score and
ten calibration bins from real probability/outcome pairs. `historical_backtest`
evaluates each market/selection from saved as-of contexts and later confirmed
outcomes, rejecting identity mismatches and future history. Empty data remains
unavailable. Synthetic test fixtures validate these utilities, not production
accuracy. Production calibration and historical/forward metrics still require
genuine observation-filtered snapshots/outcomes; current corrected results must
not be used as a historical pre-match dataset.

Production after-values have not been measured for this code: there was no
deployment. The audit is a baseline, not evidence that this branch has increased
live price coverage. Most source/line gaps intentionally remain missing.

## Staging verification

Use an isolated database and disable real Telegram credentials. Install
`requirements-dev.txt` and Chromium. Run `python -m pytest -q -rP` with an
isolated `TEST_POSTGRES_URL`; run all four `tests/*_runtime.cjs` scripts and
the JavaScript syntax checks in `.github/workflows/test.yml`.

Regenerate prediction boards on staging through the existing worker (new board
version invalidates the previous payload). Compare the same database snapshot,
cutoff and flags with the baseline model. Query statistics and odds coverage;
verify samples, exact lines, missing reasons and 15-minute expiry. Disable one
engine, restart and regenerate, and verify other market options remain. Inject
an engine failure only in a test registry. Verify no more than four distinct
matches in the daily Istanbul coupon and unchanged .70/.80/.80 gates; ensure
manual one-selection-per-match validation and retained selections still pass.

No new Render services, master merge or production deployment is part of this PR.
