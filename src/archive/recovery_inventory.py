"""Local restore content comparison; never contacts source services."""
import hashlib
import tempfile
from pathlib import Path
from contextlib import closing
from psycopg import sql

def row_fingerprint(conn, query):
    """Hash every row, preserving duplicates; sorting occurs locally, not on source.

    Only 32-byte row hashes enter the temporary SQLite sorter. Server-side cursor
    batches bound application memory even for large JSON tables. PostgreSQL's
    row_to_json textual form preserves exact numeric values and original IDs.
    """
    import sqlite3
    with tempfile.TemporaryDirectory(prefix='recovery-hashes-') as folder:
        with closing(sqlite3.connect(str(Path(folder)/'hashes.sqlite'))) as spool:
            spool.execute('CREATE TABLE hashes (value BLOB NOT NULL)')
            count = 0
            with conn.cursor(name='recovery_rows') as cursor:
                cursor.itersize = 256
                cursor.execute(query)
                while rows := cursor.fetchmany(256):
                    spool.executemany('INSERT INTO hashes VALUES (?)',
                        [(hashlib.sha256(row[0].encode('utf-8')).digest(),) for row in rows])
                    count += len(rows)
            checksum = hashlib.sha256()
            for (value,) in spool.execute('SELECT value FROM hashes ORDER BY value'):
                checksum.update(value)
    return count, checksum.hexdigest()


def inventory(conn, *, fingerprints=True):
    # No table-name allowlist: unknown future application tables are included.
    relations=conn.execute("SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','m','f') ORDER BY 1,2").fetchall()
    if any(kind=='f' for _,_,kind in relations):raise ValueError('foreign_tables_require_separate_backup')
    if len(relations)>1000:raise ValueError('table_inventory_budget')
    tables=[]
    # Stable textual representation across source/verifier settings.
    conn.execute("SET LOCAL TimeZone='UTC'")
    conn.execute("SET LOCAL DateStyle='ISO, YMD'")
    conn.execute("SET LOCAL IntervalStyle='postgres'")
    conn.execute("SET LOCAL extra_float_digits=3")
    conn.execute("SET LOCAL bytea_output='hex'")
    for namespace,name,kind in relations:
        table = dict(schema=namespace,table=name,kind=kind)
        if fingerprints:
            count, checksum = row_fingerprint(conn, sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t').format(sql.Identifier(namespace),sql.Identifier(name)))
            table['row_multiset_sha256'] = checksum
        else:
            count=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()[0]
        table['rows'] = count
        tables.append(table)
    sequences=[]
    for namespace,name in conn.execute("SELECT schemaname,sequencename FROM pg_sequences WHERE schemaname !~ '^pg_' ORDER BY 1,2"):
        value,called=conn.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()
        sequences.append(dict(schema=namespace,name=name,value=value,called=called))
    large_objects=conn.execute('SELECT count(*) FROM pg_largeobject_metadata').fetchone()[0]
    result = dict(tables=tables,sequences=sequences,large_objects=large_objects)
    if fingerprints:
        # Managed PostgreSQL non-superusers may lack SELECT on pg_largeobject
        # even when there are zero large objects. pg_largeobject_metadata is
        # readable and proves the zero-object case without privileged access.
        # Never skip page hashing for a nonzero count: fail closed if unreadable.
        if large_objects == 0:
            result['large_object_pages_sha256'] = hashlib.sha256(b'').hexdigest()
        else:
            _, result['large_object_pages_sha256'] = row_fingerprint(conn,
                sql.SQL('SELECT row_to_json(t)::text FROM (SELECT loid,pageno,data FROM pg_largeobject) t'))
        _, result['large_object_ids_sha256'] = row_fingerprint(conn,
            sql.SQL('SELECT row_to_json(t)::text FROM (SELECT oid FROM pg_largeobject_metadata) t'))
        # pg_restore validates normal FK constraints; preserve definitions and validation state too.
        result['constraints'] = conn.execute("""
            SELECT n.nspname,c.relname,k.conname,k.contype,k.convalidated,
                   pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            ORDER BY 1,2,3
        """).fetchall()
        if any(not item[4] for item in result['constraints']):
            raise ValueError('unvalidated_constraint_requires_review')
    return result


