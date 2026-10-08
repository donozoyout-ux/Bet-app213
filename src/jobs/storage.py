import logging
from sqlalchemy import select
from src.models import Team, Season, Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, OddsSnapshot, utcnow
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
        setattr(match, k, v)
    return match


async def store_odds(session, match, odds, final):
    for name, markets in odds.items():
        bookmaker = await session.scalar(select(Bookmaker).where(Bookmaker.name == name))
        for key, model in [('1x2', Odds1X2), ('ah', AsianHandicap), ('ou', AsianTotals)]:
            data = markets[key]
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
