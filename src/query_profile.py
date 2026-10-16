"""Safe stage names, counts and durations; no SQL text, bind values or IDs."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import time

phase=ContextVar('query_phase',default='background')
metrics=ContextVar('query_metrics',default=None)
log=logging.getLogger(__name__)


@contextmanager
def profile(name):
    started=time.monotonic();count={};token=metrics.set(count)
    try:
        with stage(name):yield
    finally:
        metrics.reset(token)
        log.info('[PROFILE] stage=%s duration_ms=%.1f sql_count=%s sql_ms=%.1f',
            name,(time.monotonic()-started)*1000,sum(v[0] for v in count.values()),sum(v[1] for v in count.values()))
        for key,(n,ms) in count.items():
            log.info('[PROFILE] phase=%s sql_count=%s sql_ms=%.1f',key,n,ms)


@contextmanager
def stage(name):
    token=phase.set(name);started=time.monotonic()
    try:yield
    finally:
        log.info('[PHASE] stage=%s duration_ms=%.1f',name,(time.monotonic()-started)*1000)
        phase.reset(token)


def record(statement,elapsed):
    bucket=metrics.get();name=phase.get();ms=elapsed*1000
    if bucket is not None:
        n,total=bucket.get(name,(0,0));bucket[name]=(n+1,total+ms)
    if elapsed>=.25:
        log.warning('[SQL] phase=%s sql_id=%s duration_ms=%.1f',name,
            hashlib.sha256(statement.encode()).hexdigest()[:12],ms)
