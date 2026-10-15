"""Read-only collection health: discovery success is independent of odds failures."""
from datetime import timedelta
from sqlalchemy import select,func,String
from src.models import Match,MatchStatistics,League,ScraperJob,JobItem,CollectorRetry,utcnow
from .worker import aware


async def collection_health(session,now=None):
    now=now or utcnow()
    leagues=(await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.priority,League.id))).all()
    jobs=(await session.scalars(select(ScraperJob).where(ScraperJob.league_id.in_([l.id for l in leagues]),
        ScraperJob.kind.in_(['update','backfill'])).order_by(ScraperJob.id.desc()))).all()
    latest={};success={}
    for job in jobs:
        latest.setdefault(job.league_id,job)
        if job.discovery_complete and job.discovery_finished_at and not any(v.get('errors',0) for v in (job.discovery_metrics or {}).values()):
            stamp=aware(job.discovery_finished_at)
            success[job.league_id]=max(stamp,success.get(job.league_id,stamp))
    rows=[]
    for league in leagues:
        job=latest.get(league.id);metrics=list((job.discovery_metrics or {}).values()) if job else []
        stamp=success.get(league.id)
        rows.append(dict(league_id=league.id,external_id=league.external_id,name=league.name,
            last_successful_discovery_at=stamp,stale=stamp is None or stamp<now-timedelta(hours=26),
            discovered=sum(v.get('discovered',0) for v in metrics),added=sum(v.get('added',0) for v in metrics),
            updated=sum(v.get('updated',0) for v in metrics),errors=sum(v.get('errors',0) for v in metrics),
            job_status=job.status if job else None))
    failed=await session.scalar(select(func.count(JobItem.id)).join(ScraperJob).join(League).where(League.enabled.is_(True),
        ScraperJob.created_at>=now-timedelta(hours=24),JobItem.status=='failed'))
    active=await session.scalar(select(func.count(ScraperJob.id)).join(League).where(League.enabled.is_(True),ScraperJob.status.in_(['queued','running'])))
    partial=await session.scalar(select(func.count(ScraperJob.id)).join(League).where(League.enabled.is_(True),
        ScraperJob.created_at>=now-timedelta(hours=24),ScraperJob.status.in_(['partial','failed'])))
    retries=await session.scalar(select(func.count(CollectorRetry.key)).where(CollectorRetry.failures>0,CollectorRetry.next_attempt_at>now))
    partial_sources=await session.scalar(select(func.count(CollectorRetry.key)).join(Match,
        CollectorRetry.key=='quotes:full_time:'+Match.external_match_id.cast(String)).join(League).where(
            League.enabled.is_(True),Match.status=='scheduled',Match.kickoff_at>now,CollectorRetry.reason=='quote_partial_source'))
    updated=await session.scalar(select(func.max(Match.last_scraped_at)))
    stats=await session.scalar(select(func.max(MatchStatistics.updated_at)).where(MatchStatistics.collection_status=='available'))
    last_update=max((aware(t) for t in (updated,stats) if t is not None),default=None)
    return dict(last_successful_discovery_at=max(success.values(),default=None),last_data_update_at=last_update,
        stale_fixture_leagues=sum(r['stale'] for r in rows),failed_collection_items_24h=failed,
        partial_collection_jobs_24h=partial,active_collection_jobs=active,collection_retry_cooldowns=retries,partial_quote_sources=partial_sources,fixture_collection=rows)
