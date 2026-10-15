"""Read-only learning analysis, offline export replay or configured source DB.

python -m scripts.learning_report --input docs/learning-phase-2-source.json --output report.json
No initialization, report publication, scraping, Telegram or model changes.
"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
from fastapi.encoders import jsonable_encoder
from src.analytics.settlements import decision_item, digest
from src.analytics.learning_metrics import analyze, METHOD


def date(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def exported_source(records):
    items=[]
    coverage=dict(snapshot_matches=len(records),saved_predictions=0,recorded_terminal=0,
        match_pending=0,result_data_pending=0,invalid_capture=0)
    manifest=hashlib.sha256()
    for exported in sorted(records,key=lambda r:r['match_id']):
        raw=dict(exported,picks=json.loads(exported['picks_json']))
        snapshot=SimpleNamespace(match_id=raw['match_id'],league_id=raw['league_id'],
            captured_at=date(raw['captured_at']),model_version=raw['model_version'],picks=raw['picks'])
        kickoff=date(raw['kickoff_at']) if raw['kickoff_at'] else None
        valid=kickoff is not None and snapshot.captured_at < kickoff
        coverage['saved_predictions']+=len(snapshot.picks)
        keys=set()
        for exported_record in raw['settlements']:
            record={k:v for k,v in exported_record.items() if k not in {'actual_json','evidence_json'}}
            record.update(actual=json.loads(exported_record['actual_json']),evidence=json.loads(exported_record['evidence_json']))
            if valid:
                latest=SimpleNamespace(**dict(record,recorded_at=date(record['recorded_at'])))
                items.append(decision_item(snapshot,SimpleNamespace(id=snapshot.match_id,kickoff_at=kickoff),latest,record['cursor']))
                keys.add(record['pick_index'])
        if not valid:
            coverage['invalid_capture']+=len(snapshot.picks)
        else:
            field='result_data_pending' if raw['status'] in {'finished','cancelled','abandoned'} else 'match_pending'
            coverage[field]+=len(snapshot.picks)-len(keys)
        manifest.update(digest(dict(match_id=snapshot.match_id,picks=snapshot.picks,league_id=snapshot.league_id,
            model_version=snapshot.model_version,captured_at=snapshot.captured_at.isoformat(),
            kickoff_at=kickoff.isoformat() if kickoff else None,status=raw['status'])).encode())
    items.sort(key=lambda r:r['cursor'])
    coverage['recorded_terminal']=len(items)
    fingerprint=digest(dict(manifest=manifest.hexdigest(),terminal=jsonable_encoder(items),coverage=coverage))
    return items,coverage,fingerprint


async def configured_source():
    from src.db import database
    from src.analytics.learning import source_data
    try:
        async with database.session() as session:
            items,coverage,fingerprint,observed=await source_data(session)
            return items,coverage,fingerprint,observed.isoformat()
    finally:
        await database.close()


def report(items,coverage,fingerprint,observed_at):
    result=analyze(items)
    for lesson in result['lessons']:
        lesson['payload'].update(date=observed_at,method_version=METHOD,data_fingerprint=fingerprint)
    return dict(method_version=METHOD,data_fingerprint=fingerprint,
        basis='verified_persisted_settlements_latest_revisions',source_observed_at=observed_at,
        coverage=dict(coverage,eligible_predictions=result['summary']['predictions'],exclusions=result['excluded']),
        **result)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input');parser.add_argument('--output');args=parser.parse_args()
    if args.input:
        with open(args.input,encoding='utf-8') as file:export=json.load(file)
        source=(*exported_source(export['records']),export['audit_completed_at_utc'])
    else:source=asyncio.run(configured_source())
    output=json.dumps(report(*source),indent=2,ensure_ascii=False,allow_nan=False)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(output+'\n',encoding='utf-8')
    else:print(output)
