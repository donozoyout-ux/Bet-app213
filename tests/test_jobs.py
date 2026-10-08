from sqlalchemy import select, func
from src.models import ScraperJob, JobItem, Match, Bookmaker, OddsSnapshot, League, utcnow
from src.jobs.storage import store_history
from src.jobs.worker import enqueue, run_job
from src.scrapers.goaloo.odds import parse_odds
from .test_goaloo import fixture
from datetime import timezone, timedelta


async def install_source(monkeypatch,db):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    async def seasons(*args):return ['2024-2025']
    async def data(*args):return fixture('goaloo_league.json')
    async def odds(*args):return parse_odds(fixture('goaloo_odds.json'),True)
    monkeypatch.setattr(worker,'discover_seasons',seasons)
    monkeypatch.setattr(worker,'season_data',data)
    monkeypatch.setattr(worker,'fetch_odds',odds)
    async def no_stats(*args):
        from src.scrapers.goaloo.client import SourceError
        raise SourceError('Statistics not included in the odds-only fixture')
    monkeypatch.setattr(worker,'fetch_statistics',no_stats)
    return worker


async def test_backfill_complete_and_restart_skips_complete_matches(db,monkeypatch):
    await install_source(monkeypatch,db)
    job = await enqueue('backfill')
    await run_job(job.id)
    async with db.session() as session:
        stored = await session.get(ScraperJob,job.id)
        assert (stored.status,stored.processed_matches,stored.failed_matches)==('completed',10,0)
        assert await session.scalar(select(func.count(Match.id)))==10
    next_job = await enqueue('backfill')
    await run_job(next_job.id)
    async with db.session() as session:
        job = await session.get(ScraperJob,next_job.id)
        assert job.status == 'completed' and job.total_matches == 0


async def test_match_failure_continues_and_resume_retries_only_failure(db,monkeypatch):
    worker = await install_source(monkeypatch,db)
    calls=[]
    async def odds(client,external_id,final):
        calls.append(external_id)
        if external_id==2590898:raise ValueError('broken source')
        return parse_odds(fixture('goaloo_odds.json'),True)
    monkeypatch.setattr(worker,'fetch_odds',odds)
    job=await enqueue('backfill')
    await run_job(job.id)
    async with db.session() as session:
        stored=await session.get(ScraperJob,job.id)
        assert stored.status=='partial' and stored.processed_matches==9 and stored.failed_matches==1
        assert '2590898' in stored.last_error
    calls.clear()
    async def fixed(client,external_id,final):
        calls.append(external_id);return parse_odds(fixture('goaloo_odds.json'),True)
    monkeypatch.setattr(worker,'fetch_odds',fixed)
    await enqueue('backfill',resume_id=job.id)
    await run_job(job.id)
    assert calls==[2590898]
    async with db.session() as session:
        stored=await session.get(ScraperJob,job.id)
        assert stored.status=='completed' and stored.processed_matches==10 and stored.failed_matches==0


async def test_interrupted_running_item_is_recovered(db,monkeypatch):
    worker = await install_source(monkeypatch,db)
    job = await enqueue('backfill')
    from src.scrapers.goaloo.client import GoalooClient
    async with GoalooClient() as client:
        await worker.discover(job.id,client)
    async with db.session() as session:
        job=await session.get(ScraperJob,job.id);job.status='running'
        item=await session.scalar(select(JobItem).where(JobItem.job_id==job.id));item.status='running'
        await session.commit()
    await worker.work(once=True)
    async with db.session() as session:
        assert (await session.get(ScraperJob,job.id)).status=='completed'


async def test_snapshot_storage_is_idempotent(db,monkeypatch):
    await install_source(monkeypatch,db)
    job=await enqueue('backfill');await run_job(job.id)
    async with db.session() as session:
        match=await session.scalar(select(Match));book=await session.scalar(select(Bookmaker))
        timestamp=utcnow();history=[('1x2',timestamp,{'mt':123,'odds':{'u':'1.5'}})]
        await store_history(session,match.id,book.id,history)
        await store_history(session,match.id,book.id,history)
        await session.commit()
        assert await session.scalar(select(func.count(OddsSnapshot.id)))==1


async def test_malformed_match_does_not_discard_round_and_is_retryable(db,monkeypatch):
    worker=await install_source(monkeypatch,db)
    payload=fixture('goaloo_league.json')
    payload['ScheduleList']['R_1'][0][6]='unrecognized score'
    async def malformed(*args):return payload
    monkeypatch.setattr(worker,'season_data',malformed)
    job=await enqueue('backfill');await run_job(job.id)
    async with db.session() as session:
        stored=await session.get(ScraperJob,job.id)
        assert (stored.status,stored.total_matches,stored.processed_matches,stored.failed_matches)==('partial',10,9,1)
    async def fixed(*args):return fixture('goaloo_league.json')
    monkeypatch.setattr(worker,'season_data',fixed)
    await enqueue('backfill',resume_id=job.id);await run_job(job.id)
    async with db.session() as session:
        stored=await session.get(ScraperJob,job.id)
        assert stored.status=='completed' and stored.failed_matches==0


async def test_daily_refreshes_schedules_but_limits_odds_to_recent_matches(db,monkeypatch):
    worker=await install_source(monkeypatch,db)
    payload=fixture('goaloo_league.json')
    payload['ScheduleList']['R_1'][0][3]=utcnow().astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    async def recent(*args):return payload
    monkeypatch.setattr(worker,'season_data',recent)
    calls=[]
    async def odds(client,external_id,final):
        calls.append(external_id);return parse_odds(fixture('goaloo_odds.json'),True)
    monkeypatch.setattr(worker,'fetch_odds',odds)
    job=await enqueue('update');await run_job(job.id)
    assert calls==[2590898]
    async with db.session() as session:
        assert await session.scalar(select(func.count(Match.id)))==10


async def test_single_match_refresh_updates_scores_and_odds(db,monkeypatch):
    worker=await install_source(monkeypatch,db)
    job=await enqueue('backfill');await run_job(job.id)
    async with db.session() as session:
        match=await session.scalar(select(Match).where(Match.external_match_id==2590898))
        internal_id=match.id
    from src.scrapers.goaloo.matches import parse_matches
    data=next(parse_matches(fixture('goaloo_league.json'),1))
    data['ft_home']=2
    async def details(*args):return data
    monkeypatch.setattr(worker,'fetch_match_details',details)
    job=await enqueue('match',match_id=internal_id);await run_job(job.id)
    async with db.session() as session:
        assert (await session.get(Match,internal_id)).ft_home==2
        assert (await session.get(ScraperJob,job.id)).total_matches==1
