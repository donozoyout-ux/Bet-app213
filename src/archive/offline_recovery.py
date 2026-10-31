"""Lossless owner-operated restore; no compaction or remote database input."""
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
from .recovery_format import CHUNK, MAX_DUMP, SCHEMA, sha, unseal
from .crypto import key_bytes
from .recovery_inventory import inventory

class CutoverBlocked(RuntimeError):
    pass

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


def _docker(args, *, env=None, timeout=180):
    try:
        return subprocess.run(['docker',*args],env=env,check=True,capture_output=True,
                              text=True,timeout=timeout).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # pg_restore VERBOSITY=verbose reports SQLSTATE; never echo SQL/stderr.
        match = re.search(r'\bERROR:\s+([0-9A-Z]{5}):', exc.stderr or '')
        error = CutoverBlocked('isolated_docker_operation_failed')
        error.sqlstate = match[1] if match else None
        raise error from None
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
        cid=_docker(['run','--pull=never','-d','--rm','-p',f'127.0.0.1:{port}:5432',
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
    _docker(['exec','--env','PGOPTIONS=-c log_error_verbosity=verbose',container,'pg_restore','--exit-on-error','--single-transaction',
             '--no-owner','--no-acl','-U','postgres','-d','postgres',
             '/tmp/owned-hot-recovery.dump'],timeout=1200)


def _pg_dump(container, local_file):
    _docker(['exec',container,'pg_dump','-U','postgres','-d','postgres',
             '--format=custom','--compress=6','--no-owner','--no-acl',
             '--file=/tmp/owned-hot-seed.dump'],timeout=1200)
    _docker(['cp',container+':/tmp/owned-hot-seed.dump',str(local_file)],timeout=300)
    if not 0<local_file.stat().st_size<=MAX_DUMP:
        raise CutoverBlocked('hot_seed_dump_budget')


def safe_failure(stage, exc):
    state = getattr(exc, 'sqlstate', None)
    state = state if isinstance(state, str) and re.fullmatch('[0-9A-Z]{5}', state) else 'unavailable'
    return f'RECOVERY_INCOMPLETE stage={stage} sqlstate={state}'


def local_inventory(params):
    # Generated by ephemeral_pg18, never accepted from URL/env/CLI.
    with psycopg.connect(**params) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='120s'")
        if not 180000 <= int(conn.execute('SHOW server_version_num').fetchone()[0]) < 190000:
            raise CutoverBlocked('postgres18_required')
        return inventory(conn)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--output-root', required=True)
    args = parser.parse_args()
    stage = 'key_validation'
    try:
        key = Path(args.key_file).read_text(encoding='utf-8-sig').strip()
        key_bytes(key)
        stage = 'new_private_output'
        root = Path(args.output_root).resolve()
        # Windows wrapper must create an owner-only ACL before Python runs.
        if os.name == 'nt':
            if not root.is_dir() or any(root.iterdir()):
                raise CutoverBlocked('empty_private_wrapper_directory_required')
        else:
            root.mkdir(mode=0o700)
        owner = 'verify-' + secrets.token_hex(12)
        with tempfile.TemporaryDirectory(dir=root) as private:
            dump = Path(private)/'original.dump'
            stage = 'original_zip_authentication'
            manifest = load_offline_rescue(args.zip, key, dump)
            stage = 'first_pg18_start'
            with ephemeral_pg18(5433, owner) as (container, params):
                stage = 'first_pg18_restore'
                _restore(container, dump)
                stage = 'first_inventory'
                first = local_inventory(params)
            # Reconstruct again from authenticated encrypted parts, not a pruned dump.
            dump.unlink()
            stage = 'second_zip_authentication'
            load_offline_rescue(args.zip, key, dump)
            stage = 'second_pg18_start'
            with ephemeral_pg18(5434, owner) as (container, params):
                stage = 'second_pg18_restore'
                _restore(container, dump)
                stage = 'second_inventory'
                second = local_inventory(params)
            stage = 'full_content_parity'
            if first != second:
                raise CutoverBlocked('independent_restore_mismatch')
            report = dict(zip_sha256=ORIGINAL_ZIP_SHA256, dump_sha256=ORIGINAL_DUMP_SHA256,
                          manifest_sha256=sha(json.dumps(manifest, sort_keys=True).encode()),
                          inventory=first, independent_local_restore_verified=True,
                          live_source_parity_verified=False, github_readthrough_verified=False,
                          cleanup_authorized=False, compaction='not_performed')
            with (root/'verification.json').open('x', encoding='utf-8') as output:
                json.dump(report, output, sort_keys=True, indent=2)
        print('LOCAL_RESTORE_VERIFIED source_parity=false cleanup_authorized=false')
    except Exception as exc:
        print(safe_failure(stage, exc), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    os.umask(0o077)
    main()


