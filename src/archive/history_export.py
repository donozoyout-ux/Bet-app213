"""Daily immutable publication. Source SELECTs, staged assets, verified catalog last."""
import argparse
import asyncio
import base64
import os
import re
from datetime import datetime, timezone
from .board_backup import BoardStore
from .history_format import SCHEMA, MAX_CHUNK, canonical, sha, seal, unseal, parse, schema_digest, validate_catalog
from .history_plan import HistorySource, plan
from .history_postgres import verify_postgres
from .source import snapshot

REPOSITORY='donozoyout-ux/kuponanalizii-data-archive'
MAX_NEW_ASSETS=80
MAX_PARTITIONS=2048


class HistoryStore(BoardStore):
    def debit(self):
        self.calls=getattr(self,'calls',0)+1
        if self.calls>500: raise ValueError('Archive API budget; resume next run')

    async def request(self,method,path,**kwargs):
        self.debit()
        # Rate limiting or ambiguous writes stop this run; no short retry storm.
        response=await self.client.request(method,'https://api.github.com'+path,**kwargs)
        if not response.is_success:raise ValueError('Archive API unavailable; resume after rate-limit recovery')
        return response

    async def download(self,asset):
        self.debit()
        return await super().download(asset)

    async def open(self,generation,create=False):
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',generation): raise ValueError('Generation ID')
        self.archive_id=generation
        if not hasattr(self,'known'):
            releases,_=await self.inventory()
            metadata=(await self.request('GET',f'/repos/{self.repo}')).json()
            if create and metadata.get('permissions',{}).get('push') is not True: raise ValueError('Archive write permission')
            self.total=0;count=0
            for release in releases:
                embedded=release.get('assets')
                # GitHub includes small release inventories. Only paginate
                # possibly truncated/large ones; never fan out one GET per scope.
                if not isinstance(embedded,list) or len(embedded)>=100:
                    release['assets']=await self.asset_list(release['id'])
                count+=len(release['assets'])
                if count>10000: raise ValueError('Archive asset inventory budget')
                self.total+=sum(a['size'] for a in release['assets'])
            self.known={r['tag_name']:r for r in releases}
            self.asset_count=count
            self.new_assets=0
        self.release=self.known.get('history-v1-'+generation)
        if self.release is None:
            if not create: raise ValueError('Generation missing')
            if self.total>=self.budget: raise ValueError('Archive storage budget')
            self.release=(await self.request('POST',f'/repos/{self.repo}/releases',json={
                'tag_name':'history-v1-'+generation,'name':'history-v1-'+generation,'draft':True,
                'body':'Staged history. No production success until encrypted verified catalog publication.'})).json()
            self.release['assets']=[]
            self.known['history-v1-'+generation]=self.release
        self.assets={a['name']:a for a in self.release['assets']}
        if len(self.assets)!=len(self.release['assets']): raise ValueError('Duplicate assets')

    async def put_raw(self,name,raw):
        if name not in {'plan.enc','index.enc','catalog.enc','receipt.enc'} and not re.fullmatch(r'chunk-\d{6}\.enc',name):
            raise ValueError('Unexpected asset name')
        if len(raw)>MAX_CHUNK: raise ValueError('Asset plaintext budget')
        existing=await self.get(name)
        if existing is not None:
            if unseal(existing,self.key,self.archive_id,name)!=raw: raise ValueError('Immutable source conflict; use a new run ID')
            return existing,self.assets[name]
        if not self.release.get('draft'): raise ValueError('Published generation cannot be extended')
        if self.new_assets>=MAX_NEW_ASSETS: raise ValueError('Per-run asset budget; resume same ID')
        if self.asset_count>=10000:raise ValueError('Repository asset inventory budget')
        blob=seal(raw,self.key,self.archive_id,name)
        if self.total+len(blob)>self.budget: raise ValueError('Archive storage budget')
        self.debit()
        response=await self.client.post(f'https://uploads.github.com/repos/{self.repo}/releases/{self.release["id"]}/assets',
            params={'name':name},headers={'Content-Type':'application/octet-stream'},content=blob)
        if response.status_code!=201: raise ValueError('Upload uncertain; resume same ID')
        self.assets[name]=response.json();self.total+=len(blob);self.new_assets+=1
        self.asset_count+=1
        self.release['assets'].append(self.assets[name])
        if await self.get(name)!=blob: raise ValueError('Read-after-write verification failed')
        return blob,self.assets[name]

    async def finish(self,proof=None):
        if self.release.get('draft'):
            changes={'draft':False}
            if proof is not None:
                body='history-proof-v1:'+base64.b64encode(seal(canonical(proof),self.key,self.archive_id,'summary.enc')).decode()
                if len(body)>16000:raise ValueError('Publication receipt budget')
                changes['body']=body
            await self.request('PATCH',f'/repos/{self.repo}/releases/{self.release["id"]}',json=changes)
            self.release.update(changes)


