"""Owner-triggered full relational archive with same-snapshot parity gates."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
from .crypto import key_bytes, write_new
from .github_relational import GitHubRelationalArchive, package_dump
from .offline_recovery import ephemeral_pg18, _restore, local_inventory
from .recovery_format import canonical
from .relational_source import capture_source

class ArchiveStageError(RuntimeError):
    def __init__(self,stage):self.stage=stage


def configuration(env):
    if env.get('RELATIONAL_ARCHIVE_ACTIVATED')!='true':
        raise ValueError('Explicit owner activation required')
    required=('RELATIONAL_SOURCE_DATABASE_URL','RELATIONAL_GITHUB_TOKEN',
              'RELATIONAL_GITHUB_REPOSITORY','RELATIONAL_ENCRYPTION_KEY')
    if any(not env.get(key) for key in required):raise ValueError('Protected archive configuration missing')
    if env['RELATIONAL_GITHUB_REPOSITORY']!='donozoyout-ux/kuponanalizii-data-archive':
        raise ValueError('Unexpected private archive repository')
    key_bytes(env['RELATIONAL_ENCRYPTION_KEY'])
    return env


def inventory_summary(inventory):
    return dict(inventory_sha256=hashlib.sha256(canonical(inventory)).hexdigest(),
                relation_count=len(inventory['tables']),row_count=sum(r['rows'] for r in inventory['tables']),
                schema_object_count=sum(v['objects'] for v in inventory['schema_objects'].values()),
                sequence_count=len(inventory['sequences']),large_object_count=inventory['large_objects'],
                foreign_key_count=inventory['foreign_key_count'],
                json_payloads_included=inventory['json_payloads_included_in_row_fingerprints'])


async def execute(env=None,*,pg_dump='pg_dump',transport=None,output_root=None):
    env=configuration(os.environ if env is None else env)
    if output_root is None:raise ValueError('Private owner-only output directory required')
    root=Path(output_root).resolve()
    if not root.is_dir() or any(root.iterdir()):raise ValueError('New empty private output directory required')
    stage='source_snapshot'
    owner='relational-'+secrets.token_hex(12)
    try:
        with tempfile.TemporaryDirectory(prefix='full-relational-',dir=root) as private:
            work=Path(private);dump=work/'source.dump';readback=work/'github.dump';package_dir=work/'assets';package_dir.mkdir()
            captured=capture_source(env['RELATIONAL_SOURCE_DATABASE_URL'],dump,pg_dump=pg_dump)
            source_inventory=captured.pop('source_inventory')
            stage='first_isolated_restore'
            with ephemeral_pg18(5433,owner) as (container,params):
                _restore(container,dump)
                first_inventory=local_inventory(params)
            if first_inventory!=source_inventory:raise ValueError('Source and first restore inventories differ')
            stage='encrypt_full_dump'
            package=package_dump(dump,{**captured,'source_inventory':source_inventory},
                                 env['RELATIONAL_ENCRYPTION_KEY'],package_dir)
            stage='github_upload_and_readback'
            archive=GitHubRelationalArchive(env['RELATIONAL_GITHUB_REPOSITORY'],env['RELATIONAL_GITHUB_TOKEN'],
                                            env['RELATIONAL_ENCRYPTION_KEY'],transport=transport)
            try:
                release=await archive.create_draft(package,readback)
                stage='github_independent_restore'
                with ephemeral_pg18(5434,owner) as (container,params):
                    _restore(container,readback)
                    remote_inventory=local_inventory(params)
                if remote_inventory!=source_inventory:
                    raise ValueError('GitHub restored inventory differs from source snapshot')
                stage='publish_verified_private_archive'
                published=await archive.publish(release,remote_inventory)
            finally:
                await archive.close()
            summary=inventory_summary(source_inventory)
            receipt=dict(schema='betapp-relational-verification-v1',backup_id=package['backup_id'],
                captured_at=datetime.now(timezone.utc).isoformat(),
                source_snapshot_sha256=captured['source_snapshot_sha256'],dump_sha256=captured['dump_sha256'],
                inventory=summary,private_repository=env['RELATIONAL_GITHUB_REPOSITORY'],
                release_tag=published.get('tag_name'),encrypted_asset_count=len(package['chunks'])+1,
                independent_local_restore_verified=True,source_parity_verified=True,
                github_download_restore_verified=True,roles_and_acls_included=False,
                cleanup_authorized=False)
            write_new(root/'verification.json',json.dumps(receipt,sort_keys=True,indent=2).encode())
            return receipt
    except Exception:
        raise ArchiveStageError(stage) from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Confirm a bounded read-only source capture and private GitHub archive publication')
    parser.add_argument('--output-root',required=True)
    args=parser.parse_args()
    if not args.execute:parser.error('Explicit --execute is required')
    os.umask(0o077)
    try:
        import asyncio
        asyncio.run(execute(output_root=args.output_root))
        print('RELATIONAL_ARCHIVE_VERIFIED source_parity=true github_restore=true cleanup_authorized=false')
    except ArchiveStageError as exc:
        print(f'RELATIONAL_ARCHIVE_INCOMPLETE stage={exc.stage} sqlstate=unavailable')
        logging.error('Relational archive incomplete; protected diagnostic is available locally.')
        raise SystemExit(1) from None
    except Exception as exc:
        print('RELATIONAL_ARCHIVE_INCOMPLETE stage=configuration sqlstate=unavailable')
        logging.error('Relational archive incomplete; protected diagnostic is available locally.')
        raise SystemExit(1) from None


if __name__=='__main__':main()
