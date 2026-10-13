"""Measured exact-price coverage of the current board, never theoretical coverage."""
from collections import Counter,defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


async def source_report(session, now, match_ids=None):
    """Projected field provenance, bounded to the requested fixtures when supplied.

    The HTTP endpoint supplies upcoming IDs. The explicit all-records mode remains
    available for offline audits, never as the default live endpoint workload.
    """
    from math import isfinite
    from sqlalchemy import select
    from sqlalchemy.orm import defer
    from src.models import Odds1X2, AsianTotals, AsianHandicap, MarketOdds, Bookmaker
    from src.analytics.odds_matching import price_observed_at, price_matches_line
    buckets = {}
    for model, family in [(Odds1X2, 'result'), (AsianTotals, 'goals'),
                          (AsianHandicap, 'asian_handicap'), (MarketOdds, None)]:
        query = select(model, Bookmaker.name, model.raw['_field_observed_at'], model.raw['_field_lines']).options(defer(model.raw)).join(Bookmaker)
        if match_ids is not None:
            if not match_ids:
                continue
            query = query.where(model.match_id.in_(match_ids))
        fields = ('home', 'draw', 'away') if model is Odds1X2 else ('home', 'away') if model is AsianHandicap else ('over', 'under')
        for row, bookmaker, stamps, lines in await session.execute(query):
            row._matching_meta = {'_field_observed_at': stamps, '_field_lines': lines}
            market, period = (row.market, row.period) if family is None else (family, 'full_time')
            line = row.latest_line if model is not Odds1X2 else None
            key = market, period, bookmaker, line
            if key not in buckets:
                buckets[key] = dict(market=market, period=period, bookmaker=bookmaker, line=line,
                    source_rows=0, latest_fields={side: dict(priced=0, observed=0, fresh=0) for side in fields},
                    last_observed_at=None)
            bucket = buckets[key]
            bucket['source_rows'] += 1
            for side in fields:
                price = getattr(row, 'latest_' + side)
                valid = isinstance(price, (int, float)) and isfinite(price) and price > 1
                if not valid:
                    continue
                values = bucket['latest_fields'][side]
                values['priced'] += 1
                stamp = price_observed_at(row, 'latest_' + side)
                if stamp is None:
                    continue
                values['observed'] += 1
                if stamp <= now and (bucket['last_observed_at'] is None or stamp > bucket['last_observed_at']):
                    bucket['last_observed_at'] = stamp
                try:
                    exact_line = model is Odds1X2 or price_matches_line(row, 'latest', side, line)
                except (TypeError, ValueError):
                    exact_line = False
                if exact_line and 0 <= (now-stamp).total_seconds() <= 900:
                    values['fresh'] += 1
    return sorted(buckets.values(), key=lambda b: (b['market'], b['period'], b['bookmaker'], str(b['line'])))


def report(records,now):
    families=defaultdict(Counter);daily=Counter();today=now.astimezone(ZoneInfo('Europe/Istanbul')).date()
    for record in records:
        kickoff=record['match']['kickoff_at']
        if isinstance(kickoff,str):kickoff=datetime.fromisoformat(kickoff.replace('Z','+00:00'))
        for rec in record['recommendations']:
            vo=rec.get('verified_odds') or {};bucket=families[rec['market']]
            bucket['selections']+=1
            if vo.get('has_odds'):bucket['matched_prices']+=1
            if vo.get('coupon_eligible'):bucket['fresh_prices']+=1
            else:bucket[vo.get('reason') or 'no_verified_odds']+=1
            if kickoff.astimezone(ZoneInfo('Europe/Istanbul')).date()!=today:continue
            daily['selections']+=1
            if rec.get('probability',0)<.7 or rec.get('reliability',0)<.8 or rec.get('score',0)<.8:daily['quality_rejected']+=1
            elif not vo.get('coupon_eligible'):daily['price_rejected']+=1
            else:daily['qualified_priced']+=1
    reason_keys=('no_source_rows','line_mismatch','stale','unsupported_bookmaker','missing_stage_price','source_market_unavailable','unknown_observation','observed_after_cutoff')
    aliases={'odds_stale':'stale','odds_unknown':'unknown_observation'}
    rejections={market:{reason:sum(count for key,count in bucket.items() if aliases.get(key,key)==reason) for reason in reason_keys} for market,bucket in sorted(families.items())}
    stamps=[book.get('observed_at') for record in records for rec in record['recommendations'] for book in (rec.get('verified_odds') or {}).get('bookmakers',{}).values() if book.get('observed_at')]
    observed=[]
    for stamp in stamps:
        try:
            parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if parsed.tzinfo is not None and parsed<=now:observed.append(parsed)
        except (AttributeError,TypeError,ValueError):pass
    last_price=max(observed,default=None)
    return dict(last_price_observed_at=last_price,rejections=rejections,checked_at=now,markets={key:dict(value) for key,value in sorted(families.items())},daily=dict(daily),basis='current_database_exact_quotes',max_age_seconds=900)
