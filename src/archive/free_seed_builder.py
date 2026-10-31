"""Owner-operated zero-cost hot PostgreSQL seed built ONLY from encrypted local recovery.

Never contacts Render, GitHub, Neon, provider APIs or production endpoints.
Two separately labeled, loopback-only Docker postgres:18 containers are used.
The original encrypted rescue ZIP is read-only; its SHA256 is pinned.
No full-source parity, historical reader activation or source cleanup is claimed.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from zipfile import ZipFile

import psycopg

from .free_hot_cutover import apply_only_to_owned_local_verifier, CutoverBlocked
from .full_recovery import inventory
from .local_recovery_store import LocalRecoveryStore
from .recovery_format import CHUNK, MAX_DUMP, SCHEMA, canonical, sha, unseal
from .crypto import key_bytes

ORIGINAL_ID='vv755-owner-rescue-20261031-e'
ORIGINAL_ZIP_SHA256='566e475ab72d3b4a6b05b88bbca9811c72f43d6bf7fea62927dff5d4fe626ae4'
ORIGINAL_DUMP_SHA256='b66fd7b17d40ea59d0abdfd66b821fd36fb2ada0e22780883754f3e565747082'
REPO='donozoyout-ux/kuponanalizii-data-archive'
MAX_PACKAGE_BYTES=512*1024*1024
MIN_EXPECTED_TABLES=37


def sha_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as reader:
        while batch:=reader.read(1024*1024):digest.update(batch)
    return digest.hexdigest()


def load_offline_rescue(zip_path, key, output_path):
    """Verify offsite rescue bytes and reconstruct original full dump privately."""
    zpath=Path(zip_path)
    if not zpath.is_file() or not 0<zpath.stat().st_size<MAX_PACKAGE_BYTES:
        raise CutoverBlocked('offline_rescue_missing_or_size')
    if sha_file(zpath)!=ORIGINAL_ZIP_SHA256:
        raise CutoverBlocked('offline_rescue_sha256_mismatch')
    with ZipFile(zpath) as archive:
        names=archive.namelist()
        expected_set={'manifest.enc','receipt.enc','summary.json'}
        if len(names)!=len(set(names)) or not expected_set<=set(names):
            raise CutoverBlocked('archive_duplicate_or_missing_files')
        if any(not re.fullmatch(r'chunk-[0-9]{6}\.enc',n) and n not in expected_set for n in names):
            raise CutoverBlocked('archive_unexpected_member')
        if archive.getinfo('manifest.enc').file_size>128*1024:
            raise CutoverBlocked('manifest_budget')
        manifest=json.loads(unseal(archive.read('manifest.enc'),key,ORIGINAL_ID,'manifest.enc'))
        if (manifest.get('schema')!=SCHEMA or manifest.get('backup_id')!=ORIGINAL_ID
                or manifest.get('format')!='pg_dump_custom'
                or manifest.get('rescue_only') is not True
                or manifest.get('source_row_content_verified') is not False
                or manifest.get('dump_sha256')!=ORIGINAL_DUMP_SHA256):
            raise CutoverBlocked('original_rescue_manifest_conflict')
        chunks=manifest.get('chunks')
        if (not isinstance(chunks,list) or not 0<len(chunks)<=MAX_DUMP//CHUNK+1
                or len(chunks)+3!=len(names)):
            raise CutoverBlocked('original_rescue_chunk_count')
        if [c['name'] for c in chunks] != [f'chunk-{i:06d}.enc' for i in range(len(chunks))]:
            raise CutoverBlocked('original_rescue_chunk_order')
        summary=json.loads(archive.read('summary.json'))
        receipt=json.loads(unseal(archive.read('receipt.enc'),key,ORIGINAL_ID,'receipt.enc'))
        for obj in (summary,receipt):
            if (obj.get('backup_id')!=ORIGINAL_ID or obj.get('restore_verified') is not True
                    or obj.get('source_row_content_verified') is not False
                    or obj.get('row_content_verified') is not False
                    or obj.get('source_deleted_rows')!=0
                    or obj.get('eviction_authorized') is not False):
                raise CutoverBlocked('original_rescue_receipt_conflict')
        if Path(output_path).exists():raise CutoverBlocked('private_output_exists')
        digest=hashlib.sha256()
        with Path(output_path).open('xb') as target:
            for item in chunks:
                name=item['name']
                if archive.getinfo(name).file_size!=item['bytes']:
                    raise CutoverBlocked('encrypted_chunk_length')
                blob=archive.read(name)
                if sha(blob)!=item['sha256']:raise CutoverBlocked('encrypted_chunk_checksum')
                raw=unseal(blob,key,ORIGINAL_ID,name)
                if len(raw)!=item['plaintext_bytes'] or sha(raw)!=item['plaintext_sha256']:
                    raise CutoverBlocked('decrypted_chunk_checksum')
                digest.update(raw)
                target.write(raw)
        if digest.hexdigest()!=ORIGINAL_DUMP_SHA256:
            raise CutoverBlocked('reconstructed_dump_checksum')
        if Path(output_path).stat().st_size>MAX_DUMP:
            raise CutoverBlocked('original_dump_budget')
    return manifest


def inspect_rescue_metadata(zip_path,key):
    """No writes, no Docker, no plaintext dump; validates fixed ZIP envelope."""
    path=Path(zip_path)
    if not path.is_file() or not 0<path.stat().st_size<MAX_PACKAGE_BYTES:
        raise CutoverBlocked('offline_rescue_missing_or_size')
    if sha_file(path)!=ORIGINAL_ZIP_SHA256:
        raise CutoverBlocked('offline_rescue_sha256_mismatch')
    with ZipFile(path) as archive:
        names=archive.namelist()
        expected={'manifest.enc','receipt.enc','summary.json'}
        if len(set(names))!=len(names) or not expected<=set(names):
            raise CutoverBlocked('archive_duplicate_or_missing_files')
        if any(n not in expected and not re.fullmatch(r'chunk-[0-9]{6}\.enc',n) for n in names):
            raise CutoverBlocked('archive_unexpected_member')
        if archive.getinfo('manifest.enc').file_size>128*1024 or archive.getinfo('receipt.enc').file_size>128*1024:
            raise CutoverBlocked('archive_metadata_budget')
        manifest=json.loads(unseal(archive.read('manifest.enc'),key,ORIGINAL_ID,'manifest.enc'))
        receipt=json.loads(unseal(archive.read('receipt.enc'),key,ORIGINAL_ID,'receipt.enc'))
        summary=json.loads(archive.read('summary.json'))
        if (manifest.get('schema')!=SCHEMA or manifest.get('backup_id')!=ORIGINAL_ID
                or manifest.get('dump_sha256')!=ORIGINAL_DUMP_SHA256
                or manifest.get('format')!='pg_dump_custom'
                or manifest.get('rescue_only') is not True
                or manifest.get('source_row_content_verified') is not False):
            raise CutoverBlocked('rescue_manifest_validation')
        for proof in (receipt,summary):
            if (proof.get('backup_id')!=ORIGINAL_ID or proof.get('restore_verified') is not True
                    or proof.get('row_content_verified') is not False
                    or proof.get('source_row_content_verified') is not False
                    or proof.get('eviction_authorized') is not False
                    or proof.get('source_deleted_rows')!=0):
                raise CutoverBlocked('rescue_receipt_validation')
        chunks=manifest.get('chunks')
        if (not isinstance(chunks,list) or not 0<len(chunks)<=MAX_DUMP//CHUNK+1
                or len(names)!=len(chunks)+3):
            raise CutoverBlocked('rescue_chunk_count')
        if [c.get('name') for c in chunks]!=[f'chunk-{i:06d}.enc' for i in range(len(chunks))]:
            raise CutoverBlocked('rescue_chunk_order')
        reconstructed=hashlib.sha256()
        for part in chunks:
            if archive.getinfo(part['name']).file_size!=part['bytes']:
                raise CutoverBlocked('rescue_chunk_size')
            blob=archive.read(part['name'])
            if sha(blob)!=part['sha256']:
                raise CutoverBlocked('rescue_ciphertext_sha256_mismatch')
            raw=unseal(blob,key,ORIGINAL_ID,part['name'])
            if len(raw)!=part['plaintext_bytes'] or sha(raw)!=part['plaintext_sha256']:
                raise CutoverBlocked('rescue_plaintext_sha256_mismatch')
            reconstructed.update(raw)
        if reconstructed.hexdigest()!=ORIGINAL_DUMP_SHA256:
            raise CutoverBlocked('rescue_dump_sha256_mismatch')
        return {'encrypted_zip_sha256_verified':True,
                'all_17_or_more_chunks_authenticated':True,
                'dump_sha256_verified_without_writing':True,
                'manifest_and_receipt_authenticated':True,
                'original_restore_verified':True,
                'full_source_row_parity_verified':False,
                'chunk_count':len(chunks),
                'output_directory_created':False,
                'database_contacted':False}


def _docker(args, *, env=None, timeout=180):
    try:
        return subprocess.run(['docker',*args],env=env,check=True,capture_output=True,
                              text=True,timeout=timeout).stdout.strip()
    except Exception:raise CutoverBlocked('isolated_docker_operation_failed') from None


def _port_is_free(port):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        try:s.bind(('127.0.0.1',port))
        except OSError:return False
    return True


@contextmanager
def ephemeral_pg18(port, owner):
    if port not in (5433,5434) or not _port_is_free(port):
        raise CutoverBlocked('local_port_in_use')
    password=secrets.token_urlsafe(32)
    env={**os.environ,'POSTGRES_PASSWORD':password}
    name=None
    try:
        cid=_docker(['run','-d','--rm','-p',f'127.0.0.1:{port}:5432',
                     '--label',f'kupon.recovery.owner-run={owner}',
                     '--env','POSTGRES_PASSWORD','postgres:18'],env=env,timeout=120)
        if not re.fullmatch('[a-f0-9]{64}',cid):
            raise CutoverBlocked('isolated_container_id_missing')
        name=cid
        if _docker(['inspect','--format',
                    '{{.Config.Image}} {{index .Config.Labels "kupon.recovery.owner-run"}}',
                    cid])!='postgres:18 '+owner:
            raise CutoverBlocked('isolated_container_identity')
        if '127.0.0.1:'+str(port) not in _docker(['port',cid,'5432/tcp']).splitlines():
            raise CutoverBlocked('isolated_container_wrong_port')
        import time
        for i in range(35):
            try:
                _docker(['exec',cid,'pg_isready','-U','postgres'],timeout=10)
                break
            except CutoverBlocked:
                if i==34:raise
                time.sleep(2)
        yield cid,dict(host='127.0.0.1',port=port,dbname='postgres',
                       user='postgres',password=password,sslmode='disable',connect_timeout=10)
    finally:
        if name:
            try:
                label=_docker(['inspect','--format',
                    '{{index .Config.Labels "kupon.recovery.owner-run"}}',name])
                if label==owner:_docker(['rm','-f','-v',name],timeout=60)
            except CutoverBlocked:
                pass


def _restore(container, local_file):
    _docker(['cp',str(local_file),container+':/tmp/owned-hot-recovery.dump'],timeout=300)
    _docker(['exec',container,'pg_restore','--exit-on-error','--single-transaction',
             '--no-owner','--no-acl','-U','postgres','-d','postgres',
             '/tmp/owned-hot-recovery.dump'],timeout=1200)


def _pg_dump(container, local_file):
    _docker(['exec',container,'pg_dump','-U','postgres','-d','postgres',
             '--format=custom','--compress=6','--no-owner','--no-acl',
             '--file=/tmp/owned-hot-seed.dump'],timeout=1200)
    _docker(['cp',container+':/tmp/owned-hot-seed.dump',str(local_file)],timeout=300)
    if not 0<local_file.stat().st_size<=MAX_DUMP:
        raise CutoverBlocked('hot_seed_dump_budget')


async def _encrypted_seed_and_verify(raw_dump, destination, key, backup_id,
                                     original_manifest, first_inventory):
    store=LocalRecoveryStore(destination,key,MAX_DUMP*2,allow_rescue_restore=True)
    await store.open(backup_id)
    digest=hashlib.sha256()
    chunks=[]
    with raw_dump.open('rb') as reader:
        for i in range(MAX_DUMP//CHUNK+1):
            raw=reader.read(CHUNK)
            if not raw:break
            digest.update(raw)
            chunks.append(await store.put(f'chunk-{i:06d}.enc',raw))
    manifest=dict(schema='hot-seed-v1',backup_id=backup_id,
                  source_recovery_id=ORIGINAL_ID,
                  source_recovery_dump_sha256=ORIGINAL_DUMP_SHA256,
                  original_rescue_only=True,original_source_row_parity_verified=False,
                  active_board_only=True,source_deleted_rows=0,
                  chunks=chunks,dump_sha256=digest.hexdigest())
    await store.put('manifest.enc',canonical(manifest))
    # Restore ONLY authenticated bytes freshly read from disk, not original pg_dump.
    with tempfile.TemporaryDirectory(prefix='hot-verify-',dir=destination.parent) as directory:
        replay=Path(directory)/'reconstructed.dump'
        recomputed=hashlib.sha256()
        with replay.open('xb') as out:
            for c in chunks:
                blob=await store.download(dict(id=c['asset_id'],size=c['bytes']))
                if sha(blob)!=c['sha256']:raise CutoverBlocked('seed_chunk_ciphertext')
                raw=unseal(blob,key,backup_id,c['name'])
                if len(raw)!=c['plaintext_bytes'] or sha(raw)!=c['plaintext_sha256']:
                    raise CutoverBlocked('seed_chunk_plaintext')
                recomputed.update(raw)
                out.write(raw)
        if recomputed.hexdigest()!=digest.hexdigest():
            raise CutoverBlocked('seed_reconstruction_checksum')
        with ephemeral_pg18(5434,backup_id) as (container,target):
            _restore(container,replay)
            with psycopg.connect(**target) as conn:
                restored=inventory(conn)
            if restored!=first_inventory:
                raise CutoverBlocked('hot_seed_independent_restore_parity')
    summary=dict(backup_id=backup_id,rescue_only=True,restore_verified=True,
                 row_content_verified=False,source_row_content_verified=False,
                 source_deleted_rows=0,eviction_authorized=False,
                 application_read_through_verified=False,
                 hot_seed_independent_restore_verified=True,
                 hot_seed_local_table_parity_verified=True,
                 offsite_verified=False,tables=len(first_inventory['tables']),
                 rows=sum(t['rows'] for t in first_inventory['tables']),
                 encrypted_chunks=len(chunks),dump_sha256=digest.hexdigest())
    await store.put('receipt.enc',canonical(summary))
    await store.request('PATCH',f'/repos/{store.repo}/releases/{store.release["id"]}',
                        json=dict(draft=False,body=canonical(summary).decode()))
    return store.package()


def main():
    import argparse
    p=argparse.ArgumentParser(description='Offline owner hot seed: not a production importer')
    p.add_argument('--zip',required=True)
    p.add_argument('--key-file',required=True)
    p.add_argument('--backup-id')
    p.add_argument('--output-root')
    p.add_argument('--inspect-only',action='store_true',help='Validate original encrypted rescue, never create a database or output directory')
    args=p.parse_args()
    if not args.inspect_only:
        if not args.backup_id or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',args.backup_id):
            p.error('Invalid unique local owner run ID')
        if args.backup_id==ORIGINAL_ID:
            p.error('Hot seed ID must differ from original rescue')
        if not args.output_root:
            p.error('A new output directory is required for seed generation')
    stage='key_file_validation'
    try:
        key=Path(args.key_file).read_text(encoding='utf-8-sig').strip()
        key_bytes(key)
        if args.inspect_only:
            stage='readonly_original_zip_metadata'
            info=inspect_rescue_metadata(args.zip,key)
            print('HOT_SEED_PREFLIGHT_OK '+json.dumps(info,sort_keys=True),flush=True)
            return
        stage='new_output_directory'
        root=Path(args.output_root).resolve()
        if root.exists() or not root.parent.is_dir():
            raise CutoverBlocked('private_new_output_root_required')
        root.mkdir(mode=0o700)
        if os.name!='nt':root.chmod(0o700)
        # All sensitive plaintext is confined to a disposable local directory.
        with tempfile.TemporaryDirectory(prefix='private-dump-',dir=root) as private:
            path=Path(private)
            full=path/'original.dump'
            stage='original_rescue_zip_decryption'
            manifest=load_offline_rescue(args.zip,key,full)
            print('HOT_SEED_STAGE: original_encrypted_rescue_verified',flush=True)
            stage='isolated_pg18_source_restore_and_compaction'
            with ephemeral_pg18(5433,args.backup_id) as (container,source):
                _restore(container,full)
                with psycopg.connect(**source) as conn:
                    original=inventory(conn)
                if len(original['tables'])!=MIN_EXPECTED_TABLES:
                    raise CutoverBlocked('original_restore_table_count')
                # Set the exact local-only proof expected by the guarded SQL.
                previous={n:os.environ.get(n) for n in (
                    'RECOVERY_VERIFY_CONTAINER','RECOVERY_BACKUP_ID',
                    'OWNER_FULL_RESCUE_VERIFIED')}
                try:
                    os.environ.update(RECOVERY_VERIFY_CONTAINER=container,
                                      RECOVERY_BACKUP_ID=args.backup_id,
                                      OWNER_FULL_RESCUE_VERIFIED='true')
                    compact=apply_only_to_owned_local_verifier(source)
                finally:
                    for name,value in previous.items():
                        if value is None:os.environ.pop(name,None)
                        else:os.environ[name]=value
                print('HOT_SEED_STAGE: isolated_board_compaction_verified',flush=True)
                with psycopg.connect(**source) as conn:
                    hot=inventory(conn)
                before={ (t['schema'],t['table']):t for t in original['tables']}
                for item in hot['tables']:
                    if item['table'] != 'board_editions' and item != before[(item['schema'],item['table'])]:
                        raise CutoverBlocked('other_table_content_changed')
                if hot['sequences']!=original['sequences'] or hot['large_objects']!=original['large_objects']:
                    raise CutoverBlocked('other_source_metadata_changed')
                hotdump=path/'hot.dump'
                _pg_dump(container,hotdump)
            destination=root/'encrypted-assets'
            stage='encrypted_hot_seed_and_second_pg18_restore'
            result=asyncio.run(_encrypted_seed_and_verify(hotdump,destination,key,
                                  args.backup_id,manifest,hot))
            print('HOT_SEED_OFFLINE_RESTORED_NO_PRODUCTION_IMPORT',flush=True)
            print(json.dumps(dict(encrypted_package=result,
                 local_compaction=compact,
                 source_database_modified=False,
                 free_database_created=False,
                 historical_read_through_verified=False,
                 source_row_content_verified=False,
                 eviction_authorized=False),sort_keys=True),flush=True)
    except CutoverBlocked as exc:
        # Only error codes manufactured in this module may be shown; never
        # propagate connection strings, encryption material or provider data.
        code=str(exc)
        safe=code if re.fullmatch('[a-z][a-z0-9_]{1,90}',code) else 'local_guard_blocked'
        print('HOT_SEED_INCOMPLETE stage='+stage+' reason='+safe+
              '; old source and original ZIP unchanged',flush=True)
        raise SystemExit(1) from None
    except Exception as exc:
        print('HOT_SEED_INCOMPLETE stage='+stage+' type='+type(exc).__name__+
              '; old source and original ZIP unchanged',flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':
    os.umask(0o077)
    main()
