"""Dry-run-only historical audit. No remediation mode exists.

Set AUDIT_DATABASE_URL securely, or pass --input with a local collect() bundle.
The local output contains evidence; review before publishing it. No credentials,
coupon IDs, root IDs or owner hashes appear in the generated audit report.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analytics.history_audit import audit
from src.analytics.history_audit_reader import collect


async def read_database():
    import asyncpg
    url = os.environ['AUDIT_DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(url, timeout=10, command_timeout=5,
        server_settings={'statement_timeout':'5000','idle_in_transaction_session_timeout':'15000',
                         'application_name':'readonly_history_audit'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            return await collect(conn)
    finally:
        await conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle = json.loads(args.input.read_text(encoding='utf-8')) if args.input else asyncio.run(read_database())
    result = audit(**bundle)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    print(json.dumps({'dry_run':True, 'selections':len(result['rows']),
                      'discrepancies':len(result['discrepancies'])}))
