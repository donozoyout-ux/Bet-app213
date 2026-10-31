import base64
import hashlib
import json
import os
import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from src.archive.cold_reader import ColdReader, HistoryUnavailable, SCHEMA, canonical
from src.api.archive_history import history_reader
from src.api.main import app


def fixture_reader(*, broken=False, duplicate=False, outage=False, redirect=False):
    key=bytes(32); blobs={}; entries=[]; requests=[]
    def encrypt(data, identity):
        nonce=os.urandom(12)
        return nonce+AESGCM(key).encrypt(nonce,canonical(data),identity.encode())
    for revision in (1,2):
        record=dict(match_id=7,board_version='a'*64,revision=revision,day='2026-10-30',
                    match=dict(id=7,external_match_id=77,league_id=1,external_league_id=11,
                               league='League',season='2026',round=1,kickoff_at='2026-10-29T22:00:00Z',
                               home_team='Home',away_team='Away',status='finished',ht_home=0,ht_away=0,
                               ft_home=1,ft_away=0,odds_complete=True,updated_at='2026-10-30T01:00:00Z',
                               odds=[dict(bookmaker='Book',bookmaker_id=1,market='1x2',opening={'home':2.0},
                                          latest={'home':1.9},closing={'home':1.8},updated_at='2026-10-29T21:00:00Z',
                                          raw={'private':'must not reach browser'})]),
                    evidence=[dict(revision=revision,market='1x2',selection='home',probability=0.6,
                                   outcome='won',recorded_at='2026-10-30T01:00:00Z',source='final_statistics',
                                   home_score=1,away_score=0)])
        blob=encrypt(record,f'{SCHEMA}:7:'+ 'a'*64+f':{revision}')
        blobs[revision+10]=blob
        entries.append(dict(match_id=7,board_version='a'*64,revision=revision,day='2026-10-30',
                            asset=revision+10,sha256=hashlib.sha256(blob).hexdigest()))
    if duplicate: entries.append(entries[0])
    manifest=dict(schema=SCHEMA,timezone='Europe/Istanbul',entries=entries)
    blobs[1]=encrypt(manifest,SCHEMA+':manifest')
    digest=hashlib.sha256(blobs[1]).hexdigest()
    if broken: blobs[12]=b'corrupt'
    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        if outage:return httpx.Response(503)
        if request.url.host == 'release-assets.githubusercontent.com':
            assert 'authorization' not in request.headers
            return httpx.Response(200,content=blobs[int(request.url.path.strip('/'))])
        assert request.url.host == 'api.github.com'
        if request.url.path == '/repos/owner/private': return httpx.Response(200,json={'private':True})
        asset=int(request.url.path.split('/')[-1])
        if redirect:return httpx.Response(302,headers={'location':f'https://release-assets.githubusercontent.com/{asset}'})
        return httpx.Response(200,content=blobs[asset])
    return ColdReader('owner/private','server-token',base64.b64encode(key).decode(),1,digest,
                      transport=httpx.MockTransport(handle)),requests


@pytest.mark.asyncio
async def test_actual_app_reads_cold_revisions_and_odds_without_database():
    reader, requests=fixture_reader(redirect=True)
    app.dependency_overrides[history_reader]=lambda:reader
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/api/history/archive?day=2026-10-30&match_id=7&limit=1')
            assert response.status_code == 200
            data=response.json(); item=data['items'][0]
            assert data['total'] == 2 and item['revision'] == 2
            assert item['match']['ft_home'] == 1
            assert item['match']['odds'][0]['closing']['home'] == 1.8
            assert item['evidence'][0]['outcome'] == 'won'
            assert 'must not reach browser' not in response.text
            assert 'server-token' not in response.text
            page=await client.get('/api/history/archive',params=dict(limit=1,offset=1,snapshot=data['snapshot'],board_version='a'*64))
            assert page.json()['items'][0]['revision'] == 1
    finally: app.dependency_overrides.pop(history_reader,None)
    assert requests


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs',[dict(broken=True),dict(duplicate=True),dict(outage=True)])
async def test_unavailable_is_not_empty_success(kwargs):
    reader,_=fixture_reader(**kwargs)
    with pytest.raises(HistoryUnavailable): await reader.page()


@pytest.mark.asyncio
async def test_manifest_change_and_date_filter():
    reader,_=fixture_reader()
    with pytest.raises(HistoryUnavailable): await reader.page(snapshot='b'*64)
    assert (await reader.page(day='2026-10-29'))['total'] == 0


@pytest.mark.asyncio
async def test_disabled_application_reports_explicit_error(monkeypatch):
    monkeypatch.delenv('HISTORY_ARCHIVE_ENABLED',raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/api/history/archive')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'history_archive_disabled'
