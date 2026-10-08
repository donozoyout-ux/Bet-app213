# Goaloo competition verification

Live verification completed at 2026-10-08T16:30:26.934569+00:00 (UTC). IDs were parsed from [Goaloo catalog](https://football.goaloo.com/jsData/infoHeaderEn.js); no numeric target IDs are defined in the discovery policy.

All 30 competition IDs, season feeds and schedules were verified. One real match odds request per competition was checked. This does not guarantee prices for every future fixture or all bookmakers.

## Club leagues — production backfill priority

- 1. English Premier League — Goaloo **36**; latest **2026-2027**; **1,140** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 2. Spanish La Liga — Goaloo **31**; latest **2026-2027**; **1,140** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 3. Italy Serie A — Goaloo **34**; latest **2026-2027**; **1,140** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 4. German Bundesliga — Goaloo **8**; latest **2026-2027**; **918** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 5. France Ligue 1 — Goaloo **11**; latest **2026-2027**; **918** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 6. Turkey Super Lig — Goaloo **30**; latest **2026-2027**; **954** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 7. Holland Eredivisie — Goaloo **16**; latest **2026-2027**; **924** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 8. Liga Portugal 1 — Goaloo **23**; latest **2026-2027**; **917** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 9. Belgian Pro League — Goaloo **5**; latest **2026-2027**; **932** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 10. Scottish Premiership — Goaloo **29**; latest **2026-2027**; **660** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 11. Austrian Bundesliga — Goaloo **3**; latest **2026-2027**; **518** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 12. Switzerland Super League — Goaloo **27**; latest **2026-2027**; **588** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 13. Greece Super League A — Goaloo **32**; latest **2026-2027**; **654** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 14. Saudi Professional League — Goaloo **292**; latest **2026-2027**; **918** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- 15. USA Major League Soccer — Goaloo **21**; latest **2026**; **1,572** dated 2024+ fixtures currently published; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.

## National competitions — after club leagues

- FIFA World Cup — Goaloo **75**; latest **2026**; **104** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (UEFA) — Goaloo **650**; latest **2025-2026**; **204** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (AFC) — Goaloo **648**; latest **2023-2025**; **168** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (CONMEBOL) — Goaloo **652**; latest **2023-2025**; **60** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (CAF) — Goaloo **651**; latest **2023-2025**; **211** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (CONCACAF) — Goaloo **653**; latest **2024-2025**; **96** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- FIFA World Cup qualification (OFC) — Goaloo **649**; latest **2024-2025**; **18** dated 2024+ fixtures; odds endpoint **ok**, sample books: Sbobet.
- World Cup (Preliminaries) Play-Offs — Goaloo **892**; latest **2026**; **4** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- UEFA European Championship — Goaloo **67**; latest **2023-2024**; **60** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- UEFA Nations League — Goaloo **1864**; latest **2026-2027**; **316** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- Copa America — Goaloo **224**; latest **2024**; **34** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- Africa Cup of Nations — Goaloo **93**; latest **2026-2028**; **392** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- AFC Asian Cup — Goaloo **95**; latest **2024-2027**; **156** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- Concacaf Gold — Goaloo **232**; latest **2025**; **31** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.
- International Friendly — Goaloo **1366**; latest **2026**; **6,208** dated 2024+ fixtures; odds endpoint **ok**, sample books: Bet365, Sbobet, Crown.

## Source distinctions

- EURO finals and EURO qualifiers share Goaloo competition **67**. The real cup stage/group labels distinguish qualifiers and finals; no duplicate league or guessed qualifier ID is seeded.
- World Cup qualification is represented by seven separate verified confederation/play-off competitions. No artificial aggregate Goaloo ID is used.
- Some archived cup JSON files carry a current-season LeagueInfo header. ID, abbreviation, actual schedule dates and season availability are verified independently.
- National discovery also checks the prior archive year. This captures the AFC Asian Cup labelled 2021–2023 but played in 2024; matches before 2024 are excluded.
- OFC qualification sample returned prices only for Sbobet. Crown/Bet365 and missing markets remain unavailable; the UI renders a dash.
- Counts are distinct valid fixture IDs with dated kickoffs from 2024 onward, including published upcoming fixtures. They can change; unresolved participants and malformed source rows are excluded from these estimates.

## Migration and execution

- Additive league columns: competition_type, enabled, backfill_from_year, priority, schedule_format, latest_season, verified_at, created_at, updated_at. Match columns: round_label and stage_key. Job column: priority.
- Migration runs under the existing PostgreSQL transaction lock, checks columns before adding them, and retains existing match/league IDs, scores and odds. No tables or rows are deleted.
- PostgreSQL startup upserts this live-verified catalog snapshot. Run `python -m src.scrapers.goaloo.competitions --output src/scrapers/goaloo/verified_competitions.json --seed` to repeat live verification and seed the connected database.
- SQLite is a test/local mode; automatic multi-competition seeding/backfill is restricted to PostgreSQL. Local tests explicitly upsert fixtures.
- With AUTO_BACKFILL_ON_EMPTY=true and SCRAPER_WORKER_ENABLED=true, empty verified enabled competitions receive one durable backfill job each. An existing or failed/partial historical job prevents automatic duplicates; use explicit resume for recovery.
- Only the PostgreSQL worker lock owner executes jobs. Interrupted running jobs resume before queued jobs; queued historical jobs use the documented tier order. Daily update jobs take priority between large historical jobs, without interrupting a running backfill.
- The authenticated `/api/scraper/update-all` queues updates for enabled competitions; existing active league jobs are reused. The daily workflow calls it. Keepalive remains unchanged.
- `/api/diagnostics` exposes enabled/completed competition counts, active historical competition and queued backfill count without secrets.

## Dashboard

- The existing Stitch shell remains. SQL-backed enabled competitions are grouped as KULÜP LİGLERİ / MİLLİ TAKIMLAR. Tüm Ligler omits the league API filter; each selected competition uses its internal SQL ID.
- Upcoming defaults to the next seven days, globally ordered by UTC kickoff. If the filtered window is empty, only the next available Istanbul calendar match day is shown, with an explicit message.
- Season and round/group filters apply after selecting a competition. National match stage labels replace club-specific week text. Missing prices remain dashes; raw in-play odds never become closing.

The full verification evidence and per-season counts are in `src/scrapers/goaloo/verified_competitions.json`. No production backfill was run during validation; CI uses recorded source fixtures.
