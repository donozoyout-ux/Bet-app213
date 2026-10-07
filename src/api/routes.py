from datetime import date as Date, datetime, timezone, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hmac
import asyncio
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select, func, or_, text
from sqlalchemy.orm import aliased
from src.config import settings
from src.db import database
from src.models import League, Season, Team, Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, ScraperJob
from src.jobs.worker import enqueue, aware
from src.models import utcnow
from src.match_views import LIVE_STATUSES, DISPLAY_TIMEZONE, day_bounds
from .schemas import LeagueResponse, SeasonResponse, BookmakerResponse, MatchPage, MatchDetail, OddsResponse, JobResponse, JobRequest, StatusResponse, ScraperStatus
from src.diagnostics import database_summary

router = APIRouter()


@router.get('/diagnostics')
async def diagnostics():
    result = {'app': 'ok', 'database_configured': database.configured,
              'database_connected': False, 'worker_enabled': settings.worker_enabled,
              'auto_backfill_enabled': settings.auto_backfill_on_empty,
              'latest_job_status': None, 'total_matches': None, 'total_odds': None}
    if database.configured and database.ready:
        try:
            summary = await asyncio.wait_for(database_summary(database), timeout=5)
            result.update({key: summary[key] for key in ('latest_job_status', 'total_matches', 'total_odds')})
            result['database_connected'] = True
        except Exception:
            logging.getLogger(__name__).exception('[DB] diagnostics unavailable')
    return result


async def session_dependency():
    if not database.configured or not database.ready:
        raise HTTPException(503, 'Database not ready. Configure DATABASE_URL and initialize PostgreSQL.')
    async with database.session() as session:
        yield session


async def require_scraper_token(authorization: str | None = Header(default=None)):
    if not settings.scraper_token:
        raise HTTPException(503, 'Scraper controls disabled: configure SCRAPER_API_TOKEN.')
    if not authorization or not hmac.compare_digest(authorization, f'Bearer {settings.scraper_token}'):
        raise HTTPException(401, 'A valid scraper Bearer token is required.')


def job_response(job, league_name=None):
    data = JobResponse.model_validate(job)
    data.current_league = league_name
    return data


@router.get('/status', response_model=StatusResponse)
async def status():
    if not database.configured or not database.ready:
        return StatusResponse(database='unavailable' if database.configured else 'unconfigured', app_env=settings.app_env)
    async with database.session() as session:
        await session.execute(text('SELECT 1'))
        total = await session.scalar(select(func.count(Match.id)))
        odds_total = sum([await session.scalar(select(func.count(model.id))) for model in [Odds1X2, AsianHandicap, AsianTotals]])
        last = await session.scalar(select(func.max(Match.last_scraped_at)))
    return StatusResponse(database='connected', database_engine=database.engine.dialect.name, total_matches=total, total_odds=odds_total, last_scraped_at=aware(last), app_env=settings.app_env)


@router.get('/leagues', response_model=list[LeagueResponse])
async def leagues(session=Depends(session_dependency)):
    return (await session.scalars(select(League).order_by(League.name))).all()


@router.get('/leagues/{league_id}/seasons', response_model=list[SeasonResponse])
async def seasons(league_id: int, session=Depends(session_dependency)):
    if not await session.get(League, league_id):
        raise HTTPException(404, 'League not found (use the internal ID from /api/leagues)')
    rows = (await session.scalars(select(Season).where(Season.league_id == league_id).order_by(Season.season_name.desc()))).all()
    rounds = (await session.execute(select(Match.season_id, Match.round).where(Match.league_id == league_id).distinct())).all()
    result = []
    for row in rows:
        item = SeasonResponse.model_validate(row)
        item.rounds = sorted(r for season_id, r in rounds if season_id == row.id)
        result.append(item)
    return result


@router.get('/bookmakers', response_model=list[BookmakerResponse])
async def bookmakers(session=Depends(session_dependency)):
    return (await session.scalars(select(Bookmaker).order_by(Bookmaker.id))).all()


def match_query():
    home, away = aliased(Team), aliased(Team)
    query = select(Match, League, Season, home.name, away.name).join(League, Match.league_id == League.id).join(Season, Match.season_id == Season.id).join(home, Match.home_team_id == home.id).join(away, Match.away_team_id == away.id)
    return query, home, away


def match_response(row):
    match, league, season, home, away = row
    fields = ['id', 'external_match_id', 'league_id', 'round', 'status', 'ht_home', 'ht_away', 'ft_home', 'ft_away', 'odds_complete']
    return {**{f: getattr(match, f) for f in fields}, 'kickoff_at': aware(match.kickoff_at), 'updated_at': aware(match.updated_at),
            'external_league_id': league.external_id, 'league': league.name, 'season': season.season_name,
            'home_team': home, 'away_team': away}


