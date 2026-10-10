"""Durable, atomically published league boards. HTTP never runs the model."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, or_, text
from src.models import League, Match, PredictionBoard, utcnow
from src.jobs.worker import aware
from src.match_views import day_bounds, DISPLAY_TIMEZONE

MODEL_VERSION='quality-poisson-v2-cards-board-v1'
TTL=timedelta(minutes=10)
REFRESH_LOCK=213006
log=logging.getLogger(__name__)


async def refresh_one(db, now=None, force_league=None):
    """One league per checkpoint; cancellation leaves the previous board intact.

    Called by the scraper while it owns the global worker lock. A second lock
    also protects explicit refresh callers; it uses the same writer connection.
    """
    from src.api.routes import uncached_predictions
    from src.analytics.service import revision
    now=now or utcnow()
    async with asyncio.timeout(90):
        async with db.session() as session:
            if session.bind.dialect.name=='postgresql':
                if not await session.scalar(text('SELECT pg_try_advisory_xact_lock(:id)'),{'id':REFRESH_LOCK}):return False
            query=select(League.id).outerjoin(PredictionBoard).where(League.enabled.is_(True))
            if force_league is not None:query=query.where(League.id==force_league)
            else:query=query.where(or_(PredictionBoard.league_id.is_(None),PredictionBoard.expires_at<=now,PredictionBoard.model_version!=MODEL_VERSION))
            league=await session.scalar(query.order_by(PredictionBoard.generated_at.asc().nullsfirst(),League.priority,League.id).limit(1))
            if league is None:return False
            source=hashlib.sha256(json.dumps(jsonable_encoder(await revision(session)),sort_keys=True).encode()).hexdigest()
            items,generated,evaluated=await uncached_predictions(session,str(league),None,now=now,publish=False)
            board=await session.get(PredictionBoard,league)
            if board is None:board=PredictionBoard(league_id=league);session.add(board)
            board.model_version=MODEL_VERSION;board.generated_at=generated;board.expires_at=generated+TTL
            board.source_revision=source
            board.payload=jsonable_encoder(dict(items=items,availability=session.info.get('count_availability',[]),evaluated=evaluated))
            # Publish the immutable picks and board together, after a complete calculation.
            from src.analytics.performance import capture_many
            await capture_many(session,session.info.get('board_observations',[]),now)
            await session.commit()
            log.info('[PREDICTIONS] cached league=%s evaluated=%s generated_at=%s',league,evaluated,generated.isoformat())

            return True


async def refresh_all(db, now=None):
    """Refreshes all enabled leagues sequentially."""
    now = now or utcnow()
    refreshed = []
    async with db.session() as session:
        leagues = (await session.scalars(select(League.id).where(League.enabled.is_(True)).order_by(League.priority, League.id))).all()
    for league_id in leagues:
        if await refresh_one(db, now=now, force_league=league_id):
            refreshed.append(league_id)
    return refreshed



async def read_board(session,league,date,team,now):
    query=select(League.id,PredictionBoard).outerjoin(PredictionBoard).where(League.enabled.is_(True))
    if league:query=query.where(League.id==int(league)) if league.isdigit() else query.where(League.name.ilike('%'+league+'%'))
    rows=(await session.execute(query)).all()
    # Current statuses prevent a cached prematch pick appearing after kickoff or completion.
    eligible=set(await session.scalars(select(Match.id).join(League).where(League.enabled.is_(True),Match.status=='scheduled',Match.kickoff_at>now,Match.kickoff_at<=now+timedelta(days=7))))
    items=[];availability=[];times=[];revisions={};missing=[];stale=[];evaluated=0
    for id,board in rows:
        if board is None or board.model_version!=MODEL_VERSION:missing.append(id);continue
        times.append(aware(board.generated_at));revisions[str(id)]=board.source_revision
        expired=aware(board.expires_at)<=now
        if expired:stale.append(id)
        evaluated+=board.payload['evaluated']
        for item in board.payload['items']:
            item={**item,'match':dict(item['match'])}
            m=item['match'];kickoff=datetime.fromisoformat(m['kickoff_at'])
            if m['id'] not in eligible:continue
            if date:
                start,end=day_bounds(date,DISPLAY_TIMEZONE)
                if not start<=kickoff<end:continue
            if team and team.casefold() not in (m['home_team']+' '+m['away_team']).casefold():continue
            m['kickoff_at']=kickoff
            items.append(item)
        availability.extend((id,a) for id,a in board.payload['availability'] if id in eligible)
    items.sort(key=lambda i:(i['match']['kickoff_at'],i['match']['id']))
    session.info['count_availability']=availability
    session.info['prediction_snapshot']={
        'status':'warming' if missing and not times else 'partial' if missing else 'stale' if stale else 'fresh',
        'stale':bool(stale or missing),'generated_at':min(times) if times else None,
        'expires_at':min(times)+TTL if times else None,'model_version':MODEL_VERSION,
        'source_revisions':revisions,'missing_leagues':missing,'stale_leagues':stale}
    return items,now,evaluated
