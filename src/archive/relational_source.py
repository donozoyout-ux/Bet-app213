"""Bounded, same-snapshot, SELECT-only PostgreSQL capture for full archives."""
import hashlib
import os
from pathlib import Path
from sqlalchemy.engine import make_url
import subprocess
import sys
import threading
import time
import psycopg
from .recovery_format import MAX_DUMP
from .recovery_inventory import inventory, sequence_inventory

MAX_SOURCE_BYTES=8*1024*1024*1024
MAX_RELATIONS=1000
MAX_SNAPSHOT_SECONDS=300
MAX_STATEMENT_MS=120_000
LOCK_TIMEOUT_MS=2_000


def _reader_role(conn):
    unsafe=conn.execute("""
        SELECT EXISTS (SELECT 1 FROM pg_roles r
          WHERE pg_has_role(current_user,r.oid,'MEMBER')
            AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls))
        OR has_database_privilege(current_user,current_database(),'CREATE')
        OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            AND has_schema_privilege(current_user,n.oid,'CREATE'))
        OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','v','m','f','S')
          AND (pg_has_role(current_user,c.relowner,'MEMBER')
            OR CASE WHEN c.relkind='S' THEN has_sequence_privilege(current_user,c.oid,'USAGE,UPDATE')
               ELSE has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
                 OR has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE') END))
        OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            AND c.relkind IN ('r','p') AND c.relrowsecurity)
    """).fetchone()[0]
    if unsafe:raise ValueError('Dedicated SELECT-only source credential required')
    if conn.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
        raise ValueError('Source transaction is not read only')


def _libpq_url(url):
    # libpq does not recognize SQLAlchemy driver-qualified schemes.
    if url.startswith('postgresql+psycopg://'):
        return 'postgresql://' + url.removeprefix('postgresql+psycopg://')
    if url.startswith('postgresql+asyncpg://'):
        return 'postgresql://' + url.removeprefix('postgresql+asyncpg://')
    return url


def capture_source(url, dump_path, *, pg_dump='docker:postgres:18', timeout=MAX_SNAPSHOT_SECONDS,
                   allow_local_test_source=False):
    """Dump and fingerprint one exported PostgreSQL snapshot without source writes.

    The source snapshot transaction stays open while pg_dump imports its exact
    snapshot. Its total lifetime is bounded; errors never include connection
    strings, SQL or server stderr.
    """
    destination=Path(dump_path)
    if destination.exists():raise ValueError('Dump destination already exists')
    if not 1<=timeout<=MAX_SNAPSHOT_SECONDS:raise ValueError('Invalid source snapshot timeout')
    parsed=make_url(url)
    if parsed.query.get('sslmode')!='verify-full':
        if not allow_local_test_source or parsed.host not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('Source TLS hostname verification is required')
    started=time.monotonic()
    with psycopg.connect(url,connect_timeout=10,application_name='betapp-full-archive-readonly') as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute(f"SET LOCAL statement_timeout='{MAX_STATEMENT_MS}ms'")
        conn.execute(f"SET LOCAL lock_timeout='{LOCK_TIMEOUT_MS}ms'")
        conn.execute(f"SET LOCAL idle_in_transaction_session_timeout='{timeout*1000}ms'")
        conn.execute("SET LOCAL search_path=public,pg_catalog")
        timer=threading.Timer(timeout,conn.cancel);timer.daemon=True;timer.start()
        try:
            return _capture_transaction(conn,url,parsed,destination,pg_dump,timeout,started,
                                        allow_local_test_source=allow_local_test_source)
        finally:
            timer.cancel()


