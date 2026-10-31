"""Read-only disk-pressure rescue: dump first, encrypt, isolated local restore.

This is NOT proof of exact source-row parity. The ordinary backup path has
stronger parity checks, but may not finish on an exhausted source disk.
No source writes, cleanup, migration, deployment, GitHub API or secrets in logs.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import psycopg

from .full_recovery import client, connection_parameters, inventory
from .local_recovery_store import LocalRecoveryStore
from .recovery_format import CHUNK, MAX_DUMP, SCHEMA, canonical, sha, unseal
from .crypto import key_bytes


def dump_without_source_data_scan(source, directory):
    """Avoid materializing/analyzing source payload rows before pg_dump."""
    options = '-c default_transaction_read_only=on -c statement_timeout=600000 -c lock_timeout=2000'
    with psycopg.connect(**source, options=options, autocommit=True) as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if conn.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
            raise ValueError('source_not_readonly')
        if conn.execute('SELECT current_database()').fetchone()[0] != source['dbname']:
            raise ValueError('source_identity')
        version = int(conn.execute('SHOW server_version_num').fetchone()[0])
        if not 160000 <= version < 190000:
            raise ValueError('unsupported_postgres_version')
        snapshot = conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
        print('RESCUE_STAGE: pg_dump_readonly_started', flush=True)
        client('pg_dump', source, directory, '--format=custom', '--compress=0',
               '--no-owner', '--no-acl', '--snapshot='+snapshot,
               '--file=/backup/full.dump', readonly=True)
        print('RESCUE_STAGE: pg_dump_readonly_complete', flush=True)
        # Small system-catalog inventory ONLY; no source table scans / fingerprints.
        tables = sorted([list(r) for r in conn.execute(
            "SELECT n.nspname,c.relname,c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
            "AND c.relkind IN ('r','p','m','f')")])
        if any(kind == 'f' for _, _, kind in tables):
            raise ValueError('foreign_tables_require_separate_backup')
        sequences = sorted([list(r) for r in conn.execute(
            "SELECT schemaname,sequencename FROM pg_sequences "
            "WHERE schemaname !~ '^pg_'")])
        large_objects = conn.execute(
            'SELECT count(*) FROM pg_largeobject_metadata').fetchone()[0]
        stamp = conn.execute('SELECT transaction_timestamp()::text').fetchone()[0]
        conn.execute('ROLLBACK')
    size = (Path(directory) / 'full.dump').stat().st_size
    if not 0 < size <= MAX_DUMP or not tables:
        raise ValueError('rescue_dump_budget_or_empty')
    return dict(source_database=source['dbname'], server_version=version,
                snapshot_at=stamp, source_catalog=dict(tables=tables,
                sequences=sequences, large_objects=large_objects))


def restore_without_source_row_hashes(target, directory, evidence):
    """Restores from authenticated encrypted chunks, then audits local contents."""
    with psycopg.connect(**target) as conn:
        if not 180000 <= int(conn.execute('SHOW server_version_num').fetchone()[0]) < 190000:
            raise ValueError('postgres18_restore_target_required')
        existing = inventory(conn)
        if existing['tables'] or existing['sequences'] or existing['large_objects']:
            raise ValueError('restore_target_not_empty')
    client('pg_restore', target, directory, '--exit-on-error',
           '--single-transaction', '--no-owner', '--no-acl',
           '--dbname=postgres', '/backup/recovered.dump')
    with psycopg.connect(**target) as conn:
        actual = inventory(conn)
        tables = sorted([[t['schema'], t['table'], t['kind']] for t in actual['tables']])
        seqs = sorted([[q['schema'],q['name']] for q in actual['sequences']])
        source_catalog = evidence['source_catalog']
        if (tables != source_catalog['tables'] or seqs != source_catalog['sequences']
                or actual['large_objects'] != source_catalog['large_objects']):
            raise ValueError('rescue_catalog_mismatch')
        # Local per-row hashes are strong evidence of restored content,
        # but NOT source-row parity; the source disk has no spare temp space.
        if not all('row_multiset_sha256' in t for t in actual['tables']):
            raise ValueError('rescue_local_hash_missing')
    return actual


async def rescue(source, target, store, backup_id, directory):
    print('RESCUE_STAGE: readonly_snapshot_started', flush=True)
    evidence = await asyncio.to_thread(dump_without_source_data_scan,source,directory)
    await store.open(backup_id)
    chunks=[]
    digest=hashlib.sha256()
    with (Path(directory) / 'full.dump').open('rb') as source_file:
        for index in range(MAX_DUMP // CHUNK + 1):
            raw = source_file.read(CHUNK)
            if not raw: break
            digest.update(raw)
            chunks.append(await store.put(f'chunk-{index:06d}.enc', raw))
    manifest=dict(schema=SCHEMA,backup_id=backup_id,format='pg_dump_custom',
                  rescue_only=True,source_row_content_verified=False,
                  owner_acl_included=False,**evidence,chunks=chunks,
                  dump_sha256=digest.hexdigest())
    pin=await store.put('manifest.enc',canonical(manifest))
    recovered_manifest=json.loads(unseal(
        await store.download(dict(id=pin['asset_id'],size=pin['bytes'])),
        store.key,backup_id,'manifest.enc'))
    if recovered_manifest != manifest:raise ValueError('rescue_manifest_mismatch')
    reconstructed=hashlib.sha256()
    with (Path(directory) / 'recovered.dump').open('xb') as out:
        for chunk in recovered_manifest['chunks']:
            encrypted=await store.download(dict(id=chunk['asset_id'],size=chunk['bytes']))
            if sha(encrypted)!=chunk['sha256']:raise ValueError('rescue_ciphertext_hash')
            raw=unseal(encrypted,store.key,backup_id,chunk['name'])
            if len(raw)!=chunk['plaintext_bytes'] or sha(raw)!=chunk['plaintext_sha256']:
                raise ValueError('rescue_plaintext_hash')
            out.write(raw)
            reconstructed.update(raw)
    if reconstructed.hexdigest()!=manifest['dump_sha256']:
        raise ValueError('rescue_dump_checksum')
    print('RESCUE_STAGE: isolated_pg18_restore_started',flush=True)
    actual=await asyncio.to_thread(
        restore_without_source_row_hashes,target,directory,evidence)
    print('RESCUE_STAGE: isolated_pg18_restore_complete',flush=True)
    summary=dict(schema=SCHEMA,backup_id=backup_id,rescue_only=True,
                 restore_verified=True,source_row_content_verified=False,
                 row_content_verified=False,offsite_verified=False,
                 eviction_authorized=False,application_read_through_verified=False,
                 source_deleted_rows=0,dump_sha256=manifest['dump_sha256'],
                 tables=len(actual['tables']),
                 rows=sum(t['rows'] for t in actual['tables']),
                 chunks=len(chunks),manifest=pin,
                 source_catalog_verified=True,
                 local_restored_row_sha256_verified=True)
    receipt=await store.put('receipt.enc',canonical(summary))
    summary['receipt']=receipt
    await store.request('PATCH',
        f'/repos/{store.repo}/releases/{store.release["id"]}',
        json=dict(draft=False,body=canonical(summary).decode()))
    return summary


async def main():
    source=connection_parameters(os.environ.get('ARCHIVE_DATABASE_URL',''),source=True)
    target=connection_parameters(os.environ['RECOVERY_VERIFY_URL'],source=False)
    key=os.environ['ARCHIVE_ENCRYPTION_KEY'].strip()
    key_bytes(key)
    backup_id=os.environ.get('RECOVERY_BACKUP_ID','')
    if (not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',backup_id)
            or os.environ.get('RECOVERY_AUTHORIZED')!='true'):
        raise ValueError('rescue_owner_authorization_required')
    offline=os.environ.get('RECOVERY_OFFLINE_DIR','')
    if not offline:raise ValueError('rescue_offline_only')
    store=LocalRecoveryStore(offline,key,2*MAX_DUMP,allow_rescue_restore=True)
    try:
        with tempfile.TemporaryDirectory(prefix='kupon-rescue-') as directory:
            summary=await rescue(source,target,store,backup_id,directory)
            package=store.package()
            print('RESCUE_LOCAL_DUMP_RESTORED_SOURCE_ROW_PARITY_NOT_VERIFIED',flush=True)
            print(canonical(dict(summary,encrypted_package=package)).decode(),flush=True)
    finally:
        await store.close()


if __name__=='__main__':
    os.umask(0o077)
    try: asyncio.run(main())
    except Exception:
        # Do not log arbitrary exceptions, credentials, connection strings,
        # server traceback, tokens, or PostgreSQL data.
        print('RESCUE_INCOMPLETE: no cleanup, no full-source-parity claim',flush=True)
        raise SystemExit(1) from None
