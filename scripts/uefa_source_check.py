"""Read-only Goaloo archive verification, exact IDs only, no database writes."""
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.seasons import discover_seasons, season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.competitions import Competition, validate_identity, verified_snapshot
from src.analytics.uefa_coverage import stamp


async def check(source):
    now=stamp(source['as_of']); leagues={l['id']:l for l in source['leagues']}
    fixtures=[r for r in source['matches'] if leagues[r['league_id']]['external_id']==113
        and r['status']=='scheduled' and now<=stamp(r['kickoff_at'])<now+__import__('datetime').timedelta(days=30)]
    internal={r[k] for r in fixtures for k in ('home_team_id','away_team_id')}
    teams={t['external_id']:t for t in source['teams'] if t['id'] in internal}
    stored={r['external_match_id'] for r in source['matches']}; matches={}; errors=[]; requests=0
    # The verified catalog is the boundary; no guessed URLs or fuzzy team names.
    async with GoalooClient() as client:
        for l in verified_snapshot():
            if l['schedule_format']!='league' or l['country'] not in {
                'England','Spain','Italy','Germany','France','Turkey','Netherlands','Portugal',
                'Belgium','Scotland','Austria','Switzerland','Greece'}: continue
            try:
                requests+=1
                seasons=await discover_seasons(client,l['external_id'],2024,overlap=True)
                for season in [s for s in seasons if 2023<=int(s[:4])<=now.year][-4:]:
                    requests+=1
                    payload=await season_data(client,l['external_id'],season)
                    validate_identity(Competition(**l),payload,season)
                    for round_no in discover_rounds(payload):
                        rejected=[]
                        for row in parse_matches(payload,round_no,rejected):
                            if (row['external_league_id']!=l['external_id'] or row['status']!='finished'
                                or row['kickoff_at'] is None or not datetime(2024,1,1,tzinfo=timezone.utc)<=row['kickoff_at']<=now
                                or row['ft_home'] is None or row['ft_away'] is None): continue
                            if not {row['home_external_id'],row['away_external_id']}&teams.keys(): continue
                            matches[row['external_match_id']]=dict(goaloo_match_id=row['external_match_id'],league_id=l['external_id'],
                                season=season,kickoff_at=row['kickoff_at'].isoformat(),home=row['home_external_id'],away=row['away_external_id'],
                                ft_home=row['ft_home'],ft_away=row['ft_away'],already_stored=row['external_match_id'] in stored)
                        if rejected: errors.append(dict(league_id=l['external_id'],season=season,round=round_no,rejected_rows=len(rejected)))
            except (SourceError, ValueError) as exc:
                errors.append(dict(league_id=l['external_id'],error=type(exc).__name__))
    return dict(checked_at=datetime.now(timezone.utc).isoformat(),requests=requests,errors=errors,
        teams=[dict(goaloo_team_id=t,name=v['name'],source_results=sum(t in (r['home'],r['away']) for r in matches.values()),
            missing_stored=sum(t in (r['home'],r['away']) and not r['already_stored'] for r in matches.values())) for t,v in sorted(teams.items())],
        matches=sorted(matches.values(),key=lambda r:(r['kickoff_at'],r['goaloo_match_id'])),database_writes=0)


if __name__=='__main__':
    import sys
    source=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    Path(sys.argv[2]).write_text(json.dumps(asyncio.run(check(source)),ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
