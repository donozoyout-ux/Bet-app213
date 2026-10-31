"""Pinned atomic partition index and leased bounded reader cache."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import os
import re
from .history_format import MAX_CHUNK, parse, sha, unseal, timestamp
from .history_reader import GitHubHistoryObjects, HistoryReader, HistoryUnavailable


class HistoryIndex:
    def __init__(self,objects,key,run_id,asset,digest):
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',run_id) or not re.fullmatch('[a-f0-9]{64}',digest): raise ValueError('Index pin')
        if type(asset['id']) is not int or asset['id']<=0 or not 32<=asset['size']<=MAX_CHUNK+65536: raise ValueError('Index asset')
        self.objects,self.key,self.run_id,self.asset,self.digest=objects,key,run_id,asset,digest
        self.data=None;self.lock=asyncio.Lock();self.lease=asyncio.Lock();self.cache=OrderedDict()
        self.retry_at=0

    async def prepare(self):
        import time
        async with self.lock:
            if self.data is not None:return self.data
            if time.monotonic()<self.retry_at: raise HistoryUnavailable('Index cooldown')
            try:
                async with asyncio.timeout(30):
                    raw=await self.objects.get(self.asset)
                    if len(raw)!=self.asset['size'] or sha(raw)!=self.digest: raise ValueError('Index digest')
                    data=parse(unseal(raw,self.key,self.run_id,'index.enc'))
                    if data['schema']!='history-index-v1' or data['run_id']!=self.run_id or data['restore_verified'] is not True or data['eviction_authorized'] is not False:
                        raise ValueError('Index contract')
                    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',data['source_id']): raise ValueError('Index source')
                    timestamp(data['created_at'])
                    partitions=data['partitions']
                    if len(partitions)>2048: raise ValueError('Partition budget')
                    generations=set();covered=set()
                    for part in partitions:
                        if not re.fullmatch('[a-f0-9]{64}',part['generation']) or part['generation'] in generations:
                            raise ValueError('Duplicate generation')
                        generations.add(part['generation'])
                        if part['source_id']!=data['source_id']: raise ValueError('Cross-source partition')
                        if type(part['plaintext_bytes']) is not int or not 0<part['plaintext_bytes']<=64*1024*1024:raise ValueError('Partition size budget')
                        if len(part['matches'])>256: raise ValueError('Partition identity budget')
                        for match in part['matches']:
                            if match['identity']!=f"{data['source_id']}:{match['external_match_id']}": raise ValueError('Identity mismatch')
                            if any(type(match[k]) is not int or match[k]<=0 for k in ('id','external_match_id','league','home','away')): raise ValueError('Identity type')
                            timestamp(match['kickoff_at']);covered.add(match['id'])
                    report=data['coverage']
                    retained={r['id'] for r in report['retained']}
                    if covered!=set(report['covered_ids']) or covered & retained or len(covered)+len(retained)!=report['inventory_count']:
                        raise ValueError('Coverage reconciliation failed')
                    self.data=data
            except Exception:
                self.retry_at=time.monotonic()+60
                raise HistoryUnavailable('Verified index unavailable') from None
            return self.data

    @asynccontextmanager
    async def session(self,generation):
        data=await self.prepare()
        # One archive SQL lease per process bounds peak restore/memory and means
        # cached files are never evicted while another reader is using them.
        async with self.lease:
            part=next((p for p in data['partitions'] if p['generation']==generation),None)
            if part is None: raise HistoryUnavailable('Unknown archive scope')
            if generation not in self.cache:
                while len(self.cache)>=8:
                    _,old=self.cache.popitem(last=False)
                    await old.close()
                # A facade avoids closing the shared authenticated transport.
                class Objects:
                    async def get(_,asset): return await self.objects.get(asset)
                    async def close(_): pass
                self.cache[generation]=HistoryReader(Objects(),key=self.key,generation=generation,
                    catalog_asset={'id':part['asset_id'],'size':part['size']},catalog_sha256=part['sha256'])
            reader=self.cache[generation];self.cache.move_to_end(generation)
            async with reader.session() as session:
                from pathlib import Path
                def weight(r):
                    return (Path(r.directory.name)/'history.sqlite').stat().st_size if r.directory else 0
                while sum(weight(r) for r in self.cache.values())>256*1024*1024 and len(self.cache)>1:
                    _,old=self.cache.popitem(last=False);await old.close()
                if weight(reader)>256*1024*1024:raise HistoryUnavailable('Archive local cache budget')
                if not getattr(reader,'index_verified',False):
                    from sqlalchemy import select
                    from src.models import Match
                    actual={r.id:r.external_match_id for r in await session.scalars(select(Match))}
                    expected={m['id']:m['external_match_id'] for m in part['matches']}
                    if actual!=expected or reader.catalog['source_id']!=part['source_id']:
                        raise HistoryUnavailable('Index/partition identity mismatch')
                    reader.index_verified=True
                yield session,reader

    async def selected(self,league=None,teams=None,cutoff=None):
        data=await self.prepare();found=[]
        for part in data['partitions']:
            matches=[m for m in part['matches'] if (league is None or m['league']==league)
                and (teams is None or m['home'] in teams or m['away'] in teams)
                and (cutoff is None or timestamp(m['kickoff_at'])<cutoff)]
            if matches: found.append((part,matches))
        if len(found)>8: raise HistoryUnavailable('Historical query exceeds eight scopes; select a narrower scope')
        if sum(p['plaintext_bytes'] for p,_ in found)>64*1024*1024:raise HistoryUnavailable('Historical query exceeds restore budget')
        return found

    async def close(self):
        for reader in self.cache.values():await reader.close()
        self.cache.clear();await self.objects.close()


_index=None


def configured_index():
    global _index
    if os.getenv('HISTORY_INDEX_ENABLED')!='true': raise HistoryUnavailable('History index disabled')
    if _index is None:
        try:
            key=os.environ['HISTORY_ARCHIVE_KEY']
            candidate=HistoryIndex(None,key,os.environ['HISTORY_INDEX_RUN_ID'],
                {'id':int(os.environ['HISTORY_INDEX_ASSET_ID']),'size':int(os.environ['HISTORY_INDEX_BYTES'])},os.environ['HISTORY_INDEX_SHA256'])
            candidate.objects=GitHubHistoryObjects('donozoyout-ux/kuponanalizii-data-archive',os.environ['HISTORY_ARCHIVE_TOKEN'],key)
            _index=candidate
        except Exception: raise HistoryUnavailable('History index configuration unavailable') from None
    return _index


async def close_index():
    global _index
    if _index is not None:await _index.close()
    _index=None
