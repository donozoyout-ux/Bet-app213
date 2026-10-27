"""Opt-in hot-tier write admission. Never deletes, rewrites history or resizes."""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import event, text
from sqlalchemy.sql.dml import Insert


class StorageAdmissionError(RuntimeError):
    pass


def enabled(): return os.getenv('HISTORY_HOT_TIER','false').lower()=='true'


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
        mutation=re.match(r'\s*(INSERT|UPDATE|DELETE)\b',statement,re.I)
        if not mutation: return
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
        now=time.monotonic()
        if now-state['checked']>=5:
            # SELECT recursion is excluded above. Includes other databases/WAL
            # headroom rather than assuming pg_database_size is filesystem free.
            used=conn.exec_driver_sql('SELECT sum(pg_database_size(oid)) FROM pg_database').scalar_one()
            board=conn.exec_driver_sql("SELECT coalesce(pg_total_relation_size(to_regclass('board_editions')),0)").scalar_one()
            state.update(checked=now,database_bytes=int(used),board_bytes=int(board))
        db_cap,board_cap=limits()
        reason='database_high_watermark' if state['database_bytes']>=db_cap else None
        if isinstance(command,Insert) and table=='board_editions':
            if state['board_bytes']>=board_cap: reason='board_high_watermark'
            if len(context.compiled_parameters)!=1:reason='board_bulk_publication_disabled'
            for row in context.compiled_parameters:
                for key,payload in row.items():
                    if (key=='payload' or key.startswith('payload_m')) and len(json.dumps(payload,ensure_ascii=False).encode())>2*1024*1024:
                        reason='board_document_budget'
                leagues=[v for k,v in row.items() if k=='league_id' or k.startswith('league_id_m')]
                if len(leagues)!=1:reason='board_bulk_publication_disabled'
                for league in set(leagues):
                    latest=conn.execute(text('SELECT max(created_at) FROM board_editions WHERE league_id=:league'),{'league':league}).scalar_one()
                    if latest and (datetime.now(timezone.utc)-latest).total_seconds()<300: reason='board_publication_cooldown'
        state.update(blocked=reason is not None,reason=reason)
        if reason: raise StorageAdmissionError(reason)


def status(engine):
    state=getattr(getattr(engine,'sync_engine',engine),'_history_capacity',{})
    return dict(enabled=enabled(),**state,db_stop_bytes=limits()[0],board_stop_bytes=limits()[1],
                eviction_enabled=False,observed=bool(state.get('checked')))


async def collector_gate(database):
    if not enabled():return
    if database.engine.dialect.name!='postgresql':return
    async with database.session() as session:
        used=int(await session.scalar(text('SELECT sum(pg_database_size(oid)) FROM pg_database')))
        board=int(await session.scalar(text("SELECT coalesce(pg_total_relation_size(to_regclass('board_editions')),0)")))
        reason='database_high_watermark' if used>=limits()[0] else 'board_high_watermark' if board>=limits()[1] else None
        database.engine.sync_engine._history_capacity.update(checked=time.monotonic(),database_bytes=used,board_bytes=board,blocked=reason is not None,reason=reason)
        if reason:raise StorageAdmissionError(reason)
