"""Restore to a new, isolated audit catalog; never mutate live application tables."""
import sqlite3
from contextlib import closing
from pathlib import Path
from src.archive.crypto import write_new
from src.archive.schema import canonical,validate


def restore_new(data,destination):
    data=validate(data);destination=Path(destination)
    # Reserve destination exclusively. Existing databases are never overwritten.
    write_new(destination,b'')
    try:
        with closing(sqlite3.connect(destination)) as conn, conn:
            conn.execute('CREATE TABLE archive_metadata (payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE archive_records (kind TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind, identity))')
            conn.execute('INSERT INTO archive_metadata VALUES (?)',(canonical({k:v for k,v in data.items() if k not in ('forecasts','decisions','coupons','reports')}).decode(),))
            for kind in ('forecasts','decisions','coupons','reports'):
                for record in data[kind]:
                    identity=str(record['match_id'] if kind=='forecasts' else record['id'])
                    conn.execute('INSERT INTO archive_records VALUES (?,?,?)',(kind,identity,canonical(record).decode()))
            for table in ('archive_records','archive_metadata'):
                for operation in ('UPDATE','DELETE','INSERT'):
                    conn.execute(f"CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'read-only audit archive'); END")
            if conn.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise ValueError('Catalog integrity failure')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def read_catalog(path):
    import json
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as conn:
        data=json.loads(conn.execute('SELECT payload FROM archive_metadata').fetchone()[0])
        for kind in ('forecasts','decisions','coupons','reports'):
            data[kind]=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM archive_records WHERE kind=? ORDER BY identity',(kind,))]
    return validate(data)
