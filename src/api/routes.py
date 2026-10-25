from src.match_views import upcoming_predicate, active_bounds
from datetime import date as Date, datetime, timezone, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hmac
import asyncio
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select, func, or_, text
from sqlalchemy.orm import aliased, defer
from src.config import settings
from src.db import database
from src.models import League, Season, Team, Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, MarketOdds, ScraperJob, MatchStatistics
from src.jobs.worker import enqueue, aware, enqueue_all_updates
from src.models import utcnow
from src.match_views import LIVE_STATUSES, DISPLAY_TIMEZONE, day_bounds
from .schemas import LeagueResponse, SeasonResponse, BookmakerResponse, MatchPage, MatchDetail, OddsResponse, JobResponse, JobRequest, StatusResponse, ScraperStatus
from src.diagnostics import database_summary
from src.analytics.service import statistics as compute_statistics, revision as prediction_revision
from .prediction_schemas import PredictionPage, Statistics, GlobalPicks
from src.analytics.recommendations import market_matches, summarize_availability
from .read_cache import summary_cache, prediction_cache, performance_cache, revision_cache

from src.query_profile import profile,stage

router = APIRouter()


@router.get('/diagnostics')
async def diagnostics():
    result = {'app': 'ok', 'database_configured': database.configured,
              'database_connected': False, 'worker_enabled': settings.worker_enabled,
              'auto_backfill_enabled': settings.auto_backfill_on_empty,
              'latest_job_status': None, 'total_matches': None, 'total_odds': None,
              'enabled_competitions': None, 'completed_competitions': None,
              'active_backfill_competition': None, 'queued_backfills': None,'enabled_leagues':[],'match_counts_by_league':[],'stats_rows':None,'matches_with_corners':None,'matches_with_cards':None,'matches_with_referee':None,'active_backfill':None,'stats_coverage_by_league':[],'active_stats_backfill':None,'queued_stats_backfills':[]}
    if database.configured and database.ready:
        try:
            summary = await summary_cache.get(database.engine, 'diagnostics', lambda: database_summary(database))
            result.update({key: summary[key] for key in ('latest_job_status', 'total_matches', 'total_odds', 'enabled_competitions', 'completed_competitions', 'active_backfill_competition', 'queued_backfills')})
            result.update({key:summary[key] for key in ('enabled_leagues','match_counts_by_league','stats_rows','matches_with_corners','matches_with_cards','matches_with_referee','active_backfill','stats_coverage_by_league','active_stats_backfill','queued_stats_backfills')})
            result['database_connected'] = True
            result['fixture_discovery'] = summary.get('fixture_discovery', [])
        except Exception:
            logging.getLogger(__name__).exception('[DB] diagnostics unavailable')
    return result


async def session_dependency():
    if not database.configured or not database.ready:
        raise HTTPException(503, 'Database not ready. Configure DATABASE_URL and initialize PostgreSQL.')
    async with database.session() as session:
        async with asyncio.timeout(12):
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
    if database.configured and database.ready:
        return await summary_cache.get(database.engine, 'status', uncached_status)
    return await uncached_status()


async def uncached_status():
    if not database.configured or not database.ready:
        return StatusResponse(database='unavailable' if database.configured else 'unconfigured', app_env=settings.app_env)
    async with database.session() as session:
        await session.execute(text('SELECT 1'))
        total = await session.scalar(select(func.count(Match.id)))
        from datetime import timedelta
        new_matches_24h = await session.scalar(select(func.count(Match.id)).where(Match.created_at >= utcnow() - timedelta(hours=24)))
        latest_new_match_at = await session.scalar(select(func.max(Match.created_at)))
        odds_total = await session.scalar(select(sum(select(func.count(model.id)).scalar_subquery() for model in (Odds1X2, AsianHandicap, AsianTotals))))
        last = await session.scalar(select(func.max(Match.last_scraped_at)))
        stats_last=await session.scalar(select(func.max(MatchStatistics.updated_at)))
        last=max((aware(value) for value in [last,stats_last] if value is not None),default=None)
        from src.jobs.monitoring import collection_health
        collection=await collection_health(session,utcnow())
    return StatusResponse(database='connected', database_engine=database.engine.dialect.name, total_matches=total, new_matches_24h=new_matches_24h, latest_new_match_at=latest_new_match_at, total_odds=odds_total, last_scraped_at=aware(last), app_env=settings.app_env,**collection)


@router.get('/leagues', response_model=list[LeagueResponse])
async def leagues(session=Depends(session_dependency)):
    return (await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.competition_type, League.priority, League.name))).all()


