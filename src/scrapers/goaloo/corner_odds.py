"""Parser for Goaloo corner OU odds (type=14, t=4, ex=1).

Verified on 2026-10-10: endpoint returns ``Data.oddsList`` with each row
containing ``cid``, ``cn``, ``type`` and ``odds`` (same ``f/l/r`` and
``u/g/d`` schema as AH/OU standalone endpoints).

``u`` = over price (Hong Kong), ``g`` = line, ``d`` = under price (Hong Kong).
Prices are converted to decimal by adding 1.
"""
import logging
from .client import SourceError
from .odds import BOOKMAKERS, bookmaker_name, price
from .totals import total_line

log = logging.getLogger('goaloo')


def parse_corner_odds(payload, final=False):
    """Parse corner OU payload into {bookmaker: {market_data}}."""
    try:
        rows = payload['Data']['oddsList']
    except (KeyError, TypeError) as exc:
        raise SourceError('Goaloo corner OU schema changed') from exc
    if not isinstance(rows, list):
        raise SourceError('Goaloo corner oddsList is not a list')
    result = {}
    for row in rows:
        if row.get('type') != 2:
            continue
        cid = int(row.get('cid', -1))
        if cid not in BOOKMAKERS:
            continue
        name = bookmaker_name(row.get('cn', ''))
        if name != BOOKMAKERS[cid]:
            raise SourceError('Corner OU bookmaker ID/name mismatch')
        if name in result:
            raise SourceError('Corner OU duplicate bookmaker')
        odds = row.get('odds') or {}
        if not isinstance(odds, dict):
            raise SourceError('Corner OU odds schema changed')
        data = {'raw': row}
        for stage, source in [('opening', 'f'), ('latest', 'l'), ('closing', 'l')]:
            values = odds.get(source, {}) if stage != 'closing' or final else {}
            if not isinstance(values, dict):
                raise SourceError('Corner OU odds schema changed')
            data[f'{stage}_line'] = total_line(values.get('g'))
            data[f'{stage}_over'] = price(values.get('u'), True)
            data[f'{stage}_under'] = price(values.get('d'), True)
        result[name] = data
    return result


async def fetch_corner_odds(client, external_match_id, half_time=False, final=False):
    """Fetch corner OU odds from verified Goaloo endpoint.

    Args:
        half_time: if True, fetch first-half corner odds (h=1)
        final: if True, treat latest as closing prices
    """
    payload = await client.get('https://www.goaloo.com/ajax/soccerajax',
        {'type': 14, 't': 4, 'id': external_match_id, 'h': 1 if half_time else 0,
         's': -1 if final else 0, 'ex': 1})
    return parse_corner_odds(payload, final)
