# BetApp213

FastAPI serves the existing Stitch dashboard from `src/api/dashboard.html`.
The active Goaloo adapters use verified JSON/AJAX responses, SQLAlchemy async
persists matches and bookmaker markets, and a durable database-backed worker
collects verified global club and national-team competitions from 2024 onward.
Archived NowGoal/SofaScore modules are isolated under `legacy/`.

## Local setup (Python 3.12)

```sh
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1; Unix: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Create a PostgreSQL database and user (or an existing managed PostgreSQL service).
For an isolated local database with Docker available:

```sh
docker run --name betapp-postgres -e POSTGRES_USER=betapp -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=betapp213 -p 5432:5432 -d postgres:16
```

Copy `.env.example` to `.env`, set `DATABASE_URL` and a random
`SCRAPER_API_TOKEN`, then start:

```sh
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Open `/` for the dashboard or `/docs` for typed API documentation. Database
initialization creates tables and seeds Premier League/Crown/Bet365/Sbobet
idempotently, guarded by a PostgreSQL advisory transaction lock. This is the
base schema; an additive, idempotent competition migration extends existing tables without deleting data. Future schema
changes require versioned migrations rather than dropping production tables.

With no `DATABASE_URL`, `/health`, `/`, `/docs`, and `/api/status` still work;
database-dependent endpoints return 503. `/health` reports process liveness,
not database readiness. `/api/status` reports actual database availability.
Initialization/reconnection runs in the background, so a slow or unavailable
database does not delay `/health` or serving the dashboard.
No fabricated matches, scores, odds, uptime, bankroll, latency or predictions
are shown. Empty/unavailable panels show “Veri bekleniyor” or “Henüz veri yok”.

## Configuration

- `DATABASE_URL`: production PostgreSQL URL; `postgres://` and `postgresql://`
  normalize to asyncpg. SQLite via aiosqlite is only for local tests.
- `APP_ENV`: `development` or `production`; production rejects SQLite.
- `LOG_LEVEL`: Python log level, default INFO.
- `SCRAPER_API_TOKEN`: required Bearer token for all collection POST routes.
  The public dashboard has no scraper controls or token input. This token is
  for the authenticated scheduled workflow or administrative API calls only.
- `SCRAPER_WORKER_ENABLED`: default true. With false, use CLI to consume jobs.
- `AUTO_BACKFILL_ON_EMPTY`: default true. After PostgreSQL connects, startup
  checks Premier League match count under the enqueue advisory lock. An empty
  database gets one durable backfill from 2024. Existing matches or an existing
  backfill prevent another job. Running/interrupted work resumes through the
  worker; failed/partial jobs can be explicitly resumed using the CLI. The web
  server remains responsive while initialization and collection run in the
  background. SQLite never triggers automatic provider collection.
- `GOALOO_REQUEST_INTERVAL`: minimum spacing in seconds (default 1, minimum .2).
- `GOALOO_TIMEOUT`, `GOALOO_RETRIES`, `GOALOO_CONCURRENCY`: 25 seconds, 3 attempts,
  2 maximum concurrent HTTP requests by default. Match processing is sequential
  to keep provider traffic conservative.
- `GOALOO_SAVE_SNAPSHOTS`: default false; capture returned timestamped movements
  for each bookmaker in addition to required summary opening/closing fields.

## Collection

```sh
python -m src.jobs.backfill --league 36 --start-year 2024
python -m src.jobs.daily_update --league 36
python -m src.jobs.backfill --resume JOB_ID
```

`--enqueue-only` queues a durable job without waiting. The web process consumes
jobs in the background. The CLI can also consume them and waits for its own job
to reach a terminal status. Only one worker per PostgreSQL database holds the
collection advisory lock, so web workers/CLI processes do not duplicate work.
Do not run multiple SQLite workers; SQLite is a test-only alternative.

Backfill discovers seasons and rounds, saves every schedule/result row and
queues incomplete matches. Each round is committed separately; each processed
match is committed separately. A restarted worker automatically resumes a
`running` job and unfinished items. Rediscovering after interruption only
refreshes lightweight season JSON and upserts existing rows; completed match
odds are not redownloaded. `partial` jobs retain failed match IDs/errors and
can be resumed using the same ID. `processed_matches` counts successful items;
`failed_matches` counts failures, so their sum is attempted items.

