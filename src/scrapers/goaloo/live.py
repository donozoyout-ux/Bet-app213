"""Verified match-page identity and the score endpoint used by that same page."""
import re
from .client import SourceError
from .results import STATUSES
from .statistics import Page


def identity(source, match_id, league_id, home_id, away_id):
    block = re.search(r'var\s+_matchInfo\s*=\s*\{(.*?)\}', source, re.S)
    if not block:
        raise SourceError('Live match identity missing')
    values = dict((key, int(value)) for key,value in re.findall(
        r"\b(sId|sclassId|hId|gId|state)\s*:\s*parseInt\(['\"](-?\d+)['\"]\)", block[1]))
    if any(values.get(key)!=expected for key,expected in
           [('sId',match_id),('sclassId',league_id),('hId',home_id),('gId',away_id)]):
        raise SourceError('Live match identity mismatch')
    return values


def parse_scoreboard(payload):
    data = payload.get('Data')
    if payload.get('ErrCode') != 0 or not isinstance(data,dict):
        raise SourceError('Live score response unavailable')
    try:
        state = int(data['state'])
    except (ValueError,TypeError,KeyError) as exc:
        raise SourceError('Live state missing') from exc
    if state not in STATUSES:
        raise SourceError('Unrecognized live state')
    if not isinstance(data.get('html'), str):
        raise SourceError('Live scoreboard missing')
    page = Page(data['html'])
    # Scope to the primary scoreboard, never event/penalty/other-match scores.
    half = next((n for n in page.root.walk() if {'half','end'} & set(n.attrs.get('class','').split())), None)
    scores = [n.text() for n in half.walk() if 'score' in n.attrs.get('class','').split()] if half else []
    if len(scores)!=2 and state in {-1,1,2,3,4,5}:
        raise SourceError('Live scoreboard schema changed')
    score = lambda s: int(s) if re.fullmatch(r'\d{1,2}',s) else None
    home,away = map(score,scores) if len(scores)==2 else (None,None)
    if state==0:home=away=None
    if state in {-1,1,2,3,4,5} and (home is None or away is None):
        raise SourceError('Live score missing')
    clock = page.by_id('matchTime')
    raw_minute = clock.text() if clock else ''
    # Source phase-start times and scheduled kickoff are NOT a reported minute.
    minute = re.fullmatch(r"(\d{1,3}(?:\+\d{1,2})?)\s*['′]?",raw_minute)
    return dict(status=STATUSES[state],ft_home=home,ft_away=away,
                live_minute=minute[1] if minute and state>0 else None,
                raw={'state':state,'score_html':data['html'],'reported_minute':raw_minute})


async def fetch_live(client, match_id, league_id, home_id, away_id):
    source = await client.get_text(f'https://www.goaloo.com/match/live-{match_id}')
    identity(source,match_id,league_id,home_id,away_id)
    payload = await client.get('https://www.goaloo.com/Ajax/SoccerAjax/',params={'type':11,'id':match_id})
    return parse_scoreboard(payload), source
