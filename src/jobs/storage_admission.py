"""Production-default hot-tier admission. Never deletes or rewrites history."""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import event, text
from sqlalchemy.sql.dml import Insert


class StorageAdmissionError(RuntimeError):
    pass


def enabled():
    # Production cannot accidentally opt out with an absent/false migration flag.
    return (os.getenv('APP_ENV','').lower()=='production' or
            os.getenv('RENDER','').lower()=='true' or
            os.getenv('HISTORY_HOT_TIER','false').lower()=='true')


def board_limits():
    return dict(document_bytes=min(131072, int(os.getenv('HISTORY_BOARD_DOCUMENT_BYTES','131072'))),
                daily_bytes=min(8388608, int(os.getenv('HISTORY_BOARD_DAILY_BYTES','8388608'))),
                daily_count=min(64, int(os.getenv('HISTORY_BOARD_DAILY_COUNT','64'))),
                interval_seconds=max(900, int(os.getenv('HISTORY_BOARD_INTERVAL_SECONDS','900'))))


def limits():
    # May be lowered by operators; never raised beyond the conservative free cap.
    return min(700_000_000,int(os.getenv('HISTORY_DB_STOP_BYTES','700000000'))), min(200_000_000,int(os.getenv('HISTORY_BOARD_STOP_BYTES','200000000')))


def check_new_matches(parameters, now):
    for row in parameters:
        kickoffs=[value for key,value in row.items() if key=='kickoff_at' or key.startswith('kickoff_at_m')]
        if not kickoffs: raise StorageAdmissionError('hot_tier_unbounded_match_insert')
        for value in kickoffs:
            if isinstance(value,str): value=datetime.fromisoformat(value)
            if value is None: raise StorageAdmissionError('hot_tier_unknown_kickoff')
            if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
            if value < now-timedelta(hours=24): raise StorageAdmissionError('historical_insert_disabled')


def install(engine):
    state={'checked':0,'database_bytes':None,'board_bytes':None,'blocked':False,'reason':None}
    engine._history_capacity=state
    @event.listens_for(engine,'before_cursor_execute')
    def admit(conn,cursor,statement,parameters,context,executemany):
        if not enabled(): return
        sql=re.sub(r'/\*.*?\*/|--[^\n]*','',statement,flags=re.S).lstrip()
        mutation=(re.search(r'\b(INSERT|UPDATE|DELETE|COPY|TRUNCATE)\b',sql,re.I)
                  if re.match(r'WITH\b',sql,re.I) else
                  re.match(r'(INSERT|UPDATE|DELETE|COPY|TRUNCATE)\b',sql,re.I))
        if not mutation: return
        if mutation[1].upper() in {'COPY','TRUNCATE'}:
            raise StorageAdmissionError('unbounded_write_disabled')
        compiled=getattr(context,'compiled',None)
        command=getattr(compiled,'statement',None)
        table=getattr(getattr(command,'table',None),'name',None)
        if isinstance(command,Insert) and table=='matches':
            check_new_matches(context.compiled_parameters,datetime.now(timezone.utc))
        elif mutation[1].upper()=='INSERT' and re.search(r'\bmatches\b',statement,re.I) and table is None:
            raise StorageAdmissionError('raw_match_insert_disabled')
        if mutation[1].upper()=='INSERT' and table is None and re.search(r'\bboard_editions\b',statement,re.I):
            raise StorageAdmissionError('raw_board_insert_disabled')
        if conn.dialect.name!='postgresql': return  # Test-only SQLite; production requires PG.
        # Serialize all cooperating application writers across processes. Recheck
        # on every statement: a process-local 5s cache permits concurrent bursts.
        if not conn.exec_driver_sql('SELECT pg_try_advisory_xact_lock(213007)').scalar_one():
            raise StorageAdmissionError('storage_writer_busy')
        now=time.monotonic()
        # SELECT recursion is excluded above. Includes other logical databases; reserves headroom for WAL and
        # filesystem overhead not measured by pg_database_size.
        used=conn.exec_driver_sql('SELECT sum(pg_database_size(oid)) FROM pg_database').scalar_one()
        board=conn.exec_driver_sql("SELECT coalesce(pg_total_relation_size(to_regclass('board_editions')),0)").scalar_one()
        state.update(checked=now,database_bytes=int(used),board_bytes=int(board))
        db_cap,board_cap=limits()
        reason='database_high_watermark' if state['database_bytes']>=db_cap else None
        if isinstance(command,Insert) and table=='board_editions':
            if state['board_bytes']>=board_cap: reason='board_high_watermark'
            if len(context.compiled_parameters)!=1:reason='board_bulk_publication_disabled'
            for row in context.compiled_parameters:
                created=row.get('created_at')
                if not isinstance(created,datetime) or abs((datetime.now(timezone.utc)-created.replace(tzinfo=created.tzinfo or timezone.utc)).total_seconds())>60:
                    reason='board_publication_time_invalid'
                for key,payload in row.items():
                    if (key=='payload' or key.startswith('payload_m')) and len(json.dumps(payload,ensure_ascii=False).encode())>board_limits()['document_bytes']:
                        reason='board_document_budget'
                leagues=[v for k,v in row.items() if k=='league_id' or k.startswith('league_id_m')]
                if len(leagues)!=1:reason='board_bulk_publication_disabled'
                for league in set(leagues):
                    latest=conn.execute(text('SELECT max(created_at) FROM board_editions WHERE league_id=:league'),{'league':league}).scalar_one()
                    if latest and (datetime.now(timezone.utc)-latest).total_seconds()<board_limits()['interval_seconds']: reason='board_publication_cooldown'
            # Conservative rolling byte reservation avoids repeatedly detoasting
            # hundreds of MB of legacy JSON. At most 64 x 128 KiB per 24h.
            # Rolling budget includes all leagues and remains effective after restart.
            count=conn.exec_driver_sql(
                "SELECT count(*) FROM board_editions WHERE created_at >= now()-interval '24 hours'").scalar_one()
            incoming=sum(len(json.dumps(r.get('payload',{}),ensure_ascii=False).encode()) for r in context.compiled_parameters)
            if count>=board_limits()['daily_count'] or count*131072+incoming>board_limits()['daily_bytes']:
                reason='board_daily_budget'
        state.update(blocked=reason is not None,reason=reason)
        if reason: raise StorageAdmissionError(reason)


def status(engine):
    state=getattr(getattr(engine,'sync_engine',engine),'_history_capacity',{})
    return dict(enabled=enabled(),**state,db_stop_bytes=limits()[0],board_stop_bytes=limits()[1],
                board_limits=board_limits(),eviction_enabled=False,observed=bool(state.get('checked')))


async def collector_gate(database):
    if not enabled():return
    if database.engine.dialect.name!='postgresql':return
    async with database.session() as session:
        used=int(await session.scalar(text('SELECT sum(pg_database_size(oid)) FROM pg_database')))
        board=int(await session.scalar(text("SELECT coalesce(pg_total_relation_size(to_regclass('board_editions')),0)")))
        reason='database_high_watermark' if used>=limits()[0] else 'board_high_watermark' if board>=limits()[1] else None
        database.engine.sync_engine._history_capacity.update(checked=time.monotonic(),database_bytes=used,board_bytes=board,blocked=reason is not None,reason=reason)
        if reason:raise StorageAdmissionError(reason)
