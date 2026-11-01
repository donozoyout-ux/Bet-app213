import base64
import hashlib
import json
from pathlib import Path
import httpx
import pytest
from src.archive.recovery_format import canonical
from src.archive.github_relational import package_dump, restore_package, GitHubRelationalArchive
from src.archive.relational_archive import configuration, inventory_summary

KEY=base64.b64encode(b'r'*32).decode()
INVENTORY=dict(tables=[dict(schema='public',table='matches',kind='r',rows=2,
    row_multiset_sha256='a'*64)],sequences=[],large_objects=0,
    large_object_pages_sha256=hashlib.sha256(b'').hexdigest(),large_object_ids_sha256='b'*64,
    constraints=dict(objects=0,sha256='f'*64),foreign_key_count=0,json_payloads_included_in_row_fingerprints=True,
    schema_objects={'columns':dict(objects=3,sha256='c'*64),'json_columns':dict(objects=0,sha256='d'*64),
                    'constraints':dict(objects=0,sha256='f'*64)})


def package(tmp_path):
    raw=(b'row archive contains all tables, JSON and prices\n'*1200)
    dump=tmp_path/'full.dump';dump.write_bytes(raw)
    source=dict(source_snapshot_sha256='e'*64,source_inventory=INVENTORY)
    folder=tmp_path/'assets';folder.mkdir()
    result=package_dump(dump,source,KEY,folder)
    return raw,dump,source,result


def test_encrypted_full_dump_package_roundtrip_and_corruption_fail_closed(tmp_path):
    raw,dump,source,archive=package(tmp_path)
    assert len(archive['chunks'])==1
    assert b'row archive' not in archive['manifest_path'].read_bytes()
    item,path=archive['chunks'][0];encrypted=path.read_bytes()
    assert b'row archive' not in encrypted
    dest=tmp_path/'downloaded.dump'
    restored=restore_package(archive['manifest_path'].read_bytes(),{item['name']:encrypted},KEY,dest,archive['backup_id'])
    assert dest.read_bytes()==raw
    assert restored['dump_sha256']==hashlib.sha256(raw).hexdigest()
    bad=tmp_path/'bad.dump'
    broken=encrypted[:-1]+bytes([encrypted[-1]^1])
    with pytest.raises(ValueError):restore_package(archive['manifest_path'].read_bytes(),{item['name']:broken},KEY,bad,archive['backup_id'])
    assert not bad.exists()


def test_package_requires_snapshot_inventory_and_unique_paths(tmp_path):
    raw,dump,source,archive=package(tmp_path)
    with pytest.raises(FileExistsError):package_dump(dump,source,KEY,tmp_path/'assets')
    with pytest.raises(ValueError):package_dump(dump,{},KEY,tmp_path/'empty')


def test_receipt_contains_only_sanitized_inventory_counts_and_hashes():
    summary=inventory_summary(INVENTORY)
    assert summary['relation_count']==1 and summary['row_count']==2
    assert summary['schema_object_count']==3
    assert 'matches' not in str(summary)
    config=dict(RELATIONAL_ARCHIVE_ACTIVATED='true',RELATIONAL_SOURCE_DATABASE_URL='postgresql://reader:secret@localhost/db?sslmode=verify-full',
        RELATIONAL_GITHUB_TOKEN='secret',RELATIONAL_GITHUB_REPOSITORY='donozoyout-ux/kuponanalizii-data-archive',
        RELATIONAL_ENCRYPTION_KEY=KEY)
    assert configuration(config)['RELATIONAL_GITHUB_REPOSITORY'].endswith('data-archive')
    with pytest.raises(ValueError):configuration({**config,'RELATIONAL_GITHUB_REPOSITORY':'owner/public'})


class GitHubRelease:
    def __init__(self,corrupt=False):self.assets={};self.release=None;self.requests=[];self.corrupt=corrupt
    def __call__(self,request):
        self.requests.append(request)
        path=request.url.path
        if request.url.host=='api.github.com' and path=='/repos/test/private':
            return httpx.Response(200,json=dict(private=True,archived=False,full_name='test/private'))
        if path=='/repos/test/private/releases' and request.method=='GET':return httpx.Response(200,json=[])
        if path=='/repos/test/private/releases' and request.method=='POST':
            self.release=dict(id=5,**json.loads(request.content));return httpx.Response(201,json=self.release)
        if request.url.host=='uploads.github.com':
            name=request.url.params['name'];blob=request.content;asset=dict(id=100+len(self.assets),name=name,size=len(blob))
            self.assets[asset['id']]=blob;return httpx.Response(201,json=asset)
        if '/releases/assets/' in path:
            ident=int(path.rsplit('/',1)[-1]);blob=self.assets[ident]
            if self.corrupt and self.release.get('draft') and self.assets:
                blob=blob[:-1]+bytes([blob[-1]^1]);self.corrupt=False
            return httpx.Response(200,content=blob)
        if request.method=='PATCH':
            self.release.update(json.loads(request.content));return httpx.Response(200,json=self.release)
        raise AssertionError((request.method,request.url))


@pytest.mark.asyncio
async def test_private_github_upload_readback_and_restore_gate(tmp_path):
    raw,dump,source,package_value=package(tmp_path);fake=GitHubRelease()
    archive=GitHubRelationalArchive('test/private','synthetic-token',KEY,transport=httpx.MockTransport(fake))
    try:
        readback=tmp_path/'github.dump'
        release=await archive.create_draft(package_value,readback)
        assert release['draft'] is True and readback.read_bytes()==raw
        with pytest.raises(ValueError):await archive.publish(release,dict(INVENTORY,foreign_key_count=1))
        assert fake.release['draft'] is True
        published=await archive.publish(release,INVENTORY)
        assert published['draft'] is False
        assert all(r.method!='DELETE' for r in fake.requests)
        assert all('synthetic-token' not in r.url.query.decode(errors='ignore') for r in fake.requests)
    finally:await archive.close()


@pytest.mark.asyncio
async def test_corrupt_github_readback_never_publishes(tmp_path):
    raw,dump,source,package_value=package(tmp_path);fake=GitHubRelease(corrupt=True)
    archive=GitHubRelationalArchive('test/private','synthetic-token',KEY,transport=httpx.MockTransport(fake))
    try:
        with pytest.raises(ValueError):await archive.create_draft(package_value,tmp_path/'github.dump')
        assert fake.release['draft'] is True
        assert not any(r.method=='PATCH' for r in fake.requests)
        assert not (tmp_path/'github.dump').exists()
    finally:await archive.close()
