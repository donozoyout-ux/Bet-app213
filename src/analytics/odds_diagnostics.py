"""Measured exact-price coverage of the current board, never theoretical coverage."""
from collections import Counter,defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


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