@router.get('/leagues/{league_id}/seasons', response_model=list[SeasonResponse])
async def seasons(league_id: int, session=Depends(session_dependency)):
    if not await session.scalar(select(League.id).where(League.id==league_id,League.enabled.is_(True))):
        raise HTTPException(404, 'League not found (use the internal ID from /api/leagues)')
    rows = (await session.scalars(select(Season).where(Season.league_id == league_id).order_by(Season.season_name.desc()))).all()
    rounds = (await session.execute(select(Match.season_id, Match.round, Match.round_label).where(Match.league_id == league_id).distinct())).all()
    result = []
    for row in rows:
        item = SeasonResponse.model_validate(row)
        item.rounds = sorted({r for season_id, r, label in rounds if season_id == row.id})
        item.round_labels = {r: label for season_id, r, label in rounds if season_id == row.id and label}
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
    fields = ['id', 'external_match_id', 'league_id', 'round', 'round_label', 'stage_key', 'status', 'ht_home', 'ht_away', 'ft_home', 'ft_away', 'odds_complete']
    return {**{f: getattr(match, f) for f in fields}, 'live_minute': match.live_minute, 'status_observed_at': aware(match.status_observed_at), 'kickoff_at': aware(match.kickoff_at), 'updated_at': aware(match.updated_at),
            'external_league_id': league.external_id, 'league': league.name, 'competition_type': league.competition_type, 'season': season.season_name,
            'home_team': home, 'away_team': away}


@router.get('/matches', response_model=MatchPage)
async def matches(league: str | None = None, season: str | None = None,
                  round: int | None = Query(default=None, ge=1), date: Date | None = None,
                  status: str | None = None, team: str | None = None, bookmaker: str | None = None,
                  limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), include_odds: bool = False,
                  view: Literal['all', 'live', 'today', 'upcoming', 'history'] = 'all',
                  display_timezone: str = DISPLAY_TIMEZONE,
                  sort: Literal['kickoff','league','team'] = 'kickoff',
                  upcoming_days: int = Query(default=7, ge=1, le=3650),
                  session=Depends(session_dependency)):
    query, home, away = match_query()
    query = query.where(League.enabled.is_(True))
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, 'Unknown display timezone')
    now = utcnow()
    ascending = view in {'today', 'upcoming'}
    if view == 'live':
        query = query.where(Match.status.in_(LIVE_STATUSES))
    elif view == 'today':
        start, end = day_bounds(date or now.astimezone(zone).date(), display_timezone)
        query = query.where(Match.kickoff_at >= start, Match.kickoff_at < end)
    elif view == 'upcoming':
        query = query.where(Match.status == 'scheduled', Match.kickoff_at > now)
    elif view == 'history':
        query = query.where(Match.status == 'finished')
    if league:
        query = query.where(League.id == int(league)) if league.isdigit() else query.where(League.name.ilike(f'%{league}%'))
    if season:
        query = query.where(Season.season_name == season.replace('/', '-'))
    if round is not None:
        query = query.where(Match.round == round)
    if date and view != 'today':
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
    upcoming_expanded = False
    if view == 'upcoming' and not date:
        window = query.where(Match.kickoff_at <= now + timedelta(days=upcoming_days))
        in_window = await session.scalar(select(func.count()).select_from(window.subquery()))
        if in_window:
            query = window
        else:
            nearest = await session.scalar(select(func.min(query.subquery().c.kickoff_at)))
            if nearest:
                start, end = day_bounds(aware(nearest).astimezone(zone).date(), display_timezone)
                query = query.where(Match.kickoff_at >= start, Match.kickoff_at < end)
                upcoming_expanded = True
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    order = (Match.kickoff_at.asc(), Match.id.asc()) if ascending else (Match.kickoff_at.desc(), Match.id.desc())
    if sort == 'league':order = (League.name.asc(), *order)
    elif sort == 'team':order = (home.name.asc(), *order)
    rows = (await session.execute(query.order_by(*order).limit(limit).offset(offset))).all()
    items = [match_response(row) for row in rows]
    if include_odds and items:
        grouped = await odds_data_bulk(session, [item['id'] for item in items])
        for item in items:
            item['odds'] = grouped.get(item['id'], [])
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'upcoming_expanded': upcoming_expanded}