Missing bookmaker markets remain incomplete and retryable; they never produce
synthetic prices. Cancelled/postponed/abandoned/pending matches keep schedule
information but are excluded from odds backfill. Scheduled matches are refreshed
again because their closing odds do not exist yet.

Daily update refreshes the latest two seasons, which stores newly scheduled
fixtures and result corrections throughout those seasons. It refreshes odds
for matches within the previous 7 days through the next 7 days. Run it more
frequently if near-live collection is desired; dashboard polling only reads
the database and does not trigger provider collection.

## API

- `GET /health`, `GET /api/status`
- `GET /api/leagues`, `GET /api/leagues/{league_id}/seasons`
- `GET /api/bookmakers`
- `GET /api/matches`: `league` (internal ID or name), `season`, `round`, `date`
  (calendar day in `display_timezone`, default Europe/Istanbul), `status`,
  `team`, `bookmaker`, `limit`, `offset`, `view`;
  `include_odds=true` includes markets using batched queries.
- `GET /api/matches/{match_id}`, `GET /api/matches/{match_id}/odds`
- `GET /api/scraper/status`, `GET /api/scraper/jobs/{job_id}`
- `POST /api/scraper/backfill`, `POST /api/scraper/update`: JSON
  `{"league_id":36,"start_year":2024}`; optional `resume_job_id`.
- `POST /api/scraper/match/{match_id}`: refresh an already recorded match.

GET resource paths use **internal database IDs** from list responses. Collection
request `league_id` is the **Goaloo external ID**. Match records expose both IDs.
Collection POSTs return 202 with a job ID. Unknown resources return 404;
unavailable database returns 503; conflicting jobs return 409. Stack traces
are logged on the server and never returned by the API.

Odds are decimal throughout the API. AH lines are signed from the home team's
perspective. Initial, latest prematch, and closing fields are distinct.
Closing is populated only after kickoff/started status or a final result and
always comes from the provider's prematch value, never in-play. See
`docs/goaloo.md` for endpoint/field provenance and known provider limitations.

The dashboard reads health every 30 seconds, matches every 25 seconds, and job
progress every 4 seconds while a job is queued/running (30 seconds when idle).
Its Tümü/Canlı/Bugün/Yaklaşan/Geçmiş tabs use server-side `view=all|live|today|
upcoming|history` filters. Canlı includes all in-progress states, including
half-time, extra time and penalties. Bugün includes every status in the Istanbul
calendar day and sorts ascending. Yaklaşan includes only scheduled matches
strictly after now, within 7 days, ascending; `upcoming_days` can extend this
window, or `date` selects a later calendar day without the default window.
Geçmiş includes finished matches, newest first, with total-count pagination.

League, season, round, team and date selectors browse recorded history. Season
responses include their recorded rounds. Only database leagues are shown;
unsupported AI/statistics/event panels and public scraper controls are removed.
All dates display in Europe/Istanbul and remain UTC in storage. Crown is the
default, and bookmaker tabs select each company's separate prices. Finished
matches use Opening → Closing, showing unavailable closing as a dash; scheduled
matches use Opening → latest prematch. In-play raw odds never become closing.
Collection progress is a compact status message from `/api/scraper/status`.

## Render and daily scheduling

`render.yaml` defines a Python 3.12.8 web service and managed PostgreSQL. Review
Render's resource plan/billing choices before applying the blueprint. The start
command binds `$PORT` and uses one gunicorn worker; the scraper runs as an async
background task and all progress is durable in PostgreSQL. No browser binaries,
Prefect, Docker, Redis or pandas are installed in production.
Render uses the explicit ASGI application target `src.api.main:app`. The
`application` alias remains available for existing module-only deployments.

