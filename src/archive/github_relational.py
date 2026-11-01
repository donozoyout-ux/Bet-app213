"""Encrypted, immutable full-dump assets in a private GitHub Release."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import httpx
from .crypto import key_bytes
from .recovery_format import CHUNK, MAX_DUMP, canonical, seal, unseal

SCHEMA='betapp-relational-release-v1'
MAX_MANIFEST=CHUNK
MAX_ASSET=CHUNK+65536
MAX_RELEASE_BYTES=MAX_DUMP+64*1024*1024


def sha_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()


def package_dump(dump_path, source, key, folder):
    """Encrypt each bounded dump segment; manifest and schema inventory are encrypted too."""
    key_bytes(key);dump=Path(dump_path);folder=Path(folder)
    if not dump.is_file() or not 0<dump.stat().st_size<=MAX_DUMP:raise ValueError('Full dump size budget exceeded')
    dump_sha=sha_file(dump);snapshot=source.get('source_snapshot_sha256')
    inventory=source.get('source_inventory')
    if not re.fullmatch('[a-f0-9]{64}',str(snapshot)) or not isinstance(inventory,dict):
        raise ValueError('Same-snapshot inventory required')
    backup_id=hashlib.sha256((SCHEMA+':'+snapshot+':'+dump_sha).encode()).hexdigest()[:32]
    chunks=[];package_bytes=0
    with dump.open('rb') as stream:
        index=0
        while raw:=stream.read(CHUNK):
            name=f'chunk-{index:06d}.enc'
            blob=seal(raw,key,backup_id,name)
            path=folder/name
            from .crypto import write_new
            write_new(path,blob)
            chunks.append(dict(name=name,bytes=len(blob),ciphertext_sha256=hashlib.sha256(blob).hexdigest(),
                               plaintext_bytes=len(raw),plaintext_sha256=hashlib.sha256(raw).hexdigest()))
            package_bytes+=len(blob);index+=1
            if index>MAX_DUMP//CHUNK+1 or package_bytes>MAX_RELEASE_BYTES:
                raise ValueError('Full archive asset budget exceeded')
    manifest=dict(schema=SCHEMA,backup_id=backup_id,source_snapshot_sha256=snapshot,
                  dump_sha256=dump_sha,dump_bytes=dump.stat().st_size,
                  inventory_sha256=hashlib.sha256(canonical(inventory)).hexdigest(),
                  inventory=inventory,chunks=chunks)
    manifest_blob=seal(canonical(manifest),key,backup_id,'manifest.enc')
    if len(manifest_blob)>MAX_MANIFEST:raise ValueError('Encrypted manifest size budget exceeded')
    manifest_path=folder/'manifest.enc'
    from .crypto import write_new
    write_new(manifest_path,manifest_blob)
    return dict(backup_id=backup_id,manifest=manifest,manifest_path=manifest_path,
                manifest_ciphertext_sha256=hashlib.sha256(manifest_blob).hexdigest(),
                chunks=[(item,folder/item['name']) for item in chunks],
                package_bytes=package_bytes+len(manifest_blob))


def restore_package(manifest_blob, chunks, key, destination, backup_id):
    """Authenticate all ciphertext, AAD, plaintext hashes and global dump hash."""
    destination=Path(destination)
    if destination.exists():raise ValueError('Restore destination already exists')
    if not re.fullmatch('[a-f0-9]{32}',str(backup_id)):raise ValueError('Invalid archive identity')
    try:manifest=json.loads(unseal(manifest_blob,key,backup_id,'manifest.enc'))
    except Exception:raise ValueError('Manifest authentication failed') from None
    if (manifest.get('schema')!=SCHEMA or manifest.get('backup_id')!=backup_id
            or not re.fullmatch('[a-f0-9]{64}',str(manifest.get('dump_sha256')))
            or not 0<manifest.get('dump_bytes',0)<=MAX_DUMP
            or hashlib.sha256(canonical(manifest.get('inventory'))).hexdigest()!=manifest.get('inventory_sha256')):
        raise ValueError('Manifest identity or digest mismatch')
    if not isinstance(chunks,dict) or set(chunks)!={item.get('name') for item in manifest.get('chunks',[])}:
        raise ValueError('Archive chunk inventory mismatch')
    digest=hashlib.sha256();total=0
    try:
        with destination.open('xb') as output:
            for item in manifest['chunks']:
                blob=chunks[item['name']]
                if len(blob)!=item['bytes'] or hashlib.sha256(blob).hexdigest()!=item['ciphertext_sha256']:
                    raise ValueError('Encrypted chunk digest mismatch')
                raw=unseal(blob,key,backup_id,item['name'])
                if len(raw)!=item['plaintext_bytes'] or hashlib.sha256(raw).hexdigest()!=item['plaintext_sha256']:
                    raise ValueError('Plaintext chunk digest mismatch')
                total+=len(raw)
                if total>MAX_DUMP:raise ValueError('Full dump byte budget exceeded')
                digest.update(raw);output.write(raw)
            output.flush();os.fsync(output.fileno())
        if total!=manifest['dump_bytes'] or digest.hexdigest()!=manifest['dump_sha256']:
            raise ValueError('Full dump digest mismatch')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return manifest


class GitHubRelationalArchive:
    def __init__(self,repo,token,key,*,transport=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not token:raise ValueError('Private archive configuration required')
        key_bytes(key);self.repo=repo;self.key=key
        self.client=httpx.AsyncClient(transport=transport,timeout=45,follow_redirects=False,
            headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28'})

    async def close(self):await self.client.aclose()

    async def request(self,method,path,**kwargs):
        response=await self.client.request(method,'https://api.github.com'+path,**kwargs)
        if not response.is_success:raise ValueError('Private archive API request failed')
        return response

    async def create_draft(self,package,readback_path):
        repo=(await self.request('GET',f'/repos/{self.repo}')).json()
        if repo.get('private') is not True or repo.get('archived') is True or repo.get('full_name','').lower()!=self.repo.lower():
            raise ValueError('Full relational archive requires the exact private repository')
        manifest=package['manifest'];backup_id=package['backup_id'];tag='relational-v1-'+backup_id
        assets=[('manifest.enc',Path(package['manifest_path']))]+[(item['name'],Path(path)) for item,path in package['chunks']]
        if sum(path.stat().st_size for _,path in assets)>MAX_RELEASE_BYTES:
            raise ValueError('Release budget exceeded')
        for name,path in assets:
            blob=path.read_bytes()
            expected=package['manifest']
            if name=='manifest.enc':
                if hashlib.sha256(blob).hexdigest()!=package['manifest_ciphertext_sha256']:
                    raise ValueError('Manifest changed before upload')
            else:
                item=next(entry for entry in expected['chunks'] if entry['name']==name)
                if len(blob)!=item['bytes'] or hashlib.sha256(blob).hexdigest()!=item['ciphertext_sha256']:
                    raise ValueError('Encrypted chunk changed before upload')
        releases=(await self.request('GET',f'/repos/{self.repo}/releases?per_page=100')).json()
        if any(r.get('tag_name')==tag for r in releases):raise ValueError('Archive identity already exists; verify it manually')
        body=json.dumps(dict(schema=SCHEMA,backup_id=backup_id,manifest_asset='manifest.enc',
                             source_inventory_sha256=manifest['inventory_sha256']),sort_keys=True,separators=(',',':'))
        release=(await self.request('POST',f'/repos/{self.repo}/releases',json={
            'tag_name':tag,'name':'Verified relational archive '+backup_id,'body':body,'draft':True})).json()
        uploaded=[]
        try:
            for name,path in assets:
                blob=path.read_bytes()
                response=await self.client.post(f"https://uploads.github.com/repos/{self.repo}/releases/{int(release['id'])}/assets",
                    params={'name':name},headers={'Content-Type':'application/octet-stream'},content=blob)
                if response.status_code!=201:raise ValueError('Encrypted asset upload failed; draft retained')
                uploaded.append(response.json())
        except Exception:
            raise ValueError('Archive upload incomplete; private draft retained for manual review') from None
        release.update(assets=uploaded,body=body,tag_name=tag,draft=True)
        manifest_blob=await self.download_asset(next(a for a in uploaded if a['name']=='manifest.enc'))
        # Manifest ciphertext AAD binds its identity; its authenticated backup id
        # is supplied out-of-band from the deterministic release tag.
        if hashlib.sha256(manifest_blob).hexdigest()!=package['manifest_ciphertext_sha256']:
            raise ValueError('Read-back manifest checksum mismatch; draft remains unpublished')
        manifest=json.loads(unseal(manifest_blob,self.key,backup_id,'manifest.enc'))
        if manifest!=package['manifest'] or hashlib.sha256(canonical(manifest['inventory'])).hexdigest()!=manifest['inventory_sha256']:
            raise ValueError('Read-back manifest verification failed; draft remains unpublished')
        destination=Path(readback_path)
        if destination.exists():raise ValueError('Read-back dump destination already exists')
        digest=hashlib.sha256();total=0
        try:
            with destination.open('xb') as output:
                for item in manifest['chunks']:
                    asset=next((a for a in uploaded if a['name']==item['name']),None)
                    if asset is None:raise ValueError('Read-back asset missing; draft remains unpublished')
                    blob=await self.download_asset(asset)
                    if len(blob)!=item['bytes'] or hashlib.sha256(blob).hexdigest()!=item['ciphertext_sha256']:
                        raise ValueError('Read-back chunk checksum mismatch; draft remains unpublished')
                    raw=unseal(blob,self.key,backup_id,item['name'])
                    if len(raw)!=item['plaintext_bytes'] or hashlib.sha256(raw).hexdigest()!=item['plaintext_sha256']:
                        raise ValueError('Read-back plaintext checksum mismatch; draft remains unpublished')
                    total+=len(raw)
                    if total>MAX_DUMP:raise ValueError('Read-back dump size budget exceeded')
                    digest.update(raw);output.write(raw)
                output.flush();os.fsync(output.fileno())
            if total!=manifest['dump_bytes'] or digest.hexdigest()!=manifest['dump_sha256']:
                raise ValueError('Read-back full dump digest mismatch; draft remains unpublished')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        release['manifest']=manifest;release['readback_path']=str(destination)
        return release

    async def download_asset(self,asset):
        size=int(asset.get('size',0))
        if not 0<size<=MAX_ASSET:raise ValueError('Release asset size limit')
        url=f"https://api.github.com/repos/{self.repo}/releases/assets/{int(asset['id'])}"
        async with self.client.stream('GET',url,headers={'Accept':'application/octet-stream'}) as response:
            if response.status_code in {301,302,303,307,308}:
                target=response.headers.get('location','');parsed=urlsplit(target)
                if parsed.scheme!='https' or parsed.hostname not in {'release-assets.githubusercontent.com','objects.githubusercontent.com'} or parsed.username or parsed.password or parsed.port not in (None,443):
                    raise ValueError('Unexpected release asset redirect')
            elif response.status_code==200:
                return await self._read_asset(response,size)
            else:raise ValueError('Release asset download failed')
        async with httpx.AsyncClient(timeout=45,follow_redirects=False) as public:
            async with public.stream('GET',target) as response:
                if response.status_code!=200:raise ValueError('Signed release download failed')
                return await self._read_asset(response,size)

    @staticmethod
    async def _read_asset(response,size):
        value=bytearray()
        async for part in response.aiter_bytes():
            value.extend(part)
            if len(value)>MAX_ASSET or len(value)>size:raise ValueError('Release asset byte limit')
        if len(value)!=size:raise ValueError('Incomplete release asset')
        return bytes(value)

    async def publish(self,release,restored_inventory):
        if not release.get('draft') or not release.get('readback_path') or not release.get('manifest'):
            raise ValueError('Independent restore verification required before publishing archive')
        if restored_inventory!=release['manifest'].get('inventory'):
            raise ValueError('GitHub restore inventory differs; archive remains draft')
        result=(await self.request('PATCH',f"/repos/{self.repo}/releases/{int(release['id'])}",json={'draft':False})).json()
        return result
