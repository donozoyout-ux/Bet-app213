"""Exact-ID domestic context for UEFA fixtures, separate from the cup baseline.

Pooling raw goal rates across competitions without opponent-strength calibration
would inflate confidence. Expose observed domestic form for analysis instead of
silently substituting it for the competition-specific probability sample.
"""
from datetime import timedelta
from sqlalchemy import select,or_
from sqlalchemy.orm import aliased
from src.models import Match,League,Team
from src.jobs.worker import aware
from .predictions import Result,team_stats

DOMESTIC_LEAGUES=(36,31,34,8,11,30,16,23)
UEFA=(103,113,2187)


async def domestic_context(session,match,cutoff):
    cache=session.info.setdefault('domestic_context',{})
    output=dict(basis='observed_domestic_form_separate_from_cup_model',cutoff=cutoff,
                affects_model_probability=False,confidence_uplift=False,identity='stored_goaloo_team_id')
    for side,team in [('home',match.home_team_id),('away',match.away_team_id)]:
        key=(team,cutoff)
        if key not in cache:
            home,away=aliased(Team),aliased(Team)
            rows=(await session.execute(select(Match.id,Match.home_team_id,Match.away_team_id,Match.ft_home,Match.ft_away,
                Match.kickoff_at,home.name,away.name,Match.season_id,League.id,League.external_id,League.name)
                .join(League,League.id==Match.league_id).join(home,home.id==Match.home_team_id).join(away,away.id==Match.away_team_id)
                .where(League.enabled.is_(True),League.external_id.in_(DOMESTIC_LEAGUES),League.schedule_format=='league',
                    Match.id!=match.id,or_(Match.home_team_id==team,Match.away_team_id==team),Match.status=='finished',
                    Match.ft_home>=0,Match.ft_away>=0,Match.kickoff_at<=cutoff-timedelta(hours=3),
                    Match.kickoff_at>=cutoff-timedelta(days=3*366),Match.created_at<=cutoff,Match.updated_at<=cutoff,
                    or_(Match.last_scraped_at.is_(None),Match.last_scraped_at<=cutoff))
                .order_by(Match.kickoff_at.desc(),Match.id.desc()).limit(10))).all()
            history=[Result(*r[:5],aware(r[5]),r[6],r[7],r[8]) for r in rows]
            competitions={r[9]:dict(league_id=r[9],external_id=r[10],name=r[11]) for r in rows}
            cache[key]=dict(form=team_stats(history,team),available=len(history)>=5,
                source_competitions=list(competitions.values()),match_ids=[r.id for r in history])
        output[side]=cache[key]
    return output
