"""Disposable synthetic PG18 only; never use a URL from the environment."""
import base64
import json
import os
from zipfile import ZipFile
import psycopg
import pytest
from src.archive import offline_recovery as r
from src.archive.recovery_format import seal, sha, SCHEMA

pytestmark = pytest.mark.skipif(os.getenv('RUN_OFFLINE_RECOVERY_DOCKER') != '1', reason='explicit disposable Docker PG18 test')


def test_two_lossless_restores_with_immutable_trigger(tmp_path, monkeypatch, capsys):
    raw = tmp_path/'fixture.dump'
    with r.ephemeral_pg18(5433, 'synthetic-fixture') as (container, params):
        with psycopg.connect(**params) as conn:
            conn.execute('CREATE TABLE board_editions(version text PRIMARY KEY, payload jsonb)')
            conn.execute("INSERT INTO board_editions VALUES ('old', '{\"history\":true}'),('new','{}')")
            conn.execute('CREATE TABLE board_heads(version text REFERENCES board_editions(version))')
            conn.execute("INSERT INTO board_heads VALUES ('new')")
            conn.execute('CREATE TABLE matches(id serial PRIMARY KEY, payload text)')
            conn.execute("INSERT INTO matches(payload) VALUES ('old match')")
            conn.execute("SELECT lo_from_bytea(0,decode('abcdef','hex'))")
            conn.execute("CREATE FUNCTION immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'secret'; END $$")
            conn.execute('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON board_editions FOR EACH ROW EXECUTE FUNCTION immutable_history()')
        expected = r.local_inventory(params)
        with psycopg.connect(**params) as conn:
            with pytest.raises(psycopg.errors.RaiseException) as error:
                conn.execute("DELETE FROM board_editions WHERE version='old'")
            assert error.value.sqlstate == 'P0001'
            conn.rollback()
        r._pg_dump(container, raw)
    key = base64.b64encode(bytes(32)).decode()
    keyfile = tmp_path/'key'; keyfile.write_text(key)
    payload = raw.read_bytes()
    chunk = seal(payload, key, r.ORIGINAL_ID, 'chunk-000000.enc')
    manifest = dict(schema=SCHEMA,backup_id=r.ORIGINAL_ID,format='pg_dump_custom',
                    rescue_only=True,source_row_content_verified=False,dump_sha256=sha(payload),
                    chunks=[dict(name='chunk-000000.enc',bytes=len(chunk),sha256=sha(chunk),
                                 plaintext_bytes=len(payload),plaintext_sha256=sha(payload))])
    receipt = dict(backup_id=r.ORIGINAL_ID,restore_verified=True,source_row_content_verified=False,
                   row_content_verified=False,source_deleted_rows=0,eviction_authorized=False)
    archive = tmp_path/'synthetic.zip'
    with ZipFile(archive, 'x') as z:
        z.writestr('chunk-000000.enc',chunk)
        z.writestr('manifest.enc',seal(json.dumps(manifest).encode(),key,r.ORIGINAL_ID,'manifest.enc'))
        z.writestr('receipt.enc',seal(json.dumps(receipt).encode(),key,r.ORIGINAL_ID,'receipt.enc'))
        z.writestr('summary.json',json.dumps(receipt))
    monkeypatch.setattr(r,'ORIGINAL_ZIP_SHA256',r.sha_file(archive))
    monkeypatch.setattr(r,'ORIGINAL_DUMP_SHA256',sha(payload))
    output=tmp_path/'result'
    monkeypatch.setattr('sys.argv',['offline','--zip',str(archive),'--key-file',str(keyfile),'--output-root',str(output)])
    r.main()
    report=json.loads((output/'verification.json').read_text())
    assert report['inventory'] == json.loads(json.dumps(expected))
    assert report['independent_local_restore_verified'] is True
    assert report['cleanup_authorized'] is False
    assert report['live_source_parity_verified'] is False
    assert r.sha_file(archive) == r.ORIGINAL_ZIP_SHA256
    assert 'secret' not in capsys.readouterr().out
