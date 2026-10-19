# Issue #109 — market sample reliability

The overall micro hit rate remains total wins / total won-or-lost picks, including losing singletons. No probabilities, outcomes, duplicate exclusions, settlement evidence, pagination or source records are changed. No database writes, backfills, paid service changes, Telegram/NVIDIA calls, merge or deploy.

## Display
- Default threshold: 20 **decided predictions**, where decided = won + lost. Pending, missing, disputed and void do not satisfy the threshold.
- Exact markets are grouped into comparable and insufficient samples. Both remain visible. Within each group the descending Wilson 95% lower bound determines order; decided count and stable market ID break ties. Raw success percentages do not determine the rank.
- Historical rates retain their exact numerator/denominator. Zero decided means no rate and no interval. Separate columns retain won/lost/pending/missing/disputed/void counts.
- Average model confidence is explicitly not accuracy; average model historical sample count is explicitly not the number of decided predictions.
- Wilson is a nominal binomial uncertainty interval, not a guarantee. The screen notes that multiple picks from the same match are not independent.

## Acceptance examples
0/1 retains 0% and a 0–79.3% interval; 1/1 retains 100% and a 20.7–100% interval. Both appear below the comparable group. 36/43 retains 83.7% with about 70.0–91.9%; 22/27 retains 81.5% with about 63.3–91.8%.

Tests cover n=0,1,2,4,19,20,27,43 with non-decided statuses, micro weighting, singleton losses, threshold boundary, rank, pagination and desktop/mobile overflow. The screenshots are clearly synthetic presentation fixtures using the real page assets and API response shape; no test data was loaded into production.

Run `python -m pytest tests/test_performance_samples.py tests/test_performance_status.py tests/test_market_terminal.py -q` and `node tests/market_runtime.cjs`. Screenshot reproduction: set `UI_SCREENSHOT_DIR=docs/issue-109-screenshots` and run `tests/test_performance_samples.py`. Full PostgreSQL-enabled CI is required before review; this PR is not authorized for merge or deploy.
