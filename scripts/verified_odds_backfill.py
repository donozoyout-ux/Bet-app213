"""Supervised NULL-only Polish/Danish archive repair; no fixture creation."""
import asyncio
import json
import os
from dataclasses import fields
from datetime import datetime, timezone, timedelta
import httpx

from sqlalchemy import select, text, func
from sqlalchemy.orm import aliased
from src.db import database
from src.models import League, Match, Team, Season, ScraperJob, JobItem, ScraperIssue, utcnow
from src.jobs.europe_capacity import check_capacity
from src.jobs.verified_odds import complete_expression, fill_only, is_complete
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.competitions import Competition, validate_identity
from src.scrapers.goaloo.seasons import season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import fetch_odds, odds_gaps
from scripts.europe_bounded_history import REPORT, selected_records, check_api_health

SOFT_STOP_BYTES = 650_000_000
TERMINAL = ('source_unavailable', 'source_conflict')


async def batch_health():
    await check_api_health()
    if not os.getenv('EUROPE_API_GUARD_URL'):
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for path in ('/api/predictions?limit=6','/api/coupons/daily','/api/matches?status=live&limit=20'):
                (await client.get('https://bet-app213.onrender.com'+path)).raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError('Archive repair paused: prediction/coupon/live API guard failed') from exc


async def capacity(session):
    result = await check_capacity(session)
    if result['database_bytes'] >= SOFT_STOP_BYTES:
        raise RuntimeError('Archive repair paused: approaching 700 MB cap (650 MB admission stop)')
    return result


def verify_fixture(stored, source, league_id):
    """An archive quote is usable only for the same finished fixture."""
    if not source or source['external_league_id'] != league_id or source['status'] != 'finished':
        return False
    if any(stored[k] != source[k] for k in ('external_match_id','home_external_id','away_external_id')):
        return False
    if source['kickoff_at'] != stored['kickoff_at']:
        return False
    return all(stored[k] is None or stored[k] == source[k]
               for k in ('ft_home','ft_away','ht_home','ht_away'))


def attempts(error):
    if error and error.startswith('attempts='):
        return int(error.split(';',1)[0].split('=',1)[1])
    return 0


async def checkpoint(session, job, result):
    await session.flush()
    counts = dict((await session.execute(select(JobItem.status,func.count()).where(
        JobItem.job_id == job.id).group_by(JobItem.status))).all())
    job.total_matches = sum(counts.values())
    job.processed_matches = counts.get('completed',0)
    job.no_odds_matches = sum(counts.get(s,0) for s in TERMINAL)
    job.failed_matches = counts.get('failed',0) + int(await session.scalar(
        select(func.count()).select_from(ScraperIssue).where(ScraperIssue.job_id == job.id)))
    job.checkpoint_at = utcnow()
    metrics = dict(job.discovery_metrics or {})
    previous = metrics.get('_stage3_verified_odds',{})
    cumulative = {key:previous.get(key,0)+result.get(key,0) for key in
                  ('attempted','inserted_rows','updated_rows','verified','source_gaps','conflicts','request_errors')}
    metrics['_stage3_verified_odds'] = {**cumulative,'checkpoint_at':job.checkpoint_at.isoformat()}
    job.discovery_metrics = metrics  # Preserve admission proof and older discovery evidence.


async def candidates(session, league_id, job_id, limit):
    home, away = aliased(Team), aliased(Team)
    terminal = select(JobItem.match_id).where(JobItem.job_id == job_id,JobItem.status.in_(TERMINAL))
    query = select(Match.id,Match.external_match_id,Match.kickoff_at,Match.ft_home,Match.ft_away,
        Match.ht_home,Match.ht_away,Season.season_name,
        home.external_id.label('home_external_id'),away.external_id.label('away_external_id')).join(
        Season,Match.season_id == Season.id).join(home,Match.home_team_id == home.id).join(
        away,Match.away_team_id == away.id).where(Match.league_id == league_id,
        Match.status == 'finished',Match.kickoff_at >= datetime(2024,1,1,tzinfo=timezone.utc),
        Match.kickoff_at <= utcnow()-timedelta(hours=3),~complete_expression(),
        Match.id.not_in(terminal)).order_by(Match.kickoff_at.desc(),Match.id.desc()).limit(limit)
    return [dict(r) for r in (await session.execute(query)).mappings()]