@router.get('/prediction-fixtures')
async def prediction_fixtures(date: Date | None=None,league: int | None=Query(default=None,gt=0,le=2147483647),
        status: Literal['all','upcoming','live','finished']='all',
        limit: int=Query(default=25,ge=1,le=100),offset: int=Query(default=0,ge=0),
        session=Depends(session_dependency)):
    from src.analytics.fixture_predictions import fixture_page
    from .read_cache import fixture_cache
    now=utcnow();day=date or now.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).date()
    if not 1970<=day.year<=2100:raise HTTPException(400,'Date must be between 1970 and 2100')
    return await fixture_cache.get(session.bind,('fixtures-v1',day,league,status,limit,offset),
        lambda:fixture_page(session,day,now,league=league,status=status,limit=limit,offset=offset))


@router.get('/predictions', response_model=PredictionPage)
async def predictions(league: str | None = None, date: Date | None = None,
                      limit: int = Query(default=8, ge=1, le=20), priced_only: bool=False,
                      confidence: Literal['high','medium','low'] | None=None, min_probability: float=Query(default=0,ge=0,le=1),
                      market: Literal['all','result','goals','btts','corners','cards','yellow_cards','red_cards','asian_handicap','double_chance','first_half_goals','second_half_goals']='all', session=Depends(session_dependency)):
    calculated,now,evaluated=await calculate_predictions(session,league,date)
    calculated=[{**item,'recommendations':[r for r in item['recommendations'] if market_matches([r],market) and (not confidence or r['confidence']==confidence) and r['probability']>=min_probability]} for item in calculated]
    if priced_only:
        from src.analytics.coupon import coupon_qualified
        stale=set(session.info.get('prediction_snapshot',{}).get('stale_leagues',[]))
        calculated=[{**item,'recommendations':[r for r in item['recommendations'] if coupon_qualified(r) and (r.get('verified_odds') or {}).get('coupon_eligible')]} for item in calculated if item['match']['league_id'] not in stale]
    calculated=[item for item in calculated if item['recommendations']]
    # Prioritize exact, fresh prices before limiting the home-page response.
    def priced(rec):
        return bool(rec.get('verified_odds',{}).get('coupon_eligible'))
    for item in calculated:
        item['recommendations'].sort(key=lambda r:not priced(r))
        item['strongest_prediction']=item['recommendations'][0]['id']
    calculated.sort(key=lambda item:not any(priced(r) for r in item['recommendations']))
    primary = [item for item in calculated if aware(item['match']['kickoff_at']) <= now + timedelta(hours=72)]
    from src.analytics.history_cache import enabled as hybrid_enabled
    enough=len(primary)>=min(5,limit)
    chosen=calculated if hybrid_enabled() or date or not enough else primary
    return {'availability':summarize_availability(session.info.get('count_availability',[]),market,len(chosen)), 'items':chosen[:limit],'window_hours':int((active_bounds(now)[1]-now).total_seconds()/3600) if hybrid_enabled() else (72 if enough and not date else 168),'generated_at':session.info['prediction_snapshot']['generated_at'],'cache':session.info['prediction_snapshot']}


async def calculate_predictions(session,league,date,team=None,statistical_all=False,include_engine_analysis=False):
    from src.analytics.board_cache import read_board
    from src.analytics.coupon import current_odds
    with profile('predictions'):
      async with asyncio.timeout(4):
        with stage('board_read'):
            result=await read_board(session,league,date,team,utcnow(),statistical_all=statistical_all,include_engine_analysis=include_engine_analysis)
        items,now,_=result
        for item in items:item['recommendations']=[dict(r) for r in (item['recommendations'] if statistical_all else item['recommendations'][:2])]
        with stage('current_quotes'):
            await current_odds(session,{i['match']['id']:i['recommendations'] for i in items},now)
        session.info['prediction_snapshot']['verified_price_matches']=len({i['match']['id'] for i in items
            if any((r.get('verified_odds') or {}).get('coupon_eligible') for r in i['recommendations'])})
        return result


from src.analytics.coupon import DraftRequest, quote_draft, settle_draft


@router.post('/coupon/quote')
async def coupon_quote(request: DraftRequest, session=Depends(session_dependency)):
    async with asyncio.timeout(4):return await quote_draft(session,request,utcnow())


@router.post('/coupon/status')
async def coupon_status(request: DraftRequest, session=Depends(session_dependency)):
    async with asyncio.timeout(4):return await settle_draft(session,request)


from src.analytics.saved_coupons import automatic,save_manual,accessible,detail as coupon_detail,summary as coupon_summary,owner_hash
from src.models import SavedCoupon


