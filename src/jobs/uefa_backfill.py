"""Explicit, bounded domestic archive backfill using the existing durable worker."""
from datetime import timedelta
from sqlalchemy import select, text, or_
from src.models import League, Match, ScraperJob, utcnow
from src.jobs.worker import aware


async def enqueue_domestic_backfill(db, now=None):
    now=now or utcnow(); output=[]
    async with db.session() as session:
        if session.bind.dialect.name=='postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        fixtures=(await session.scalars(select(Match).join(League).where(League.external_id==113,
            Match.status=='scheduled',Match.kickoff_at>=now,Match.kickoff_at<now+timedelta(days=30)))).all()
        teams={t for m in fixtures for t in (m.home_team_id,m.away_team_id)}
        sources=set(await session.scalars(select(Match.league_id).join(League).where(
            League.schedule_format=='league',or_(Match.home_team_id.in_(teams),Match.away_team_id.in_(teams)))))
        for league in await session.scalars(select(League).where(League.id.in_(sources)).order_by(League.id)):
            if not league.enabled or league.verified_at is None:
                output.append(dict(league_id=league.external_id,status='blocked_disabled_or_unverified')); continue
            previous=await session.scalar(select(ScraperJob).where(ScraperJob.league_id==league.id,
                ScraperJob.kind=='domestic_history').order_by(ScraperJob.id.desc()).limit(1))
            active=await session.scalar(select(ScraperJob.id).where(ScraperJob.league_id==league.id,
                ScraperJob.status.in_(['queued','running'])).limit(1))
            if active or previous and aware(previous.created_at)>now-timedelta(days=7):
                output.append(dict(league_id=league.external_id,status='active_or_cooldown')); continue
            job=ScraperJob(kind='domestic_history',league_id=league.id,start_year=2024,
                priority=league.priority+100,created_at=now)
            session.add(job);await session.flush()
            output.append(dict(league_id=league.external_id,status='queued',job_id=job.id))
        await session.commit()
    return output
