"""Bounded, resumable encrypted board_editions export. Never deletes anything.

This is a lossless table archive, NOT a full operational database backup.
The initial key inventory fixes scope; resume rereads and compares every row.
Mutable tables require a separate single-snapshot design, not this protocol.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from sqlalchemy import text, bindparam
from src.archive.board_format import SCHEMA, MAX_BYTES, MAX_KEYS, canonical, sha, seal, unseal, records, frame
from src.archive.crypto import key_bytes
from src.archive.github import ReleaseArchive
from src.archive.source import snapshot

PREFIX = 'board-backup-v1-'
TABLE_SCHEMA = {'version':'varchar(64) primary key', 'league_id':'integer',
                'created_at':'timestamp with time zone', 'payload':'json'}


def validate_plan(plan, archive_id):
    if (plan.get('schema') != SCHEMA or plan.get('archive_id') != archive_id or
        plan.get('table') != 'board_editions' or plan.get('columns') != TABLE_SCHEMA):
        raise ValueError('Unsupported backup scope/schema')
    keys = plan.get('keys')
    if not isinstance(keys, list) or len(keys) > MAX_KEYS or keys != sorted(set(keys)):
        raise ValueError('Invalid key inventory')
    if any(not isinstance(k, str) or not re.fullmatch('[a-f0-9]{64}', k) for k in keys):
        raise ValueError('Invalid board key')
    if type(plan.get('rows_per_chunk')) is not int or not 1 <= plan['rows_per_chunk'] <= 128:
        raise ValueError('Invalid chunk size')
    if plan.get('row_count') != len(keys) or plan.get('keys_sha256') != sha(canonical(keys)):
        raise ValueError('Invalid key inventory digest')
    return plan


def groups(plan):
    size = plan['rows_per_chunk']
    for index, start in enumerate(range(0, len(plan['keys']), size)):
        yield f'chunk-{index:06d}.enc', plan['keys'][start:start+size]


class BoardSource:
    def __init__(self, connection, schema='public'):
        if not re.fullmatch('[a-z][a-z0-9_]{0,62}',schema): raise ValueError('Invalid source schema')
        self.connection, self.schema = connection, schema
        self.table = f'"{schema}"."board_editions"'

    async def identity(self):
        await self.connection.execute(text("SET LOCAL timezone='UTC'"))
        row = (await self.connection.execute(text("SELECT current_database(), to_regclass(:table)::oid"),{'table':self.table})).one()
        columns = (await self.connection.execute(text("SELECT column_name,data_type,character_maximum_length FROM information_schema.columns WHERE table_schema=:schema AND table_name='board_editions' ORDER BY ordinal_position"),{'schema':self.schema})).all()
        expected = [('version','character varying',64), ('league_id','integer',None),
                    ('created_at','timestamp with time zone',None), ('payload','json',None)]
        if [tuple(r) for r in columns] != expected: raise ValueError('Source schema changed; review required')
        return sha(canonical([list(row), [list(r) for r in columns]]))

    async def keys(self):
        rows = await self.connection.scalars(text(f'SELECT version FROM {self.table} ORDER BY version COLLATE "C" LIMIT :limit'), {'limit':MAX_KEYS+1})
        result = list(rows)
        if len(result)>MAX_KEYS: raise ValueError('Key inventory cap exceeded')
        return result

    async def read(self, keys):
        # A too-large individual row returns NULL, never an unbounded payload.
        query = text(f'SELECT CASE WHEN octet_length(row_to_json(b)::text) <= :cap THEN row_to_json(b)::text ELSE NULL END FROM {self.table} b WHERE version IN :keys ORDER BY version COLLATE "C"').bindparams(bindparam('keys', expanding=True))
        result = await self.connection.stream(query, {'keys':keys, 'cap':MAX_BYTES})
        raw = bytearray()
        try:
            async for row in result:
                if row[0] is None: raise ValueError('Individual row exceeds archive limit')
                line = frame(row[0].encode('utf-8'))
                if len(raw)+len(line)>MAX_BYTES: raise ValueError('Chunk byte cap exceeded; use fewer rows in a NEW backup')
                raw.extend(line)
        finally: await result.close()
        value = bytes(raw)
        records(value, keys)
        return value


class BoardStore(ReleaseArchive):
    """Private Release assets are checkpoints; never wildcard-upload or prune."""
    async def asset_list(self, release_id):
        assets = []
        for page in range(1, 102):
            batch = (await self.request('GET', f'/repos/{self.repo}/releases/{release_id}/assets?per_page=100&page={page}')).json()
            assets.extend(batch)
            if len(batch)<100: return assets
        raise ValueError('Asset inventory limit exceeded')

    async def open(self, archive_id, *, create=False):
        if not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', archive_id): raise ValueError('Invalid backup id')
        self.archive_id = archive_id
        meta = (await self.request('GET', f'/repos/{self.repo}')).json()
        if meta.get('private') is not True or meta.get('archived') or meta.get('full_name','').lower()!=self.repo.lower():
            raise ValueError('Verified private repository required')
        if create and meta.get('permissions',{}).get('push') is not True:
            raise ValueError('Destination write permission required')
        releases, _ = await self.inventory()
        total = 0
        for release in releases:
            release['assets'] = await self.asset_list(int(release['id']))
            total += sum(int(a['size']) for a in release['assets'])
        self.total = total
        self.release = next((r for r in releases if r['tag_name']==PREFIX+archive_id), None)
        if self.release is None:
            if not create: raise ValueError('Backup not found')
            if total >= self.budget: raise ValueError('Repository archive byte budget exhausted')
            self.release = (await self.request('POST', f'/repos/{self.repo}/releases', json={
                'tag_name':PREFIX+archive_id, 'name':PREFIX+archive_id, 'draft':True,
                'body':'Encrypted board_editions archive; incomplete until authenticated manifest and isolated restore verification.'})).json()
            self.release['assets'] = []
        assets = self.release['assets']
        self.assets = {a['name']:a for a in assets}
        if len(self.assets)!=len(assets): raise ValueError('Duplicate asset names')

    async def get(self, name):
        if name not in self.assets: return None
        return await self.download(self.assets[name])

    async def put(self, name, blob):
        if name not in {'plan.enc','manifest.enc'} and not re.fullmatch(r'chunk-[0-9]{6}\.enc',name):
            raise ValueError('Invalid archive asset name')
        # Also guard direct callers: this upload primitive never accepts raw data.
        unseal(blob,self.key,self.archive_id,name)
        if name in self.assets:
            if await self.get(name)!=blob: raise ValueError('Immutable asset conflict')
            return
        if not self.release.get('draft'): raise ValueError('Completed release cannot be extended')
        if len(blob)>MAX_BYTES+65536 or self.total+len(blob)>self.budget:
            raise ValueError('Archive byte budget exceeded')
        response = await self.client.post(f'https://uploads.github.com/repos/{self.repo}/releases/{int(self.release["id"])}/assets',
            params={'name':name}, headers={'Content-Type':'application/octet-stream'}, content=blob)
        if response.status_code != 201: raise ValueError('Upload uncertain; resume with the SAME backup id')
        asset = response.json()
        self.assets[name] = asset
        self.total += int(asset['size'])
        if await self.get(name)!=blob: raise ValueError('Read-after-write digest mismatch')

    async def finish(self):
        if self.release.get('draft'):
            await self.request('PATCH', f'/repos/{self.repo}/releases/{int(self.release["id"])}', json={'draft':False})
            self.release['draft'] = False


def chunk_meta(name, keys, raw, blob):
    return dict(name=name, rows=len(keys), first_key=keys[0], last_key=keys[-1],
        plaintext_bytes=len(raw), plaintext_sha256=sha(raw), encrypted_bytes=len(blob), encrypted_sha256=sha(blob))


async def export(source, store, key, archive_id, rows_per_chunk=8, max_new_chunks=64):
    key_bytes(key)
    if not 1 <= max_new_chunks <= 1000: raise ValueError('Invalid per-run chunk budget')
    plan_blob = await store.get('plan.enc')
    if plan_blob is None:
        keys = await source.keys()
        plan = dict(schema=SCHEMA, archive_id=archive_id, table='board_editions', columns=TABLE_SCHEMA,
            source_identity=await source.identity(), created_at=datetime.now(timezone.utc).isoformat(),
            keys=keys, row_count=len(keys), keys_sha256=sha(canonical(keys)), rows_per_chunk=rows_per_chunk)
        validate_plan(plan, archive_id)
        plan_blob = seal(canonical(plan), key, archive_id, 'plan.enc')
        await store.put('plan.enc', plan_blob)
    else:
        plan = validate_plan(json.loads(unseal(plan_blob,key,archive_id,'plan.enc')), archive_id)
    if plan['source_identity'] != await source.identity(): raise ValueError('Source identity changed')
    chunks, uploaded = [], 0
    for name, keys in groups(plan):
        raw = await source.read(keys)
        records(raw, keys)
        existing = await store.get(name)
        if existing is not None:
            if unseal(existing,key,archive_id,name)!=raw: raise ValueError('Source/archived content changed; resume refused')
            blob = existing
        else:
            if uploaded >= max_new_chunks:
                return dict(status='incomplete', reason='per_run_chunk_budget', restore_verified=False,
                    resumable=True, uploaded_chunks=uploaded, expected_rows=plan['row_count'])
            blob = seal(raw,key,archive_id,name)
            await store.put(name,blob)
            uploaded += 1
        chunks.append(chunk_meta(name,keys,raw,blob))
    manifest = dict(schema=SCHEMA, archive_id=archive_id, plan_sha256=sha(plan_blob),
        table='board_editions', row_count=plan['row_count'], chunks=chunks,
        plaintext_bytes=sum(c['plaintext_bytes'] for c in chunks), encrypted_bytes=sum(c['encrypted_bytes'] for c in chunks))
    existing = await store.get('manifest.enc')
    if existing:
        if json.loads(unseal(existing,key,archive_id,'manifest.enc'))!=manifest: raise ValueError('Manifest conflict')
    else:
        await store.put('manifest.enc',seal(canonical(manifest),key,archive_id,'manifest.enc'))
    report = await verify(store,key,archive_id)
    if report['status']!='verified': raise ValueError('Incomplete archive')
    await store.finish()
    return report


async def verify(store, key, archive_id, destination=None):
    """Download/authenticate every chunk and restore exact bytes in isolated SQLite.

    No source connection, model execution, SQL dump execution or production writes.
    """
    plan_blob = await store.get('plan.enc')
    if plan_blob is None: return dict(status='incomplete', missing=['plan.enc'], restore_verified=False)
    plan = validate_plan(json.loads(unseal(plan_blob,key,archive_id,'plan.enc')), archive_id)
    expected = list(groups(plan))
    allowed = {'plan.enc','manifest.enc'} | {name for name,_ in expected}
    if set(store.assets)-allowed: raise ValueError('Unexpected archive assets')
    missing = [name for name,_ in expected if name not in store.assets]
    if 'manifest.enc' not in store.assets: missing.append('manifest.enc')
    if missing: return dict(status='incomplete', missing=missing, expected_rows=plan['row_count'], restore_verified=False)
    manifest = json.loads(unseal(await store.get('manifest.enc'),key,archive_id,'manifest.enc'))
    with tempfile.TemporaryDirectory(prefix='board-restore-') as temporary:
        target = Path(destination) if destination else Path(temporary)/'boards.sqlite'
        fd = os.open(target, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        os.close(fd)
        connection = sqlite3.connect(target)
        try:
            connection.execute('CREATE TABLE board_editions_archive (version TEXT PRIMARY KEY, row_json BLOB NOT NULL)')
            chunks = []
            for name, keys in expected:
                blob = await store.get(name)
                raw = unseal(blob,key,archive_id,name)
                lines = records(raw,keys)
                connection.executemany('INSERT INTO board_editions_archive VALUES (?,?)', zip(keys,lines))
                # Read back from the restored database, not just from memory.
                restored = b''.join(frame(connection.execute('SELECT row_json FROM board_editions_archive WHERE version=?',(k,)).fetchone()[0]) for k in keys)
                if restored!=raw: raise ValueError('Restore content mismatch')
                chunks.append(chunk_meta(name,keys,raw,blob))
            count = connection.execute('SELECT count(*) FROM board_editions_archive').fetchone()[0]
            wanted = dict(schema=SCHEMA,archive_id=archive_id,plan_sha256=sha(plan_blob),table='board_editions',
                row_count=count,chunks=chunks,plaintext_bytes=sum(c['plaintext_bytes'] for c in chunks),
                encrypted_bytes=sum(c['encrypted_bytes'] for c in chunks))
            if manifest!=wanted or count!=plan['row_count']: raise ValueError('Manifest/count/content integrity mismatch')
            for operation in ('UPDATE','DELETE'):
                connection.execute(f"CREATE TRIGGER reject_{operation} BEFORE {operation} ON board_editions_archive BEGIN SELECT RAISE(ABORT,'immutable archive'); END")
            connection.commit()
        except BaseException:
            connection.close()
            target.unlink(missing_ok=True)  # Only our exclusively-created, failed restore.
            raise
        finally: connection.close()
    return dict(status='verified',table='board_editions',rows=count,chunks=len(chunks),missing=[],
        restore_verified=True,full_database_backup=False,plaintext_bytes=manifest['plaintext_bytes'],
        encrypted_bytes=manifest['encrypted_bytes'],source_space_reclaimed=0)


async def run(args):
    if os.getenv('BOARD_BACKUP_ENABLED')!='true': raise ValueError('Explicit backup activation required')
    key = os.environ['BOARD_BACKUP_ENCRYPTION_KEY'];key_bytes(key)
    store = BoardStore(os.environ['BOARD_BACKUP_REPOSITORY'],os.environ['BOARD_BACKUP_TOKEN'],key,
        budget_bytes=int(os.environ['BOARD_BACKUP_BUDGET_BYTES']),keep_days=3650)
    try:
        await store.open(args.id,create=args.command=='export')
        if args.command=='export':
            # Dedicated SELECT-only principal, TLS hostname verification, one
            # repeatable-read transaction. Never imports/initializes the app DB.
            async with snapshot(os.environ['BOARD_BACKUP_DATABASE_URL']) as connection:
                result = await export(BoardSource(connection),store,key,args.id,args.rows_per_chunk,args.max_new_chunks)
        else: result = await verify(store,key,args.id,args.destination)
        print(json.dumps(result,sort_keys=True))
        if result['status']!='verified': return 2
        return 0
    finally: await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['export','verify','report','restore'])
    parser.add_argument('--id',required=True)
    parser.add_argument('--rows-per-chunk',type=int,default=8)
    parser.add_argument('--max-new-chunks',type=int,default=64)
    parser.add_argument('--destination')
    args = parser.parse_args()
    if args.command=='restore' and not args.destination: parser.error('restore requires a NEW destination')
    if args.command!='restore' and args.destination: parser.error('destination is only allowed for restore')
    try: result = asyncio.run(run(args))
    except Exception:
        print('Backup failed; no verified production backup claimed. Check protected configuration, capacity, or archive integrity.')
        result = 1
    raise SystemExit(result)


if __name__=='__main__': main()
