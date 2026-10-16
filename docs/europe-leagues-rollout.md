# Europe league expansion — staged rollout



Goal: discover **European domestic league divisions** represented in Goaloo's current catalog, not a hardcoded claim that all UEFA-member competitions are immediately ingestible.



1. Parse the provider catalog `infoHeaderEn.js` with `parse_catalog` and run `select_european_leagues`; preserve provider IDs and country/league names. Cups and non-European regions are excluded; existing UEFA competitions are maintained separately.

2. Compare discovered rows against the database and the verified snapshot. Review provider country spellings and unusual cross-border countries; explicitly resolve unmatched catalog labels, do not guess IDs.

3. Use existing `verify_competition` for each new entry in controlled batches with rate limiting; require season identity, 2024+ fixtures, representative match IDs and bookmaker odds. A verified season with no bookmaker coverage may be retained for display, **not** coupon eligibility.

4. Seed verified entries via `upsert_competitions` while leaving unknown production leagues disabled. The current production allowlist is unchanged.

5. Enable 2–3 leagues per batch after monitoring DB size, queue backlog, 504 latency, prediction publication and scraping failures; backfill from 2024 only with bounded worker capacity and resume checkpoints.

6. Maintain accurate opening/closing odds, AH and totals by Bet365, Crown and Sbobet; no fabricated lines or weakened quality/900-second freshness rule. Monitor source load and provider terms/rate limits.



This PR adds safe discovery and tests only; it does **not** claim all Europe is already collected or enable bulk scraping.





## Resumable read-only verification (follow-up to #85)



Run `python -m src.scrapers.goaloo.europe_rollout --output europe-review.json --batch-size 2`.

Resume using `--resume europe-review.json`; use repeated `--league-id` for explicit retries. Each invocation verifies at most three leagues, sequentially through the existing provider rate limiter, with a 60-second per-league budget. Failed verification remains unverified; the report is replaced atomically only after the batch. Catalog or identity changes reject resume. This command imports no database/worker initializer and cannot seed, enable or enqueue anything.



Verification includes overlapping seasons (2023–2024) but counts only actual kickoff dates from 2024; it records unique fixture counts and representative source match IDs by season. Partial/unparsed schedules cannot be marked verified. Bookmaker availability is from a historical representative source fixture and **never** establishes a current price or coupon eligibility.



Live source check 16 Oct 2026 18:22 UTC: 214 domestic league entries in 53 country labels. First batch is committed in `europe-first-batch-live.json`: ALB D1 / Goaloo 315 has 556 unique dated 2024+ fixtures and representative Crown/Bet365/Sbobet coverage; ALB D2 / Goaloo 348 has 647 and representative Bet365 coverage. Both remain disabled pending review. Counts describe provider publication, not database ingestion or complete odds coverage. 204 new candidates remain unverified; the eight existing domestic production leagues and three UEFA cups are unchanged. Catalog coverage is not a claim that every UEFA territory/division is represented.



Do not start mass ingestion while the live 504 issue remains unresolved. After the publication-read PR is deployed and verified under worker load, independently review a batch of two or three source-verified records, add only those records to the verified snapshot and explicit production scope in a separate reviewed change, and let existing serial checkpoint jobs backfill. Preserve actual-date filtering and use the existing archive repair path for the 2024 part of 2023–2024; standard club backfill currently starts at the season label 2024. Monitor queue/SQL/CPU and API latency before the next batch. This PR deliberately does not claim that Europe ingestion or live recovery is finished.


Source catalog label audit also found `Bosnia` and `Faroe`; these exact provider country labels are now included without inventing competition IDs or renaming source identities.
