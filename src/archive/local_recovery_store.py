"""Owner-only encrypted local package for browser upload (no gh CLI or token).

No network access occurs here. This is not proof of a GitHub-hosted copy.
The full recovery pipeline still restores from authenticated bytes locally.
"""
import json
import hashlib
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_STORED

from .recovery_format import seal, unseal, sha

class LocalRecoveryStore:
    repo = "offline-owner"
    is_offsite = False

    def __init__(self, destination, key, budget_bytes):
        self.destination = Path(destination).resolve()
        self.key = key
        self.budget = budget_bytes
        self.total = 0
        self.assets = {}
        self.release = None
        self.backup_id = None

    async def open(self, backup_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", backup_id):
            raise ValueError("invalid_backup_id")
        if self.destination.exists() or self.destination.is_symlink():
            raise ValueError("offline_archive_path_exists")
        if not self.destination.parent.is_dir():
            raise ValueError("offline_parent_missing")
        self.destination.mkdir(mode=0o700)
        self.backup_id = backup_id
        self.release = dict(id="local-"+backup_id, html_url=None)

    async def put(self, name, raw):
        if name not in {"manifest.enc", "receipt.enc"} and not re.fullmatch(r"chunk-[0-9]{6}[.]enc", name):
            raise ValueError("invalid_asset_name")
        blob = seal(raw, self.key, self.backup_id, name)
        if self.total + len(blob) > self.budget:
            raise ValueError("offline_budget_exceeded")
        path = self.destination / name
        with path.open("xb") as handle:
            handle.write(blob)
        path.chmod(0o600)
        self.total += len(blob)
        asset_id = len(self.assets) + 1
        self.assets[asset_id] = dict(name=name, size=len(blob))
        actual = await self.download(dict(id=asset_id, size=len(blob)))
        if sha(actual) != sha(blob) or unseal(actual, self.key, self.backup_id, name) != raw:
            raise ValueError("offline_asset_verification_failed")
        return dict(name=name, asset_id=asset_id, bytes=len(blob),
                    sha256=sha(blob), plaintext_sha256=sha(raw), plaintext_bytes=len(raw))

    async def download(self, asset):
        item = self.assets.get(asset["id"])
        if item is None or item["size"] != asset["size"]:
            raise ValueError("offline_asset_not_found")
        payload = (self.destination / item["name"]).read_bytes()
        if len(payload) != item["size"]:
            raise ValueError("offline_asset_size_mismatch")
        return payload

    async def request(self, method, path, json=None):
        if method != "PATCH" or path != "/repos/offline-owner/releases/local-"+self.backup_id:
            raise ValueError("offline_remote_call_forbidden")
        if json is None or json.get("draft") is not False:
            raise ValueError("offline_receipt_incomplete")
        body = __import__("json").loads(json["body"])
        if not body.get("restore_verified") or not body.get("row_content_verified"):
            raise ValueError("offline_restore_not_verified")
        body["offsite_verified"] = False
        body["manual_browser_upload_required"] = True
        with (self.destination / "summary.json").open("x", encoding="utf-8") as handle:
            __import__("json").dump(body, handle, sort_keys=True, ensure_ascii=False)
        return None

    def package(self):
        if not (self.destination / "summary.json").is_file():
            raise ValueError("offline_receipt_missing")
        if not (self.destination / "receipt.enc").is_file():
            raise ValueError("offline_encrypted_receipt_missing")
        archive = self.destination.parent / ("pg-recovery-"+self.backup_id+".zip")
        with archive.open("xb") as target:
            from zipfile import ZipFile
            with ZipFile(target, mode="w", compression=ZIP_STORED, allowZip64=True) as packed:
                for path in sorted(self.destination.iterdir()):
                    if not path.is_file() or path.name not in ("summary.json", "manifest.enc", "receipt.enc") and not re.fullmatch(r"chunk-[0-9]{6}[.]enc", path.name):
                        raise ValueError("offline_unexpected_asset")
                    packed.write(path, arcname=path.name)
        if archive.stat().st_size >= 2_000_000_000:
            raise ValueError("github_release_single_asset_limit")
        archive.chmod(0o600)
        checksum = hashlib.sha256()
        with archive.open("rb") as reader:
            while block := reader.read(1024 * 1024):
                checksum.update(block)
        return dict(path=str(archive), size=archive.stat().st_size,
                    sha256=checksum.hexdigest())

    async def close(self):
        return None
