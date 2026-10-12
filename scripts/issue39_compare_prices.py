"""Compare master/current matching on the SAME read-only PostgreSQL export."""
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analytics.odds_matching import match_odds_for_candidate


def compare(snapshot):
    baseline={}
    exec(subprocess.check_output(['git','show','5224a01:src/analytics/odds_matching.py'],text=True,encoding='utf-8'),baseline)
    now=datetime.fromisoformat(snapshot['captured_at'].replace('Z','+00:00'))
    quotes=defaultdict(lambda:defaultdict(list))
    for entry in snapshot['quotes']:
        row=entry['quote']
        row['updated_at']=datetime.fromisoformat(row['updated_at'].replace('Z','+00:00')) if row.get('updated_at') else None
        quotes[entry['match_id']][entry['family']].append((SimpleNamespace(**row),entry['bookmaker']))
    results={}
    for name,matcher in [('before',baseline['match_odds_for_candidate']),('after',match_odds_for_candidate)]:
        markets=defaultdict(Counter);counts=Counter()
        daily_matches=set()
        for item in snapshot['candidates']:
            rec=item['recommendation'];rows=quotes[item['match_id']]
            result=matcher(rec,item['match_id'],item['status'],odds_1x2_rows=rows['result'],
                asian_totals_rows=rows['goals'],asian_handicap_rows=rows['asian_handicap'],
                period_odds_rows=[r for key,values in rows.items() if key not in ('result','goals','asian_handicap') for r in values],cutoff=now)
            bucket=markets[rec['market']];bucket['selections']+=1
            bucket['priced']+=bool(result.get('has_odds'))
            bucket['fresh']+=bool(result.get('coupon_eligible'))
            bucket['unknown_observation']+=result.get('freshness')=='unknown'
            if result.get('reason'):bucket[result['reason']]+=1
            qualifies=rec.get('probability',0)>=.70 and rec.get('reliability',0)>=.80 and rec.get('score',0)>=.80
            counts['quality_and_fresh']+=qualifies and bool(result.get('coupon_eligible'))
            kickoff=datetime.fromisoformat(item['kickoff_at'].replace('Z','+00:00'))
            if qualifies and result.get('coupon_eligible') and kickoff>now and kickoff.astimezone(ZoneInfo('Europe/Istanbul')).date()==now.astimezone(ZoneInfo('Europe/Istanbul')).date():
                daily_matches.add(item['match_id'])
            counts['priced']+=bool(result.get('has_odds'))
            counts['fresh']+=bool(result.get('coupon_eligible'))
            counts['selections']+=1
        counts['daily_independent_eligible_matches']=len(daily_matches)
        counts['daily_coupon_matches']=min(4,len(daily_matches))
        results[name]={'totals':dict(counts),'markets':{k:dict(v) for k,v in sorted(markets.items())}}
    return {'basis':'same_read_only_production_postgresql_snapshot_no_deployment',
            'captured_at':now.isoformat(),'baseline':'5224a01',**results}


if __name__=='__main__':
    snapshot=json.loads(Path(sys.argv[1]).read_text())[0]
    Path(sys.argv[2]).write_text(json.dumps(compare(snapshot),indent=2))
