"""Durable, atomically published league boards. HTTP never runs the model."""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, or_, text, literal
from sqlalchemy.orm import defer
from src.query_profile import stage
from src.models import League, Match, PredictionBoard, BoardHead, utcnow
from src.jobs.worker import aware
from src.match_views import day_bounds, DISPLAY_TIMEZONE
from src.api.read_cache import ReadCache

# Only model JSON is reused. Match state, publication expiry and bookmaker
# observations are always read again; this cache cannot renew evidence.
payload_cache=ReadCache(ttl=600,limit=4)

MODEL_VERSION='quality-poisson-v3-specialist-adapters-v1'
_DISABLED=','.join(sorted(filter(None,os.getenv('DISABLED_MARKET_ENGINES','').split(','))))
if _DISABLED:MODEL_VERSION+='-flags-'+hashlib.sha256(_DISABLED.encode()).hexdigest()[:12]
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
            with stage('input_revision'):
                inputs=await revision(session)
            session.info['board_revision']=inputs
            source=hashlib.sha256(json.dumps(jsonable_encoder(inputs),sort_keys=True).encode()).hexdigest()
            query=select(League.id).outerjoin(PredictionBoard).outerjoin(BoardHead,BoardHead.league_id==League.id).where(League.enabled.is_(True))
            if force_league is not None:query=query.where(League.id==force_league)
            else:query=query.where(or_(PredictionBoard.league_id.is_(None),PredictionBoard.expires_at<=now+timedelta(minutes=2),PredictionBoard.model_version!=MODEL_VERSION,PredictionBoard.source_revision!=source))
            league=await session.scalar(query.order_by(BoardHead.checked_at.asc().nullsfirst(),League.priority,League.id).limit(1))
            if league is None:return False
            with stage('model_calculation'):
                items,generated,evaluated=await uncached_predictions(session,str(league),None,now=now,publish=False)
            board=await session.get(PredictionBoard,league)
            if board is None:
                board=PredictionBoard(league_id=league,model_version=MODEL_VERSION,generated_at=generated,
                    expires_at=generated+TTL,source_revision=source)
                session.add(board)
            candidate_payload=jsonable_encoder(dict(items=items,availability=session.info.get('count_availability',[]),evaluated=evaluated,engine_analysis=session.info.get('engine_analysis',[]),coupon_records=session.info.get('coupon_records',[])))
            from src.analytics.publications import publish_board
            await publish_board(session,board,now,candidate_payload=candidate_payload,model_version=MODEL_VERSION)
            board.model_version=MODEL_VERSION;board.expires_at=generated+TTL;board.source_revision=source
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



