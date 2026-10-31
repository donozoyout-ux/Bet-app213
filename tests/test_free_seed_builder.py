"""Synthetic offline rescue ZIP, secret guarding and isolated Docker seeding."""
import base64
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED

import pytest

from src.archive import free_seed_builder as seed
from src.archive.free_hot_cutover import CutoverBlocked
from src.archive.recovery_format import canonical, seal, sha


KEY = base64.b64encode(b'k'*32).decode()
RAW = b'NO REAL USER DATA IN THIS TEST'


def synthetic_rescue_zip(tmp_path, monkeypatch, *, corrupt=False):
    ident=seed.ORIGINAL_ID
    manifest=dict(schema=seed.SCHEMA,backup_id=ident,format='pg_dump_custom',
                  rescue_only=True,source_row_content_verified=False,
                  dump_sha256=sha(RAW),chunks=[])
    blob=seal(RAW,KEY,ident,'chunk-000000.enc')
    entry=dict(name='chunk-000000.enc',asset_id=1,bytes=len(blob),
               sha256=sha(blob),plaintext_bytes=len(RAW),plaintext_sha256=sha(RAW))
    manifest['chunks']=[entry]
    summary=dict(backup_id=ident,restore_verified=True,rescue_only=True,
                 source_row_content_verified=False,row_content_verified=False,
                 source_deleted_rows=0,eviction_authorized=False,
                 application_read_through_verified=False)
    zpath=tmp_path/'private-rescue.zip'
    with ZipFile(zpath,'w',compression=ZIP_STORED) as archive:
        archive.writestr('chunk-000000.enc',blob)
        archive.writestr('manifest.enc',seal(canonical(manifest),KEY,ident,'manifest.enc'))
        archive.writestr('receipt.enc',seal(canonical(summary),KEY,ident,'receipt.enc'))
        archive.writestr('summary.json',canonical(summary))
    monkeypatch.setattr(seed,'ORIGINAL_ZIP_SHA256',sha(zpath.read_bytes()))
    monkeypatch.setattr(seed,'ORIGINAL_DUMP_SHA256',sha(RAW))
    return zpath


def test_readonly_zip_preflight_without_creating_any_output(tmp_path, monkeypatch):
    from src.archive.free_seed_builder import inspect_rescue_metadata
    archive=synthetic_rescue_zip(tmp_path,monkeypatch)
    files_before={p.name for p in tmp_path.iterdir()}
    result=inspect_rescue_metadata(archive,KEY)
    assert result['manifest_and_receipt_authenticated'] is True
    assert result['dump_sha256_verified_without_writing'] is True
    assert result['all_17_or_more_chunks_authenticated'] is True
    assert result['chunk_count']==1
    assert result['database_contacted'] is False
    assert result['output_directory_created'] is False
    assert {p.name for p in tmp_path.iterdir()}==files_before


def test_readonly_preflight_never_starts_docker(tmp_path,monkeypatch):
    from src.archive.free_seed_builder import main
    import sys
    archive=synthetic_rescue_zip(tmp_path,monkeypatch)
    keyfile=tmp_path/'recovery.key'
    keyfile.write_text(KEY,encoding='utf-8')
    monkeypatch.setattr(seed,'_docker',lambda *a,**kw: (_ for _ in ()).throw(
        AssertionError('preflight_must_not_start_docker')))
    monkeypatch.setattr(sys,'argv',['free_seed_builder','--zip',str(archive),
                                    '--key-file',str(keyfile),'--inspect-only'])
    main()
    assert not list(tmp_path.glob('private-dump-*'))


def test_immutable_encrypted_rescue_reconstructs_locally(tmp_path, monkeypatch):
    archive=synthetic_rescue_zip(tmp_path,monkeypatch)
    out=tmp_path/'private.dump'
    manifest=seed.load_offline_rescue(archive,KEY,out)
    assert manifest['backup_id']==seed.ORIGINAL_ID
    assert out.read_bytes()==RAW
    with pytest.raises(CutoverBlocked,match='private_output_exists'):
        seed.load_offline_rescue(archive,KEY,out)


def test_original_offsite_sha_and_ciphertext_fail_closed(tmp_path,monkeypatch):
    archive=synthetic_rescue_zip(tmp_path,monkeypatch)
    monkeypatch.setattr(seed,'ORIGINAL_ZIP_SHA256','0'*64)
    with pytest.raises(CutoverBlocked,match='offline_rescue_sha256_mismatch'):
        seed.load_offline_rescue(archive,KEY,tmp_path/'x.dump')
    monkeypatch.setattr(seed,'ORIGINAL_ZIP_SHA256',sha(archive.read_bytes()))
    with pytest.raises(Exception):
        seed.load_offline_rescue(archive,base64.b64encode(b'x'*32).decode(),tmp_path/'wrong.dump')


def test_no_git_or_external_database_or_plaintext_upload_apis():
    code=Path('src/archive/free_seed_builder.py').read_text()
    for forbidden in ('ARCHIVE_DATABASE_URL','DATABASE_URL','HISTORY_ARCHIVE_TOKEN',
                      'https://api.github.com','httpx.post','TRUNCATE public','DROP DATABASE'):
        assert forbidden not in code
    assert 'source_database_modified=False' in code
    assert 'eviction_authorized=False' in code
    assert 'original_source_row_parity_verified=False' in code
    assert "if label==owner:_docker(['rm','-f','-v',name]" in code


def test_owner_container_port_is_strict(monkeypatch):
    monkeypatch.setattr(seed,'_port_is_free',lambda port: False)
    with pytest.raises(CutoverBlocked,match='local_port_in_use'):
        with seed.ephemeral_pg18(5433,'fixture-test'):
            pass
    with pytest.raises(CutoverBlocked,match='local_port_in_use'):
        with seed.ephemeral_pg18(5432,'fixture-test'):
            pass
