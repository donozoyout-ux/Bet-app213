"""Measured exact-price coverage of the current board, never theoretical coverage."""
from collections import Counter,defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


async def source_report(session, now):
    """Aggregate actual persisted latest fields by exact source identity and line."""
    from sqlalchemy import select, func, case, and_
    from src.models import Odds1X2, AsianTotals, AsianHandicap, MarketOdds, Bookmaker
    rows = []
    for model, family in [(Odds1X2, 'result'), (AsianTotals, 'goals'),
                          (AsianHandicap, 'asian_handicap'), (MarketOdds, None)]:
        dimensions = [Bookmaker.name]
        if family is None:
            dimensions += [model.market, model.period]
        if model is not Odds1X2:
            dimensions += [model.latest_line]
        fields = ('home', 'draw', 'away') if model is Odds1X2 else ('home', 'away') if model is AsianHandicap else ('over', 'under')
        aggregates = [func.count(model.id)]
        for side in fields:
            value = getattr(model, 'latest_' + side)
            stamp = model.raw['_field_observed_at']['latest_' + side].as_string()
            valid = and_(value > 1, value.is_not(None))
            fresh = and_(valid, stamp >= (now - timedelta(minutes=15)).isoformat(), stamp <= now.isoformat())
            aggregates += [func.count(case((valid, 1))), func.count(case((and_(valid, stamp.is_not(None)), 1))),
                           func.count(case((fresh, 1)))]
        query = select(*dimensions, *aggregates).join(Bookmaker).group_by(*dimensions)
        for record in (await session.execute(query)).all():
            values = list(record)
            bookmaker = values.pop(0)
            market, period = (values.pop(0), values.pop(0)) if family is None else (family, 'full_time')
            line = values.pop(0) if model is not Odds1X2 else None
            count = values.pop(0)
            prices = {}
            for side in fields:
                prices[side] = dict(zip(('priced', 'observed', 'fresh'), values[:3]))
                del values[:3]
            rows.append(dict(market=market, period=period, bookmaker=bookmaker, line=line,
                             source_rows=count, latest_fields=prices))
    return rows


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
    return dict(checked_at=now,markets={key:dict(value) for key,value in sorted(families.items())},daily=dict(daily),basis='current_database_exact_quotes',max_age_seconds=900)