@router.post('/coupons/automatic')
async def automatic_coupon(strategy: Literal['balanced','cautious','value']='balanced', max_matches: str | None=Query(default=None,include_in_schema=False),session=Depends(session_dependency)):
    if max_matches is not None:raise HTTPException(422,'Automatic coupon size is determined by the dynamic engine; remove max_matches')
    from src.analytics.publications import publish_daily
    with profile('daily_evaluation'):
      async with asyncio.timeout(10):
        result=await publish_daily(session,utcnow(),strategy)
        await session.commit()
        from src.analytics.publications import read_daily
        return await read_daily(session,utcnow(),strategy)


@router.get('/coupons/daily')
async def daily_coupon(strategy: Literal['balanced','cautious','value']='balanced',session=Depends(session_dependency)):
    from src.analytics.publications import read_daily
    with profile('daily_read'):
        return await read_daily(session,utcnow(),strategy)


@router.get('/coupons/diagnostics')
async def daily_coupon_diagnostics(session=Depends(session_dependency)):
    """Current candidate census only; no generation, collection or delivery."""
    from src.analytics.daily_pool import daily_pool
    from zoneinfo import ZoneInfo
    from src.analytics.publications import read_daily
    now=utcnow()
    async def inspect_current():
        await daily_pool(session,now)
        publication=await read_daily(session,now)
        return {**session.info['coupon_diagnostics'],'checked_at':now,
            'publication':dict(status=publication.get('status','published' if publication.get('items') else 'no_eligible_selections'),reason=publication.get('reason'),selections=len(publication.get('items',[])),checked_at=publication.get('data_updates',{}).get('checked_at'),mixed_bookmakers=publication.get('mixed_bookmakers',False)),
            'read_only':True}
    async with asyncio.timeout(4):
        return await summary_cache.get(session.bind,('daily_coupon_diagnostics',now.astimezone(ZoneInfo('Europe/Istanbul')).date()),inspect_current)


@router.get('/dashboard/revision')
async def dashboard_revision(session=Depends(session_dependency)):
    from src.analytics.publications import revisions
    async with asyncio.timeout(4):
        return await revision_cache.get(session.bind,'publication_manifest',lambda:revisions(session,utcnow()))


@router.get('/odds-coverage')
async def odds_coverage(session=Depends(session_dependency)):
    from src.analytics.odds_diagnostics import report, source_report
    records,now,_=await calculate_predictions(session,None,None,include_engine_analysis=True)
    from copy import deepcopy
    from src.analytics.coupon import current_odds
    from src.analytics.engines import attach_coverage
    analysis=deepcopy(session.info.get('engine_analysis', []))
    await current_odds(session,{r['match_id']:r['candidates'] for r in analysis},now)
    for row in analysis:
        attach_coverage(row['market_engines'],row.pop('candidates'))
    from src.models import League
    ids=list(await session.scalars(select(Match.id).join(League).where(League.enabled.is_(True),Match.status=='scheduled',Match.kickoff_at>now,upcoming_predicate(now))))
    return {**report(records,now),'cache':session.info['prediction_snapshot'], 'engine_analysis':analysis,
            'source_scope':'enabled_upcoming_7_days','source_match_count':len(ids),
            'source_rows': await source_report(session, now, match_ids=ids)}


@router.post('/coupons')
async def create_coupon(request: DraftRequest,x_coupon_key: str | None=Header(default=None),session=Depends(session_dependency)):
    async with asyncio.timeout(10):
        row=await save_manual(session,request,x_coupon_key,utcnow());await session.commit();return await coupon_detail(session,row)


@router.put('/coupons/{coupon_id}')
async def edit_coupon(coupon_id: str,request: DraftRequest,x_coupon_key: str | None=Header(default=None),session=Depends(session_dependency)):
    async with asyncio.timeout(10):
        row=await save_manual(session,request,x_coupon_key,utcnow(),base_id=coupon_id);await session.commit();return await coupon_detail(session,row)


@router.get('/coupons')
async def coupon_history(x_coupon_key: str | None=Header(default=None),limit: int=Query(default=10,ge=1,le=50),offset: int=Query(default=0,ge=0),session=Depends(session_dependency)):
    allowed=SavedCoupon.owner_hash.is_(None)
    if x_coupon_key:allowed=or_(allowed,SavedCoupon.owner_hash==owner_hash(x_coupon_key))
    query=select(SavedCoupon.id,SavedCoupon.root_id,SavedCoupon.revision,SavedCoupon.kind,SavedCoupon.created_at,func.json_array_length(SavedCoupon.payload['items']).label('selections'),SavedCoupon.payload['combined_odds'].label('combined_odds')).where(allowed).order_by(SavedCoupon.created_at.desc(),SavedCoupon.revision.desc(),SavedCoupon.id).limit(limit).offset(offset)
    return {'items':[dict(r._mapping) for r in await session.execute(query)],'limit':limit,'offset':offset}