@router.get('/matches', response_model=MatchPage)
async def matches(league: str | None = None, season: str | None = None,
                  round: int | None = Query(default=None, ge=1), date: Date | None = None,
                  status: str | None = None, team: str | None = None, bookmaker: str | None = None,
                  limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), include_odds: bool = False,
                  view: Literal['all', 'live', 'today', 'upcoming', 'history'] = 'all',
                  display_timezone: str = DISPLAY_TIMEZONE,
                  upcoming_days: int = Query(default=7, ge=1, le=3650),
                  session=Depends(session_dependency)):
    query, home, away = match_query()
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, 'Unknown display timezone')
    now = utcnow()
    ascending = view in {'today', 'upcoming'}
    if view == 'live':
        query = query.where(Match.status.in_(LIVE_STATUSES))
    elif view == 'today':
        start, end = day_bounds(now.astimezone(zone).date(), display_timezone)
        query = query.where(Match.kickoff_at >= start, Match.kickoff_at < end)
    elif view == 'upcoming':
        query = query.where(Match.status == 'scheduled', Match.kickoff_at > now)
        if not date:
            query = query.where(Match.kickoff_at <= now + timedelta(days=upcoming_days))
    elif view == 'history':
        query = query.where(Match.status == 'finished')
    if league:
        query = query.where(League.id == int(league)) if league.isdigit() else query.where(League.name.ilike(f'%{league}%'))
    if season:
        query = query.where(Season.season_name == season.replace('/', '-'))
    if round is not None:
        query = query.where(Match.round == round)
    if date:
        start, end = day_bounds(date, display_timezone)
        query = query.where(Match.kickoff_at >= start, Match.kickoff_at < end)
    if status:
        query = query.where(Match.status == status)
    if team:
        query = query.where(or_(home.name.ilike(f'%{team}%'), away.name.ilike(f'%{team}%')))
    if bookmaker:
        book = await session.scalar(select(Bookmaker.id).where(Bookmaker.name.ilike(bookmaker)))
        if book is None:
            raise HTTPException(400, 'Unknown bookmaker. Use Crown, Bet365 or Sbobet.')
        query = query.where(or_(*[select(model.id).where(model.match_id == Match.id, model.bookmaker_id == book).exists() for model in [Odds1X2, AsianHandicap, AsianTotals]]))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    order = (Match.kickoff_at.asc(), Match.id.asc()) if ascending else (Match.kickoff_at.desc(), Match.id.desc())
    rows = (await session.execute(query.order_by(*order).limit(limit).offset(offset))).all()
    items = [match_response(row) for row in rows]
    if include_odds and items:
        grouped = await odds_data_bulk(session, [item['id'] for item in items])
        for item in items:
            item['odds'] = grouped.get(item['id'], [])
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


async def odds_data(session, match_id):
    return (await odds_data_bulk(session, [match_id])).get(match_id, [])


async def odds_data_bulk(session, match_ids):
    result = {}
    for model, market in [(Odds1X2, '1x2'), (AsianHandicap, 'ah'), (AsianTotals, 'ou')]:
        rows = (await session.execute(select(model, Bookmaker.name).join(Bookmaker).where(model.match_id.in_(match_ids)))).all()
        for row, name in rows:
            keys = ['home', 'draw', 'away'] if market == '1x2' else (['line', 'home', 'away'] if market == 'ah' else ['line', 'over', 'under'])
            result.setdefault(row.match_id, []).append(OddsResponse(bookmaker=name, bookmaker_id=row.bookmaker_id, market=market,
                opening={k: getattr(row, 'opening_' + k) for k in keys}, latest={k: getattr(row, 'latest_' + k) for k in keys},
                closing={k: getattr(row, 'closing_' + k) for k in keys}, updated_at=aware(row.updated_at), raw=row.raw,
                line_perspective='home' if market == 'ah' else None))
    return result


@router.get('/matches/{match_id}', response_model=MatchDetail)
async def match_detail(match_id: int, session=Depends(session_dependency)):
    query, _, _ = match_query()
    row = (await session.execute(query.where(Match.id == match_id))).first()
    if not row:
        raise HTTPException(404, 'Match not found (use the internal ID from /api/matches)')
    return {**match_response(row), 'odds': await odds_data(session, match_id), 'raw': row[0].raw}


@router.get('/matches/{match_id}/odds', response_model=list[OddsResponse])
async def match_odds(match_id: int, session=Depends(session_dependency)):
    if not await session.get(Match, match_id):
        raise HTTPException(404, 'Match not found')
    return await odds_data(session, match_id)


@router.get('/scraper/status', response_model=ScraperStatus)
async def scraper_status(session=Depends(session_dependency)):
    rows = (await session.execute(select(ScraperJob, League.name).join(League).order_by(ScraperJob.id.desc()).limit(10))).all()
    return {'status': 'running' if any(j.status in {'queued', 'running'} for j, _ in rows) else 'idle',
            'jobs': [job_response(j, name) for j, name in rows]}


@router.get('/scraper/jobs/{job_id}', response_model=JobResponse)
async def job_detail(job_id: int, session=Depends(session_dependency)):
    row = (await session.execute(select(ScraperJob, League.name).join(League).where(ScraperJob.id == job_id))).first()
    if not row:
        raise HTTPException(404, 'Scraper job not found')
    return job_response(*row)


async def submit(kind, request, session, match_id=None):
    try:
        job = await enqueue(kind, request.league_id, request.start_year, match_id, request.resume_job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    league = await session.get(League, job.league_id)
    return job_response(job, league.name)


@router.post('/scraper/backfill', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def backfill(request: JobRequest, session=Depends(session_dependency)):
    return await submit('backfill', request, session)


@router.post('/scraper/update', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def daily_update(request: JobRequest, session=Depends(session_dependency)):
    return await submit('update', request, session)


@router.post('/scraper/match/{match_id}', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def scrape_match(match_id: int, session=Depends(session_dependency)):
    match = await session.get(Match, match_id)
    if not match:
        raise HTTPException(404, 'Match not found')
    league = await session.get(League, match.league_id)
    return await submit('match', JobRequest(league_id=league.external_id), session, match_id)
