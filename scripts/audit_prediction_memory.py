"""Read-only snapshot audit. Does not initialize schema or contact any scraper.

DATABASE_URL=... python -m scripts.audit_prediction_memory
python -m scripts.audit_prediction_memory --input docs/learning-phase-1-audit-input.json
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import json
from types import SimpleNamespace
from sqlalchemy import select, text
from src.analytics.performance import settle
from src.models import PredictionSnapshot, Match, MatchStatistics


def summarize(rows):
    totals=Counter(); markets=defaultdict(Counter)
    for row in rows:
        for pick in row['picks']:
            state, _ = settle(SimpleNamespace(**row), SimpleNamespace(**row), pick)
            totals[state]+=1
            markets[pick['market']][state]+=1
    finished=[r for r in rows if r['status']=='finished']
    coverage={'finished_snapshot_matches':len(finished),
        'missing_final_statistics':sum(not r['is_final'] for r in finished)}
    for label,fields in {'ft':('ft_home','ft_away'),'ht':('ht_home','ht_away'),
        'corners':('home_corners','away_corners'),'yellows':('home_yellow_cards','away_yellow_cards'),
        'reds':('home_red_cards','away_red_cards')}.items():
        coverage['missing_'+label]=sum(any(r.get(f) is None for f in fields) for r in finished)
    return dict(snapshot_matches=len(rows), predictions=sum(totals.values()),
        outcomes={k:totals[k] for k in ('won','lost','pending','awaiting_data','void')},
        markets=markets, finished_coverage=coverage,
        basis='current_source_evaluation_not_persisted_settlement')


async def audit():
    from src.db import database
    try:
        if not database.configured:raise RuntimeError('DATABASE_URL is required')
        async with database.session() as session:
            # Must be first statement. Fail closed on unsupported databases.
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            rows=[]
            query=select(PredictionSnapshot,Match,MatchStatistics).join(Match,Match.id==PredictionSnapshot.match_id).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id)
            from src.jobs.worker import aware
            invalid=0
            for snapshot,match,stats in (await session.execute(query)).all():
                if not match.kickoff_at or aware(snapshot.captured_at)>=aware(match.kickoff_at):
                    invalid+=1;continue
                fields=('home_corners','away_corners','home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards')
                rows.append(dict(match_id=match.id,status=match.status,ft_home=match.ft_home,ft_away=match.ft_away,
                    ht_home=match.ht_home,ht_away=match.ht_away,is_final=bool(stats and stats.is_final),picks=snapshot.picks,
                    **{f:getattr(stats,f,None) for f in fields}))
            return dict(**summarize(rows), invalid_capture_matches=invalid)
    finally:await database.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input');args=parser.parse_args()
    if args.input:
        with open(args.input,encoding='utf-8') as file:result=summarize(json.load(file)['rows'])
    else:result=asyncio.run(audit())
    print(json.dumps(result,indent=2,ensure_ascii=False))
