# Goaloo match statistics — live verification

Inspected the six requested competitions before implementing parsers. The authoritative historical fields are server-rendered on the match live page, scoped to `#ftstat` and `#hf1stat`; the incident timeline is scoped to `#eventsTable`. No JavaScript from Goaloo is executed.

## Verified matches and endpoints

- English Premier League (36): Manchester United / Fulham, match **2590898** — [live statistics](https://www.goaloo.com/match/live-2590898); [analysis](https://www.goaloo.com/football/english-premier-league-manchester-united-vs-fulham/h2h-2590898). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.
- Spanish La Liga (31): Athletic Bilbao / Getafe, match **2596611** — [live statistics](https://www.goaloo.com/match/live-2596611); [analysis](https://www.goaloo.com/football/spanish-la-liga-athletic-bilbao-vs-getafe/h2h-2596611). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.
- Italy Serie A (34): Genoa / Inter Milan, match **2606870** — [live statistics](https://www.goaloo.com/match/live-2606870); [analysis](https://www.goaloo.com/football/italy-serie-a-genoa-vs-inter-milan/h2h-2606870). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.
- German Bundesliga (8): Borussia Monchengladbach / Bayer Leverkusen, match **2607246** — [live statistics](https://www.goaloo.com/match/live-2607246); [analysis](https://www.goaloo.com/football/german-bundesliga-borussia-monchengladbach-vs-bayer-leverkusen/h2h-2607246). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.
- Turkey Super Lig (30): Galatasaray / Hatayspor, match **2613603** — [live statistics](https://www.goaloo.com/match/live-2613603); [analysis](https://www.goaloo.com/football/turkey-super-lig-galatasaray-vs-hatayspor/h2h-2613603). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.
- FIFA World Cup (75): Mexico / South Africa, match **2906701** — [live statistics](https://www.goaloo.com/match/live-2906701); [analysis](https://www.goaloo.com/football/fifa-world-cup-mexico-vs-south-africa/h2h-2906701). Corners/HT, yellow cards, shots/target, fouls, offsides, possession and events returned. Referee: **not exposed in inspected markup or metadata**.

Red-card fields were absent in the EPL, La Liga, Serie A, Bundesliga and Süper Lig samples; they remain NULL. The World Cup sample explicitly returned red cards (home 1 / away 2). Bundesliga away yellow cards and Süper Lig home yellow cards explicitly returned zero. They remain zero. This is a six-match availability observation, not a guarantee for every competition fixture.

## Field mapping

- `#ftstat li span.stat-title` identifies each statistic. The two `span.stat-c` values are home/away.
- Corner Kicks → home_corners / away_corners; Corner Kicks(HT) → home_corners_ht / away_corners_ht. If the latter is absent, Corner Kicks inside the explicit `#hf1stat` block can supply HT values.
- Yellow Cards / Red Cards → corresponding nullable card counts. Absent red rows are not inferred as zero from a lack of red events.
- Shots → shots; Shots On Goal → shots_on_target; Fouls → fouls; Offsides → offsides; Possession → percentage values 0–100, with the percent sign removed.
- Non-numeric placeholders, negative values and malformed counts become NULL. Other source rows are retained only in raw evidence; unrequested metrics are not manufactured.

## Events

- Each five-cell incident row contains home player text, home icon, minute, away icon, away player text. Team side comes from the actual icon column.
- Primary image codes, verified against Goaloo’s page event legend: 1 goal, 2 red, 3 yellow, 7 penalty scored, 8 own goal, 9 second-yellow red, 11 substitution, 13 penalty missed, 14 VAR, 30 penalty saved. A corner is normalized only when the actual primary icon label identifies a corner. Unknown codes remain unknown with raw evidence.
- Minute 90+1 is stored as minute 90, stoppage_minute 1. Missing time/player names remain NULL. Substitution and assist secondary names are retained only when actual player links exist.
- Secondary substitution-in/out image codes 4/5 are not interpreted as primary corner/card events.
- Source keys include the canonical incident plus duplicate occurrence index. Repeated fetches upsert the same events; a verified new timeline reconciles removed/corrected incidents. Missing timelines do not erase old events.
- Aggregate counts are not invented from event absence. A second-yellow red remains an event and is not added again to an already published Red Cards total.

## Live AJAX and referees

- The page JavaScript requests `https://www.goaloo.com/ajax/soccerajax?type=15&id=2590898`. The historical EPL response returned ErrCode 0 and an empty JavaScript initialization payload; it did not provide historical statistics. The collector uses the real HTML fields instead of interpreting empty AJAX data as zeros.
- The live and discovered H2H/analysis pages for all six samples contained no referee name or external referee ID. No competition/referee endpoint is confirmed by this investigation. The current parser therefore emits referee=null.
- A nullable referee table/FK and aggregation support are available for genuinely verified future assignments. Names are never guessed from stadiums, players, ads or competition identity. Same-name identities without an external ID are kept within their competition.
- Referee tendencies require at least 10 completed, as-of observations per metric. Absent card/foul values do not enter denominators; insufficient history shows Yetersiz hakem verisi. Referee data is not used in forecasts in this release.

## Persistence, forecasts and backfill

- New tables: match_statistics (one row per match), match_events and referees. Additive match columns: referee_id and referee_observed_at. Existing scores, odds and IDs survive migration.
- All optional fields are nullable. A checked page without stats is recorded as unavailable rather than repeatedly retried or populated with zeros. Explicit single-match collection can refresh a previously observed row.
- Last-5/10 windows contain the actual last completed matches, including matches whose statistics are unavailable. Each average/rate exposes its usable denominator. Complete corner pairs or all four yellow/red card fields are required for totals.
- Card model definition, as selected by the user: **home yellow + home red; away yellow + away red, each published card count contributes 1**. Missing red counts make combined totals and card forecasts unavailable. No bookmaker settlement convention (e.g. red counts double) is assumed.
- Corner/card expectations use the existing transparent venue-rate shrinkage method with weight 5 toward the SAME competition baseline. Minimums: 20 complete competition observations, each team 5, each relevant home/away split 3. Count estimates are bounded to 30 per side; total Poisson tails yield the requested half-goal-line over probabilities. No random or fabricated forecasts.
- Only finished matches and final statistics known before cutoff enter history. Match created/updated/last-scraped, statistics observation time and a three-hour completion buffer prevent future leakage. The current match’s captured actual stats/events are displayed separately from prediction inputs.
- `python -m src.jobs.stats_backfill --league 36 --enqueue-only` creates an explicit durable job. `--resume JOB_ID` resumes only unfinished/failed items. The existing global PostgreSQL worker lock ensures one competition executes at a time.
- Authenticated endpoints: POST /api/scraper/stats-backfill and POST /api/scraper/statistics/{internal_match_id}. GET /api/matches/{id}/statistics returns calculated corner/card/referee data in additional_statistics and nullable prediction fields.
- Normal live/finished odds updates attempt statistics collection too; a missing HTML statistics source does not discard successfully collected odds. No statistics jobs are automatically queued at startup, and no massive production collection was triggered during implementation.

Captured relevant HTML blocks are stored under tests/fixtures/match_statistics_*.html. Fixtures retain real source IDs and field structures.
