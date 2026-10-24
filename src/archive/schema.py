"""No provider payloads, free text, credentials or executable restored state."""
from datetime import datetime, timezone
from typing import Literal
import hashlib
import json
import math
import re
from pydantic import BaseModel, ConfigDict, Field

SCHEMA='kupon-owned-audit-v2'
MAX_BYTES=32*1024*1024
MAX_ROWS=50000
MARKETS={'result','goals','btts','corners','cards','yellow_cards','red_cards',
         'asian_handicap','double_chance','first_half_goals','second_half_goals'}

class Pick(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    index: int=Field(ge=0)
    market: str
    selection: str
    probability: float | None=Field(default=None,ge=0,le=1,allow_inf_nan=False)

class Forecast(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    match_id: int=Field(gt=0)
    captured_at: str
    model_digest: str=Field(pattern=r'^[a-f0-9]{64}$')
    picks: list[Pick]

class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    id: int=Field(gt=0)
    match_id: int=Field(gt=0)
    pick_index: int=Field(ge=0)
    revision: int=Field(ge=1)
    outcome: Literal['won','lost','void']
    recorded_at: str

class Coupon(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    id: str=Field(pattern=r'^[a-f0-9-]{36}$')
    revision: int=Field(ge=1)
    created_at: str

class Report(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    id: int=Field(gt=0)
    created_at: str
    method_digest: str=Field(pattern=r'^[a-f0-9]{64}$')

class LeagueCoverage(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    league_code: int=Field(gt=0)
    fixtures: int=Field(ge=0)
    newest_fixture_date: str | None
    odds_complete: int=Field(ge=0)
    finished: int=Field(ge=0)
    statistics_complete: int=Field(ge=0)
    missing_odds: int=Field(ge=0)
    missing_statistics: int=Field(ge=0)
    backlog: int=Field(ge=0)

class Coverage(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    day: str
    leagues: list[LeagueCoverage]
    database_bytes: int=Field(ge=0)
    quota_bytes: int=Field(gt=0)
    previous_database_bytes: int | None=Field(default=None,ge=0)
    growth_bytes: int | None=None
    previous_day: str | None=None
    missing_run_days: int=Field(ge=0)
    expected_league_codes: list[int]=Field(default_factory=list)
    missing_league_codes: list[int]=Field(default_factory=list)

class Audit(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['kupon-owned-audit-v2']=SCHEMA
    created_at: str
    rights_reference: str=Field(pattern=r'^APP-[0-9]{1,12}$')
    forecasts: list[Forecast]
    decisions: list[Decision]
    coupons: list[Coupon]
    reports: list[Report]
    coverage: Coverage


def stamp(value):
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def digest(value):return hashlib.sha256(str(value).encode()).hexdigest()


def selection_ok(market,selection):
    if not isinstance(selection,str):return False
    fixed={'result':{'home','draw','away'},'btts':{'yes','no'},'double_chance':{'1x','x2','12'}}
    if market in fixed:return selection in fixed[market]
    pattern=r'(home|away)_(plus|minus)_[0-9]{1,2}_[0257]5?' if market=='asian_handicap' else r'(over|under)_[0-9]{1,2}_[0257]5?'
    return bool(re.fullmatch(pattern,selection))


def validate(data):
    audit=Audit.model_validate(data)
    groups=[audit.forecasts,audit.decisions,audit.coupons,audit.reports]
    if any(len(rows)>MAX_ROWS for rows in groups):raise ValueError('Archive row limit')
    datetime.fromisoformat(audit.created_at)
    from datetime import date
    day=date.fromisoformat(audit.coverage.day)
    if day.isoformat()!=audit.coverage.day:raise ValueError('Invalid coverage day')
    if len(audit.coverage.leagues)>500:raise ValueError('League limit')
    if len({r.league_code for r in audit.coverage.leagues})!=len(audit.coverage.leagues):raise ValueError('Duplicate league')
    expected=audit.coverage.expected_league_codes
    if len(expected)>500 or len(set(expected))!=len(expected) or any(v<=0 for v in expected):raise ValueError('Invalid expected leagues')
    if audit.coverage.missing_league_codes!=sorted(set(expected)-{r.league_code for r in audit.coverage.leagues}):raise ValueError('Invalid missing leagues')
    for row in audit.coverage.leagues:
        if row.newest_fixture_date:date.fromisoformat(row.newest_fixture_date)
        if row.odds_complete+row.missing_odds!=row.fixtures or row.statistics_complete+row.missing_statistics!=row.finished:raise ValueError('Invalid coverage counts')
    previous=audit.coverage.previous_day
    if previous:
        delta=(day-date.fromisoformat(previous)).days
        if delta<=0 or audit.coverage.missing_run_days!=delta-1:raise ValueError('Invalid previous day')
        if audit.coverage.previous_database_bytes is None or audit.coverage.growth_bytes!=audit.coverage.database_bytes-audit.coverage.previous_database_bytes:raise ValueError('Invalid storage trend')
    elif audit.coverage.previous_database_bytes is not None or audit.coverage.growth_bytes is not None or audit.coverage.missing_run_days:raise ValueError('Unknown storage baseline')
    for rows,key in [(audit.forecasts,'match_id'),(audit.decisions,'id'),(audit.coupons,'id'),(audit.reports,'id')]:
        if len({getattr(r,key) for r in rows})!=len(rows):raise ValueError('Duplicate archive identity')
    snapshots={r.match_id:r for r in audit.forecasts}
    seen=set()
    for forecast in audit.forecasts:
        datetime.fromisoformat(forecast.captured_at)
        if len({p.index for p in forecast.picks})!=len(forecast.picks):raise ValueError('Duplicate pick')
        for p in forecast.picks:
            if p.market not in MARKETS or not selection_ok(p.market,p.selection):raise ValueError('Invalid pick')
    for d in audit.decisions:
        datetime.fromisoformat(d.recorded_at)
        if d.match_id not in snapshots or d.pick_index not in {p.index for p in snapshots[d.match_id].picks}:raise ValueError('Orphan decision')
        identity=(d.match_id,d.pick_index,d.revision)
        if identity in seen:raise ValueError('Duplicate decision revision')
        seen.add(identity)
    for r in audit.coupons:datetime.fromisoformat(r.created_at)
    for r in audit.reports:datetime.fromisoformat(r.created_at)
    return audit.model_dump()


def canonical(data):return json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()


async def collect(connection,rights_reference,now=None):
    """Use only explicit columns. Caller owns a read-only consistent transaction."""
    from sqlalchemy import select
    from src.models import PredictionSnapshot as P, PredictionSettlement as S, SavedCoupon as C, LearningReport as L, Match
    def aware(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    data=dict(schema_version=SCHEMA,created_at=stamp(now or datetime.now(timezone.utc)),rights_reference=rights_reference,
              forecasts=[],decisions=[],coupons=[],reports=[])
    query=select(P.match_id,P.captured_at,P.model_version,P.picks,Match.kickoff_at).join(Match,Match.id==P.match_id).order_by(P.match_id).limit(MAX_ROWS+1)
    forecast_rows=(await connection.execute(query)).mappings().all()
    if len(forecast_rows)>MAX_ROWS:raise ValueError('Archive row limit')
    for row in forecast_rows:
        if not row['kickoff_at'] or aware(row['captured_at'])>=aware(row['kickoff_at']):continue
        picks=[]
        for i,p in enumerate(row['picks']):
            market=p.get('market');selection=p.get('selection')
            if market not in MARKETS or not selection_ok(market,selection):continue
            probability=p.get('probability')
            if isinstance(probability,bool) or not isinstance(probability,(int,float)) or not math.isfinite(probability) or not 0<=probability<=1:probability=None
            picks.append(dict(index=i,market=market,selection=selection,probability=probability))
        data['forecasts'].append(dict(match_id=row['match_id'],captured_at=stamp(row['captured_at']),model_digest=digest(row['model_version']),picks=picks))
    eligible={(f['match_id'],p['index']) for f in data['forecasts'] for p in f['picks']}
    decision_rows=(await connection.execute(select(S.id,S.match_id,S.pick_index,S.revision,S.outcome,S.recorded_at).order_by(S.id).limit(MAX_ROWS+1))).mappings().all()
    if len(decision_rows)>MAX_ROWS:raise ValueError('Archive row limit')
    for r in decision_rows:
        if (r['match_id'],r['pick_index']) in eligible:data['decisions'].append(dict(r,recorded_at=stamp(r['recorded_at'])))
    for r in (await connection.execute(select(C.id,C.revision,C.created_at).order_by(C.id).limit(MAX_ROWS+1))).mappings():
        data['coupons'].append(dict(r,created_at=stamp(r['created_at'])))
    for r in (await connection.execute(select(L.id,L.created_at,L.method_version).order_by(L.id).limit(MAX_ROWS+1))).mappings():
        data['reports'].append(dict(id=r['id'],created_at=stamp(r['created_at']),method_digest=digest(r['method_version'])))
    # Coverage is attached by the same read-only transaction before validation.
    return data
