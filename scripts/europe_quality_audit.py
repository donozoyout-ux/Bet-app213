import asyncio,json
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from src.scrapers.goaloo.client import GoalooClient
from src.scrapers.goaloo.competitions import *
from src.scrapers.goaloo.odds import fetch_odds,odds_gaps
TARGETS=[('England','ENG LCH'),('Belgium','BEL D1'),('Scotland','SCO PR'),('Austria','AUT D1'),('Switzerland','SUI SL'),('Denmark','DEN SASL'),('Norway','NOR D1'),('Sweden','SWE D1'),('Poland','POL PR'),('Greece','GRE D1'),('Czech Republic','CZE D1'),('Croatia','CRO D1')]
async def main():
 out=Path('docs/europe-quality-live.json')
 report={'checked_at':datetime.now(timezone.utc).isoformat(),'from_year':2024,'production_activation':False,'competitions':[]}
 if out.exists():
  report=json.loads(out.read_text(encoding='utf-8'))
 async with GoalooClient() as c:
  catalog=parse_catalog(await c.get_text(CATALOG_URL))
  for offset in range(0,len(TARGETS),2):
   for country,short in TARGETS[offset:offset+2]:
    if any(x['country']==country and x['catalog_name']==short and x['verified'] for x in report['competitions']):continue
    report['competitions']=[x for x in report['competitions'] if (x['country'],x['catalog_name'])!=(country,short)]
    r=next(x for x in catalog if x['country']==country and x['catalog_name']==short)
    item=await verify_competition(c,Competition(**r,competition_type='club',priority=100+offset),include_overlapping=True)
    row=asdict(item); samples=[]; errors=0
    if item.verified:
     for season,count in item.season_counts.items():
      if not count:continue
      payload=await season_data(c,item.external_id,season)
      matches={m['external_match_id']:m for section in discover_rounds(payload) for m in parse_matches(payload,section,[]) if m['kickoff_at'] and m['kickoff_at'].year>=2024 and m['status']=='finished' and m['kickoff_at']<=datetime.now(timezone.utc)}
      ordered=sorted(matches.values(),key=lambda m:m['kickoff_at'])
      # Deterministic early/middle/late finished samples in every season.
      for index in sorted({0,len(ordered)//2,len(ordered)-1}) if ordered else []:
       m=ordered[index]
       try:
        odds=await fetch_odds(c,m['external_match_id'],True)
        gaps=odds_gaps(odds,True)
        samples.append({'season':season,'match_id':m['external_match_id'],'kickoff':m['kickoff_at'].isoformat(),'gaps':gaps,'books':list(odds),'error':None})
       except Exception as exc:
        errors+=1;samples.append({'season':season,'match_id':m['external_match_id'],'error':type(exc).__name__})
    row['odds_samples']=samples;row['sample_error_rate']=errors/len(samples) if samples else None
    row['complete_sample_count']=sum(not x.get('error') and not x['gaps'] for x in samples)
    row['sample_count']=len(samples)
    row['approved']=bool(item.verified and len(samples)>=6 and row['complete_sample_count']/len(samples)>=.8 and not errors)
    row['selection_basis']='>=6 finished samples across seasons; >=80% complete Crown/Bet365/Sbobet FT 1X2/AH/OU opening and closing; zero sample request errors'
    report['competitions'].append(row)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:row[k] for k in ('external_id','name','verified','historical_matches','sample_count','complete_sample_count','approved','error')}),flush=True)
   print('batch saved',offset//2+1,flush=True)
async def coverage():
 out=Path('docs/europe-quality-live.json')
 report=json.loads(out.read_text(encoding='utf-8'))
 async with GoalooClient() as c:
  for row in report['competitions']:
   if row.get('fixture_coverage'):continue
   seasons={}; all_matches={}; error_rows=0; request_errors=0
   for season in row.get('seasons') or []:
    try:
     payload=await season_data(c,row['external_id'],season)
     validate_identity(Competition(**{k:row[k] for k in ('external_id','catalog_name','country','competition_type','schedule_format','priority')}),payload,season)
     matches={}; errors=[]
     for section in discover_rounds(payload):
      for match in parse_matches(payload,section,errors):
       if match['kickoff_at'] and match['kickoff_at'].year>=2024:matches[match['external_match_id']]=match
     error_rows+=len(errors)
     finished=[m for m in matches.values() if m['status']=='finished' and m['kickoff_at']<=datetime.now(timezone.utc)]
     seasons[season]={'dated_fixtures':len(matches),'finished_matches':len(finished),'ft_and_ht':sum(all(m[k] is not None for k in ('ft_home','ft_away','ht_home','ht_away')) for m in finished),'unparsed_rows':len(errors),'error_examples':errors[:2]}
     all_matches.update(matches)
    except Exception as exc:
     request_errors+=1;seasons[season]={'source_error':type(exc).__name__}
   row['fixture_coverage']=seasons
   row['finished_matches']=sum(m['status']=='finished' and m['kickoff_at']<=datetime.now(timezone.utc) for m in all_matches.values())
   row['dated_fixtures']=len(all_matches)
   row['schedule_parse_error_count']=error_rows
   row['schedule_request_error_rate']=request_errors/len(row['seasons']) if row.get('seasons') else None
   if error_rows or request_errors:row['approved']=False
   out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
   print(row['external_id'],row['finished_matches'],error_rows,flush=True)
import sys
asyncio.run(coverage() if '--counts-only' in sys.argv else main())
