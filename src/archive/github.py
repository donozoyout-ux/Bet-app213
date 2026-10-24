"""Private, date-idempotent encrypted Releases. No wildcard uploads or API writes to Neon."""
import asyncio
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlparse
import httpx
from src.archive.crypto import unseal
from src.archive.schema import MAX_BYTES, canonical

PREFIX='owned-audit-v2-'
ASSET='owned-audit.enc.json'


class ReleaseArchive:
    def __init__(self, repository, token, key, *, budget_bytes, keep_days, transport=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not token:
            raise ValueError('Private repository and token required')
        if budget_bytes<=0 or not 7<=keep_days<=3650:raise ValueError('Invalid archive limits')
        self.repo=repository;self.key=key;self.budget=budget_bytes;self.keep=keep_days
        self.client=httpx.AsyncClient(transport=transport,timeout=30,follow_redirects=False,
            headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28'})

    async def close(self):await self.client.aclose()

    async def request(self, method, path, **kwargs):
        # GET retries only. Mutations reconcile by deterministic tag on the next run.
        for attempt in range(3):
            try:
                response=await self.client.request(method,'https://api.github.com'+path,**kwargs)
            except httpx.TransportError:
                if method!='GET' or attempt==2:raise ValueError('Archive network unavailable') from None
            else:
                if method!='GET' or response.status_code not in {429,500,502,503,504} or attempt==2:
                    if not response.is_success:raise ValueError('Archive API rejected request')
                    return response
            await asyncio.sleep(2**attempt)

    async def inventory(self):
        meta=(await self.request('GET',f'/repos/{self.repo}')).json()
        if meta.get('private') is not True or meta.get('archived') or meta.get('full_name','').lower()!=self.repo.lower():
            raise ValueError('Target must be the verified private writable repository')
        releases=[]
        for page in range(1,11):
            rows=(await self.request('GET',f'/repos/{self.repo}/releases?per_page=100&page={page}')).json()
            releases.extend(rows)
            if len(rows)<100:break
        else:raise ValueError('Release inventory limit; review retention')
        total=sum(int(a['size']) for r in releases for a in r.get('assets',[]))
        return releases,total

    async def download(self, asset):
        if not 0<int(asset['size'])<=MAX_BYTES*2:raise ValueError('Asset size limit')
        url=f"https://api.github.com/repos/{self.repo}/releases/assets/{int(asset['id'])}"
        # GitHub may redirect asset downloads to its signed object host. Never
        # forward the repository token to that host or follow further redirects.
        async with self.client.stream('GET',url,headers={'Accept':'application/octet-stream'}) as response:
            if response.status_code in {301,302,307}:
                target=response.headers.get('location','');parsed=urlparse(target)
                if parsed.scheme!='https' or parsed.hostname not in {'release-assets.githubusercontent.com','objects.githubusercontent.com'} or parsed.username or parsed.port:
                    raise ValueError('Unexpected asset redirect')
            else:
                if response.status_code!=200:raise ValueError('Asset download failed')
                return await self.read_bounded(response,asset['size'])
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as public:
            async with public.stream('GET',target) as response:
                if response.status_code!=200:raise ValueError('Asset download failed')
                return await self.read_bounded(response,asset['size'])

    @staticmethod
    async def read_bounded(response, expected):
        result=bytearray()
        async for chunk in response.aiter_bytes():
            result.extend(chunk)
            if len(result)>MAX_BYTES*2 or len(result)>expected:raise ValueError('Asset size limit')
        if len(result)!=expected:raise ValueError('Incomplete asset')
        return bytes(result)

    async def verified(self, release, *, draft_ok=False):
        tag=release['tag_name']
        if not re.fullmatch(PREFIX+r'\d{4}-\d{2}-\d{2}',tag):raise ValueError('Invalid archive tag')
        day=date.fromisoformat(tag[len(PREFIX):]).isoformat()
        if release.get('draft') and not draft_ok:raise ValueError('Unpublished archive')
        assets=release.get('assets',[])
        if len(assets)!=1 or assets[0]['name']!=ASSET:raise ValueError('Unexpected archive assets')
        blob=await self.download(assets[0])
        manifest=json.loads(release['body'])
        expected=dict(schema='owned-release-v2',day=day,sha256=hashlib.sha256(blob).hexdigest(),bytes=len(blob))
        if manifest!=expected:raise ValueError('Release digest mismatch')
        data=unseal(blob,self.key)
        if data['coverage']['day']!=day or date.fromisoformat(data['created_at'][:10]).isoformat()!=day:raise ValueError('Release day mismatch')
        return data

    async def publish(self, data, blob, releases, total):
        day=data['coverage']['day'];tag=PREFIX+day
        if total+len(blob)>self.budget:raise ValueError('Archive storage budget exceeded')
        manifest=canonical(dict(schema='owned-release-v2',day=day,sha256=hashlib.sha256(blob).hexdigest(),bytes=len(blob))).decode()
        existing=next((r for r in releases if r['tag_name']==tag),None)
        if existing:
            if not existing.get('draft'):raise ValueError('Immutable day already published')
            # A crash after upload can be finalized only after authentication.
            if existing.get('assets'):
                recovered=await self.verified(existing,draft_ok=True)
                await self.request('PATCH',f"/repos/{self.repo}/releases/{int(existing['id'])}",json={'draft':False})
                return recovered
            release=existing
            await self.request('PATCH',f"/repos/{self.repo}/releases/{int(release['id'])}",json={'body':manifest})
        else:
            release=(await self.request('POST',f'/repos/{self.repo}/releases',json={
                'tag_name':tag,'name':tag,'body':manifest,'draft':True})).json()
        release_id=int(release['id'])
        response=await self.client.post(f'https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets',
            params={'name':ASSET},headers={'Content-Type':'application/octet-stream'},content=blob)
        if response.status_code!=201:raise ValueError('Encrypted asset upload failed; draft retained')
        # Read back the server asset and authenticate before publishing success.
        staged={**release,'tag_name':tag,'body':manifest,'draft':True,'assets':[response.json()]}
        recovered=await self.verified(staged,draft_ok=True)
        await self.request('PATCH',f'/repos/{self.repo}/releases/{release_id}',json={'draft':False})
        return recovered

    async def retain(self, day, releases):
        # Only our authenticated v2 releases, after a newer verified success.
        # Unknown releases, legacy exports and drafts are never deleted.
        for release in releases:
            tag=release.get('tag_name','')
            if not re.fullmatch(PREFIX+r'\d{4}-\d{2}-\d{2}',tag) or release.get('draft'):continue
            old=date.fromisoformat(tag[len(PREFIX):])
            if (day-old).days<self.keep:continue
            await self.verified(release)
            await self.request('DELETE',f"/repos/{self.repo}/releases/{int(release['id'])}")
