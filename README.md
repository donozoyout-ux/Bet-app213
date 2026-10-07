# BetApp213

FastAPI serves the existing Stitch dashboard from `src/api/dashboard.html`.
The active Goaloo adapters use verified JSON/AJAX responses, SQLAlchemy async
persists matches and bookmaker markets, and a durable database-backed worker
collects the English Premier League (Goaloo ID 36) from the 2024–2025 season.
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
initial schema; `create_all` does not alter existing tables. Future schema
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
  If absent the controls are disabled. Dashboard password field keeps it in
  memory only; no token is embedded in HTML or persisted to browser storage.
- `SCRAPER_WORKER_ENABLED`: default true. With false, use CLI to consume jobs.
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
for matches within the previous 7 days through the next 2 days. Run it more
frequently if near-live collection is desired; dashboard polling only reads
the database and does not trigger provider collection.

## API

- `GET /health`, `GET /api/status`
- `GET /api/leagues`, `GET /api/leagues/{league_id}/seasons`
- `GET /api/bookmakers`
- `GET /api/matches`: `league` (internal ID or name), `season`, `round`, `date`
  (UTC calendar day), `status`, `team`, `bookmaker`, `limit`, `offset`;
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
League, season, round, status and team controls filter the match table. The
season/round selectors filter display; backfill always discovers all seasons
from 2024. Bookmaker tabs show each company's separate markets.

## Render and daily scheduling

`render.yaml` defines a Python 3.12 web service and managed PostgreSQL. Review
Render's resource plan/billing choices before applying the blueprint. The start
command binds `$PORT` and uses one gunicorn worker; the scraper runs as an async
background task and all progress is durable in PostgreSQL. No browser binaries,
Prefect, Docker, Redis or pandas are installed in production.
The module-only Gunicorn target `src.api.main` resolves the exported
`application` alias; Uvicorn's local target remains `src.api.main:app`.

For the existing Render service configure `DATABASE_URL`, `APP_ENV=production`,
and `SCRAPER_API_TOKEN`, then use the build/start commands from `render.yaml`.
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
```

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
