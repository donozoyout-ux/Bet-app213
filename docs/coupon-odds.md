# Verified coupon drafts

The recommendation engine keeps its existing quality gates and selects at most
two independent strong recommendations per match. Kuponum allows one selection
per match, with no fixed distinct-match limit. It is an in-memory informational
draft; no bet is placed. Saved versions and Telegram delivery are described in automatic-coupons-telegram.md.

Model probability, model confidence and bookmaker decimal prices are separate
values. Crown, Bet365 and Sbobet quotes are shown side by side. The user chooses
one bookmaker for new manual selections; existing selections retain their verified source. The combined price
is the product of the selected quoted prices, rounded for display only.

## Exact prices and freshness

Full-time 1X2, total and handicap tables are matched by match ID and selection.
`market_odds` supplies explicitly tagged first-half/second-half goals, corners
and any supported card markets. Market, period, side and exact line must agree.
Missing period tags are rejected. Opening prices remain descriptive and never
substitute a missing/moved latest price. A finished match uses a verified closing
price only; in-play prices never become closing prices.

Each quote carries its own observation timestamp and a 15-minute expiry. An
obsolete higher price cannot replace a fresh lower price. The best-price timestamp
belongs to that bookmaker, not to the most recently updated competing bookmaker.
Unknown/future timestamps and expired quotes cannot be added to a draft.

Scraper writes preserve old observations and store per-price timestamps and
associated lines in existing raw JSON. A partial fetch cannot freshen a retained
price or attach it to a newly observed different line. Existing rows without this
metadata use their recorded row `updated_at` as the available observation timestamp;
new successful observations establish the more precise per-price metadata.
No schema migration or historical prediction rewrite is required.

## Read-only API checks

- `POST /api/coupon/quote` accepts distinct match IDs without a fixed count cap, recommendation
  IDs and whitelisted bookmakers. It checks the current model-version board,
  board expiry, qualified top-two membership, current kickoff/status and current
  database odds. Missing/stale quotes produce explicit reasons and no combined
  price. Prices are read in bounded batches without recalculating prediction models.
- `POST /api/coupon/status` calculates outcomes for valid draft selection IDs
  against stored scores/final statistics. It does not claim that a draft was
  historically published or persist new predictions. Live/scheduled matches are
  pending. Missing final statistics remain awaiting-data, never zero. First-half
  goals use known halftime scores; second-half goals require both halftime and
  final scores. Integer-line pushes are void.

Quote validation runs again when adding, refreshing and copying. A pre-match
selection becomes a historical draft price after kickoff and keeps its original
source/time while outcomes update. Won/lost results never imply that a bet was
placed or paid. Refresh errors clear loading states and retain the draft for retry.

Tests use explicitly synthetic stored fixtures through real SQLite/PostgreSQL
tables and FastAPI responses. Chromium covers add limits, combined calculation,
copy, clear, mobile layout and settlement. The coupon JavaScript runtime also
tests mismatched periods/lines, stale prices, API/server failures, clipboard denial
and clearing during an in-flight validation.