async def read_board(session,league,date,team,now,statistical_all=False,include_engine_analysis=False):
    from src.analytics.coupon import coupon_qualified as quality
    # Read small, current validity metadata before reusing publication JSON.
    query=select(League.id,PredictionBoard,BoardHead.version
    ).options(defer(PredictionBoard.payload)).outerjoin(PredictionBoard).outerjoin(BoardHead,BoardHead.league_id==League.id).where(League.enabled.is_(True))
    if league:query=query.where(League.id==int(league)) if league.isdigit() else query.where(League.name.ilike('%'+league+'%'))
    rows=(await session.execute(query)).all()
    valid=[(id,b,v) for id,b,v in rows if b is not None and b.model_version==MODEL_VERSION
           and aware(b.expires_at)>now and aware(b.generated_at)<=now]
    async def load_payloads():
        data=PredictionBoard.payload
        if session.bind.dialect.name=='postgresql':
            from sqlalchemy.dialects.postgresql import JSONB
            data=data.cast(JSONB)
        base=select(PredictionBoard.league_id,data.label('data'))\
            .where(or_(*[(PredictionBoard.league_id==id) & (PredictionBoard.source_revision==b.source_revision)
                & (PredictionBoard.generated_at==b.generated_at) for id,b,v in valid])).cte('publication_json')
        # JSON columns otherwise get parsed independently for every extraction.
        # Materialize the JSONB conversion once; send only requested fields.
        base=base.prefix_with('MATERIALIZED',dialect='postgresql')
        data=base.c.data
        records=await session.execute(select(base.c.league_id,data['items'],data['availability'],data['evaluated'],
            data['coupon_records'] if statistical_all else literal(None),
            data['engine_analysis'] if include_engine_analysis else literal(None)))
        return {id:dict(items=items,availability=available,evaluated=evaluated,coupon_records=coupons,engine_analysis=analysis)
                for id,items,available,evaluated,coupons,analysis in records}
    key=(statistical_all,include_engine_analysis,tuple(sorted((id,v or b.source_revision,b.generated_at) for id,b,v in valid)))
    payloads=await payload_cache.get(session.bind,key,load_payloads) if valid else {}
    # Current statuses prevent a cached prematch pick appearing after kickoff or completion.
    eligible=dict((await session.execute(select(Match.id,Match.kickoff_at).join(League).where(League.enabled.is_(True),Match.status=='scheduled',Match.kickoff_at>now,Match.kickoff_at<=now+timedelta(days=7)))).all())
    items=[];availability=[];engine_analysis=[];times=[];expiries=[];revisions={};missing=[];stale=[];evaluated=0;versions={}
    for id,board,version in rows:
        if board is None or board.model_version!=MODEL_VERSION:missing.append(id);continue
        expired=aware(board.expires_at)<=now or aware(board.generated_at)>now
        if expired:
            stale.append(id)
            continue  # One delayed league must not erase other fresh publications.
        if id not in payloads:missing.append(id);continue  # publication changed between reads
        # One JSON decode per source publication, rather than repeated JSON
        # extraction/TOAST reads for every visitor and every display field.
        payload=payloads[id]
        source=payload.get('items',[]);board_availability=payload.get('availability',[])
        board_evaluated=payload.get('evaluated',0)
        coupon_records=payload.get('coupon_records',[])
        analysis_rows=payload.get('engine_analysis',[]) if include_engine_analysis else []
        versions[str(id)]=version or board.source_revision
        times.append(aware(board.generated_at));expiries.append(aware(board.expires_at));revisions[str(id)]=board.source_revision
        evaluated+=board_evaluated
        if statistical_all:
            source=[{'match':r['match'],'recommendations':r['candidates'],'as_of':board.generated_at} for r in (coupon_records or [])]
        for item in source:
            item={**item,'match':dict(item['match'])}
            m=item['match'];kickoff=aware(datetime.fromisoformat(m['kickoff_at']))
            if m['id'] not in eligible:continue
            # A rescheduled fixture must await a new board, not retain yesterday's date.
            if aware(kickoff)!=aware(eligible[m['id']]):continue
            if date:
                start,end=day_bounds(date,DISPLAY_TIMEZONE)
                if not start<=kickoff<end:continue
            if team and team.casefold() not in (m['home_team']+' '+m['away_team']).casefold():continue
            m['kickoff_at']=kickoff
            items.append(item)
        engine_analysis.extend(r for r in (analysis_rows or []) if r['match_id'] in eligible)
        availability.extend((id,a) for id,a in (board_availability or []) if id in eligible)
    items.sort(key=lambda i:(i['match']['kickoff_at'],i['match']['id']))
    session.info['engine_analysis']=engine_analysis
    session.info['count_availability']=availability
    session.info['prediction_snapshot']={
        'status':('warming' if missing else 'stale') if not times else 'partial' if missing or stale else 'fresh',
        'checked_at':now,'stale':not bool(times),'generated_at':min(times) if times else None,
        'latest_generated_at':max(times) if times else None,
        'expires_at':min(expiries) if expiries else None,'model_version':MODEL_VERSION,
        'publication_versions':versions,'source_revisions':revisions,'missing_leagues':missing,'stale_leagues':stale,
        'scheduled_matches':len(eligible),
        'statistical_matches':len({i['match']['id'] for i in items}),
        'quality_matches':len({i['match']['id'] for i in items if any(quality(r) for r in i['recommendations'])})}
    return items,now,evaluated