For the existing Render service configure `DATABASE_URL`, `APP_ENV=production`,
`AUTO_BACKFILL_ON_EMPTY=true`, `SCRAPER_WORKER_ENABLED=true` and
`SCRAPER_API_TOKEN`, then use the build/start commands from `render.yaml`.
No database credentials or Render deployment access are included in this repo.

The daily GitHub workflow queues `/api/scraper/update` at 04:15 UTC (07:15
Istanbul). Set repository secret `SCRAPER_API_TOKEN` to the Render value and
optional repository variable `BETAPP_URL`. The server owns execution/progress;
the workflow only verifies that enqueueing succeeded. Check job status for
completion. The existing keepalive workflow is separate from collection.

## Tests and verification

```sh
python -m compileall src legacy
python -m pytest -q
python scripts/smoke_test.py https://bet-app213.onrender.com
python -m src.jobs.verify --goaloo
```

The Render start command is:

```sh
gunicorn -k uvicorn.workers.UvicornWorker --workers 1 --timeout 60 -b 0.0.0.0:$PORT src.api.main:app
```

`scripts/smoke_test.py` checks HTTP assets, liveness and either connected or
expected degraded database responses. `python -m src.jobs.verify` reads the
configured database without creating schema or jobs, reports table/record counts
and the latest job, and exits nonzero for missing/unreachable/incomplete databases.
`--goaloo` additionally validates season discovery, one schedule and one odds
endpoint with three real requests; it does not start a backfill. Run the CLI in
Render's environment (or with an external test PostgreSQL URL) to verify production.
No connection strings, tokens or job error messages are included in its output.

`GET /api/diagnostics` returns non-secret configuration flags, database readiness,
latest job status and counts; unknown counts are null, and database failure keeps
the diagnostics/liveness HTTP service available. Database initialization and
worker supervision run in the background with retries and full exception logs.

CI uses Python 3.12.8 and real PostgreSQL. It launches the exact Render command
from a different working directory with no database, an unreachable database,
and a connected database. Gunicorn tests require Linux; Windows runs the other
tests and can smoke-test Uvicorn locally. Node executes the dashboard against
empty data, HTTP 503 and removed optional controls without browser dependencies.

The suite uses saved actual Goaloo responses and mocked network calls. It tests
startup without a database, idempotent initialization, schedule/results parsing,
odds formats/bookmaker identity, API filtering, safe error responses, durable
jobs, resumption and snapshot deduplication. SQLite covers offline storage tests.
Set `TEST_POSTGRES_URL` for PostgreSQL integration tests; GitHub CI provisions
an isolated PostgreSQL 16 service. Never point this variable to production.

## Adding a league

Insert a `leagues` row with its Goaloo `external_id`, name/country/source, then
use `--league EXTERNAL_ID` or the collection API. Seasons and teams are discovered
and normalized automatically. The current adapter supports `R_n` league rounds;
validate a new league's fixtures/odds and extend its adapter for cup/group formats.
Do not assume a cup has the Premier League's source schema.

## Global competitions and national teams

See [live discovery evidence and backfill order](docs/competition-discovery.md) for all 30 verified IDs, latest seasons, source-specific formats, odds availability and fixture-count estimates.

```sh
python -m src.scrapers.goaloo.competitions
python -m src.scrapers.goaloo.competitions --output src/scrapers/goaloo/verified_competitions.json --seed
```

The targets are selected by catalog country/abbreviation, then their IDs, names, season feed and schedules are checked live. Seeding refuses unverified records. Startup seeds the recorded verified snapshot into PostgreSQL, retaining existing EPL data and disabled flags. Each job validates its source identity again before storing schedule data.

`AUTO_BACKFILL_ON_EMPTY=true` and `SCRAPER_WORKER_ENABLED=true` queue one historical job for each empty, enabled, verified competition; the existing PostgreSQL advisory worker lock permits only one executing job globally. Jobs survive restart and run by priority. An existing partial/failed backfill is resumed explicitly rather than duplicated automatically. The daily authenticated workflow calls `/api/scraper/update-all` for every enabled competition.

The dashboard defaults to upcoming across all enabled competitions. Its seven-day window expands only to the next available match day when empty. The response includes `upcoming_expanded`; calendar dates use Europe/Istanbul. The sidebar groups only records returned by PostgreSQL. National tournaments retain real stage/group labels and year-based seasons; no fake participants or seasons are generated.

