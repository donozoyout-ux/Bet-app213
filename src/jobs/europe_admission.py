"""Admission for two reviewed leagues; prediction/quote thresholds are unchanged."""
from datetime import datetime, timezone, timedelta
import json
import hashlib
from pathlib import Path
from sqlalchemy import select, and_, func, case, text
from src.models import Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, League, ScraperJob, utcnow

EVIDENCE = Path(__file__).resolve().parents[2]/'docs/europe-stage2-source-audit.json'
STAGED_IDS = (6, 7)
POLICY = 'europe-stage2-20261017'


async def retained_admission(session, league):
    """Keep proved collection admission as ordinary discovery adds unpriced rows.

    This never marks any newly discovered fixture/quote as prediction-qualified.
    The existing supervised job is the durable ledger; no duplicate job/table.
    """
    if league.external_id not in STAGED_IDS:
        return False
    job=await session.scalar(select(ScraperJob).where(ScraperJob.league_id==league.id,
        ScraperJob.kind=='europe_bounded').order_by(ScraperJob.id).limit(1))
    metrics=job.discovery_metrics if job and isinstance(job.discovery_metrics,dict) else {}
    proof=metrics.get('_stage2_admission',{})
    if not isinstance(proof,dict):
        return False
    try:
        return bool(proof.get('policy')==POLICY and proof.get('external_id')==league.external_id
            and proof.get('ready') is True and proof.get('source_reviewed') is True
            and proof['finished']>=50 and proof['complete_three_books']/proof['finished']>=.95
            and proof['recent_complete']>=20 and len(proof['source_sha256'])==64
            and datetime.fromisoformat(proof['admitted_at']))
    except (KeyError,ValueError,TypeError,ZeroDivisionError):
        return False


def source_review_passes(external_id):
    try:
        report = json.loads(EVIDENCE.read_text(encoding='utf-8'))
        row = next(r for r in report['leagues'] if r['external_id']==external_id)
        current = row['current_season_samples']
        return (external_id in STAGED_IDS and report['read_only'] is True
            and not row['source_errors'] and not row['mismatches']
            and len(row['odds_samples'])>=3
            and all(not s['gaps'] and not s['changed_stored_fields'] for s in row['odds_samples'])
            and sum(s['final'] for s in current)>=3
            and sum(not s['final'] for s in current)>=2
            and all(not s['gaps'] for s in current)
            and all(s['parse_errors']==0 for s in row['seasons'].values()))
    except (OSError,ValueError,KeyError,TypeError,StopIteration):
        return False


async def admission_status(session, league):
    """Count stored non-NULL fields, not the cached odds_complete flag."""
    now = utcnow()
    columns = [getattr(model,f'{stage}_{field}').is_not(None)
        for model,fields in ((Odds1X2,('home','draw','away')),
            (AsianHandicap,('line','home','away')),(AsianTotals,('line','over','under')))
        for stage in ('opening','closing') for field in fields]
    columns.extend(getattr(Match,f).is_not(None) for f in ('ft_home','ft_away','ht_home','ht_away'))
    query = select(Match.id,Match.kickoff_at,func.sum(case((and_(*columns),1),else_=0)).label('complete_books')).select_from(Match).join(
        Bookmaker,Bookmaker.name.in_(('Crown','Bet365','Sbobet'))).outerjoin(
        Odds1X2,and_(Odds1X2.match_id==Match.id,Odds1X2.bookmaker_id==Bookmaker.id)).outerjoin(
        AsianHandicap,and_(AsianHandicap.match_id==Match.id,AsianHandicap.bookmaker_id==Bookmaker.id)).outerjoin(
        AsianTotals,and_(AsianTotals.match_id==Match.id,AsianTotals.bookmaker_id==Bookmaker.id)).where(
        Match.league_id==league.id,Match.status=='finished',
        Match.kickoff_at>=datetime(2024,1,1,tzinfo=timezone.utc),Match.kickoff_at<=now-timedelta(hours=3)
        ).group_by(Match.id,Match.kickoff_at)
    rows = (await session.execute(query)).all()
    total = len(rows)
    complete = sum(r.complete_books==3 for r in rows)
    recent = sum(r.complete_books==3 and (r.kickoff_at.replace(tzinfo=timezone.utc) if r.kickoff_at.tzinfo is None else r.kickoff_at)>=now-timedelta(days=180) for r in rows)
    reviewed = source_review_passes(league.external_id)
    return dict(external_id=league.external_id,finished=total,complete_three_books=complete,
        recent_complete=recent,source_reviewed=reviewed,
        ready=bool(reviewed and total>=50 and complete/total>=.95 and recent>=20))


async def activate_reviewed(session, ids):
    if not ids or len(ids)!=len(set(ids)) or not set(ids)<=set(STAGED_IDS):
        raise ValueError('Activation is limited to reviewed Poland/Denmark')
    if session.bind.dialect.name=='postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(213001)'))
    results=[]
    for id in ids:
        league=await session.scalar(select(League).where(League.external_id==id))
        if league is None:
            raise ValueError('Reviewed league does not exist')
        if await retained_admission(session,league):
            league.enabled=True
            results.append(dict(external_id=id,ready=True,retained=True))
            continue
        status=await admission_status(session,league)
        if not status['ready']:
            raise ValueError(f'League {id} has insufficient verified recent history')
        job=await session.scalar(select(ScraperJob).where(ScraperJob.league_id==league.id,
            ScraperJob.kind=='europe_bounded').order_by(ScraperJob.id).limit(1))
        if job is None:
            raise ValueError('Existing supervised history checkpoint is required')
        job.discovery_metrics={**(job.discovery_metrics or {}),'_stage2_admission':{
            **status,'policy':POLICY,'admitted_at':utcnow().isoformat(),
            'source_sha256':hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()}}
        league.enabled=True
        results.append(status)
    await session.flush()
    return results
