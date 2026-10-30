"""Synthetic bytes/fixtures only. Real PG test uses existing disposable CI services."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid
import pytest
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from src.archive.full_recovery import connection_parameters,HOST,DB,backup,inventory
from src.archive.recovery_format import seal,unseal,sha,CHUNK

KEY=base64.urlsafe_b64encode(b'r'*32).decode()


def test_source_pin_rejects_old_database_without_echoing_credentials():
    good=f'postgresql://reader:secret@{HOST}/{DB}?sslmode=require'
    assert connection_parameters(good,source=True)['dbname']==DB
    for value in (good.replace(DB,'kuponanaliziivv7'),good.replace(HOST,'other.invalid'),good+'&options=unsafe',good.replace('require','disable')):
        with pytest.raises(ValueError,match='approved_source_required') as error:connection_parameters(value,source=True)
        assert 'secret' not in str(error.value)
    with pytest.raises(ValueError,match='isolated_restore'):connection_parameters(good,source=False)
    assert connection_parameters('postgresql://v:test@127.0.0.1:5433/postgres',source=False)['port']==5433


def test_recovery_authenticated_context_corruption_and_bound():
    blob=seal(b'synthetic byte fixture',KEY,'fixture','chunk-000000.enc')
    assert unseal(blob,KEY,'fixture','chunk-000000.enc')==b'synthetic byte fixture'
    for bad,ident,name in [(blob[:-1]+bytes([blob[-1]^1]),'fixture','chunk-000000.enc'),(blob,'other','chunk-000000.enc'),(blob,'fixture','manifest.enc')]:
        with pytest.raises(Exception):unseal(bad,KEY,ident,name)
    with pytest.raises(ValueError,match='chunk_budget'):seal(b'x'*(CHUNK+1),KEY,'fixture','chunk-000000.enc')


class MemoryStore:
    key=KEY;repo='synthetic/private'
    def __init__(self):self.assets={};self.release={'id':1};self.published=False;self.corrupt=False
    async def open(self,backup_id):self.backup_id=backup_id
    async def put(self,name,raw):
        blob=seal(raw,KEY,self.backup_id,name);ident=len(self.assets)+1;self.assets[ident]=blob
        return dict(name=name,asset_id=ident,bytes=len(blob),sha256=sha(blob),plaintext_sha256=sha(raw),plaintext_bytes=len(raw))
    async def download(self,asset):
        blob=self.assets[asset['id']]
        return blob[:-1]+bytes([blob[-1]^1]) if self.corrupt and asset['id']==1 else blob
    async def request(self,method,path,**kwargs):
        assert method=='PATCH' and kwargs['json']['draft'] is False
        self.published=True


async def test_remote_corruption_never_publishes_success(tmp_path,monkeypatch):
    import src.archive.full_recovery as module
    def dump(params,directory):
        (Path(directory)/'full.dump').write_bytes(b'synthetic dump')
        return dict(inventory={'tables':[]})
    def restore(*args):raise AssertionError('Corrupt input must not reach restore')
    monkeypatch.setattr(module,'dump_snapshot',dump);monkeypatch.setattr(module,'restore_verify',restore)
    store=MemoryStore();store.corrupt=True
    with pytest.raises(ValueError,match='ciphertext_checksum'):await backup({}, {},store,'fixture',tmp_path)
    assert store.published is False


async def test_real_postgres_full_snapshot_remote_restore_counts(tmp_path):
    source_url=os.getenv('TEST_POSTGRES_URL');target_url=os.getenv('TEST_RECOVERY_VERIFY_URL')
    if not source_url or not target_url:pytest.skip('Disposable PostgreSQL services unavailable')
    p=make_url(source_url)
    source=dict(host=p.host,port=p.port or 5432,dbname=p.database,user=p.username,password=p.password,sslmode='disable')
    target=connection_parameters(target_url,source=False)
    fixture='recovery_fixture_'+uuid.uuid4().hex
    restored_schemas=set()
    try:
        with psycopg.connect(**source) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(fixture)))
            conn.execute(sql.SQL('CREATE TABLE {}.unknown_future_table (id bigint generated always as identity primary key, exact numeric, payload json)').format(sql.Identifier(fixture)))
            conn.execute(sql.SQL("INSERT INTO {}.unknown_future_table(exact,payload) VALUES (0.12345678901234567890123456789, %s)").format(sql.Identifier(fixture)),('{"synthetic":true,"unicode":"Türkçe"}',))
        store=MemoryStore()
        report=await backup(source,target,store,'ci-fixture',tmp_path)
        assert store.published and report['restore_verified'] and report['source_deleted_rows']==0
        with psycopg.connect(**source) as conn:before=inventory(conn)
        with psycopg.connect(**target) as conn:
            after=inventory(conn);restored_schemas={r['schema'] for r in after['tables']}
            assert before==after
            assert str(conn.execute(sql.SQL('SELECT exact FROM {}.unknown_future_table').format(sql.Identifier(fixture))).fetchone()[0])=='0.12345678901234567890123456789'
    finally:
        # Only fixture namespace on source; destination is the explicit disposable
        # CI verifier at localhost:5433/postgres, never a Render address.
        with psycopg.connect(**source) as conn:conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(fixture)))
        with psycopg.connect(**target) as conn:
            for schema in restored_schemas:
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                if schema=='public':conn.execute('CREATE SCHEMA public')
