"""Replay a read-only export; optionally enqueue existing verified domestic sources.

Run from repository root: python -m scripts.uefa_coverage_report --input FILE --output FILE
No network/database writes without the explicit --enqueue-backfill switch.
"""
import argparse
import asyncio
import gzip
import json
from pathlib import Path
from src.analytics.uefa_coverage import report


async def enqueue():
    from src.db import database
    from src.jobs.uefa_backfill import enqueue_domestic_backfill
    try:
        return await enqueue_domestic_backfill(database)
    finally:
        await database.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input'); parser.add_argument('--output')
    parser.add_argument('--enqueue-backfill',action='store_true')
    args=parser.parse_args()
    if args.enqueue_backfill:
        print(json.dumps(asyncio.run(enqueue()),ensure_ascii=False))
    elif args.input and args.output:
        path=Path(args.input)
        source=json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_text(encoding='utf-8'))
        Path(args.output).write_text(json.dumps(report(source),ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    else:
        parser.error('--input and --output, or --enqueue-backfill required')