@router.get('/coupons/{coupon_id}')
async def get_coupon(coupon_id: str,x_coupon_key: str | None=Header(default=None),session=Depends(session_dependency)):
    return await coupon_detail(session,await accessible(session,coupon_id,x_coupon_key))


@router.get('/telegram/status')
async def telegram_status(session=Depends(session_dependency)):
    from src.jobs.telegram import status
    return await status(session,utcnow())


@router.post('/telegram/verify',dependencies=[Depends(require_scraper_token)])
async def verify_telegram_connection():
    from src.jobs.telegram_verification import verify
    return await verify()


@router.post('/telegram/test-message', dependencies=[Depends(require_scraper_token)])
async def telegram_test_message():
    from src.jobs.telegram import send_test_message
    return await send_test_message()


@router.post('/telegram/tick',dependencies=[Depends(require_scraper_token),Depends(session_dependency)])
async def telegram_tick():
    from src.jobs.telegram import dispatch
    async with asyncio.timeout(25):return await dispatch(database)


@router.post('/coupons/{coupon_id}/telegram',dependencies=[Depends(require_scraper_token)])
async def share_coupon(coupon_id: str,x_coupon_key: str | None=Header(default=None),session=Depends(session_dependency)):
    await accessible(session,coupon_id,x_coupon_key)
    await session.rollback()  # Release the HTTP reader before claiming and sending.
    from src.jobs.telegram import dispatch
    async with asyncio.timeout(25):return await dispatch(database,coupon_id=coupon_id)


async def uncached_predictions(session,league,date,team=None,now=None,publish=True):
    now = now or utcnow()
    query, home, away = match_query()
    query = query.where(League.enabled.is_(True), Match.status == 'scheduled', Match.kickoff_at > now,
                        upcoming_predicate(now))
    if league:
        query = query.where(League.id == int(league)) if league.isdigit() else query.where(League.name.ilike(f'%{league}%'))
    if date:
        start, end = day_bounds(date, DISPLAY_TIMEZONE)
        query = query.where(Match.kickoff_at >= start, Match.kickoff_at < end)
    if team:query=query.where(or_(home.name.ilike('%'+team+'%'),away.name.ilike('%'+team+'%')))
    rows = (await session.execute(query.order_by(Match.kickoff_at.asc(), Match.id.asc()))).all()
    from src.analytics.service import prefetch_markets
    await prefetch_markets(session, [row[0] for row in rows])
    rev = session.info.get('board_revision') or await prediction_revision(session)
    calculated = [];observations=[]
    session.info['count_availability']=[]
    session.info['engine_analysis']=[]
    session.info['coupon_records']=[]
    session.info['match_analyses']={}
    for row in rows:
        await asyncio.sleep(0)  # Allow liveness and short requests between matches.
        result = await compute_statistics(session, row[0], match_response(row), now, rev)
        session.info['match_analyses'][row[0].id]=result
        session.info['count_availability'].append((row[0].id,result['market_availability']))
        session.info['coupon_records'].append(dict(match=match_response(row),candidates=result['candidates']))
        session.info['engine_analysis'].append(dict(match_id=row[0].id,
            market_engines=result['market_engines'], candidates=[c for c in result['candidates'] if c['market'] in ('result','goals','double_chance','btts')]))
        if result['recommendations']:
            item={key:result[key] for key in ('match','prediction','bookmaker_consensus','as_of','recommendations','strongest_prediction')}
            item['match_id']=row[0].id;calculated.append(item)
            observations.append((row[0],result['recommendations']))
    from src.analytics.performance import capture_many
    session.info['board_observations']=observations
    if publish and await capture_many(session,observations,utcnow()):await session.commit()
    return calculated,now,len(rows)


