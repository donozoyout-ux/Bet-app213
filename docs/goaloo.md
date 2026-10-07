# Verified Goaloo source contract

Inspected through real HTTP requests on 2026-10-07. No browser or Playwright is
needed for the verified endpoints. Goaloo may change its API; adapters reject
unknown/error schemas rather than synthesize data.

## Seasons and schedules

The league page `https://football.goaloo.com/league/2024-2025/36?round=1`
declares `_seasonPath` and `_dataPath`:

- `https://football.goaloo.com/jsData/leagueSeason/sea36.json`: `SeasonList`.
- `https://football.goaloo.com/jsData/matchResult/json/2024-2025/s36_en.json`:
  `LeagueInfo`, `TeamInfo`, and `ScheduleList` keyed by `R_1` … `R_38`.

`TeamInfo`: ID at index 0, name at index 1. Schedule row: match ID 0, league ID
1, status 2, kickoff 3, home team ID 4, away team ID 5, FT score 6, HT score 7.
Rows are saved verbatim. Kickoff is UTC+8: the site's `/scripts/v2/league` uses
`timeFromE8(row[3])`. Manchester United–Fulham 2590898 is `2024-08-17 03:00`
in the source and `2024-08-16T19:00:00Z` in storage.

## Bookmakers and markets

`https://www.goaloo.com/football/match/oddscomp-2590898` and its
`/scripts/soccer/oddsComp` expose:

`GET https://www.goaloo.com/ajax/soccerajax?type=14&t=1&id=2590898&h=0&s=-1`

Requires a Goaloo `Referer` and `X-Requested-With: XMLHttpRequest`; a request
without these returned HTTP 200 with `{"code":1002}`. Successful response:
`ErrCode=0`, `Data.mixodds`. Bookmaker ID/name pairs are verified and checked:
3 Crown, 8 Bet365, 31 Sbobet. Other bookmakers are ignored.

Each bookmaker supplies `euro`, `ah`, and `ou`, each with `f` (initial), `l`
(latest prematch), `r` (in-play). `u/g/d` are home/draw/away for euro;
home/line/away for AH; over/line/under for OU. The page labels these Initial,
Live, In-Play separately. Never use `r` as closing. `l` becomes closing only
once the match has started or is finished; before that it is stored as latest.

Euro prices are decimal; AH/OU prices are Hong Kong and become decimal by adding
1. Missing/zero odds stay NULL. Goaloo AH positive line means home gives goals;
API and database expose a signed **home** handicap (source +1 becomes home -1).
Raw source values retain original signs and format. Split lines are averaged:
0.5/1 → 0.75, 3/3.5 → 3.25. Actual captured match 2590898 Crown euro initial
1.53/4.85/5.60, prematch 1.66/4.20/4.90, in-play 1.02/15/66 demonstrates why
the in-play values cannot be used as closing.

## Optional snapshots

`type=14&t=20&id={match}&cid={bookmaker}&h=0&r1=0&r2=0&r3=0` supplies `Data.ah`,
`Data.op`, `Data.ou` history arrays. Each record includes `mt` Unix timestamp,
`odds`, `type`, `close`, `ht`, and scores. Store raw records with source UTC
timestamps and retain prematch/in-play markers. The first request is not a
guarantee of the provider's entire movement history (pagination is provider
controlled); optional storage captures the returned records, not a claim of
complete tick history. Opening/closing use the summary's explicit fields.

Sanitized real season, round-one schedule and market JSON responses are in
`tests/fixtures/`. Automated tests mock network access. Live smoke tests must
be run explicitly and never supply fallback synthetic data.
