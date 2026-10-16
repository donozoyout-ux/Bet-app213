"""Strong cached predictions + fresh database quotes; no on-demand model scan."""
from copy import deepcopy
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select,text
from sqlalchemy.orm import defer
from src.models import SavedCoupon,Match,MatchStatistics
from src.analytics.coupon import quote_draft,combined_price,draft_pick,coupon_qualified
from src.analytics.performance import settle
from src.jobs.worker import aware
from src.analytics.dynamic_coupon import select_portfolio,POLICY_VERSION

COUPON_LOCK=213009
COUPON_TZ=ZoneInfo('Europe/Istanbul')


def daily_candidates(records, now, strategy='balanced'):
    return select_portfolio(records,now,strategy)[0]


async def lock_coupons(session):
    if session.bind.dialect.name=='postgresql':await session.execute(text('SELECT pg_advisory_xact_lock(:id)'),{'id':COUPON_LOCK})


def owner_hash(key):
    if not key or len(key)<32 or len(key)>200:raise HTTPException(401,'Browser coupon key required')
    return hashlib.sha256(key.encode()).hexdigest()


def fingerprint(items, include_observation=True):
    economic=[(i['match']['id'],i['recommendation']['id'],i['quote']['bookmaker'],i['quote']['price'],i['quote']['observed_at'] if include_observation else None,
        i['recommendation']['probability'],i['recommendation']['confidence'],i['recommendation'].get('reliability'),i['recommendation'].get('score'),jsonable_encoder(i['match']['kickoff_at']),i['match']['home_team'],i['match']['away_team']) for i in items]
    return hashlib.sha256(json.dumps(sorted(economic),separators=(',',':')).encode()).hexdigest()


def payload(items):
    # Keep only coupon-relevant fields, not the entire board's evidence/three-book cache.
    rec_fields=('id','market','selection','label','probability','confidence','qualifies','score','reliability','sample_size')
    saved=[{**i,'recommendation':{k:v for k,v in i['recommendation'].items() if k in rec_fields}} for i in items]
    return jsonable_encoder(dict(items=saved,combined_odds=combined_price([i['quote']['price'] for i in items]),mixed_bookmakers=len({i['quote']['bookmaker'] for i in items})>1))


def strong(rec):
    return coupon_qualified(rec)


