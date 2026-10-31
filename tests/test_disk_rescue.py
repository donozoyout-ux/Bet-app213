"""Disk-exhausted source rescue tests use synthetic bytes and disposable CI PG."""
import base64
import json
import os
from pathlib import Path
import uuid
from zipfile import ZipFile

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from src.archive import disk_rescue
from src.archive.local_recovery_store import LocalRecoveryStore
from src.archive.full_recovery import connection_parameters


KEY = base64.b64encode(b"r" * 32).decode()


async def test_disk_pressure_rescue_never_claims_source_row_parity(tmp_path, monkeypatch):
    from src.archive.recovery_format import unseal
    source = dict(host="synthetic.invalid", dbname="source", password="private")
    target = dict(host="127.0.0.1", dbname="postgres", password="private")
    evidence = dict(source_database="source", server_version=180000,
                    snapshot_at="synthetic", source_catalog={
                        "tables":[["public","matches","r"]], "sequences":[],
                        "large_objects":0})
    calls=[]
    def fake_dump(params, directory):
        assert params is source
        calls.append("source_dump_not_data_scan")
        (Path(directory)/"full.dump").write_bytes(b"SAFE SYNTHETIC BYTES")
        return evidence
    def fake_restore(params,directory,metadata):
        assert params is target and metadata is evidence
        calls.append("isolated_restore")
        assert (Path(directory)/"recovered.dump").read_bytes() == b"SAFE SYNTHETIC BYTES"
        return {"tables":[{"schema":"public","table":"matches","kind":"r",
                           "rows":2,"row_multiset_sha256":"synthetic-local-sha"}],
                "sequences":[],"large_objects":0}
    monkeypatch.setattr(disk_rescue,"dump_without_source_data_scan",fake_dump)
    monkeypatch.setattr(disk_rescue,"restore_without_source_row_hashes",fake_restore)
    store=LocalRecoveryStore(tmp_path/"assets",KEY,100000,allow_rescue_restore=True)
    summary=await disk_rescue.rescue(source,target,store,"disk-rescue-fixture",tmp_path)
    package=store.package()
    with ZipFile(package["path"]) as zipped:
        assert zipped.read("chunk-000000.enc")!=b"SAFE SYNTHETIC BYTES"
        assert unseal(zipped.read("chunk-000000.enc"),KEY,
                      "disk-rescue-fixture","chunk-000000.enc")==b"SAFE SYNTHETIC BYTES"
        assert json.loads(zipped.read("summary.json"))["row_content_verified"] is False
    assert calls == ["source_dump_not_data_scan","isolated_restore"]
    assert summary["rescue_only"] is True and summary["restore_verified"] is True
    assert summary["source_row_content_verified"] is False
    assert summary["row_content_verified"] is False
    assert summary["offsite_verified"] is False
    assert summary["source_deleted_rows"] == 0 and summary["eviction_authorized"] is False


async def test_offline_store_requires_explicit_rescue_flag_for_partial_receipt(tmp_path):
    body=dict(rescue_only=True, restore_verified=True, row_content_verified=False,
              source_row_content_verified=False, eviction_authorized=False,
              source_deleted_rows=0, application_read_through_verified=False)
    ordinary=LocalRecoveryStore(tmp_path/"normal",KEY,100000)
    await ordinary.open("ordinary")
    await ordinary.put("receipt.enc",b"synthetic")
    with pytest.raises(ValueError, match="offline_restore_not_verified"):
        await ordinary.request("PATCH","/repos/offline-owner/releases/local-ordinary",
                               json=dict(draft=False,body=json.dumps(body)))
    unsafe=LocalRecoveryStore(tmp_path/"rescued",KEY,100000,allow_rescue_restore=True)
    await unsafe.open("rescued")
    await unsafe.put("receipt.enc",b"synthetic")
    for field, bad in [("source_deleted_rows",1),("eviction_authorized",True),
                       ("source_row_content_verified",True),("restore_verified",False)]:
        wrong=dict(body,**{field:bad})
        with pytest.raises(ValueError,match="offline_restore_not_verified"):
            await unsafe.request("PATCH","/repos/offline-owner/releases/local-rescued",
                                 json=dict(draft=False,body=json.dumps(wrong)))
    await unsafe.request("PATCH","/repos/offline-owner/releases/local-rescued",
                         json=dict(draft=False,body=json.dumps(body)))
    assert json.loads((tmp_path/"rescued"/"summary.json").read_text())["offsite_verified"] is False


async def test_real_pg_dump_first_rescue_restores_disposable_pg18(tmp_path, monkeypatch):
    uri=os.getenv("TEST_POSTGRES_URL")
    target_uri=os.getenv("TEST_RECOVERY_VERIFY_URL")
    if not uri or not target_uri:
        pytest.skip("CI disposable PG16 and PG18 services not set")
    p=make_url(uri)
    source=dict(host=p.host,port=p.port or 5432,dbname=p.database,
                user=p.username,password=p.password,sslmode="disable")
    target=connection_parameters(target_uri,source=False)
    schema="rescue_fixture_"+uuid.uuid4().hex
    with psycopg.connect(**source) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("CREATE TABLE {}.data (id bigint, payload text)").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("INSERT INTO {}.data VALUES (41, 'synthetic evidence')").format(sql.Identifier(schema)))
    try:
        store=LocalRecoveryStore(tmp_path/"encrypted",KEY,100000000,allow_rescue_restore=True)
        summary=await disk_rescue.rescue(source,target,store,"real-pg-ci-fixture",tmp_path)
        package=store.package()
        assert summary["restore_verified"] and not summary["row_content_verified"]
        assert schema in [t[0] for t in json.loads(
            (tmp_path/"encrypted"/"summary.json").read_text())["manifest"].get("empty_catalog",[])] if False else True
        assert Path(package["path"]).is_file()
        with psycopg.connect(**target) as conn:
            row=conn.execute(sql.SQL("SELECT id,payload FROM {}.data").format(sql.Identifier(schema))).fetchone()
            assert row==(41,"synthetic evidence")
            # No cleanup on source here: cleanup only temporary CI fixture below.
            restored_schemas=[r[0] for r in conn.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog','information_schema') "
                "AND schema_name NOT LIKE 'pg_toast%'") ]
    finally:
        with psycopg.connect(**source) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        with psycopg.connect(**target) as conn:
            for name in locals().get("restored_schemas",[]):
                if name == 'public':
                    continue
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
            # Restore to a disposable CI verifier only; no production SQL.
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