async def run(ids, limit=25):
    if not ids or len(ids)>2 or len(set(ids))!=len(ids) or not set(ids)<={6,7} or not 1<=limit<=25:
        raise ValueError('Only Poland 6 / Denmark 7; 1-25 matches each (at most 50 per batch)')
    records = selected_records(ids,json.loads(REPORT.read_text(encoding='utf-8')))
    if not database.configured:
        raise RuntimeError('Existing DATABASE_URL is required')
    results=[]
    async with database.engine.connect() as lock:
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(213090)')):
            raise RuntimeError('A supervised Europe import is already running')
        await lock.commit()
        try:
            async with GoalooClient() as client:
                for record in records:
                    await batch_health()
                    async with database.session() as session:
                        before=await capacity(session)
                        league=await session.scalar(select(League).where(League.external_id==record['external_id']))
                        if league is None or not league.enabled:
                            raise RuntimeError('Only already admitted leagues can be repaired')
                        # Reuse the durable pilot checkpoint; never create duplicate work.
                        job=(await session.scalars(select(ScraperJob).where(
                            ScraperJob.league_id==league.id,ScraperJob.kind=='europe_bounded'))).one()
                        if job.status!='manual_paused':
                            raise RuntimeError('Expected a manually controlled archive checkpoint')
                        job_id=job.id
                        rows=await candidates(session,league.id,job_id,limit)
                    identity=Competition(**{f.name:record[f.name] for f in fields(Competition) if f.name in record})
                    schedules={}
                    result=dict(league=record['external_id'],job_id=job_id,attempted=0,inserted_rows=0,
                        updated_rows=0,verified=0,source_gaps=0,conflicts=0,request_errors=0,before=before)
                    consecutive_errors=0
                    for stored in rows:
                        await check_api_health()
                        async with database.session() as session:
                            await capacity(session)
                        status=reason=None
                        odds=None
                        try:
                            season=stored['season_name']
                            if season not in schedules:
                                payload=await season_data(client,record['external_id'],season)
                                validate_identity(identity,payload,season)
                                indexed={}
                                for section in discover_rounds(payload):
                                    errors=[]
                                    indexed.update({r['external_match_id']:r for r in parse_matches(payload,section,errors)})
                                    if errors:
                                        raise SourceError('Unparsed source fixture rows')
                                schedules[season]=indexed
                            source=schedules[season].get(stored['external_match_id'])
                            if not verify_fixture(stored,source,record['external_id']):
                                status,reason='source_conflict','fixture_identity_or_score_conflict'
                            else:
                                odds=await fetch_odds(client,stored['external_match_id'],True)
                            consecutive_errors=0
                        except SourceError:
                            status,reason='failed','odds_or_schedule_source_error'
                            consecutive_errors+=1
                        # All source I/O finished before opening the write transaction.
                        async with database.session() as session:
                            await capacity(session)
                            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
                            await capacity(session)  # A pre-lock measurement cannot authorize a later write.
                            item=await session.scalar(select(JobItem).where(
                                JobItem.job_id==job_id,JobItem.match_id==stored['id']))
                            if item is None:
                                item=JobItem(job_id=job_id,match_id=stored['id'])
                                session.add(item)
                            attempt=attempts(item.error)+1
                            delta=dict(attempted=1)
                            if odds is not None:
                                delta.update(await fill_only(session,stored['id'],stored['external_match_id'],job_id,odds))
                                complete=await is_complete(session,stored['id'])
                                match=await session.get(Match,stored['id'])
                                match.odds_complete=complete
                                if complete:
                                    status,reason='completed',None
                                    delta['verified']=1
                                elif odds_gaps(odds,True):
                                    status,reason='source_unavailable','source_missing_archived_fields'
                                    delta['source_gaps']=1
                                else:
                                    status,reason='source_conflict','stored_price_or_line_conflict'
                            if status=='source_conflict':
                                delta['conflicts']=1
                            if status=='failed':
                                delta['request_errors']=1
                                if attempt>=3:
                                    status='source_unavailable'
                                    reason='transport_retry_exhausted'
                            item.status=status
                            item.error=f'attempts={attempt};{reason}' if reason else None
                            job=await session.get(ScraperJob,job_id)
                            job.current_season=stored['season_name']
                            await checkpoint(session,job,delta)
                            await session.commit()
                        for key,value in delta.items():
                            result[key]+=value
                        print(json.dumps(dict(league=record['external_id'],match=stored['external_match_id'],
                            status=status,reason=reason,**delta)),flush=True)
                        if consecutive_errors>=3:
                            result['stopped']='three_consecutive_source_errors'
                            break
                    async with database.session() as session:
                        result['after']=await capacity(session)
                        result['eligible_remaining']=len(await candidates(session,league.id,job_id,10000))
                    results.append(result)
                    await batch_health()
                    if result.get('stopped'):
                        break
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(213090)'))
            await lock.commit()
    print(json.dumps({'verified_odds_batches':results}),flush=True)
    return results
