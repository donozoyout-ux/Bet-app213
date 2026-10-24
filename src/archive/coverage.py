"""Aggregate only: no fixture identities, prices, scores, names or raw content."""
from sqlalchemy import select, func, case, and_, text
from src.models import League, Match, MatchStatistics, ScraperJob, JobItem


async def collect_coverage(connection, day, quota_bytes, previous=None, expected_leagues=()):
    # Preaggregate one-to-many queues, avoiding duplicate fixture counts.
    pending=(select(ScraperJob.league_id,
        func.count().label('backlog')).select_from(JobItem)
        .join(ScraperJob, ScraperJob.id==JobItem.job_id)
        .where(JobItem.status.in_(['queued','running','failed']))
        .group_by(ScraperJob.league_id).subquery())
    finished=Match.status=='finished'
    complete=and_(finished, MatchStatistics.is_final.is_(True),
        MatchStatistics.collection_status=='available',
        *(getattr(MatchStatistics,key).is_not(None) for key in
          ('home_corners','away_corners','home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards')))
    query=(select(League.external_id.label('league_code'),
        func.count(Match.id).label('fixtures'),
        func.max(Match.kickoff_at).label('newest'),
        func.sum(case((Match.odds_complete.is_(True),1),else_=0)).label('odds_complete'),
        func.sum(case((finished,1),else_=0)).label('finished'),
        func.sum(case((complete,1),else_=0)).label('statistics_complete'),
        func.coalesce(pending.c.backlog,0).label('backlog'))
        .select_from(League).outerjoin(Match,Match.league_id==League.id)
        .outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id)
        .outerjoin(pending,pending.c.league_id==League.id)
        .where(League.enabled.is_(True))
        .group_by(League.id,League.external_id,pending.c.backlog)
        .order_by(League.external_id))
    rows=(await connection.execute(query)).mappings().all()
    leagues=[]
    for row in rows:
        values={k:int(row[k] or 0) for k in ('league_code','fixtures','odds_complete','finished','statistics_complete','backlog')}
        values['newest_fixture_date']=row['newest'].date().isoformat() if row['newest'] else None
        values['missing_odds']=values['fixtures']-values['odds_complete']
        values['missing_statistics']=values['finished']-values['statistics_complete']
        leagues.append(values)
    size=int(await connection.scalar(text('SELECT pg_database_size(current_database())')))
    result=dict(day=day.isoformat(),leagues=leagues,database_bytes=size,
                quota_bytes=quota_bytes,previous_database_bytes=None,growth_bytes=None,
                previous_day=None,missing_run_days=0,expected_league_codes=list(expected_leagues),
                missing_league_codes=sorted(set(expected_leagues)-{r['league_code'] for r in leagues}))
    if previous:
        from datetime import date
        result.update(previous_day=previous['day'],previous_database_bytes=previous['database_bytes'],
            growth_bytes=size-previous['database_bytes'],
            missing_run_days=max(0,(day-date.fromisoformat(previous['day'])).days-1))
    return result
