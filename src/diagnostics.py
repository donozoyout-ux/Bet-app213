"""Read-only checks. Deliberately exclude URLs, exception messages and job errors."""
from sqlalchemy import select, func, text, inspect, case, and_
from src.models import Base, League, Match, Odds1X2, AsianHandicap, AsianTotals, ScraperJob, MatchStatistics


async def statistics_coverage(session):
    final=and_(Match.status=='finished',MatchStatistics.is_final.is_(True))
    corners=and_(final,MatchStatistics.home_corners.is_not(None),MatchStatistics.away_corners.is_not(None))
    cards=and_(final,*[getattr(MatchStatistics,f).is_not(None) for f in
        ('home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards')])
    query=select(League.id,League.external_id,League.name,
        func.count(Match.id),func.count(case((Match.status=='finished',1))),
        func.count(MatchStatistics.match_id),func.count(case((corners,1))),
        func.count(case((cards,1))),func.count(case((and_(corners,cards),1)))).select_from(League).outerjoin(
        Match,Match.league_id==League.id).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id).where(
        League.enabled.is_(True)).group_by(League.id,League.external_id,League.name,League.priority).order_by(League.priority,League.id)
    result=[]
    for id,external_id,name,total,finished,rows,corner_count,card_count,both in (await session.execute(query)).all():
        percent=lambda n:round(100*n/finished,2) if finished else None
        result.append(dict(league_id=id,external_id=external_id,name=name,total_matches=total,finished_matches=finished,
            total_stats_rows=rows,matches_with_corners=corner_count,matches_with_cards=card_count,
            stats_coverage_percent=dict(corners=percent(corner_count),cards=percent(card_count),both=percent(both)),
            coverage_denominator='finished_matches',card_basis='yellow_plus_red_requires_all_four'))
    return result


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
        result['stats_coverage_by_league']=await statistics_coverage(session)
        stats_jobs=(await session.execute(select(ScraperJob,League.name).join(League).where(
            League.enabled.is_(True),ScraperJob.kind=='stats_backfill',ScraperJob.status.in_(['running','queued']))
            .order_by(ScraperJob.priority,ScraperJob.id))).all()
        serialize=lambda job,name:dict(id=job.id,league_id=job.league_id,league=name,status=job.status,
            processed=job.processed_matches,total=job.total_matches,failed=job.failed_matches)
        result['active_stats_backfill']=next((serialize(j,n) for j,n in stats_jobs if j.status=='running'),None)
        result['queued_stats_backfills']=[serialize(j,n) for j,n in stats_jobs if j.status=='queued']
        leagues=(await session.execute(select(League.id,League.external_id,League.name,func.count(Match.id)).outerjoin(Match).where(League.enabled.is_(True)).group_by(League.id,League.external_id,League.name).order_by(League.priority))).all()
        result['enabled_leagues']=[dict(id=id,external_id=external_id,name=name) for id,external_id,name,count in leagues]
        result['match_counts_by_league']=[dict(league_id=id,name=name,matches=count) for id,external_id,name,count in leagues]
        stat_query=select(func.count(MatchStatistics.match_id)).join(Match).join(League).where(League.enabled.is_(True))
        result['stats_rows']=await session.scalar(stat_query)
        result['matches_with_corners']=await session.scalar(stat_query.where(MatchStatistics.is_final.is_(True),MatchStatistics.home_corners.is_not(None),MatchStatistics.away_corners.is_not(None)))
        result['matches_with_cards']=await session.scalar(stat_query.where(MatchStatistics.is_final.is_(True),*[getattr(MatchStatistics,f).is_not(None) for f in ['home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards']]))
        result['matches_with_referee']=await session.scalar(select(func.count(Match.id)).join(League).where(League.enabled.is_(True),Match.referee_id.is_not(None)))
        running=await session.scalar(select(ScraperJob).join(League).where(League.enabled.is_(True),ScraperJob.kind.in_(['backfill','stats_backfill']),ScraperJob.status=='running').limit(1))
        result['active_backfill']=dict(id=running.id,kind=running.kind,processed=running.processed_matches,total=running.total_matches,failed=running.failed_matches) if running else None
        result['league_count'] = await session.scalar(select(func.count(League.id)))
        result['enabled_competitions'] = await session.scalar(select(func.count(League.id)).where(League.enabled.is_(True)))
        complete = select(ScraperJob.league_id).where(ScraperJob.kind == 'backfill', ScraperJob.status == 'completed').distinct()
        result['completed_competitions'] = await session.scalar(select(func.count(League.id)).where(League.enabled.is_(True), League.id.in_(complete)))
        result['active_backfill_competition'] = await session.scalar(select(League.name).join(ScraperJob).where(League.enabled.is_(True),ScraperJob.kind.in_(['backfill','stats_backfill']), ScraperJob.status == 'running').limit(1))
        result['queued_backfills'] = await session.scalar(select(func.count(ScraperJob.id)).join(League).where(League.enabled.is_(True), ScraperJob.kind.in_(['backfill','stats_backfill']), ScraperJob.status == 'queued'))
        result['total_matches'] = await session.scalar(select(func.count(Match.id)))
        result['total_odds'] = sum([await session.scalar(select(func.count(model.id))) for model in (Odds1X2, AsianHandicap, AsianTotals)])
        job = await session.scalar(select(ScraperJob).join(League).where(League.enabled.is_(True)).order_by(ScraperJob.id.desc()).limit(1))
        result['latest_job_status'] = job.status if job else None
        result['latest_job'] = {'id': job.id, 'kind': job.kind, 'status': job.status,
                                'processed': job.processed_matches, 'total': job.total_matches,
                                'failed': job.failed_matches} if job else None
        return result
