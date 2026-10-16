# Europe league expansion — staged rollout

Goal: discover **European domestic league divisions** represented in Goaloo's current catalog, not a hardcoded claim that all UEFA-member competitions are immediately ingestible.

1. Parse the provider catalog `infoHeaderEn.js` with `parse_catalog` and run `select_european_leagues`; preserve provider IDs and country/league names. Cups and non-European regions are excluded; existing UEFA competitions are maintained separately.
2. Compare discovered rows against the database and the verified snapshot. Review provider country spellings and unusual cross-border countries; explicitly resolve unmatched catalog labels, do not guess IDs.
3. Use existing `verify_competition` for each new entry in controlled batches with rate limiting; require season identity, 2024+ fixtures, representative match IDs and bookmaker odds. A verified season with no bookmaker coverage may be retained for display, **not** coupon eligibility.
4. Seed verified entries via `upsert_competitions` while leaving unknown production leagues disabled. The current production allowlist is unchanged.
5. Enable 2–3 leagues per batch after monitoring DB size, queue backlog, 504 latency, prediction publication and scraping failures; backfill from 2024 only with bounded worker capacity and resume checkpoints.
6. Maintain accurate opening/closing odds, AH and totals by Bet365, Crown and Sbobet; no fabricated lines or weakened quality/900-second freshness rule. Monitor source load and provider terms/rate limits.

This PR adds safe discovery and tests only; it does **not** claim all Europe is already collected or enable bulk scraping.
