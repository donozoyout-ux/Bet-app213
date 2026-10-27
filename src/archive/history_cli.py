"""Verify a pinned history generation, optionally retain its isolated restore.

No upload, production database access, source eviction or provider calls.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from .history_reader import configured_reader, close_reader


async def run(output=None):
    reader = configured_reader()
    try:
        await reader.prepare()
        if output is not None:
            target = Path(output)
            created = False
            try:
                digest = hashlib.sha256()
                with target.open('xb') as destination:
                    created = True
                    with (Path(reader.directory.name)/'history.sqlite').open('rb') as source:
                        while block := source.read(1024*1024):
                            digest.update(block); destination.write(block)
                actual = hashlib.sha256()
                with target.open('rb') as restored:
                    while block := restored.read(1024*1024): actual.update(block)
                if actual.digest() != digest.digest(): raise ValueError('Restore copy verification')
            except BaseException:
                if created: target.unlink(missing_ok=True)
                raise
        return dict(status='verified', generation=reader.generation,
            source_id=reader.catalog['source_id'], coverage=reader.catalog['coverage'],
            counts=reader.catalog['counts'], full_database_backup=False,
            source_space_reclaimed=0, isolated_restore_retained=output is not None)
    finally:
        await close_reader()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'restore'])
    parser.add_argument('--output')
    args = parser.parse_args()
    if (args.command == 'restore') != bool(args.output): parser.error('restore requires a new --output path; verify has no output')
    try: print(json.dumps(asyncio.run(run(args.output)), sort_keys=True))
    except Exception: parser.exit(1, 'History verification failed; no production state changed.\n')


if __name__ == '__main__': main()
