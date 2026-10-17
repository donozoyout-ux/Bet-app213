"""Read-only source revalidation of exported PostgreSQL fixtures and price samples."""
import asyncio
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from src.scrapers.goaloo.client import GoalooClient
from src.scrapers.goaloo.competitions import Competition, validate_identity
from src.scrapers.goaloo.seasons import season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import fetch_odds, odds_gaps


async def main():
    root = Path('docs')
    observations = json.loads((root/'europe-stage2-db-observations.json').read_text())
    prices = json.loads((root/'europe-stage2-price-observations.json').read_text())
    report = json.loads((root/'europe-quality-live.json').read_text())
    by_league = defaultdict(list)
    for row in observations:
        by_league[row['external_id']].append(row)
    output = {'checked_at':datetime.now(timezone.utc).isoformat(), 'read_only':True, 'leagues':[]}
    async with GoalooClient() as client:
        for record in report['competitions']:
            id = record['external_id']
            if id not in by_league:
                continue
            identity = Competition(**{f.name:record[f.name] for f in fields(Competition) if f.name in record})
            stored = by_league[id]
            entry = dict(external_id=id,name=record['name'],stored_finished=len(stored),seasons={},mismatches=[],odds_samples=[],source_errors=[])
            seasons = set(row['season_name'] for row in stored) | {record['latest_season']}
            latest_rows = {}
            for season in sorted(seasons):
                try:
                    payload = await season_data(client,id,season)
                    validate_identity(identity,payload,season)
                    rows,errors = {},[]
                    for section in discover_rounds(payload):
                        for m in parse_matches(payload,section,errors):
                            rows[m['external_match_id']] = m
                    now = datetime.now(timezone.utc)
                    if season==record['latest_season']:
                        latest_rows=rows
                    entry['seasons'][season] = dict(fixtures=len(rows),parse_errors=len(errors),finished=sum(m['status']=='finished' for m in rows.values()),upcoming_45_days=sum(m['kickoff_at'] is not None and now<=m['kickoff_at']<=now+timedelta(days=45) for m in rows.values()))
                    for row in (r for r in stored if r['season_name']==season):
                        source = rows.get(row['external_match_id'])
                        diffs = ['missing_fixture'] if source is None else [key for key in ('home_external_id','away_external_id','ht_home','ht_away','ft_home','ft_away') if source[key]!=row[key]]
                        if diffs:
                            entry['mismatches'].append(dict(match_id=row['external_match_id'],fields=diffs))
                except Exception as exc:
                    entry['source_errors'].append(dict(season=season,error=type(exc).__name__))
            samples = [stored[i] for i in sorted({0,(len(stored)-1)//2,len(stored)-1})]
            for row in samples:
                try:
                    odds = await fetch_odds(client,row['external_match_id'],True)
                    diffs=[]
                    for p in (p for p in prices if p['external_match_id']==row['external_match_id']):
                        for market in ('1x2','ah','ou'):
                            for key,old in (p[market] or {}).items():
                                if not key.startswith(('opening_','closing_')) or old is None:
                                    continue
                                current=odds.get(p['name'],{}).get(market,{}).get(key)
                                if current is None or abs(float(current)-float(old))>0.00001:
                                    diffs.append(f"{p['name']}/{market}/{key}")
                    entry['odds_samples'].append(dict(match_id=row['external_match_id'],gaps=odds_gaps(odds,True),changed_stored_fields=diffs))
                except Exception as exc:
                    entry['source_errors'].append(dict(match_id=row['external_match_id'],error=type(exc).__name__))
            if id in (6,7):
                now=datetime.now(timezone.utc)
                recent=sorted([m for m in latest_rows.values() if m['status']=='finished' and m['kickoff_at'] and now-timedelta(days=180)<=m['kickoff_at']<=now],key=lambda m:m['kickoff_at'],reverse=True)[:3]
                future=sorted([m for m in latest_rows.values() if m['kickoff_at'] and now<m['kickoff_at']<=now+timedelta(days=7)],key=lambda m:m['kickoff_at'])[:2]
                entry['current_season_samples']=[]
                for m in recent+future:
                    try:
                        final=m['status']=='finished'
                        odds=await fetch_odds(client,m['external_match_id'],final)
                        entry['current_season_samples'].append(dict(match_id=m['external_match_id'],kickoff=m['kickoff_at'].isoformat(),final=final,gaps=odds_gaps(odds,final)))
                    except Exception as exc:
                        entry['source_errors'].append(dict(match_id=m['external_match_id'],error=type(exc).__name__))
            if id==133:
                # The sole incomplete stored Croatia fixture from the SQL audit.
                entry['incomplete_fixture_recheck']=dict(match_id=2399949,gaps=odds_gaps(await fetch_odds(client,2399949,True),True))
            output['leagues'].append(entry)
            (root/'europe-stage2-source-audit.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(dict(id=id,stored=len(stored),mismatches=len(entry['mismatches']),source_errors=len(entry['source_errors']),complete_samples=sum(not s['gaps'] for s in entry['odds_samples']))),flush=True)


if __name__=='__main__':
    asyncio.run(main())
