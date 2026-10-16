"""Bounded, short-lived read snapshots and single-flight computation per database.

No expired-snapshot fallback on error; successful reads have a bounded TTL.
Waiting callers do not check out connections. Cancellation releases the lock.
"""
import asyncio
from collections import OrderedDict
from copy import deepcopy
import time
from weakref import WeakKeyDictionary, WeakValueDictionary


class ReadCache:
    def __init__(self, ttl=30, limit=128):
        self.ttl, self.limit = ttl, limit
        self.databases = WeakKeyDictionary()

    async def get(self, engine, key, compute):
        # Local SQLite tests deliberately see writes immediately.
        if engine.dialect.name != 'postgresql':
            return await compute()
        state = self.databases.setdefault(engine, (WeakValueDictionary(), OrderedDict()))
        locks, entries = state
        lock = locks.setdefault(key, asyncio.Lock())
        cached = entries.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return deepcopy(cached[1])
        async with asyncio.timeout(12):
            async with lock:
                cached = entries.get(key)
                if cached and time.monotonic() - cached[0] < self.ttl:
                    entries.move_to_end(key)
                    return deepcopy(cached[1])
                result = await compute()
                entries[key] = (time.monotonic(), deepcopy(result))
                while len(entries) > self.limit:
                    old_key, _ = entries.popitem(last=False)
                    # Weak lock references disappear once no request holds or awaits them.
                return result


summary_cache = ReadCache(ttl=20)
prediction_cache = ReadCache(ttl=30)
performance_cache = ReadCache(ttl=30)

revision_cache = ReadCache(ttl=3)