@router.get('/predictions/best',response_model=GlobalPicks)
async def global_predictions(league: str | None=None,date: Date | None=None,team: str | None=None,sort: Literal['ranking','kickoff','probability','odds','value']='ranking',bookmaker: Literal['all','Crown','Bet365','Sbobet'] | None=None,limit: int=Query(default=20,ge=1,le=100),offset: int=Query(default=0,ge=0),
                             statistical_all: bool=False, priced_only: bool=False, market: Literal['all','result','goals','btts','corners','cards','yellow_cards','red_cards','asian_handicap','double_chance','first_half_goals','second_half_goals']='all',confidence: Literal['high','medium','low'] | None=None,min_probability: float=Query(default=0,ge=0,le=1),session=Depends(session_dependency)):
    matches,now,evaluated=await calculate_predictions(session,league,date,team,statistical_all=statistical_all)
    picks=[{'match':item['match'],'recommendation':r,'as_of':item['as_of']} for item in matches for r in item['recommendations'] if market_matches([r],market) and (not confidence or r['confidence']==confidence) and r['probability']>=min_probability]
    def _vo(r):
        return r.get('verified_odds') if isinstance(r, dict) else getattr(r, 'verified_odds', None)
    def _odds_val(r, field, bk=None):
        vo = _vo(r)
        if not vo: return None
        if isinstance(vo, dict):
            if bk and bk != 'all': return vo.get('bookmakers', {}).get(bk, {}).get(field)
            return vo.get(field)
        if bk and bk != 'all':
            b = getattr(vo, 'bookmakers', {}).get(bk)
            return getattr(b, field, None) if b else None
        return getattr(vo, field, None)
    if bookmaker and bookmaker != 'all':
        picks = [p for p in picks if _odds_val(p['recommendation'], 'current_odds', bookmaker) is not None]
    if priced_only:
        picks=[p for p in picks if (_vo(p['recommendation']) or {}).get('coupon_eligible') and (_vo(p['recommendation']) or {}).get('freshness')=='fresh' and (not bookmaker or bookmaker=='all' or (_vo(p['recommendation']) or {}).get('bookmakers',{}).get(bookmaker,{}).get('coupon_eligible'))]
    total=len(picks)
    picks.sort(key=lambda item:(-item['recommendation']['score'],-item['recommendation']['reliability'],item['match']['kickoff_at'],item['match']['id'],item['recommendation']['id']))
    if sort=='kickoff':picks.sort(key=lambda item:(item['match']['kickoff_at'],item['match']['id']))
    elif sort=='probability':picks.sort(key=lambda item:-item['recommendation']['probability'])
    elif sort=='odds':
        field = 'current_odds' if bookmaker and bookmaker != 'all' else 'best_price'
        picks.sort(key=lambda item:(-(_odds_val(item['recommendation'], field, bookmaker) or 0), -item['recommendation']['probability']))
    elif sort=='value':
        field = 'value' if bookmaker and bookmaker != 'all' else 'best_value'
        picks.sort(key=lambda item:(-(_odds_val(item['recommendation'], field, bookmaker) if _odds_val(item['recommendation'], field, bookmaker) is not None else -999), -item['recommendation']['probability']))
    return {'availability':summarize_availability(session.info.get('count_availability',[]),market,len(picks)), 'items':picks[offset:offset+limit],'generated_at':session.info['prediction_snapshot']['generated_at'],'cache':session.info['prediction_snapshot'],'evaluated_matches':evaluated,'total':total}


@router.get('/matches/{match_id}/statistics', response_model=Statistics)
async def match_statistics(match_id: int, session=Depends(session_dependency)):
    with profile('analysis_read'):
        return await read_published_statistics(match_id,session)


async def read_published_statistics(match_id,session):
    query, _, _ = match_query()
    row = (await session.execute(query.options(defer(Match.raw)).where(Match.id == match_id, League.enabled.is_(True)))).first()
    if not row:
        raise HTTPException(404, 'Match not found')
    from src.analytics.publications import read_match_analysis
    data=await read_match_analysis(session,row[0],utcnow())
    if data is None:
        if row[0].kickoff_at and aware(row[0].kickoff_at)<=utcnow():
            raise HTTPException(404,dict(code='analysis_archive_missing',message='Kayıtlı maç öncesi analiz yok; geçmişe dönük tahmin üretilmez.'))
        raise HTTPException(503,dict(code='analysis_publication_pending',message='Kaydedilmiş maç analizi henüz güncel değil; arka plan yayını bekleniyor.'))
    data['match']=match_response(row)
    return data


async def odds_data(session, match_id):
    return (await odds_data_bulk(session, [match_id])).get(match_id, [])


