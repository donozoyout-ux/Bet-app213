"""Pinned, encrypted GitHub history snapshots; GET only and no database writes."""
import hashlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .crypto import key_bytes

MAX_BYTES = 8 * 1024 * 1024
SCHEMA = 'history-read-v1'


class HistoryUnavailable(ValueError):
    pass


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    market: str = Field(max_length=100)
    selection: str = Field(max_length=100)
    probability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    outcome: Literal['won', 'lost', 'void', 'pending']
    recorded_at: datetime
    source: str = Field(max_length=100)
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def decode(blob, key, identity):
    if not 28 <= len(blob) <= MAX_BYTES:
        raise HistoryUnavailable('history_unavailable')
    return json.loads(AESGCM(key_bytes(key)).decrypt(blob[:12], blob[12:], identity.encode()))


class ColdReader:
    def __init__(self, repo, token, key, manifest_asset, manifest_sha256, *, transport=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise HistoryUnavailable('history_configuration')
        if not re.fullmatch('[a-f0-9]{64}', manifest_sha256) or not token:
            raise HistoryUnavailable('history_configuration')
        key_bytes(key)
        self.repo, self.token, self.key = repo, token, key
        self.asset, self.digest = int(manifest_asset), manifest_sha256
        self.transport = transport

    async def download(self, client, asset, digest):
        if type(asset) is not int or asset <= 0 or not re.fullmatch('[a-f0-9]{64}', digest):
            raise HistoryUnavailable('history_integrity')
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/octet-stream'}
        url = f'https://api.github.com/repos/{self.repo}/releases/assets/{asset}'
        async with client.stream('GET', url, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urlsplit
                redirect = response.headers.get('location', '')
                target = urlsplit(redirect)
                if (target.scheme != 'https' or target.hostname not in
                        {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                        or target.username or target.password or target.port not in (None, 443)):
                    raise HistoryUnavailable('history_redirect')
                # No default client Authorization: signed asset GET receives no token.
                async with client.stream('GET', redirect) as asset_response:
                    return await self.read_blob(asset_response, digest)
            if response.status_code != 200:
                raise HistoryUnavailable('history_unavailable')
            return await self.read_blob(response, digest)

    async def read_blob(self, response, digest):
        if response.status_code != 200:
            raise HistoryUnavailable('history_unavailable')
        blob = bytearray()
        async for part in response.aiter_bytes():
            blob.extend(part)
            if len(blob) > MAX_BYTES:
                raise HistoryUnavailable('history_budget')
        if hashlib.sha256(blob).hexdigest() != digest:
            raise HistoryUnavailable('history_integrity')
        return bytes(blob)

    async def page(self, *, day=None, match_id=None, board_version=None, limit=25, offset=0, snapshot=None):
        try:
            if not 1 <= limit <= 50 or not 0 <= offset <= 50000:
                raise HistoryUnavailable('history_query')
            if snapshot is not None and snapshot != self.digest:
                raise HistoryUnavailable('history_snapshot_changed')
            async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False) as client:
                meta = await client.get(f'https://api.github.com/repos/{self.repo}',
                                        headers={'Authorization': f'Bearer {self.token}'})
                if meta.status_code != 200 or meta.json().get('private') is not True:
                    raise HistoryUnavailable('history_private_repository_required')
                manifest = decode(await self.download(client, self.asset, self.digest), self.key, SCHEMA + ':manifest')
                if manifest['schema'] != SCHEMA or manifest['timezone'] != 'Europe/Istanbul':
                    raise HistoryUnavailable('history_schema')
                entries = manifest['entries']
                if not isinstance(entries, list) or len(entries) > 50000:
                    raise HistoryUnavailable('history_budget')
                identities = set()
                for entry in entries:
                    identity = (entry['match_id'], entry['board_version'], entry['revision'])
                    if (type(entry['match_id']) is not int or entry['match_id'] <= 0
                            or type(entry['revision']) is not int or entry['revision'] < 1
                            or not re.fullmatch('[a-f0-9]{64}', entry['board_version'])
                            or date.fromisoformat(entry['day']).isoformat() != entry['day']
                            or identity in identities):
                        raise HistoryUnavailable('history_identity')
                    identities.add(identity)
                selected = [e for e in entries if (day is None or e['day'] == day)
                            and (match_id is None or e['match_id'] == match_id)
                            and (board_version is None or e['board_version'] == board_version)]
                selected.sort(key=lambda e: (e['day'], e['match_id'], e['board_version'], e['revision']), reverse=True)
                items = []
                page_bytes = 0
                for entry in selected[offset:offset+limit]:
                    identity = f"{SCHEMA}:{entry['match_id']}:{entry['board_version']}:{entry['revision']}"
                    blob = await self.download(client, entry['asset'], entry['sha256'])
                    page_bytes += len(blob)
                    if page_bytes > 16 * 1024 * 1024:
                        raise HistoryUnavailable('history_page_budget')
                    record = decode(blob, self.key, identity)
                    for field in ('match_id', 'board_version', 'revision', 'day'):
                        if record[field] != entry[field]: raise HistoryUnavailable('history_identity')
                    kickoff = datetime.fromisoformat(record['match']['kickoff_at'])
                    if kickoff.tzinfo is None or kickoff.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat() != entry['day']:
                        raise HistoryUnavailable('history_date')
                    if record['match']['id'] != entry['match_id']:
                        raise HistoryUnavailable('history_identity')
                    # Explicit public projection: arbitrary archived rows/raw payloads never leak.
                    from src.api.schemas import MatchResponse
                    match = MatchResponse.model_validate(record['match']).model_dump(mode='json')
                    for odds in match['odds']:
                        odds.pop('raw', None)
                    evidence = [Evidence.model_validate(e).model_dump(mode='json') for e in record['evidence']]
                    items.append(dict(match=match, day=entry['day'], match_id=entry['match_id'],
                                      board_version=entry['board_version'], revision=entry['revision'], evidence=evidence))
                return dict(items=items,total=len(selected),limit=limit,offset=offset,
                            snapshot=self.digest,source='github_archive',cleanup_authorized=False)
        except HistoryUnavailable:
            raise
        except Exception:
            raise HistoryUnavailable('history_unavailable') from None
