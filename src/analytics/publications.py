"""Durable publication heads; HTTP reads never run a model or create a coupon."""
from copy import deepcopy
from datetime import timedelta, datetime
import math
import hashlib
import json
from zoneinfo import ZoneInfo
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, text, or_, inspect
from sqlalchemy.orm import undefer,defer
from src.models import (BoardEdition, BoardHead, DailyCouponPublication, PredictionBoard,
                        Match, League, Odds1X2, AsianTotals, AsianHandicap, MarketOdds, SavedCoupon)
from src.jobs.worker import aware


def analysis_evidence(value):
    # Moving the inspection cutoff alone is not a new statistical observation.
    if isinstance(value,dict):return {k:analysis_evidence(v) for k,v in value.items() if k!='cutoff'}
    if isinstance(value,list):return [analysis_evidence(v) for v in value]
    return value


async def publish_match_analyses(session,analyses,board_version,model_version,now,expires):
    from src.models import PublishedMatchAnalysis
    existing={r.match_id:r for r in await session.scalars(select(PublishedMatchAnalysis).where(PublishedMatchAnalysis.match_id.in_(analyses)))} if analyses else {}
    for match_id,data in analyses.items():
        payload=jsonable_encoder(data)
        evidence=analysis_evidence(stable(payload))
        version=digest(evidence)
        row=existing.get(match_id)
        if row is None:
            row=PublishedMatchAnalysis(match_id=match_id,version=version,payload=payload,generated_at=now)
            session.add(row)
        elif row.version!=version:
            row.version=version;row.payload=payload;row.generated_at=now
        row.board_version=board_version;row.model_version=model_version
        row.kickoff_at=aware(datetime.fromisoformat(payload['match']['kickoff_at']))
        row.checked_at=now;row.expires_at=expires


async def read_match_analysis(session,match,now):
    from src.models import PublishedMatchAnalysis
    from src.analytics.board_cache import MODEL_VERSION
    historical=bool(match.kickoff_at and aware(match.kickoff_at)<=now)
    query=select(PublishedMatchAnalysis).options(undefer(PublishedMatchAnalysis.payload)).where(
        PublishedMatchAnalysis.match_id==match.id,PublishedMatchAnalysis.checked_at<=now,
        PublishedMatchAnalysis.kickoff_at==match.kickoff_at)
    if not historical:
        if match.status!='scheduled':return None
        query=query.join(BoardHead,(BoardHead.league_id==match.league_id)&(BoardHead.version==PublishedMatchAnalysis.board_version))\
            .join(PredictionBoard,PredictionBoard.league_id==BoardHead.league_id)\
            .where(PublishedMatchAnalysis.model_version==MODEL_VERSION,PublishedMatchAnalysis.expires_at>now,
                PredictionBoard.model_version==MODEL_VERSION,PredictionBoard.expires_at>now,PredictionBoard.generated_at<=now)
    row=await session.scalar(query)
    if row is None:return None
    data=deepcopy(row.payload)
    context=data.get('engine_context',{})
    if context and (context.get('home_team_id')!=match.home_team_id or context.get('away_team_id')!=match.away_team_id):return None
    data['as_of']=aware(row.checked_at)
    data['publication']=dict(version=row.version,board_version=row.board_version,model_version=row.model_version,
        generated_at=aware(row.generated_at),checked_at=aware(row.checked_at),status='archived' if historical else 'current')
    data['historical']=historical
    data['cache_seconds']=0 if historical else max(1,int((aware(row.expires_at)-aware(row.checked_at)).total_seconds()))
    if historical:
        for rec in data.get('candidates',[])+data.get('recommendations',[]):
            vo=rec.get('verified_odds') or {};vo.update(coupon_eligible=False,reason='match_not_scheduled',freshness='stale')
            for price in vo.get('bookmakers',{}).values():price.update(coupon_eligible=False,freshness='stale')
        return data
    from src.analytics.coupon import current_odds
    await current_odds(session,{match.id:data.get('candidates',[])},now)
    current={r['id']:r for r in data.get('candidates',[])}
    data['recommendations']=[current.get(r['id'],r) for r in data.get('recommendations',[])]
    return data