## Statistical predictions and full-width dashboard

The sidebar is removed. The current dark navy/cyan terminal is market-first: enabled leagues, summary cards, category navigation, dense selected-pick tables and audited performance. Rows open a wide desktop / full-screen mobile match analysis dialog; eight tabs keep each match context isolated.

Endpoints:

- `GET /api/predictions?league=<internal ID or name>&date=YYYY-MM-DD&limit=8`
- `GET /api/matches/{match_id}/statistics`

Selection is chronological. Predictions prefer the next 72 hours; if fewer than five sufficiently sampled matches are available (or fewer than the requested limit), the candidate window expands to seven days. Explicit dates intersect that seven-day window. No monthly far-future fallback applies to prediction cards. Matches without qualifying recommendations are excluded. Empty filters show Bu filtrede eşikleri geçen tahmin bulunmuyor; unavailable APIs show Bağlantı bekleniyor.

The model uses only observed, completed, scored results from the SAME competition. At most 2000 recent results within three years are examined; the latest 200 define competition home/away averages. Team form uses last 5/10 matches; venue profiles use the last 10 occurrences on the corresponding home/away side. H2H is descriptive and optional. No club results or club baselines enter a national competition's calculation. Venue means the provider's nominal side. The goal model does not use stadium, neutral-venue or event-level features. Ancillary statistics may be collected separately; measured xG and first-scorer features are not inferred.

Minimums are 20 competition results, 5 results per team and 3 per relevant venue. Venue scoring/conceding rates are regularized with baseline weight 5. Home expected goals = regularized home scoring × regularized away conceding / competition home average; away is analogous. A zero scoring baseline returns insufficient data rather than a 100%/0% estimate. Expected goals are bounded to 0–8 for numerical stability. Independent Poisson score probabilities yield 1X2, over/under 2.5 and BTTS. These are model estimates labelled Beklenen Gol, not measured shot-based statistics or ML predictions.

Confidence is a deterministic data-quality category, not an empirically calibrated accuracy probability. Medium requires each team 8 results, each venue 5, competition 50, last-5/10 scoring and conceding differences ≤0.5, and a model/market maximum difference ≤15 percentage points. High additionally requires team 10, venue 8, competition 100, at least two bookmakers and difference ≤10 points. National confidence is capped at medium. Missing market support or smaller valid samples produce low confidence. Both distinct team/venue sample size and competition baseline sample size are exposed.

Only complete decimal 1X2 prices >1 and finite values enter margin normalization: `(1/odd) / sum(1/odd)`. Valid bookmaker probabilities are averaged equally. Upcoming uses latest prematch; finished/started uses the stored prematch closing fields when available. Raw in-play prices are never read. Model/Piyasa differences are percentage points and are not labelled automatic value bets.

The cutoff is `min(now, kickoff)`. A prior result needs finished status, known full-time scores, kickoff at least three hours before cutoff, and created/updated/last-scraped observations no later than cutoff. This is deliberately conservative: historical archives imported later cannot produce trustworthy as-of forecasts and may return insufficient data. Bookmaker observations after cutoff are excluded from normalization, consensus and confidence; archived odds can still be shown separately with their observation time. The model never uses the target outcome or later fixtures.

A bounded process-local cache holds 128 statistics entries for ten minutes. Database count/MAX revisions detect normal external updates; SQLAlchemy after-commit invalidation handles every local committed mutation, including corrections below the global maximum timestamp. No Redis or new runtime dependency is introduced. Cards refresh at most every ten minutes and on league/date/manual changes. Fixtures from real stored EPL responses cover frontend populated, insufficient, failure, missing-element, click and stale-response cases.

No production scraping, queue launch or deployment is part of the prediction feature validation. The existing serialized multi-competition backfill architecture is retained. Enabled competitions need enough observed history before numerical estimates appear; production is scoped to the eight verified rollout leagues.

## Corners, cards and match events

