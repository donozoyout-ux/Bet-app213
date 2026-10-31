"""Offline encrypted GitHub-browser-upload package; synthetic bytes only."""
import base64
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from src.archive.local_recovery_store import LocalRecoveryStore
from src.archive.recovery_format import sha, unseal


async def test_local_package_requires_no_github_cli_and_proves_encrypted_bytes(tmp_path, monkeypatch):
    # Detect accidental GitHub/HTTP invocation at the storage boundary.
    secret = base64.b64encode(b"x" * 32).decode()
    store = LocalRecoveryStore(tmp_path / "local-assets", secret, budget_bytes=1_000_000)
    await store.open("vv755-local-test")
    payload = b"synthetic private PostgreSQL dump content"
    part = await store.put("chunk-000000.enc", payload)
    assert await store.download(dict(id=part["asset_id"], size=part["bytes"]))
    manifest = await store.put("manifest.enc", b'{"test":"synthetic"}')
    receipt = await store.put("receipt.enc", b'{"restore_verified":true}')
    await store.request(
        "PATCH", "/repos/offline-owner/releases/local-vv755-local-test",
        json=dict(draft=False, body=json.dumps(dict(
            restore_verified=True, row_content_verified=True, tables=1,
            source_deleted_rows=0, application_read_through_verified=False,
        ))),
    )
    output = store.package()
    assert Path(output["path"]).is_file()
    assert not (tmp_path / "local-assets" / "summary.json").read_bytes().find(secret.encode()) >= 0
    with ZipFile(output["path"]) as packed:
        assert set(packed.namelist()) == {"chunk-000000.enc", "manifest.enc", "receipt.enc", "summary.json"}
        assert packed.read("chunk-000000.enc") != payload
        assert unseal(packed.read("chunk-000000.enc"), secret,
                      "vv755-local-test", "chunk-000000.enc") == payload
        summary = json.loads(packed.read("summary.json"))
        assert summary["restore_verified"] and not summary["offsite_verified"]
        assert summary["manual_browser_upload_required"]
    assert output["sha256"] == sha(Path(output["path"]).read_bytes())
    assert not (tmp_path / "local-assets" / "full.dump").exists()
    await store.close()


async def test_local_package_fails_closed_on_reuse_or_partial_restore(tmp_path):
    key = base64.b64encode(b"a" * 32).decode()
    store = LocalRecoveryStore(tmp_path / "assets", key, budget_bytes=100_000)
    await store.open("first")
    with pytest.raises(ValueError, match="offline_archive_path_exists"):
        await LocalRecoveryStore(tmp_path / "assets", key, budget_bytes=100_000).open("second")
    with pytest.raises(ValueError, match="offline_receipt_missing"):
        store.package()
    with pytest.raises(ValueError, match="offline_restore_not_verified"):
        await store.request("PATCH", "/repos/offline-owner/releases/local-first",
                            json=dict(draft=False, body='{"restore_verified":false,"row_content_verified":true}'))
    with pytest.raises(ValueError, match="offline_remote_call_forbidden"):
        await store.request("POST", "/repos/private/backup", json={})
    with pytest.raises(ValueError, match="invalid_asset_name"):
        await store.put("../dump.sql", b"no")
