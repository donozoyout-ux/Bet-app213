"""Read-only, resumable source verification; never seeds/enables leagues.

python -m src.scrapers.goaloo.europe_rollout --output europe-review.json
Repeat with --resume europe-review.json; at most three leagues per invocation.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from .client import GoalooClient,SourceError
from .competitions import (CATALOG_URL,PRODUCTION_LEAGUE_IDS,normalize_name,
    parse_catalog,select_european_leagues,verify_competition)


def identity(item):
    return [item.external_id,item.catalog_name,item.country,item.schedule_format]


async def verify_batch(client,previous=None,batch_size=2,league_ids=None,timeout=60):
    if not 1<=batch_size<=3:raise ValueError('Batch size must be between 1 and 3')
    targets=select_european_leagues(parse_catalog(await client.get_text(CATALOG_URL)))
    identities=[identity(item) for item in targets]
    revision=hashlib.sha256(json.dumps(identities,ensure_ascii=False).encode()).hexdigest()
    if previous and (previous.get('catalog_revision')!=revision or previous.get('from_year')!=2024):
        raise SourceError('Catalog changed: start a new review report; prior verification is not reused')
    current={x.external_id:x for x in targets}
    requested=set(league_ids or [])
    if requested-current.keys():raise ValueError('Requested league is absent from the current European catalog')
    records={}
    for row in (previous or {}).get('competitions',[]):
        target=current.get(row.get('external_id'))
        if target is None or [row.get(k) for k in ('external_id','catalog_name','country','schedule_format')]!=identity(target):
            raise SourceError('Resume report identity does not match catalog')
        if row['external_id'] in records:raise SourceError('Duplicate resume league')
        records[row['external_id']]=row
    # Existing production leagues are preserved, not re-ingested by this tool.
    candidates=[x for x in targets if x.external_id not in PRODUCTION_LEAGUE_IDS
        and (not requested or x.external_id in requested)
        and not records.get(x.external_id,{}).get('verified')]
    attempted=[]
    for target in candidates[:batch_size]:
        attempted.append(target.external_id)
        try:
            async with asyncio.timeout(timeout):
                await verify_competition(client,target,include_overlapping=True)
        except TimeoutError:
            target.verified=False;target.error='Source verification time budget exceeded'
        # Source schedule verification never implies fresh coupon prices.
        row=asdict(target)
        row.update(activation='disabled_pending_review',coupon_eligible=False)
        records[target.external_id]=row
    countries=sorted({x.country for x in targets})
    return dict(catalog_url=CATALOG_URL,catalog_revision=revision,from_year=2024,
        checked_at=datetime.now(timezone.utc).isoformat(),catalog_count=len(targets),
        countries=countries,attempted=attempted,production_activation=False,
        remaining=sum(x.external_id not in PRODUCTION_LEAGUE_IDS and not records.get(x.external_id,{}).get('verified') for x in targets),
        competitions=[records[k] for k in sorted(records)])


async def main(args):
    previous=json.loads(Path(args.resume).read_text(encoding='utf-8')) if args.resume else None
    async with GoalooClient() as client:
        report=await verify_batch(client,previous,args.batch_size,args.league_id)
    destination=Path(args.output);temporary=destination.with_suffix(destination.suffix+'.tmp')
    temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(destination)
    print(json.dumps(dict(attempted=report['attempted'],remaining=report['remaining'],production_activation=False)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--resume')
    parser.add_argument('--batch-size',type=int,choices=(1,2,3),default=2)
    parser.add_argument('--league-id',type=int,action='append')
    asyncio.run(main(parser.parse_args()))