async def odds_data_bulk(session, match_ids):
    from src.analytics.odds_matching import price_observed_at
    result = {}
    for model, market in [(Odds1X2, '1x2'), (AsianHandicap, 'ah'), (AsianTotals, 'ou'), (MarketOdds, None)]:
        rows = (await session.execute(select(model, Bookmaker.name).join(Bookmaker).where(model.match_id.in_(match_ids)))).all()
        for row, name in rows:
            keys = ['home', 'draw', 'away'] if market == '1x2' else (['line', 'home', 'away'] if market == 'ah' else ['line', 'over', 'under'])
            result.setdefault(row.match_id, []).append(OddsResponse(bookmaker=name, bookmaker_id=row.bookmaker_id, market=market or row.market,
                period=row.period if model is MarketOdds else 'full_time',
                field_observed_at={stage+'_'+side:price_observed_at(row,stage+'_'+side)
                    for stage in ('opening','latest','closing') for side in keys if side!='line'},
                opening={k: getattr(row, 'opening_' + k) for k in keys}, latest={k: getattr(row, 'latest_' + k) for k in keys},
                closing={k: getattr(row, 'closing_' + k) for k in keys}, updated_at=aware(row.updated_at), raw=row.raw,
                line_perspective='home' if market == 'ah' else None))
    return result


@router.get('/matches/{match_id}', response_model=MatchDetail)
async def match_detail(match_id: int, session=Depends(session_dependency)):
    query, _, _ = match_query()
    row = (await session.execute(query.where(Match.id == match_id,League.enabled.is_(True)))).first()
    if not row:
        raise HTTPException(404, 'Match not found (use the internal ID from /api/matches)')
    return {**match_response(row), 'odds': await odds_data(session, match_id), 'raw': row[0].raw}


@router.get('/matches/{match_id}/odds', response_model=list[OddsResponse])
async def match_odds(match_id: int, session=Depends(session_dependency)):
    if not await session.scalar(select(Match.id).join(League).where(Match.id==match_id,League.enabled.is_(True))):
        raise HTTPException(404, 'Match not found')
    return await odds_data(session, match_id)


@router.get('/scraper/status', response_model=ScraperStatus)
async def scraper_status(session=Depends(session_dependency)):
    rows = (await session.execute(select(ScraperJob, League.name).join(League).where(League.enabled.is_(True)).order_by(ScraperJob.id.desc()).limit(10))).all()
    return {'status': 'running' if any(j.status in {'queued', 'running'} for j, _ in rows) else 'idle',
            'jobs': [job_response(j, name) for j, name in rows]}


@router.get('/scraper/jobs/{job_id}', response_model=JobResponse)
async def job_detail(job_id: int, session=Depends(session_dependency)):
    row = (await session.execute(select(ScraperJob, League.name).join(League).where(ScraperJob.id == job_id,League.enabled.is_(True)))).first()
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


@router.post('/scraper/update-all', response_model=list[JobResponse], status_code=202, dependencies=[Depends(require_scraper_token)])
async def daily_update_all(session=Depends(session_dependency)):
    jobs = await enqueue_all_updates()
    names = dict((await session.execute(select(League.id, League.name))).all())
    return [job_response(job, names.get(job.league_id)) for job in jobs]


