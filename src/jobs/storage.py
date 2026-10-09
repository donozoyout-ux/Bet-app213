import logging
import hashlib
from sqlalchemy import select
from src.models import Team, Season, Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, OddsSnapshot, utcnow
from src.models import MatchStatistics, MatchEvent, Referee
from sqlalchemy import delete
from src.scrapers.goaloo.odds import complete_odds

log = logging.getLogger('scraper')


async def get_or_create(session, model, keys, defaults=None):
    row = await session.scalar(select(model).filter_by(**keys))
    if row is None:
        row = model(**keys, **(defaults or {}))
        session.add(row)
        await session.flush()
    return row


async def store_match(session, league_id, season_name, data):
    season = await get_or_create(session, Season, {'league_id': league_id, 'season_name': season_name})
    home = await get_or_create(session, Team, {'external_id': data['home_external_id']}, {'name': data['home_name']})
    away = await get_or_create(session, Team, {'external_id': data['away_external_id']}, {'name': data['away_name']})
    home.name, away.name = data['home_name'], data['away_name']
    keys = ['round', 'kickoff_at', 'status', 'ht_home', 'ht_away', 'ft_home', 'ft_away', 'raw']
    values = {k: data[k] for k in keys}
    values.update({k: data[k] for k in ['round_label', 'stage_key'] if k in data})
    values.update(league_id=league_id, season_id=season.id, home_team_id=home.id, away_team_id=away.id)
    match = await get_or_create(session, Match, {'external_match_id': data['external_match_id']}, values)
    for k, v in values.items():
        if v is not None or k not in {'ht_home','ht_away','ft_home','ft_away'}:
            setattr(match, k, v)
    return match


async def store_odds(session, match, odds, final):
    for name, markets in odds.items():
        bookmaker = await session.scalar(select(Bookmaker).where(Bookmaker.name == name))
        for key, model in [('1x2', Odds1X2), ('ah', AsianHandicap), ('ou', AsianTotals)]:
            data = markets.get(key)
            if not data:continue
            row = await get_or_create(session, model, {'match_id': match.id, 'bookmaker_id': bookmaker.id}, {'raw': data['raw']})
            for field, value in data.items():
                if field == 'raw' or value is not None:
                    # Missing source fields never erase previously verified opening/closing values.
                    setattr(row, field, value)
            log.info('[ODDS] bookmaker=%s market=%s match_id=%s saved', name, key, match.external_match_id)
    match.last_scraped_at = utcnow()
    match.odds_complete = final and complete_odds(odds, final=True)


async def store_history(session, match_id, bookmaker_id, history):
    for market, timestamp, raw in history:
        await get_or_create(session, OddsSnapshot,
            {'match_id': match_id, 'bookmaker_id': bookmaker_id, 'market': market, 'source_timestamp': timestamp}, {'raw': raw})


async def store_statistics(session,match,data):
    from src.scrapers.goaloo.statistics import STAT_FIELDS
    if data['raw'].get('match_id')!=match.external_match_id:
        raise ValueError('Statistics snapshot does not match stored fixture')
    row=await session.get(MatchStatistics,match.id)
    if row is None:
        row=MatchStatistics(match_id=match.id);session.add(row)
    was_final=bool(row.is_final)
    if was_final and not data['is_final']:return row
    for key in STAT_FIELDS:
        observed=data['statistics'].get(key)
        if observed is not None or data['is_final'] and not was_final:setattr(row,key,observed)
    row.raw=data['raw'];row.is_final=bool(row.is_final or data['is_final'])
    row.collection_status='available' if any(getattr(row,key) is not None for key in STAT_FIELDS) else 'unavailable'
    row.updated_at=utcnow()
    referee=data.get('referee')
    if referee and referee.get('name'):
        # Anonymous same-name referees are kept within a competition, not guessed
        # to be the same person across leagues. The current parser emits none.
        identity=referee.get('external_id') or f"{match.league_id}:{referee['name'].strip().casefold()}"
        key=hashlib.sha256(('goaloo:'+str(identity)).encode()).hexdigest()
        ref=await get_or_create(session,Referee,{'source_key':key},{'name':referee['name'],'external_id':str(referee['external_id']) if referee.get('external_id') is not None else None})
        match.referee_id,match.referee_observed_at=ref.id,utcnow()
    if data['events_available']:
        existing={event.source_key:event for event in (await session.scalars(select(MatchEvent).where(MatchEvent.match_id==match.id))).all()}
        keys=[]
        for item in data['events']:
            keys.append(item['source_key']);event=existing.get(item['source_key'])
            if event is None:event=MatchEvent(match_id=match.id,**item);session.add(event)
            else:
                for key,value in item.items():setattr(event,key,value)
            event.updated_at=utcnow()
        query=delete(MatchEvent).where(MatchEvent.match_id==match.id)
        if keys:query=query.where(MatchEvent.source_key.not_in(keys))
        await session.execute(query)
    await session.flush()
    return row
