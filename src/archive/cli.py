"""Read-only export / isolated restore CLI. Secrets are environment variables only."""
import argparse
import asyncio
import logging
import os
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.archive.crypto import seal,read,write_new
from src.archive.schema import collect
from src.archive.restore import restore_new


async def export():
    url=os.environ['ARCHIVE_DATABASE_URL']
    if url.startswith(('postgres://','postgresql://')):url='postgresql+asyncpg://'+url.split('://',1)[1]
    # Scheduled export requires a server-enforced read-only principal/session.
    if not url.startswith('postgresql+asyncpg://'):raise ValueError('Scheduled exports require read-only PostgreSQL source credentials')
    key=os.environ['ARCHIVE_ENCRYPTION_KEY'];rights=os.environ['ARCHIVE_RIGHTS_REFERENCE']
    engine=create_async_engine(url,connect_args={'timeout':10,'command_timeout':60})
    try:
        async with engine.connect() as connection:
            connection=await connection.execution_options(isolation_level='REPEATABLE READ')
            async with connection.begin():
                await connection.execute(text('SET TRANSACTION READ ONLY'))
                # A dedicated SELECT-only principal is required, not an owner URL.
                privileged=await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolcreatedb OR rolcreaterole)) OR EXISTS (SELECT 1 FROM (VALUES ('prediction_snapshots'),('prediction_settlements'),('saved_coupons'),('learning_reports')) AS t(name) WHERE has_table_privilege(current_user,t.name,'INSERT,UPDATE,DELETE,TRUNCATE'))"))
                if privileged:raise ValueError('Archive source principal must be SELECT-only')
                data=await collect(connection,rights)
        blob=seal(data,key)
        destination=Path(os.environ.get('ARCHIVE_OUTPUT','owned-audit.enc.json'))
        write_new(destination,blob)
        # Round-trip decrypt, validate, then isolated database restore before upload.
        import tempfile
        from src.archive.restore import read_catalog
        with tempfile.TemporaryDirectory() as tmp:
            restored=Path(tmp)/'verify.sqlite'
            restore_new(read(destination,key),restored)
            actual=read_catalog(restored)
            for kind in ('forecasts','decisions','coupons','reports'):
                if sorted(actual[kind],key=str)!=sorted(data[kind],key=str):raise ValueError('Restore verification mismatch')
        if os.getenv('ARCHIVE_UPLOAD_ENABLED')=='true':
            from src.archive.github import upload
            await upload(os.environ['ARCHIVE_REPOSITORY'],os.environ['ARCHIVE_GITHUB_TOKEN'],blob,key)
    finally:await engine.dispose()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['export','restore']);parser.add_argument('--input');parser.add_argument('--destination');args=parser.parse_args()
    try:
        if args.command=='export':asyncio.run(export())
        else:
            if not args.input or not args.destination:parser.error('restore requires --input and --destination')
            restore_new(read(args.input,os.environ['ARCHIVE_ENCRYPTION_KEY']),args.destination)
    except Exception:
        # Never print DSNs, database exception parameters or archive contents.
        logging.error('Archive operation failed; no successful backup or restore is claimed.')
        raise SystemExit(1) from None

if __name__=='__main__':main()
