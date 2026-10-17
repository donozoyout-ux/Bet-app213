# Europe stage two: Poland and Denmark

Read-only production/source review: 17 October 2026. Initial SQL size was
478,328,511 application bytes / 501,728,844 aggregate database bytes. All SQL
size measurements exclude WAL and filesystem overhead. The 700,000,000-byte
historical import stop remains unchanged; no storage service or resize is added.

## Source and stored bookmaker audit

All **708 stored finished 2024+ fixtures** below were checked against their live
Goaloo season schedules: IDs, both teams, FT and HT scores matched. Thirty
early/middle/late stored fixture samples were re-fetched for odds, including
90 bookmaker snapshots: no populated stored opening/closing fields differed.
Latest-season schedules were separately validated for all ten leagues. No final
request or parser error occurred. This is an observed audit, not a future SLA.

| Goaloo ID | League | Stored finished 2024+ | Complete three-book FT markets | Decision |
|---|---|---:|---:|---|
| 37 | Championship | 25 | 25 | Hold: old/thin history; two admission slots first |
| 6 | Poland Ekstraklasa | 25 | 25 | Stage after recent-history import and DB gate |
| 7 | Denmark Superliga | 25 | 25 | Stage after recent-history import and DB gate |
| 22 | Norway Eliteserien | 25 | 25 | Hold: only early 2024 stored |
| 26 | Sweden Allsvenskan | 25 | 25 | Hold: only early 2024 stored |
| 137 | Czech First League | 25 | 25 | Hold: old/thin history; bounded rollout |
| 133 | Croatia HNL | 25 | 24 | Hold: Sbobet missing on fixture 2399949, confirmed live |
| 3 | Austria Bundesliga | 463 | 286 | Hold: only 61.8% complete pricing; preserve legacy job |
| 27 | Switzerland Super League | 35 | 35 | Hold: old 2024 history, next candidate after this stage |
| 32 | Greece Super League | 35 | 35 | Hold: old 2024 history, next candidate after this stage |

Austria's 553 total rows include 90 non-finished rows; these are not 553 finished
training observations. The seven new leagues' 25-row pilots and Swiss/Greek
35-row pilots have no current-season observations yet. A count-only promotion
would obscure this gap.

Poland and Denmark each also passed three recent finished and two upcoming
seven-day odds checks, with complete Crown/Bet365/Sbobet 1X2, AH and OU fields.
Their latest schedules contain respectively 50 / 35 upcoming fixtures within
45 days. Both have continuous 2024+ source coverage and 12/12 complete samples
in the earlier multi-season review. Selecting two bounds the additional live
collector work. This is admission to collection and evaluation, not a promise
of qualified coupons: existing per-team/venue/league, stability and fresh quote
requirements remain unchanged.

Evidence: europe-stage2-db-observations.json, europe-stage2-price-observations.json,
europe-stage2-bookmaker-audit.json, europe-stage2-source-audit.json. Public source
IDs/scores/prices only, no credentials or user/coupon payloads. Re-run the source
audit with `python -m scripts.europe_stage2_audit` after refreshing the read-only
SQL exports if needed; it never connects to PostgreSQL or calls Telegram/NVIDIA.

## Staged execution

Keep both leagues disabled initially. Run one explicitly supervised batch:
IDs 6,7, limit 25 per league (50 total), recent_first=true,
activate_reviewed=true. Newest finished fixtures at least three hours old come
first. Existing external IDs, corrected scores, completed items and job IDs are
preserved; the same europe_bounded job resumes instead of creating a duplicate.

After each league's commits, activation reads actual stored fields rather than
the cached odds_complete flag. Require at least 50 finished 2024+ rows, at least
95% with all 54 opening/closing prices/lines and FT/HT, and at least 20 complete
rows within 180 days. Source evidence must pass. If any condition fails, retain
data and leave the league disabled. Startup applies the same gate to these two
IDs only; established 15-league scope and existing paused jobs are preserved.

Capacity is checked before every historical network/write boundary and before
activation. The API health guard runs every five imports; fail/timeout stops
between commits. No broad backfill is queued on activation. Existing daily
updates, source retries and stats jobs retain the PR93 lock, durable checkpoint,
deduplication and bounded-slice behavior. Existing Albania update 2040 finished
partial with 13 real source gaps during this read-only audit; its evidence is
not cleared or represented as a successful three-book collection.

Validation includes SQLite and isolated PostgreSQL admission with real field
queries, missing-book rejection despite cached complete flags, recent-history
requirements, preservation of established scope, and PostgreSQL recent-first
resume/score-correction regression. Full CI and deployed health are required
before running the production batch.