@router.post('/scraper/stats-backfill', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def statistics_backfill(request: JobRequest, session=Depends(session_dependency)):
    return await submit('stats_backfill',request,session)


@router.post('/scraper/statistics/{match_id}', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def statistics_single(match_id: int, session=Depends(session_dependency)):
    match=await session.get(Match,match_id)
    if not match or match.status!='finished':raise HTTPException(404,'Stored completed match not found')
    league=await session.get(League,match.league_id)
    return await submit('stats_backfill',JobRequest(league_id=league.external_id),session,match_id)


@router.post('/scraper/match/{match_id}', response_model=JobResponse, status_code=202, dependencies=[Depends(require_scraper_token)])
async def scrape_match(match_id: int, session=Depends(session_dependency)):
    match = await session.get(Match, match_id)
    if not match:
        raise HTTPException(404, 'Match not found')
    league = await session.get(League, match.league_id)
    return await submit('match', JobRequest(league_id=league.external_id), session, match_id)


@router.get('/predictions/yesterday-winners')
async def yesterday_winners(session=Depends(session_dependency)):
    """Yesterday's actual winning picks published strictly before kickoff (Istanbul day)."""
    from src.models import PredictionSnapshot
    from src.analytics.performance import settle
    from sqlalchemy.orm import defer
    from datetime import datetime
    istanbul = ZoneInfo('Europe/Istanbul')
    yesterday = datetime.now(istanbul).date() - timedelta(days=1)
    start, end = day_bounds(yesterday, DISPLAY_TIMEZONE)
    home_team = aliased(Team)
    away_team = aliased(Team)
    query = (
        select(Match, PredictionSnapshot, MatchStatistics, home_team.name, away_team.name, League.name)
        .join(PredictionSnapshot, PredictionSnapshot.match_id == Match.id)
        .outerjoin(MatchStatistics, MatchStatistics.match_id == Match.id)
        .join(home_team, home_team.id == Match.home_team_id)
        .join(away_team, away_team.id == Match.away_team_id)
        .join(League, League.id == Match.league_id)
        .options(defer(Match.raw), defer(MatchStatistics.raw))
        .where(League.enabled.is_(True), Match.status == 'finished',
               Match.kickoff_at >= start, Match.kickoff_at < end,
               PredictionSnapshot.captured_at < Match.kickoff_at)
        .order_by(Match.kickoff_at.desc(), Match.id.desc())
        .limit(150)
    )
    rows = (await session.execute(query)).all()
    won = []
    saved = 0
    settled = 0
    for match, snapshot, stats, home_name, away_name, league_name in rows:
        for pick in snapshot.picks or []:
            saved += 1
            try:
                outcome, actual = settle(match, stats, pick)
            except (KeyError, ValueError, TypeError):
                continue
            if outcome in ('won', 'lost'):
                settled += 1
            if outcome != 'won':
                continue
            won.append({'match_id': match.id, 'league': league_name, 'home_team': home_name,
                        'away_team': away_name, 'kickoff_at': match.kickoff_at,
                        'ft_home': match.ft_home, 'ft_away': match.ft_away,
                        'label': pick.get('label'), 'market': pick.get('market'),
                        'probability': pick.get('probability'), 'actual': actual,
                        'captured_at': snapshot.captured_at})
    return {'date': yesterday.isoformat(), 'items': won[:6],
            'total_won': len(won), 'saved': saved, 'settled': settled,
            'basis': 'immutable_prematch_snapshots'}


@router.get('/performance-trends')
async def performance_trends(as_of: Date | None=None,league: str | None=None,market: str='all',exact_market: str | None=None,confidence: Literal['high','medium','low'] | None=None,date_from: Date | None=None,date_to: Date | None=None,session=Depends(session_dependency)):
    from src.analytics.performance_trends import trends, ISTANBUL
    if date_from and date_to and date_from>date_to:raise HTTPException(400,'Invalid date range')
    anchor=as_of or utcnow().astimezone(ISTANBUL).date()
    if anchor<Date(1970,1,1) or anchor>utcnow().astimezone(ISTANBUL).date():
        raise HTTPException(status_code=400,detail='Observation date must be between 1970 and today')
    return await performance_cache.get(session.bind, ('trends-v3',anchor,league,market,exact_market,confidence,date_from,date_to),lambda:trends(session,anchor,league,market,exact_market,confidence,date_from,date_to))


@router.get('/market-performance')
async def market_performance(league: str | None=None,market: Literal['all','result','goals','btts','corners','cards','yellow_cards','red_cards','asian_handicap','double_chance','first_half_goals','second_half_goals']='all',exact_market: str | None=None,date_from: Date | None=None,date_to: Date | None=None,confidence: Literal['high','medium','low'] | None=None,limit: int=Query(default=50,ge=1,le=100),offset: int=Query(default=0,ge=0),session=Depends(session_dependency)):
    if date_from and date_to and date_from>date_to:raise HTTPException(400,'Invalid date range')
    from src.analytics.performance import performance
    start=day_bounds(date_from,DISPLAY_TIMEZONE)[0] if date_from else None
    end=day_bounds(date_to,DISPLAY_TIMEZONE)[1] if date_to else None
    return await performance_cache.get(session.bind, (league,start,end,market,exact_market,confidence,limit,offset),
        lambda: performance(session,league,start,end,market,exact_market,confidence,limit,offset))


@router.post('/scraper/stats-backfill-enabled',response_model=list[JobResponse],dependencies=[Depends(require_scraper_token),Depends(session_dependency)])
async def stats_rollout():
    from src.jobs.worker import enqueue_stats_rollout
    return [job_response(job) for job in await enqueue_stats_rollout()]


@router.get('/history/seasons')
async def history_seasons(session=Depends(session_dependency)):
    from src.analytics.season_archive import read_or_query
    async with asyncio.timeout(4):
        return await read_or_query(session)


@router.get('/chart-diagnostics')
async def chart_diagnostics(session=Depends(session_dependency)):
    from src.analytics.chart_diagnostics import chart_diagnostics as inspect_charts
    from src.api.read_cache import chart_cache
    async with asyncio.timeout(6):
        return await chart_cache.get(session.bind, 'cohort-v1', lambda: inspect_charts(session))
