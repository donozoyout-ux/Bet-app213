"""Independent missing-success check. Actions failure notifications, no messaging API."""
import asyncio
from datetime import datetime, timezone
import os
import httpx


async def check(repository, token, *, now=None, transport=None):
    # Validate the path shape without making a write-capable client.
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not token:
        raise ValueError('Missing watchdog configuration')
    async with httpx.AsyncClient(transport=transport,timeout=20,follow_redirects=False) as client:
        response=await client.get(f'https://api.github.com/repos/{repository}/actions/workflows/owned-audit.yml/runs',
            params={'status':'success','per_page':30},headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'})
        if response.status_code!=200:raise ValueError('Cannot verify daily run history')
    now=now or datetime.now(timezone.utc)
    successful=[r for r in response.json()['workflow_runs'] if r.get('event') in {'schedule','workflow_dispatch'} and r.get('conclusion')=='success']
    if not successful:raise ValueError('No successful daily archive run')
    latest=max(datetime.fromisoformat(r['updated_at'].replace('Z','+00:00')) for r in successful)
    if not 0<=(now-latest).total_seconds()<=36*3600:raise ValueError('Daily archive success is overdue')


def main():
    try:asyncio.run(check(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN']))
    except Exception:
        print('::error::No verifiable successful daily archive in the last 36 hours. Inspect Actions and protected configuration.')
        raise SystemExit(1) from None


if __name__=='__main__':main()
