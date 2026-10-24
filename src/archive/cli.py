"""Daily recoverable audited subset, never a full operational backup."""
import argparse
import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import tempfile
from src.archive.crypto import key_bytes, seal, unseal, read, write_new
from src.archive.schema import collect, validate, canonical
from src.archive.source import snapshot
from src.archive.coverage import collect_coverage
from src.archive.github import ReleaseArchive, PREFIX
from src.archive.restore import restore_new, read_catalog


def verify_restore(data):
    with tempfile.TemporaryDirectory(prefix='owned-audit-') as folder:
        path=Path(folder)/'verification.sqlite'
        restore_new(data,path)
        actual=read_catalog(path)
        # SQLite identity ordering is lexical; compare canonical record sets.
        for kind in ('forecasts','decisions','coupons','reports'):
            if sorted(map(canonical,actual[kind]))!=sorted(map(canonical,data[kind])):
                raise ValueError('Restored records differ')
        if actual['coverage']!=data['coverage']:raise ValueError('Restored coverage differs')


def configuration(env):
    required=('ARCHIVE_DATABASE_URL','ARCHIVE_ENCRYPTION_KEY','ARCHIVE_GITHUB_TOKEN',
              'ARCHIVE_REPOSITORY','ARCHIVE_RIGHTS_REFERENCE','ARCHIVE_BUDGET_BYTES',
              'NEON_QUOTA_BYTES','ARCHIVE_KEEP_DAYS','ARCHIVE_EXPECTED_LEAGUES')
    if env.get('ARCHIVE_ACTIVATED')!='true' or any(not env.get(k) for k in required):
        raise ValueError('Archive disabled or missing protected configuration')
    import re
    if not re.fullmatch(r'APP-[0-9]{1,12}',env['ARCHIVE_RIGHTS_REFERENCE']):raise ValueError('Rights review reference required')
    key_bytes(env['ARCHIVE_ENCRYPTION_KEY'])
    if int(env['NEON_QUOTA_BYTES'])<=0:raise ValueError('Configured Neon quota required')
    expected=[int(v.strip()) for v in env['ARCHIVE_EXPECTED_LEAGUES'].split(',')]
    if not expected or len(expected)>500 or len(set(expected))!=len(expected) or any(v<=0 for v in expected):raise ValueError('Reviewed expected league scope required')
    return env


def emit_report(data, path):
    data=validate(data);report=data['coverage']
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    write_new(path,canonical(report))
    # Fixed messages only; no source exceptions, names, DSNs or record payloads.
    summary=os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as output:
            output.write(f"## Owned audit {report['day']}\n\nVerified encrypted audit subset; not an operational database backup.\n\n")
            output.write(f"Neon: {report['database_bytes']} / {report['quota_bytes']} configured bytes; growth: {report['growth_bytes']}; missing run days: {report['missing_run_days']}.\n\n")
            output.write(f"Missing or disabled expected league codes: {report['missing_league_codes']}. Database size excludes other branches and provider billing overhead.\n\n")
            output.write('| League code | Fixtures | Latest date | Missing odds | Missing final statistics | Queue backlog |\n|---|---:|---|---:|---:|---:|\n')
            for row in report['leagues']:
                output.write(f"| {row['league_code']} | {row['fixtures']} | {row['newest_fixture_date'] or 'unknown'} | {row['missing_odds']} | {row['missing_statistics']} | {row['backlog']} |\n")
    if report['missing_run_days']:print('::warning::One or more daily archives were missed; inspect archive history.')
    if report['database_bytes']>=report['quota_bytes']*.8:print('::warning::Neon storage is at least 80% of the configured quota.')


async def daily(env=None, now=None, *, transport=None):
    env=configuration(os.environ if env is None else env)
    day=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    archive=ReleaseArchive(env['ARCHIVE_REPOSITORY'],env['ARCHIVE_GITHUB_TOKEN'],env['ARCHIVE_ENCRYPTION_KEY'],
        budget_bytes=int(env['ARCHIVE_BUDGET_BYTES']),keep_days=int(env['ARCHIVE_KEEP_DAYS']),transport=transport)
    try:
        releases,total=await archive.inventory()
        current=next((r for r in releases if r['tag_name']==PREFIX+day.isoformat()),None)
        if current and (not current.get('draft') or current.get('assets')):
            data=await archive.verified(current,draft_ok=True)
            verify_restore(data)
            if current.get('draft'):
                await archive.request('PATCH',f"/repos/{archive.repo}/releases/{int(current['id'])}",json={'draft':False})
        else:
            older=sorted((r for r in releases if r['tag_name'].startswith(PREFIX) and not r.get('draft') and r['tag_name']<PREFIX+day.isoformat()),key=lambda r:r['tag_name'],reverse=True)
            previous=(await archive.verified(older[0]))['coverage'] if older else None
            async with snapshot(env['ARCHIVE_DATABASE_URL']) as connection:
                data=await collect(connection,env['ARCHIVE_RIGHTS_REFERENCE'],now=now)
                data['coverage']=await collect_coverage(connection,day,int(env['NEON_QUOTA_BYTES']),previous,
                    expected_leagues=[int(v.strip()) for v in env['ARCHIVE_EXPECTED_LEAGUES'].split(',')])
            data=validate(data)
            blob=seal(data,env['ARCHIVE_ENCRYPTION_KEY'])
            verify_restore(unseal(blob,env['ARCHIVE_ENCRYPTION_KEY']))
            data=await archive.publish(data,blob,releases,total)
        await archive.retain(day,releases)
        emit_report(data,Path(env.get('ARCHIVE_REPORT','archive-output/coverage.json')))
        return data
    finally:await archive.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['daily','restore'])
    parser.add_argument('--input');parser.add_argument('--destination')
    args=parser.parse_args()
    try:
        if args.command=='daily':asyncio.run(daily())
        else:
            if not args.input or not args.destination:parser.error('restore requires --input and --destination')
            data=read(args.input,os.environ['ARCHIVE_ENCRYPTION_KEY'])
            verify_restore(data)
            restore_new(data,args.destination)
    except Exception:
        logging.error('Archive failed; inspect protected configuration and service availability. No successful daily run is claimed.')
        raise SystemExit(1) from None


if __name__=='__main__':main()
