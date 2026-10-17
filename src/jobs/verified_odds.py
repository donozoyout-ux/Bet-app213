"""Atomic fill-only archived odds. Never refresh latest quotes or replace prices."""
from sqlalchemy import select, and_, or_, case, func, cast, literal
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy.types import JSON
from src.models import Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, utcnow

BOOKS = ('Crown','Bet365','Sbobet')
MARKETS = ((Odds1X2,'1x2',('home','draw','away')),
           (AsianHandicap,'ah',('line','home','away')),
           (AsianTotals,'ou',('line','over','under')))


def complete_expression():
    checks=[]
    for book in BOOKS:
        for model,_,sides in MARKETS:
            checks.append(select(model.id).join(Bookmaker,model.bookmaker_id==Bookmaker.id).where(
                model.match_id==Match.id,Bookmaker.name==book,
                *(getattr(model,f'{stage}_{side}').is_not(None)
                  for stage in ('opening','closing') for side in sides)).correlate(Match).exists())
    return and_(*checks)


async def is_complete(session,match_id):
    return bool(await session.scalar(select(complete_expression()).select_from(Match).where(Match.id==match_id)))


def update_plan(table, incoming, sides, provenance):
    """Evaluate against the conflicting row at write time, including line safety."""
    fields=[f'{stage}_{side}' for stage in ('opening','closing') for side in sides]
    conditions={}
    for stage in ('opening','closing'):
        names=[f'{stage}_{side}' for side in sides]
        agree=and_(*(or_(table.c[n].is_(None),incoming[n].is_(None),
                          func.abs(table.c[n]-incoming[n])<1e-8) for n in names))
        if 'line' in sides:
            # A missing line cannot be guessed for already stored side prices.
            agree=and_(agree,incoming[f'{stage}_line'].is_not(None),
                or_(table.c[f'{stage}_line'].is_not(None),
                    and_(*(table.c[f'{stage}_{s}'].is_(None) for s in sides if s!='line'))))
        for name in names:
            conditions[name]=and_(agree,table.c[name].is_(None),incoming[name].is_not(None))
    raw=cast(func.coalesce(table.c.raw,literal({},type_=JSON)),JSONB)
    stamps=func.coalesce(raw['_field_observed_at'],literal({},type_=JSONB))
    lines=func.coalesce(raw['_field_lines'],literal({},type_=JSONB))
    stamp_args=[];line_args=[]
    now=utcnow()
    for name in fields:
        stamp_args.extend([name,case((conditions[name],now.isoformat()),else_=None)])
        if 'line' in sides and not name.endswith('_line'):
            line_args.extend([name,case((conditions[name],incoming[name.split('_',1)[0]+'_line']),else_=None)])
    metadata=func.jsonb_build_object(
        '_field_observed_at',stamps.op('||')(func.jsonb_strip_nulls(func.jsonb_build_object(*stamp_args))),
        '_field_lines',lines.op('||')(func.jsonb_strip_nulls(func.jsonb_build_object(*line_args))) if line_args else lines,
        '_verified_odds_fill',literal(provenance,type_=JSONB))
    values={name:case((conditions[name],incoming[name]),else_=table.c[name]) for name in fields}
    values.update(raw=raw.op('||')(metadata),updated_at=now)
    return values,or_(*conditions.values())


async def fill_only(session,match_id,external_match_id,job_id,odds):
    """Three small bulk inserts/upserts; all existing non-NULL values survive.

    Only opening/closing fields are written. Match scores/statistics, latest_*
    values, their observation times and match.last_scraped_at are untouched.
    """
    books=dict((await session.execute(select(Bookmaker.name,Bookmaker.id).where(Bookmaker.name.in_(BOOKS)))).all())
    if set(books)!=set(BOOKS):
        raise ValueError('Three verified bookmakers must already exist')
    inserted=updated=0
    now=utcnow()
    for model,market,sides in MARKETS:
        rows=[]
        for book in BOOKS:
            data=odds.get(book,{}).get(market,{})
            values={f'{stage}_{side}':data.get(f'{stage}_{side}')
                    for stage in ('opening','closing') for side in sides}
            if not any(v is not None for v in values.values()):
                continue
            # Source parser supplies a line with each AH/OU observation.
            if 'line' in sides:
                for stage in ('opening','closing'):
                    if values[f'{stage}_line'] is None:
                        for side in sides:
                            values[f'{stage}_{side}']=None
            if not any(v is not None for v in values.values()):
                continue
            stamps={k:now.isoformat() for k,v in values.items() if v is not None}
            line_values={k:values[k.split('_',1)[0]+'_line'] for k,v in values.items()
                         if v is not None and 'line' in sides and not k.endswith('_line')}
            raw={**data.get('raw',{}),'_field_observed_at':stamps,'_field_lines':line_values}
            rows.append(dict(match_id=match_id,bookmaker_id=books[book],raw=raw,updated_at=now,**values))
        if not rows:
            continue
        stmt=insert(model).values(rows)
        inserted+=len((await session.scalars(stmt.on_conflict_do_nothing(
            index_elements=['match_id','bookmaker_id']).returning(model.id))).all())
        provenance=dict(source='goaloo',external_match_id=external_match_id,job_id=job_id,observed_at=now.isoformat())
        values,changed=update_plan(model.__table__,stmt.excluded,sides,provenance)
        updated+=len((await session.scalars(stmt.on_conflict_do_update(
            index_elements=['match_id','bookmaker_id'],set_=values,where=changed).returning(model.id))).all())
    return dict(inserted_rows=inserted,updated_rows=updated)
