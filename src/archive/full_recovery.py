"""Manual, complete logical recovery backup; no source writes or retention deletion."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit,unquote,parse_qs
import psycopg
from psycopg import sql
from .crypto import key_bytes
from .github import ReleaseArchive
from .board_backup import BoardStore
from .recovery_format import SCHEMA,CHUNK,MAX_DUMP,canonical,sha,seal,unseal

REPO='donozoyout-ux/kuponanalizii-data-archive'
HOST='dpg-dbgdegou01pc738m3mmg-a.frankfurt-postgres.render.com'
DB='kuponanaliziivv755'
PREFIX='postgres-recovery-v1-'


def connection_parameters(url,*,source):
    try:
        p=urlsplit(url);q=parse_qs(p.query,keep_blank_values=True)
        if p.scheme not in {'postgres','postgresql'} or p.fragment or set(q)-{'sslmode','channel_binding'}:raise ValueError()
        if not p.username or not p.password:raise ValueError()
        database=unquote(p.path.lstrip('/'))
        if source:
            if p.hostname!=HOST or database!=DB or p.port not in (None,5432):raise ValueError()
            ssl=q.get('sslmode',['require'])
            if len(ssl)!=1 or ssl[0] not in {'require','verify-ca','verify-full'}:raise ValueError()
        else:
            if p.hostname not in {'localhost','127.0.0.1','::1'} or database!='postgres' or p.port!=5433 or q:raise ValueError()
            ssl=['disable']
        result=dict(host=p.hostname,port=p.port or 5432,dbname=database,user=unquote(p.username),password=unquote(p.password),sslmode=ssl[0],connect_timeout=10)
        if source:result.update(sslmode='verify-full',sslrootcert='system')
        if 'channel_binding' in q:
            if len(q['channel_binding'])!=1 or q['channel_binding'][0] not in {'require','prefer','disable'}:raise ValueError()
            result['channel_binding']=q['channel_binding'][0]
        return result
    except Exception:raise ValueError('approved_source_required' if source else 'isolated_restore_target_required') from None


def inventory(conn):
    # No table-name allowlist: unknown future application tables are included.
    relations=conn.execute("SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','m','f') ORDER BY 1,2").fetchall()
    if any(kind=='f' for _,_,kind in relations):raise ValueError('foreign_tables_require_separate_backup')
    if len(relations)>1000:raise ValueError('table_inventory_budget')
    tables=[]
    for namespace,name,kind in relations:
        count=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()[0]
        tables.append(dict(schema=namespace,table=name,kind=kind,rows=count))
    sequences=[]
    for namespace,name in conn.execute("SELECT schemaname,sequencename FROM pg_sequences WHERE schemaname !~ '^pg_' ORDER BY 1,2"):
        value,called=conn.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()
        sequences.append(dict(schema=namespace,name=name,value=value,called=called))
    large_objects=conn.execute('SELECT count(*) FROM pg_largeobject_metadata').fetchone()[0]
    return dict(tables=tables,sequences=sequences,large_objects=large_objects)


def client(tool,params,directory,*args,readonly=False):
    # Credentials are environment entries, never process arguments or logs.
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    keys=dict(PGHOST='host',PGPORT='port',PGDATABASE='dbname',PGUSER='user',PGPASSWORD='password',PGSSLMODE='sslmode',PGSSLROOTCERT='sslrootcert',PGCHANNELBINDING='channel_binding')
    passed=[]
    for name,key in keys.items():
        if key in params:env[name]=str(params[key]);passed+=['--env',name]
    env['PGOPTIONS']='-c default_transaction_read_only=on -c statement_timeout=600000 -c lock_timeout=2000' if readonly else '-c statement_timeout=600000 -c lock_timeout=5000'
    passed+=['--env','PGOPTIONS']
    command=['docker','run','--rm','--network','host',*passed,'-v',str(Path(directory).resolve())+':/backup','postgres:18',tool,*args]
    try:
        subprocess.run(command,env=env,capture_output=True,check=True,timeout=1200)
    except Exception:raise ValueError(tool+'_failed') from None


def dump_snapshot(params,directory):
    source={**params,'options':'-c default_transaction_read_only=on -c statement_timeout=600000 -c lock_timeout=2000'}
    with psycopg.connect(**source,autocommit=True) as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if conn.execute('SHOW transaction_read_only').fetchone()[0]!='on':raise ValueError('source_not_readonly')
        if conn.execute('SELECT current_database()').fetchone()[0]!=params['dbname']:raise ValueError('source_identity')
        version=int(conn.execute('SHOW server_version_num').fetchone()[0])
        if not 160000<=version<190000:raise ValueError('unsupported_postgres_version')
        snapshot=conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
        before=inventory(conn)
        client('pg_dump',params,directory,'--format=custom','--compress=0','--no-owner','--no-acl','--snapshot='+snapshot,'--file=/backup/full.dump',readonly=True)
        after=inventory(conn)
        if before!=after:raise ValueError('source_sequences_changed_retry_with_writers_paused')
        stamp=conn.execute('SELECT transaction_timestamp()::text').fetchone()[0]
        conn.execute('ROLLBACK')
    path=Path(directory)/'full.dump'
    if not 0<path.stat().st_size<=MAX_DUMP:raise ValueError('dump_byte_budget')
    return dict(source_database=params['dbname'],source_host=params['host'],server_version=version,snapshot_at=stamp,inventory=before)


def restore_verify(params,directory,expected):
    with psycopg.connect(**params) as conn:
        existing=inventory(conn)
        if existing['tables'] or existing['sequences'] or existing['large_objects']:raise ValueError('restore_target_not_empty')
    client('pg_restore',params,directory,'--exit-on-error','--single-transaction','--no-owner','--no-acl','--dbname=postgres','/backup/recovered.dump')
    with psycopg.connect(**params) as conn:
        actual=inventory(conn)
        if actual!=expected:raise ValueError('restored_inventory_mismatch')
        # Real SQL read-through of the application's original JSON, not fixtures.
        checks={}
        names={(t['schema'],t['table']) for t in expected['tables']}
        for name in ('matches','board_editions','prediction_snapshots','prediction_publications','prediction_settlements','learning_reports'):
            if ('public',name) in names:
                row=conn.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t LIMIT 1').format(sql.Identifier('public',name))).fetchone()
                if row:json.loads(row[0])
                checks[name]='readable' if row else 'empty_verified'
    return dict(inventory=actual,read_through=checks)


class RecoveryStore(ReleaseArchive):
    asset_list=BoardStore.asset_list
    async def open(self,backup_id):
        if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',backup_id):raise ValueError('invalid_backup_id')
        self.backup_id=backup_id
        releases,_=await self.inventory();total=0
        for release in releases:
            assets=await self.asset_list(release['id']);total+=sum(a['size'] for a in assets)
            if release['tag_name']==PREFIX+backup_id:raise ValueError('backup_id_already_exists_use_new_snapshot_id')
        self.total=total
        if total>=self.budget:raise ValueError('repository_budget')
        self.release=(await self.request('POST',f'/repos/{self.repo}/releases',json=dict(tag_name=PREFIX+backup_id,name=PREFIX+backup_id,draft=True,body='Incomplete encrypted full recovery backup; not restore verified.'))).json()
    async def put(self,name,raw):
        if name not in {'manifest.enc','receipt.enc'} and not re.fullmatch(r'chunk-[0-9]{6}\.enc',name):raise ValueError('asset_name')
        blob=seal(raw,self.key,self.backup_id,name)
        if self.total+len(blob)>self.budget:raise ValueError('repository_budget')
        response=await self.client.post(f'https://uploads.github.com/repos/{self.repo}/releases/{self.release["id"]}/assets',params={'name':name},headers={'Content-Type':'application/octet-stream'},content=blob)
        if response.status_code!=201:raise ValueError('upload_uncertain_draft_retained')
        asset=response.json();self.total+=asset['size']
        fetched=await self.download(asset)
        if sha(fetched)!=sha(blob) or unseal(fetched,self.key,self.backup_id,name)!=raw:raise ValueError('uploaded_chunk_mismatch')
        return dict(name=name,asset_id=asset['id'],bytes=len(blob),sha256=sha(blob),plaintext_sha256=sha(raw),plaintext_bytes=len(raw))


async def backup(source_params,restore_params,store,backup_id,directory):
    print('RECOVERY_STAGE: readonly_snapshot_started',flush=True)
    evidence=await asyncio.to_thread(dump_snapshot,source_params,directory)
    print('RECOVERY_STAGE: readonly_snapshot_complete',flush=True)
    await store.open(backup_id)
    chunks=[];digest=hashlib.sha256()
    with (Path(directory)/'full.dump').open('rb') as source:
        for index in range(MAX_DUMP//CHUNK+1):
            raw=source.read(CHUNK)
            if not raw:break
            digest.update(raw);chunks.append(await store.put(f'chunk-{index:06d}.enc',raw))
    manifest=dict(schema=SCHEMA,backup_id=backup_id,**evidence,chunks=chunks,dump_sha256=digest.hexdigest(),format='pg_dump_custom',owner_acl_included=False)
    pin=await store.put('manifest.enc',canonical(manifest))
    # Reconstruct ONLY from GitHub-downloaded authenticated chunks; the original
    # local dump is not the restore input.
    remote=json.loads(unseal(await store.download({'id':pin['asset_id'],'size':pin['bytes']}),store.key,backup_id,'manifest.enc'))
    if remote!=manifest:raise ValueError('remote_manifest_mismatch')
    restored_hash=hashlib.sha256()
    with (Path(directory)/'recovered.dump').open('xb') as out:
        for item in remote['chunks']:
            blob=await store.download({'id':item['asset_id'],'size':item['bytes']})
            if sha(blob)!=item['sha256']:raise ValueError('ciphertext_checksum')
            raw=unseal(blob,store.key,backup_id,item['name'])
            if sha(raw)!=item['plaintext_sha256'] or len(raw)!=item['plaintext_bytes']:raise ValueError('plaintext_checksum')
            out.write(raw);restored_hash.update(raw)
    if restored_hash.hexdigest()!=remote['dump_sha256']:raise ValueError('dump_checksum')
    print('RECOVERY_STAGE: remote_bytes_verified_restore_started',flush=True)
    restored=await asyncio.to_thread(restore_verify,restore_params,directory,remote['inventory'])
    receipt=dict(schema=SCHEMA,backup_id=backup_id,restore_verified=True,manifest=pin,**restored,source_deleted_rows=0,eviction_authorized=False)
    receipt_pin=await store.put('receipt.enc',canonical(receipt))
    summary=dict(schema=SCHEMA,backup_id=backup_id,restore_verified=True,manifest=pin,receipt=receipt_pin,tables=len(evidence['inventory']['tables']),rows=sum(t['rows'] for t in evidence['inventory']['tables']),chunks=len(chunks),source_deleted_rows=0)
    await store.request('PATCH',f'/repos/{store.repo}/releases/{store.release["id"]}',json={'draft':False,'body':canonical(summary).decode()})
    return dict(summary,release_id=store.release['id'],release_url=store.release.get('html_url'))


async def main():
    source=connection_parameters(os.getenv('ARCHIVE_DATABASE_URL') or os.getenv('RENDER_DATABASE_URL',''),source=True)
    print('RECOVERY_PREFLIGHT: source_identity_accepted',flush=True)
    restore=connection_parameters(os.environ['RECOVERY_VERIFY_URL'],source=False)
    print('RECOVERY_PREFLIGHT: isolated_restore_target_accepted',flush=True)
    # Trailing paste whitespace is harmless; never print the key or a fingerprint.
    key=os.environ['ARCHIVE_ENCRYPTION_KEY'].strip()
    try:
        key_bytes(key)
    except ValueError:
        import base64
        try:
            decoded=base64.b64decode(key,altchars=b'-_',validate=True)
        except Exception:
            raise ValueError('archive_encryption_key_not_base64') from None
        if len(decoded)!=32:
            raise ValueError('archive_encryption_key_not_32_bytes') from None
        raise ValueError('archive_encryption_key_invalid') from None
    print('RECOVERY_PREFLIGHT: encryption_key_format_accepted',flush=True)
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}',os.getenv('RECOVERY_BACKUP_ID','')):raise ValueError('invalid_backup_id')
    if os.getenv('RECOVERY_AUTHORIZED')!='true':raise ValueError('manual_authorization_required')
    store=RecoveryStore(REPO,os.environ['ARCHIVE_GITHUB_TOKEN'],key,budget_bytes=2*MAX_DUMP,keep_days=3650)
    print('RECOVERY_PREFLIGHT: required_archive_settings_present',flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='kupon-recovery-') as directory:
            report=await backup(source,restore,store,os.environ['RECOVERY_BACKUP_ID'],directory)
            print(canonical(report).decode())
    finally:await store.close()


if __name__=='__main__':
    os.umask(0o077)
    try:asyncio.run(main())
    except Exception as error:
        # Never echo DB URLs, provider payloads, libpq errors or secret values.
        safe=str(error) if isinstance(error,ValueError) and re.fullmatch('[a-z_]+',str(error)) else 'recovery_failed'
        print('RECOVERY_INCOMPLETE: '+safe,flush=True)
        raise SystemExit(1) from None