def _capture_transaction(conn,url,parsed,destination,pg_dump,timeout,started,*,allow_local_test_source=False):
    _reader_role(conn)
    size=conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    if not 0<size<=MAX_SOURCE_BYTES:raise ValueError('Source database byte budget exceeded')
    count=conn.execute("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','m','f')""").fetchone()[0]
    if count>MAX_RELATIONS:raise ValueError('Source relation budget exceeded')
    snapshot=conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
    source_inventory=inventory(conn)
    env={**os.environ,'PGDATABASE':parsed.database,
         'PGHOST':parsed.host,'PGPORT':str(parsed.port or 5432),
         'PGUSER':parsed.username or '', 'PGPASSWORD':parsed.password or '',
         'PGSSLMODE':parsed.query.get('sslmode','verify-full'),
         'PGOPTIONS':f'-c default_transaction_read_only=on -c statement_timeout={MAX_STATEMENT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}'}
    command=[pg_dump,'--format=custom','--compress=6','--no-owner','--no-acl','--snapshot',snapshot]
    if pg_dump.startswith('docker:'):
        image=pg_dump.split(':',1)[1]
        if image!='postgres:18':raise ValueError('Only the pinned local postgres:18 image is supported')
        local_host=parsed.host in {'localhost','127.0.0.1','::1'}
        linux_local_test=allow_local_test_source and local_host and sys.platform.startswith('linux')
        if local_host and not linux_local_test:
            parsed=parsed.set(host='host.docker.internal')
        cert_args=[]
        for option,variable in (('sslrootcert','PGSSLROOTCERT'),('sslcert','PGSSLCERT'),('sslkey','PGSSLKEY')):
            cert=parsed.query.get(option)
            if cert and cert!='system':
                cert_path=Path(cert).expanduser().resolve(strict=True)
                if not cert_path.is_file():raise ValueError('TLS certificate path is not a file')
                mount=f'/run/pgcert/{variable.lower()}'
                cert_args.extend(['--volume',f'{cert_path}:{mount}:ro'])
                env[variable]=mount
        env['PGSSLMODE']=parsed.query.get('sslmode','verify-full')
        env.update(PGDATABASE=parsed.database,PGHOST=parsed.host,PGPORT=str(parsed.port or 5432),
                   PGUSER=parsed.username or '',PGPASSWORD=parsed.password or '',
                   PGSSLMODE=parsed.query.get('sslmode','verify-full'))
        network_args=['--network','host'] if linux_local_test else ['--add-host=host.docker.internal:host-gateway']
        command=['docker','run','--rm','--pull=never',*network_args,
                 *cert_args,
                 *sum((['--env',key] for key in ('PGDATABASE','PGHOST','PGPORT','PGUSER','PGPASSWORD','PGSSLMODE','PGOPTIONS')),[]),
                 *sum((['--env',key] for key in ('PGSSLROOTCERT','PGSSLCERT','PGSSLKEY') if key in env),[]),
                 '--volume',str(destination.parent.resolve())+':/archive',
                 image,'pg_dump','--format=custom','--compress=6','--no-owner','--no-acl',
                 '--snapshot',snapshot,'--file','/archive/'+destination.name]
    else:
        command.extend(['--file',str(destination)])
    try:
        subprocess.run(command,
                       env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       timeout=min(timeout,MAX_SNAPSHOT_SECONDS))
    except Exception:
        destination.unlink(missing_ok=True)
        raise ValueError('Same-snapshot pg_dump failed') from None
    if sequence_inventory(conn)!=source_inventory['sequences']:
        destination.unlink(missing_ok=True)
        raise ValueError('Sequence state changed during source capture')
    elapsed=time.monotonic()-started
    if elapsed>timeout:
        destination.unlink(missing_ok=True)
        raise ValueError('Source snapshot time budget exceeded')
    if not destination.is_file() or not 0<destination.stat().st_size<=MAX_DUMP:
        destination.unlink(missing_ok=True)
        raise ValueError('Full dump size budget exceeded')
    digest=hashlib.sha256()
    with destination.open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    return dict(dump_sha256=digest.hexdigest(),dump_bytes=destination.stat().st_size,
                source_snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
                source_database_bytes=size,source_relation_count=count,
                source_inventory=source_inventory,snapshot_seconds=round(elapsed,3))
