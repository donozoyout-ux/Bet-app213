import math
import logging
from datetime import datetime, timezone
from .client import SourceError
from .handicap import home_handicap
from .totals import total_line

BOOKMAKERS = {3: 'Crown', 8: 'Bet365', 31: 'Sbobet'}


def bookmaker_name(name):
    normalized = ''.join(c for c in name.lower() if c.isalnum())
    return {'crown': 'Crown', 'bet365': 'Bet365', 'sbobet': 'Sbobet'}.get(normalized)


def price(value, hong_kong=False):
    if value in (None, '', '-', '--'):
        return None
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        return None
    n = n + 1 if hong_kong else n
    return round(n, 6) if n > 1 else None


def normalize_market(raw, market, final=False):
    """f = initial, l = latest PREMATCH, r = in-play; r never becomes closing."""
    result = {'raw': raw}
    for stage, source in [('opening', 'f'), ('latest', 'l'), ('closing', 'l')]:
        values = raw.get(source, {}) if stage != 'closing' or final else {}
        if not isinstance(values, dict):
            raise SourceError('Market odds schema changed')
        if market == '1x2':
            result.update({f'{stage}_home': price(values.get('u')), f'{stage}_draw': price(values.get('g')), f'{stage}_away': price(values.get('d'))})
        else:
            left, right = ('home', 'away') if market == 'ah' else ('over', 'under')
            line_fn = home_handicap if market == 'ah' else total_line
            result.update({f'{stage}_line': line_fn(values.get('g')), f'{stage}_{left}': price(values.get('u'), True), f'{stage}_{right}': price(values.get('d'), True)})
    return result


def parse_odds(payload, final=False):
    try:
        rows = payload['Data']['mixodds']
    except (KeyError, TypeError) as exc:
        raise SourceError('Goaloo mixodds schema changed') from exc
    if not isinstance(rows, list):
        raise SourceError('Goaloo mixodds is not a list')
    result = {}
    for row in rows:
        cid = int(row['cid'])
        if cid not in BOOKMAKERS:
            continue
        name = bookmaker_name(row['cn'])
        if name != BOOKMAKERS[cid] or name in result:
            raise SourceError('Bookmaker ID/name mismatch or duplicate')
        result[name] = {}
        for key, market in [('euro', '1x2'), ('ah', 'ah'), ('ou', 'ou')]:
            try:
                result[name][market] = normalize_market(row.get(key) or {}, market, final)
            except (ValueError, TypeError, AttributeError):
                logging.getLogger('goaloo').warning('[ODDS] malformed market bookmaker=%s market=%s',name,market)
                result[name][market] = normalize_market({}, market, final)
    return result


def complete_odds(odds, final=True):
    stages = ['opening', 'closing'] if final else ['opening', 'latest']
    for name in BOOKMAKERS.values():
        for market in ['1x2', 'ah', 'ou']:
            row = odds.get(name, {}).get(market, {})
            keys = ['home', 'draw', 'away'] if market == '1x2' else (['line', 'home', 'away'] if market == 'ah' else ['line', 'over', 'under'])
            if any(row.get(f'{stage}_{key}') is None for stage in stages for key in keys):
                return False
    return True


async def fetch_odds(client, external_match_id, final=False):
    payload = await client.get('https://www.goaloo.com/ajax/soccerajax',
        {'type': 14, 't': 1, 'id': external_match_id, 'h': 0, 's': -1 if final else 0})
    return parse_odds(payload, final)


async def fetch_history(client, external_match_id, bookmaker_id):
    payload = await client.get('https://www.goaloo.com/ajax/soccerajax',
        {'type': 14, 't': 20, 'id': external_match_id, 'cid': bookmaker_id, 'h': 0, 'r1': 0, 'r2': 0, 'r3': 0})
    data = payload.get('Data')
    if not isinstance(data, dict):
        raise SourceError('Goaloo history schema changed')
    return [(market, datetime.fromtimestamp(row['mt'], timezone.utc), row)
            for key, market in [('op', '1x2'), ('ah', 'ah'), ('ou', 'ou')]
            for row in data.get(key, []) if row.get('mt')]
