"""Read-only checks. Deliberately exclude URLs, exception messages and job errors."""
from sqlalchemy import select, func, text, inspect
from src.models import Base, League, Match, Odds1X2, AsianHandicap, AsianTotals, ScraperJob


async def database_summary(db, verify_schema=False):
    async with db.session() as session:
        await session.execute(text('SELECT 1'))
        result = {}
        if verify_schema:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            missing = sorted(set(Base.metadata.tables) - set(tables))
            result['schema_ok'] = not missing
            result['missing_tables'] = missing
            if missing:
                return result
        result['league_count'] = await session.scalar(select(func.count(League.id)))
        result['enabled_competitions'] = await session.scalar(select(func.count(League.id)).where(League.enabled.is_(True)))
        complete = select(ScraperJob.league_id).where(ScraperJob.kind == 'backfill', ScraperJob.status == 'completed').distinct()
        result['completed_competitions'] = await session.scalar(select(func.count(League.id)).where(League.enabled.is_(True), League.id.in_(complete)))
        result['active_backfill_competition'] = await session.scalar(select(League.name).join(ScraperJob).where(ScraperJob.kind == 'backfill', ScraperJob.status == 'running').limit(1))
        result['queued_backfills'] = await session.scalar(select(func.count(ScraperJob.id)).join(League).where(League.enabled.is_(True), ScraperJob.kind == 'backfill', ScraperJob.status == 'queued'))
        result['total_matches'] = await session.scalar(select(func.count(Match.id)))
        result['total_odds'] = sum([await session.scalar(select(func.count(model.id))) for model in (Odds1X2, AsianHandicap, AsianTotals)])
        job = await session.scalar(select(ScraperJob).order_by(ScraperJob.id.desc()).limit(1))
        result['latest_job_status'] = job.status if job else None
        result['latest_job'] = {'id': job.id, 'kind': job.kind, 'status': job.status,
                                'processed': job.processed_matches, 'total': job.total_matches,
                                'failed': job.failed_matches} if job else None
        return result
