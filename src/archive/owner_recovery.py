"""Secret-safe local preflight subcommands for the PowerShell owner launcher."""
import argparse
import asyncio
import os

from .crypto import key_bytes
from .full_recovery import connection_parameters, RecoveryStore, REPO, MAX_DUMP


def check_source():
    connection_parameters(os.environ.get('ARCHIVE_DATABASE_URL', ''), source=True)


async def check_archive():
    key = os.environ.get('ARCHIVE_ENCRYPTION_KEY', '').strip()
    key_bytes(key)
    store = RecoveryStore(REPO, os.environ.get('ARCHIVE_GITHUB_TOKEN', ''), key,
                          budget_bytes=2*MAX_DUMP, keep_days=3650)
    try:
        await store.inventory()  # Verifies private repository identity; no mutations.
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=['check-source', 'check-archive'])
    args = parser.parse_args()
    try:
        if args.check == 'check-source':
            check_source()
        else:
            asyncio.run(check_archive())
    except Exception:
        print('OWNER_PREFLIGHT_FAILED: ' + args.check)
        return 1
    print('OWNER_PREFLIGHT_ACCEPTED: ' + args.check)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
