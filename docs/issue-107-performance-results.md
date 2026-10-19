# Issue #107 — performance result display

## Observed cause (read-only GET, 2026-10-19)
The production `/api/market-performance?limit=50` returned HTTP 200: all first-page items were scheduled/pending. The summary contained 499 picks, 273 binary decisions, 225 pending and one void. These pending fixtures must not be painted as wins. Capture-date descending pagination hid finished fixtures on later pages. The UI also conflated missing evidence with pending fixtures and trusted frozen settlements without checking current evidence.

## Change
Finished/cancelled/abandoned fixtures precede unfinished fixtures before pagination; capture-date filters remain unchanged. Existing market-specific settlement evaluates each saved prematch pick independently. Verified frozen evidence is reconciled with current market evidence without writing any history. Invalid frozen proof, outcome/actual disagreement, or a frozen terminal decision on a now unfinished match displays **İncelenmeli**. Missing current evidence displays **Veri eksik**, even if an older terminal observation exists. Invalid half-time scores display review instead of a betting push. Genuine pushes/cancellations remain **Geçersiz**. Postponed fixtures remain pending.

Green **✓ Tuttu** and red **✕ Kaybetti** appear in the rightmost table column and mobile result cards. Missing, disputed, void and pending have separate groups/counts; only won/lost enter hit-rate, calibration and ROI denominators. Disputed table rows expose current and recorded values. No retrospective prediction generation or settlement repair is performed.

## Validation
Run `python -m pytest tests/test_performance_status.py tests/test_market_terminal.py tests/test_publications.py -q` and `node tests/market_runtime.cjs`.
Browser fixtures use local SQLite and real ASGI routes, not production writes. Screenshots at 390 and 1536 pixels are under `issue-107-screenshots/`; they are synthetic acceptance examples. Tests assert statuses, hit-rate exclusion, immutable ledger preservation, no page errors and no document overflow. Existing market tests cover FT, BTTS, handicap, corners and cards, pagination and date filters. CI runs the complete suite including PostgreSQL checks.

No merge, deploy, production database update, backfill, Telegram or NVIDIA invocation.