async def publish_partition(store,bundle,source_id,created_at,verify):
    await store.open(bundle['generation'],create=True)
    bundle_hash=sha(canonical([source_id,bundle['generation'],bundle['index'],bundle['gaps'],bundle['counts'],bundle['eligibility']]))
    def descriptor(pin):
        return dict(generation=bundle['generation'],**pin,source_id=source_id,matches=bundle['index'],
            gaps=bundle['gaps'],plaintext_bytes=sum(map(len,bundle['chunks'])))
    body=store.release.get('body','')
    if not store.release.get('draft') and body.startswith('history-proof-v1:'):
        # Authenticated publication receipt plus immutable GitHub asset IDs lets
        # resumed large inventories avoid downloading every old scope again.
        proof=parse(unseal(base64.b64decode(body.split(':',1)[1],validate=True),store.key,store.archive_id,'summary.enc'))
        inventory={name:[asset['id'],asset['size']] for name,asset in store.assets.items()}
        if proof['bundle_hash']!=bundle_hash or proof['assets']!=inventory or proof['postgres_restore'] is not True:
            raise ValueError('Completed generation receipt conflict')
        return descriptor(proof['pin'])
    # Each partition uses deterministic content ID. A rerun reuses the original
    # observation cutoff, then validates unchanged bytes before resuming.
    old=await store.get('plan.enc')
    metadata=dict(source_id=source_id,created_at=created_at,index=bundle['index'],gaps=bundle['gaps'])
    if old:
        metadata=parse(unseal(old,store.key,store.archive_id,'plan.enc'))
        if metadata['source_id']!=source_id or metadata['index']!=bundle['index'] or metadata['gaps']!=bundle['gaps']:
            raise ValueError('Partition source conflict')
    await store.put_raw('plan.enc',canonical(metadata))
    chunks=[]
    for i,raw in enumerate(bundle['chunks']):
        name=f'chunk-{i:06d}.enc'
        blob,asset=await store.put_raw(name,raw)
        chunks.append(dict(id=asset['id'],name=name,size=len(blob),sha256=sha(blob),raw_sha256=sha(raw),raw_bytes=len(raw),rows=len(parse(raw))))
    coverage=dict(bundle['coverage']);coverage['from']=coverage.pop('from_');coverage['to']=metadata['created_at']
    catalog=dict(schema=SCHEMA,schema_digest=schema_digest(),generation=bundle['generation'],source_id=source_id,
        created_at=metadata['created_at'],coverage=coverage,chunks=chunks,counts=bundle['counts'],eligibility=bundle['eligibility'])
    validate_catalog(catalog,bundle['generation'])
    receipt=await verify(catalog,bundle['chunks'])
    if not all(receipt.get(x) is True for x in ('postgres_restore','read_through','exact_bytes')):
        raise ValueError('PostgreSQL verification required before publication')
    await store.put_raw('receipt.enc',canonical(receipt))
    blob,asset=await store.put_raw('catalog.enc',canonical(catalog))
    pin=dict(asset_id=asset['id'],size=len(blob),sha256=sha(blob))
    proof=dict(bundle_hash=bundle_hash,postgres_restore=True,pin=pin,
        assets={name:[value['id'],value['size']] for name,value in store.assets.items()})
    await store.finish(proof)
    return descriptor(pin)


async def export(source,store,run_id,now,verify):
    await store.open(run_id,create=True)
    previous=await store.get('plan.enc')
    if previous:
        checkpoint=parse(unseal(previous,store.key,run_id,'plan.enc'))
        if checkpoint['namespace']!=source.namespace or checkpoint['source_fingerprint']!=await source.fingerprint(): raise ValueError('Source namespace/database changed')
        now=datetime.fromisoformat(checkpoint['cutoff'])
    else:
        checkpoint=dict(namespace=source.namespace,cutoff=now.isoformat(),source_fingerprint=await source.fingerprint())
        await store.put_raw('plan.enc',canonical(checkpoint))
    partitions=[];report=None
    async for item in plan(source,now):
        if item.get('report'): report=item;continue
        if len(partitions)>=MAX_PARTITIONS: raise ValueError('Partition inventory budget; no partial root publication')
        partitions.append(await publish_partition(store,item,source.namespace,now.isoformat(),verify))
    if report is None: raise ValueError('Incomplete inventory')
    index=dict(schema='history-index-v1',run_id=run_id,source_id=source.namespace,
        created_at=now.isoformat(),partitions=partitions,coverage=report,
        restore_verified=bool(partitions),eviction_authorized=False)
    # Root is the atomic visibility boundary: orphaned staged/published partitions
    # are not discovered by readers until this complete index is pinned.
    await store.open(run_id,create=True)
    blob,asset=await store.put_raw('index.enc',canonical(index))
    if partitions:await store.finish()
    return dict(status='verified' if partitions else 'no_eligible_records',
        index_asset_id=asset['id'],index_bytes=len(blob),index_sha256=sha(blob),run_id=run_id,
        partitions=len(partitions),covered=len(report['covered_ids']),retained=len(report['retained']),
        source_space_reclaimed=0,eviction_authorized=False,full_database_backup=False)


async def run(args):
    if os.getenv('HISTORY_EXPORT_ENABLED')!='true': raise ValueError('Export disabled')
    # No application DATABASE_URL, migrations, worker or provider calls.
    store=HistoryStore(REPOSITORY,os.environ['HISTORY_EXPORT_TOKEN'],os.environ['HISTORY_ARCHIVE_KEY'],
        budget_bytes=int(os.environ['HISTORY_ARCHIVE_BUDGET_BYTES']),keep_days=3650)
    try:
        async def verify(catalog,chunks):
            return await verify_postgres(os.environ['HISTORY_VERIFY_DATABASE_URL'],catalog,chunks)
        async with snapshot(os.environ['HISTORY_SOURCE_DATABASE_URL']) as conn:
            return await export(HistorySource(conn,os.environ['HISTORY_SOURCE_NAMESPACE']),store,args.run_id,
                                datetime.now(timezone.utc),verify)
    finally: await store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    try:
        report=asyncio.run(run(args));print(canonical(report).decode())
        if report['status']!='verified':parser.exit(2,'No eligible verified production archive published.\n')
    except Exception: parser.exit(1,'History export incomplete; no verified production coverage claimed. Resume unchanged data with the same run ID.\n')


if __name__=='__main__': main()