See [live source findings and field mappings](docs/goaloo-match-statistics.md). The collector persists nullable real FT/HT statistics and incidents, preserving explicit zero while leaving absent fields NULL. All six inspected competitions exposed requested match statistics; none exposed a confirmed referee identity. Referee sections remain hidden without a real stored assignment.

The detail API/UI now includes corner/card last-5/10, season and venue summaries with usable denominators, expected corner/card counts and Poisson over probabilities when minimum samples are met. Combined cards mean published yellow + red counts; both sides need all four fields. Missing red totals are not treated as zero. Referee tendencies require ten real observations per metric and are not used in predictions.

```sh
python -m src.jobs.stats_backfill --league 36 --enqueue-only
python -m src.jobs.stats_backfill --league 36 --resume JOB_ID
```

The authenticated POST `/api/scraper/stats-backfill` queues a competition; POST `/api/scraper/statistics/{match_id}` refreshes one stored completed match. Existing durable progress/locking/resume behavior applies. Startup does not enqueue statistics backfills. Normal odds updates also collect available live/finished statistics without failing the odds job on absent statistics. No new external dependency is required.

### Automatic recommendation selection

All supported markets remain in the statistics API candidates. Cards contain only 0–3 qualified recommendations. Probability must reach 60%, reliability 0.70 and score 0.74, with team/venue/competition samples of at least 8/5/50. Reliability combines samples, recent stability, season agreement, completeness, confidence and actual bookmaker support. Double-chance and extreme-probability penalties limit insurance picks. Missing support is never fabricated.

One pick per correlated group is allowed: outcome (result/double chance/handicap), goal environment (totals/BTTS), corners, cards. No market receives a reserved slot. Filters inspect selected picks only. GET /api/predictions/best ranks selected picks across the seven-day window. Combined cards require published yellow and red totals for both teams.

### Compact dashboard and match analysis

The current homepage uses full-width dark market tables, compact status, category performance cards, shared league/date/selected-market/confidence filters and a collapsible match list with server-side kickoff/league/team sorting. Recommendation calculations and ranking gates are preserved. A zero-pick match remains available in the unfiltered match list. Selected-market filters cover the seven-day recommendation window; paginated global picks prevent dropping matches after the first 100 picks.

Market picks and keyboard-accessible match rows open the same native dialog: wide on desktop, full-screen at mobile widths. Its selected-match header remains above Özet, Tahminler, Gol, Korner, Kart & Hakem, Oranlar, Form and H2H tabs. Statistics load only on opening a match; tab changes reuse that response. Odds use the existing price/stage APIs. Generation checks invalidate late statistics, match and odds responses after switching or closing. Native dialog focus trapping, Escape, close and tab arrow keys are supported; #match=<id> permits direct links and browser Back. Missing observations remain unavailable.

## Dark market terminal and audited performance

The homepage is now a dark navy/cyan market terminal with summary cards, market-category navigation, dense selected-pick tables, server-side family/confidence/probability/team/date/sort filters, pagination and eight-tab match analysis. Disabled leagues are hidden. Production initialization upserts only the eight scoped verified leagues and disables others without deleting stored data.

GET /api/predictions and /api/predictions/best retain the 0–3 strongest-pick engine. First qualified prematch selections are frozen atomically in prediction_snapshots (one row per match, immutable JSON pick set); the worker captures after a successful data job and the predictions API captures published picks as well. Existing completed matches are never retroactively converted into performance records. GET /api/market-performance streams saved records and settles only from final scores or published final count statistics. Pending, missing final data and void results are distinct; success denominators include won/lost only. Dates filter capture time in Europe/Istanbul. Average reliability is a quality index, not calibrated accuracy. No profit/ROI or bookmaker settlement convention is claimed.

The new table is created additively by the existing PostgreSQL startup lock/create_all path; repeated startup is idempotent. Existing statistics/referee/event migrations remain in place. Diagnostics include enabled leagues and actual final corner/complete yellow+red/referee coverage. See docs/goaloo-match-statistics.md for verified source URLs and the authenticated, explicit serialized stats rollout. No production backfill or deployment was executed while developing this branch.
