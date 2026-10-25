"""Recover omitted HT fields from already identity-verified stored live HTML.

Called only by the settlement worker, never by an HTTP read. No network calls,
retrospective picks, ledger corrections, or replacement of known score fields.
"""
from sqlalchemy import select,func,or_
from src.models import Match,PredictionSnapshot,PredictionSettlement
from src.scrapers.goaloo.live import parse_scoreboard
from src.scrapers.goaloo.client import SourceError
from src.jobs.worker import aware


async def recover_halftime(session,*,after_match_id,now,limit=100):
    if not 1<=limit<=100 or after_match_id<0: raise ValueError('Invalid recovery bounds')
    recorded=select(func.count(PredictionSettlement.id)).where(
        PredictionSettlement.match_id==Match.id,PredictionSettlement.revision==1).correlate(Match).scalar_subquery()
    matches=await session.scalars(select(Match).join(PredictionSnapshot,PredictionSnapshot.match_id==Match.id)
        .where(Match.id>after_match_id,Match.status=='finished',
            PredictionSnapshot.captured_at<Match.kickoff_at,
            recorded<func.json_array_length(PredictionSnapshot.picks),
            or_(Match.ht_home.is_(None),Match.ht_away.is_(None)))
        .order_by(Match.id).limit(limit).with_for_update(of=Match,skip_locked=True))
    recovered=0
    for match in matches:
        raw=match.raw if isinstance(match.raw,dict) else {}
        live=raw.get('live')
        if not isinstance(live,dict) or not match.status_observed_at or aware(match.status_observed_at)>now:continue
        try: observation=parse_scoreboard({'ErrCode':0,'Data':{'state':live.get('state'),'html':live.get('score_html')}})
        except SourceError:continue
        if observation['status']!='finished' or (observation['ft_home'],observation['ft_away'])!=(match.ft_home,match.ft_away):continue
        pair=(observation['ht_home'],observation['ht_away'])
        if None in pair:continue
        if any(getattr(match,k) is not None and getattr(match,k)!=observation[k] for k in ('ht_home','ht_away')):continue
        match.ht_home,match.ht_away=pair
        match.raw={**raw,'halftime_recovery':dict(version=1,source='live.score_html',
            observed_at=aware(match.status_observed_at).isoformat(),recovered_at=now.isoformat())}
        recovered+=1
    await session.flush()
    return recovered