async def automatic(session,now,strategy='balanced',display_limit=None):
    from src.analytics.daily_pool import daily_pool
    records=await daily_pool(session,now)
    groups={r['match']['id']:[c for c in r['recommendations'] if c['id'] in {b['id'] for b in r['coupon_candidates']} and strong(c)] for r in records}
    items=[]
    for record in records:
        for rec in sorted(groups[record['match']['id']],key=lambda c:(-c['score'],-c['reliability'],c['id'])):
            books=rec.get('verified_odds',{}).get('bookmakers',{})
            usable=[b for b in books.values() if b.get('coupon_eligible') and b.get('freshness')=='fresh']
            if not usable:continue
            book=max(usable,key=lambda b:(b['current_odds'],-('Crown','Bet365','Sbobet').index(b['bookmaker'])));vo=rec['verified_odds']
            quote=dict(eligible=True,price=book['current_odds'],bookmaker=book['bookmaker'],observed_at=book['observed_at'],expires_at=book['expires_at'],period=vo['period'],line=vo.get('line'),freshness='fresh')
            candidate=dict(match=record['match'],recommendation=rec,quote=quote,status='pending')
            items.append(candidate)
    diagnostics=session.info['coupon_diagnostics']
    items,selection=select_portfolio(items,now,strategy,limit=display_limit)
    if display_limit is not None:
        selection={**selection,'display_limit':display_limit}
    diagnostics['selection']=selection
    diagnostics['selected_matches']=len(items)
    session.info['coupon_selection_explanations']={(i['match']['id'],i['recommendation']['id']):i['selection_explanation'] for i in items}
    session.info['automatic_current_payload']=payload(items) if items else None
    if not items:return None
    await lock_coupons(session)
    publication_policy=POLICY_VERSION if display_limit is None else f'{POLICY_VERSION}-daily-{display_limit}'
    digest=hashlib.sha256(f'{now.astimezone(COUPON_TZ).date().isoformat()}:{strategy}:{publication_policy}:{fingerprint(items,include_observation=display_limit is None)}'.encode()).hexdigest()
    existing=await session.scalar(select(SavedCoupon).where(SavedCoupon.automatic_fingerprint==digest))
    if existing:return existing
    if display_limit is not None:
        start=now.astimezone(COUPON_TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        previous=await session.scalars(select(SavedCoupon).where(SavedCoupon.kind=='automatic',
            SavedCoupon.created_at>=start,SavedCoupon.created_at<=now).order_by(SavedCoupon.created_at.desc()))
        for previous_coupon in previous:
            if previous_coupon.payload.get('strategy','balanced')==strategy and fingerprint(previous_coupon.payload['items'],include_observation=False)==fingerprint(items,include_observation=False):
                return previous_coupon
    id=str(uuid.uuid4());row=SavedCoupon(id=id,root_id=id,revision=1,kind='automatic',automatic_fingerprint=digest,owner_hash=None,created_at=now,payload={**payload(items),'coupon_date':now.astimezone(COUPON_TZ).date().isoformat(),'strategy':strategy,'selection_mode':'dynamic','selection_policy':selection})
    session.add(row);await session.flush();return row


async def save_manual(session,request,key,now,base_id=None):
    owner=owner_hash(key)
    data=await quote_draft(session,request,now)
    if not data['eligible']:raise HTTPException(409,'Every selection needs a fresh qualified quote')
    matches={m.id:m for m in await session.scalars(select(Match).where(Match.id.in_([i.match_id for i in request.selections])))}
    from src.api.routes import match_query,match_response
    query,_,_=match_query()
    contexts={r[0].id:match_response(r) for r in await session.execute(query.where(Match.id.in_(matches)))}
    items=[]
    for choice,quote in zip(request.selections,data['items']):
        items.append(dict(match=contexts[choice.match_id],recommendation=dict(id=choice.recommendation_id,market=quote['market'],selection=quote['selection'],label=quote['label'],probability=quote['probability'],confidence=quote['confidence'],reliability=quote['reliability'],score=quote['score'],qualifies=True),quote=quote,status='pending'))
    await lock_coupons(session)
    base=await session.get(SavedCoupon,base_id) if base_id else None
    if base_id and (base is None or base.owner_hash!=owner):raise HTTPException(404,'Coupon not found')
    revision=1;root=str(uuid.uuid4())
    if base:
        latest=await session.scalar(select(SavedCoupon).where(SavedCoupon.root_id==base.root_id).order_by(SavedCoupon.revision.desc()).limit(1))
        if latest.id!=base.id:raise HTTPException(409,'Coupon version changed; reload before editing')
        if fingerprint(items)==fingerprint(base.payload['items']):return base
        root=base.root_id;revision=base.revision+1
    id=root if revision==1 else str(uuid.uuid4())
    row=SavedCoupon(id=id,root_id=root,revision=revision,kind='manual',owner_hash=owner,automatic_fingerprint=None,created_at=now,payload=payload(items))
    session.add(row);await session.flush();return row


def summary(row):
    return dict(id=row.id,root_id=row.root_id,revision=row.revision,kind=row.kind,created_at=row.created_at,selections=len(row.payload['items']),combined_odds=row.payload['combined_odds'])


async def detail(session,row):
    result=summary(row);result.update(deepcopy(row.payload))
    ids=[i['match']['id'] for i in result['items']]
    matches={m.id:m for m in await session.scalars(select(Match).options(defer(Match.raw)).where(Match.id.in_(ids)))}
    stats={s.match_id:s for s in await session.scalars(select(MatchStatistics).options(defer(MatchStatistics.raw)).where(MatchStatistics.match_id.in_(ids)))}
    for item in result['items']:
        id=item['match']['id'];match=matches.get(id)
        outcome,actual=settle(match,stats.get(id),draft_pick(item['recommendation']['id'])) if match else ('awaiting_data',None)
        item.update(status=outcome,actual=actual)
        if match:
            item['match']['status']=match.status
            item['match']['score']=f'{match.ft_home}–{match.ft_away}' if match.ft_home is not None and match.ft_away is not None else None
            item['match']['half_time_score']=f'{match.ht_home}–{match.ht_away}' if match.ht_home is not None and match.ht_away is not None else None
    return result


async def accessible(session,id,key=None):
    row=await session.get(SavedCoupon,id)
    if row is None or row.owner_hash is not None and (not key or row.owner_hash!=owner_hash(key)):raise HTTPException(404,'Coupon not found')
    return row