def digest(value):
    return hashlib.sha256(json.dumps(jsonable_encoder(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def stable(value):
    if isinstance(value,dict):
        return {k:stable(v) for k,v in value.items() if k not in
                {'verified_odds','as_of','generated_at','expires_at','checked_at','observed_at','updated_at','last_scraped_at','status_observed_at','live_checked_at','duration_ms'}}
    if isinstance(value,list):return [stable(v) for v in value]
    return value


async def publish_board_read_model(session,board,version,data):
    """Split large evidence documents on the writer, never cast them in HTTP.

    Only one current projection per league; unchanged versions do not rewrite
    JSON. An old installation is warmed by the next normal worker checkpoint.
    """
    from src.models import BoardReadPublication
    with session.no_autoflush:
        row=await session.get(BoardReadPublication,board.league_id)
    if row is not None and row.version==version:return
    if row is None:
        row=BoardReadPublication(league_id=board.league_id)
        session.add(row)
    row.version=version
    for field in ('items','availability','coupon_records','engine_analysis'):
        setattr(row,field,deepcopy(data.get(field) or []))
    row.evaluated=data.get('evaluated',0)


async def publish_board(session,board,now,candidate_payload=None,model_version=None):
    data=board.payload if candidate_payload is None else candidate_payload
    model=model_version or board.model_version
    version=digest([board.league_id,model,stable(data)])
    with session.no_autoflush:
        head=await session.get(BoardHead,board.league_id)
    if head:head.checked_at=now
    if head and head.version==version:
        # The complete model was revalidated against current inputs. Preserve the
        # edition's ordering and generation time, while renewing validation TTL.
        previous=inspect(board).attrs.payload.history.deleted
        if previous:board.payload=previous[0]
        await publish_board_read_model(session,board,version,board.payload)
        return version
    board.model_version=model
    board.payload=data
    board.generated_at=now
    with session.no_autoflush:
        edition=await session.get(BoardEdition,version)
    if edition is None:
        session.add(BoardEdition(version=version,league_id=board.league_id,created_at=now,payload=stable(deepcopy(data))))
        await session.flush()
    else:board.generated_at=edition.created_at
    if head:head.version=version
    else:session.add(BoardHead(league_id=board.league_id,version=version,checked_at=now))
    await publish_board_read_model(session,board,version,data)
    return version


async def revisions(session,now):
    """Indexed match-bounded price metadata, never full board JSON/history scans."""
    from src.analytics.board_cache import MODEL_VERSION
    boards=(await session.execute(select(League.id,BoardHead.version,
        PredictionBoard.source_revision,PredictionBoard.expires_at,PredictionBoard.generated_at,PredictionBoard.model_version)
        .outerjoin(PredictionBoard,PredictionBoard.league_id==League.id)
        .outerjoin(BoardHead,BoardHead.league_id==League.id).where(League.enabled.is_(True))
        .order_by(League.id))).all()
    board_meta={str(a):dict(version=b or c,expires_at=aware(d) if d is not None else None,
        valid=d is not None and aware(d)>now and g is not None and aware(g)<=now and model==MODEL_VERSION)
        for a,b,c,d,g,model in boards}
    matches=(await session.execute(select(Match.id,Match.kickoff_at,Match.updated_at,Match.league_id).join(League)
        .where(League.enabled.is_(True),Match.status=='scheduled',Match.kickoff_at>now,
               Match.kickoff_at<=now+timedelta(days=7)).order_by(Match.id))).all()
    ids=[r.id for r in matches]
    from src.models import PublishedMatchAnalysis
    analysis_rows=(await session.execute(select(PublishedMatchAnalysis.match_id,PublishedMatchAnalysis.version,
        PublishedMatchAnalysis.board_version,PublishedMatchAnalysis.model_version,PublishedMatchAnalysis.kickoff_at,
        PublishedMatchAnalysis.checked_at,PublishedMatchAnalysis.expires_at).where(PublishedMatchAnalysis.match_id.in_(ids)))).all() if ids else []
    fixtures={r.id:r for r in matches}
    analyses={str(r.match_id):dict(version=r.version,checked_at=aware(r.checked_at),expires_at=aware(r.expires_at),
        valid=r.model_version==MODEL_VERSION and aware(r.checked_at)<=now<aware(r.expires_at)
            and aware(r.kickoff_at)==aware(fixtures[r.match_id].kickoff_at)
            and board_meta.get(str(fixtures[r.match_id].league_id),{}).get('valid')
            and board_meta.get(str(fixtures[r.match_id].league_id),{}).get('version')==r.board_version) for r in analysis_rows}
    prices=[]
    for model in (Odds1X2,AsianTotals,AsianHandicap,MarketOdds):
        rows=(await session.execute(select(model.id,model.updated_at)
            .where(model.match_id.in_(ids)).order_by(model.id))).all() if ids else []
        # Per-row stamps detect corrections even below another row's MAX stamp.
        prices.append([tuple(row) for row in rows])
    day=now.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat()
    board_token=digest([[(key,value['version'],value['valid']) for key,value in board_meta.items()],
                        [(r.id,r.kickoff_at) for r in matches]])
    price_token=digest(prices)
    daily=(await session.execute(select(DailyCouponPublication.strategy,DailyCouponPublication.version,DailyCouponPublication.payload['items'])
        .where(DailyCouponPublication.day==day).order_by(DailyCouponPublication.strategy))).all()
    daily_meta={}
    for strategy,version,items in daily:
        valid=bool(items) and all(i['match']['id'] in ids and price_valid(i['quote'],now)
            and board_meta.get(str(i['match'].get('league_id')),{}).get('valid') for i in (items or []))
        deadlines=[aware(datetime.fromisoformat(i['quote']['expires_at'].replace('Z','+00:00'))) for i in (items or [])] if valid else []
        deadlines += [board_meta[str(i['match']['league_id'])]['expires_at'] for i in (items or [])
            if str(i['match'].get('league_id')) in board_meta and board_meta[str(i['match']['league_id'])]['expires_at'] is not None]
        daily_meta[strategy]=dict(version=version,valid=valid,valid_until=min(deadlines) if deadlines else None)
    return dict(day=day,checked_at=now,boards=board_meta,analyses=analyses,daily=daily_meta,
        predictions=digest([board_token,price_token]),coupon_input=digest([day,board_token,price_token]),
        coupons=digest([day,[(key,value['version'],value['valid']) for key,value in daily_meta.items()]]))



def empty_daily(day,reason='not_published',strategy='balanced'):
    messages={'not_published':'Bugünün kaydedilmiş kuponu henüz hazırlanmadı; arka plan değerlendirmesi bekleniyor.',
        'analysis_unavailable':'Güncel analiz bekleniyor.', 'no_today_candidates':'Bugün uygun aday yok.',
        'quality_rejected':'Adaylar mevcut kalite eşiklerini geçmedi.',
        'price_rejected':'Taze doğrulanmış oran yok.', 'line_mismatch':'Market çizgisi veya periyodu eşleşmiyor.',
        'insufficient_prematch_data':'Adayların doğrulanmış örneklemi yetersiz.',
        'portfolio_rejected':'Kupon bağımsızlık veya risk koşullarını geçmedi.'}
    return dict(coupon_date=day,strategy=strategy,items=[],status='no_eligible_selections',reason=reason,
                message=messages.get(reason,'Uygun seçim bulunamadı.'),selection_mode='dynamic',max_matches=4)


def price_valid(quote,now):
    try:
        observed=aware(datetime.fromisoformat(quote['observed_at'].replace('Z','+00:00')))
        expires=aware(datetime.fromisoformat(quote['expires_at'].replace('Z','+00:00')))
        return (quote.get('eligible') is True and quote.get('freshness')=='fresh'
            and quote.get('bookmaker') in ('Crown','Bet365','Sbobet')
            and math.isfinite(quote['price']) and quote['price']>1
            and 0<=(now-observed).total_seconds()<=900 and expires>now)
    except (KeyError,ValueError,TypeError,AttributeError):return False


async def read_daily(session,now,strategy='balanced'):
    day=now.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat()
    row=await session.get(DailyCouponPublication,(day,strategy),options=[undefer(DailyCouponPublication.payload)])
    if row is None:
        start=now.astimezone(ZoneInfo('Europe/Istanbul')).replace(hour=0,minute=0,second=0,microsecond=0)
        saved=await session.scalar(select(SavedCoupon).where(SavedCoupon.kind=='automatic',
            SavedCoupon.created_at>=start,SavedCoupon.created_at<=now,
            or_(SavedCoupon.payload['strategy'].as_string()==strategy,SavedCoupon.payload['strategy'].as_string().is_(None)) if strategy=='balanced' else SavedCoupon.payload['strategy'].as_string()==strategy).order_by(SavedCoupon.created_at.desc()).limit(1))
        if saved is None or saved.payload.get('strategy','balanced')!=strategy or len(saved.payload.get('items',[]))>4:
            return empty_daily(day,strategy=strategy)
        from src.analytics.saved_coupons import summary
        result={**summary(saved),**deepcopy(saved.payload)}
        result['publication_version']=digest(stable(saved.payload))
        result.setdefault('data_updates',{})['checked_at']=aware(saved.created_at)
    else:
        result=deepcopy(row.payload)
        result['publication_version']=row.version
        result.setdefault('data_updates',{})['checked_at']=aware(row.checked_at)
    # Archived evidence remains intact, but started matches/expired quotes are
    # never presented as current selectable legs, even during worker outages.
    ids=[i['match']['id'] for i in result.get('items',[])]
    matches={m.id:m for m in await session.scalars(select(Match).options(defer(Match.raw)).where(Match.id.in_(ids)))} if ids else {}
    board_expiry=dict((await session.execute(select(PredictionBoard.league_id,PredictionBoard.expires_at)
        .where(PredictionBoard.league_id.in_({m.league_id for m in matches.values()})))).all()) if matches else {}
    usable=[]
    for item in result.get('items',[]):
        match=matches.get(item['match']['id']);q=item['quote']
        if (match and match.status=='scheduled' and match.kickoff_at and aware(match.kickoff_at)>now
            and aware(datetime.fromisoformat(str(item['match']['kickoff_at']).replace('Z','+00:00')))==aware(match.kickoff_at)
            and price_valid(q,now) and match.league_id in board_expiry and aware(board_expiry[match.league_id])>now):
            usable.append(item)
    if len(usable)!=len(result.get('items',[])):
        stale=empty_daily(day,'price_rejected',strategy);stale.update(publication_version=result['publication_version'],
            message='Kaydedilmiş kuponun maçı başladı veya fiyat kanıtı eskidi. Yeni doğrulama bekleniyor.')
        return stale
    if usable:
        result['valid_until']=min([aware(datetime.fromisoformat(i['quote']['expires_at'].replace('Z','+00:00'))) for i in usable]+[aware(d) for d in board_expiry.values()])
    return result


async def publish_daily(session,now,strategy='balanced'):
    from src.analytics.saved_coupons import automatic,detail
    if session.bind.dialect.name=='postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(213010)'))
    tokens=await revisions(session,now)
    state=await session.get(DailyCouponPublication,(tokens['day'],strategy),options=[undefer(DailyCouponPublication.payload)])
    if state and state.input_revision==tokens['coupon_input']:
        current=await read_daily(session,now,strategy)
        if current.get('items') or not state.payload.get('items'):return current
    coupon=await automatic(session,now,strategy,display_limit=4)
    diagnostics=session.info.get('coupon_diagnostics',{})
    if coupon:
        data=await detail(session,coupon)
        data.update(session.info.get('automatic_current_payload') or {})
        data.update(coupon_date=tokens['day'],strategy=strategy,selection_mode='dynamic',max_matches=4,
            selection_policy=diagnostics.get('selection'))
        for i in data['items']:
            i['selection_explanation']=session.info.get('coupon_selection_explanations',{}).get((i['match']['id'],i['recommendation']['id']))
    else:
        reason=('analysis_unavailable' if session.info.get('prediction_snapshot',{}).get('stale') else
                'no_today_candidates' if not diagnostics.get('total_matches') else
                'quality_rejected' if not diagnostics.get('quality_matches') else
                'line_mismatch' if any(m.get('price_rejections',{}).get('line_mismatch') for m in diagnostics.get('matches',[])) else
                'price_rejected' if not diagnostics.get('eligible_matches') else 'portfolio_rejected')
        data=empty_daily(tokens['day'],reason,strategy)
    data.update(diagnostics=diagnostics,data_updates={'checked_at':now,
        'analysis_generated_at':diagnostics.get('last_analysis_generated_at'),
        'price_observed_at':diagnostics.get('last_quality_price_observed_at')})
    data=jsonable_encoder(data)
    version=digest(stable(data))
    if state:
        state.input_revision=tokens['coupon_input'];state.checked_at=now
        if state.version!=version:state.version=version;state.payload=data
    else:session.add(DailyCouponPublication(day=tokens['day'],strategy=strategy,
        input_revision=tokens['coupon_input'],version=version,checked_at=now,payload=data))
    return data
