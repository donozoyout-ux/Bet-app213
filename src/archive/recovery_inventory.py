"""Local restore content comparison; never contacts source services."""
import hashlib
import os
import tempfile
from pathlib import Path
from contextlib import closing
from psycopg import sql

MAX_STREAM_BYTES=8*1024*1024*1024
MAX_SPOOL_BYTES=2*1024*1024*1024
MAX_ROW_BYTES=64*1024*1024

def row_fingerprint(conn, query, *, budget=None):
    """Hash every row, preserving duplicates; sorting occurs locally, not on source.

    Only 32-byte row hashes enter the temporary SQLite sorter. Server-side cursor
    batches bound application memory even for large JSON tables. PostgreSQL's
    row_to_json textual form preserves exact numeric values and original IDs.
    """
    import sqlite3
    with tempfile.TemporaryDirectory(prefix='recovery-hashes-') as folder:
        spool_path=Path(folder)/'hashes.sqlite'
        with closing(sqlite3.connect(str(spool_path))) as spool:
            spool.execute('CREATE TABLE hashes (value BLOB PRIMARY KEY, occurrences INTEGER NOT NULL) WITHOUT ROWID')
            count = 0
            streamed = 0
            with conn.cursor(name='recovery_rows') as cursor:
                cursor.itersize = 256
                cursor.execute(query)
                while rows := cursor.fetchmany(256):
                    encoded=[row[0].encode('utf-8') for row in rows]
                    if any(len(row)>MAX_ROW_BYTES for row in encoded):
                        raise ValueError('oversized_inventory_row')
                    batch_bytes=sum(map(len,encoded));streamed+=batch_bytes
                    if budget is not None:
                        next_stream=budget['stream_bytes']+batch_bytes
                        if next_stream>MAX_STREAM_BYTES:
                            raise ValueError('inventory_budget_exceeded')
                        budget['stream_bytes']=next_stream
                    elif streamed>MAX_STREAM_BYTES:
                        raise ValueError('inventory_budget_exceeded')
                    spool.executemany('INSERT INTO hashes VALUES (?,1) ON CONFLICT(value) DO UPDATE SET occurrences=occurrences+1',
                        [(hashlib.sha256(row).digest(),) for row in encoded])
                    spool.commit()
                    spool_size=os.path.getsize(spool_path)
                    if spool_size>MAX_SPOOL_BYTES:
                        raise ValueError('inventory_budget_exceeded')
                    if budget is not None:budget['spool_bytes']=max(budget['spool_bytes'],spool_size)
                    count += len(rows)
            checksum = hashlib.sha256()
            for value,occurrences in spool.execute('SELECT value,occurrences FROM hashes ORDER BY value'):
                for _ in range(occurrences):checksum.update(value)
    return count, checksum.hexdigest()


def sequence_inventory(conn):
    """Sequence state is non-MVCC, so callers can recheck it around a dump."""
    sequences=[]
    for namespace,name,data_type,start_value,min_value,max_value,increment_by,cycle,cache_size in conn.execute("""
        SELECT schemaname,sequencename,data_type,start_value,min_value,max_value,increment_by,cycle,cache_size
        FROM pg_sequences ORDER BY 1,2
    """):
        value,called=conn.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()
        sequences.append(dict(schema=namespace,name=name,value=value,called=called,
            data_type=data_type,start=start_value,minimum=min_value,maximum=max_value,
            increment=increment_by,cycle=cycle,cache=cache_size))
    return sequences


def inventory(conn, *, fingerprints=True):
    # No table-name allowlist: unknown future application tables are included.
    relations=conn.execute("SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','m','f') ORDER BY 1,2").fetchall()
    if any(kind=='f' for _,_,kind in relations):raise ValueError('foreign_tables_require_separate_backup')
    if len(relations)>1000:raise ValueError('table_inventory_budget')
    tables=[];budget={'stream_bytes':0,'spool_bytes':0}
    # Stable textual representation across source/verifier settings.
    conn.execute("SET LOCAL TimeZone='UTC'")
    conn.execute("SET LOCAL DateStyle='ISO, YMD'")
    conn.execute("SET LOCAL IntervalStyle='postgres'")
    conn.execute("SET LOCAL extra_float_digits=3")
    conn.execute("SET LOCAL bytea_output='hex'")
    for namespace,name,kind in relations:
        table = dict(schema=namespace,table=name,kind=kind)
        if fingerprints:
            # ONLY prevents a partitioned parent from hashing child rows twice;
            # each physical partition is inventoried as its own relation.
            count, checksum = row_fingerprint(conn, sql.SQL('SELECT row_to_json(t)::text FROM ONLY {}.{} t').format(sql.Identifier(namespace),sql.Identifier(name)),budget=budget)
            table['row_multiset_sha256'] = checksum
        else:
            count=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(namespace),sql.Identifier(name))).fetchone()[0]
        table['rows'] = count
        tables.append(table)
    sequences=sequence_inventory(conn)
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
                sql.SQL('SELECT row_to_json(t)::text FROM (SELECT loid,pageno,data FROM pg_largeobject) t'),budget=budget)
        _, result['large_object_ids_sha256'] = row_fingerprint(conn,
            sql.SQL('SELECT row_to_json(t)::text FROM (SELECT oid FROM pg_largeobject_metadata) t'),budget=budget)
        # Fail on unvalidated constraints and hash definitions without collecting
        # an unbounded catalog result in application memory.
        if conn.execute("""
            SELECT EXISTS (SELECT 1 FROM pg_constraint k
              JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND NOT k.convalidated)
        """).fetchone()[0]:
            raise ValueError('unvalidated_constraint_requires_review')
        # Bind the dump to the application schema, not only its rows. Values
        # returned by these catalog queries remain in the local sorter; only
        # their multiset count and digest are placed in the receipt.
        catalogs = {
            'relations': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,c.relkind,c.relpersistence,c.relrowsecurity,
                         c.relforcerowsecurity,c.reloptions,c.relispartition
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                    AND c.relkind IN ('r','p','v','m','S','f')
                  ORDER BY 1,2
                ) x
            """,
            'columns': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,c.relkind,a.attnum,a.attname,
                         format_type(a.atttypid,a.atttypmod) AS type,
                         a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default,
                         a.attidentity,a.attgenerated,a.attcollation::regcollation::text AS collation
                  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                    AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped
                  ORDER BY 1,2,4
                ) x
            """,
            'indexes': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,i.relname AS index_name,
                         ix.indisvalid,ix.indisready,ix.indisunique,ix.indisprimary,
                         pg_get_indexdef(i.oid) AS definition
                  FROM pg_index ix JOIN pg_class c ON c.oid=ix.indrelid
                  JOIN pg_class i ON i.oid=ix.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                  ORDER BY 1,2,3
                ) x
            """,
            'constraints': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,k.conname,k.contype,k.convalidated,
                         pg_get_constraintdef(k.oid) AS definition
                  FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                  ORDER BY 1,2,3
                ) x
            """,
            'triggers': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true) AS definition
                  FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND NOT t.tgisinternal
                  ORDER BY 1,2,3
                ) x
            """,
            'functions': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,p.proname,p.prokind,pg_get_function_identity_arguments(p.oid) AS identity_args,
                         pg_get_functiondef(p.oid) AS definition
                  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND p.prokind IN ('f','p')
                  ORDER BY 1,2,3,4
                ) x
            """,
            'views': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,c.relkind,pg_get_viewdef(c.oid,true) AS definition
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('v','m')
                  ORDER BY 1,2,3
                ) x
            """,
            'partitions': """
                SELECT row_to_json(x)::text FROM (
                  SELECT pn.nspname AS parent_schema,pc.relname AS parent_table,
                         cn.nspname AS child_schema,cc.relname AS child_table,
                         pg_get_expr(cc.relpartbound,cc.oid,true) AS bound
                  FROM pg_inherits i JOIN pg_class pc ON pc.oid=i.inhparent
                  JOIN pg_namespace pn ON pn.oid=pc.relnamespace
                  JOIN pg_class cc ON cc.oid=i.inhrelid JOIN pg_namespace cn ON cn.oid=cc.relnamespace
                  WHERE pn.nspname !~ '^pg_' AND pn.nspname<>'information_schema'
                  ORDER BY 1,2,3,4
                ) x
            """,
            'policies': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,p.polname,p.polcmd,p.polpermissive,
                         p.polroles::regrole[]::text AS roles,
                         pg_get_expr(p.polqual,p.polrelid,true) AS using_expression,
                         pg_get_expr(p.polwithcheck,p.polrelid,true) AS check_expression
                  FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                  ORDER BY 1,2,3
                ) x
            """,
            'types': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,t.typname,t.typtype,format_type(t.oid,NULL) AS formatted,
                         t.typnotnull,pg_get_expr(t.typdefaultbin,0) AS default,
                         (SELECT json_agg(e.enumlabel ORDER BY e.enumsortorder)::text
                            FROM pg_enum e WHERE e.enumtypid=t.oid) AS enum_labels
                  FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                    AND t.typtype IN ('e','d','r','c')
                  ORDER BY 1,2
                ) x
            """,
            'extensions': """
                SELECT row_to_json(x)::text FROM (
                  SELECT e.extname,e.extversion,n.nspname AS schema, e.extrelocatable
                  FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
                  ORDER BY 1
                ) x
            """,
            'sequence_ownership': """
                SELECT row_to_json(x)::text FROM (
                  SELECT sn.nspname AS sequence_schema,s.relname AS sequence_name,
                         tn.nspname AS table_schema,t.relname AS table_name,a.attname AS column_name,d.deptype
                  FROM pg_class s JOIN pg_namespace sn ON sn.oid=s.relnamespace
                  LEFT JOIN pg_depend d ON d.objid=s.oid AND d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass
                  LEFT JOIN pg_class t ON t.oid=d.refobjid LEFT JOIN pg_namespace tn ON tn.oid=t.relnamespace
                  LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
                  WHERE s.relkind='S' AND sn.nspname !~ '^pg_' AND sn.nspname<>'information_schema'
                  ORDER BY 1,2,3,4,5
                ) x
            """,
            'json_columns': """
                SELECT row_to_json(x)::text FROM (
                  SELECT n.nspname,c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type
                  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                  JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
                    AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
                    AND a.atttypid IN ('json'::regtype,'jsonb'::regtype)
                  ORDER BY 1,2,3
                ) x
            """,
        }
        schema_objects={}
        for name,query in catalogs.items():
            count, checksum=row_fingerprint(conn,query,budget=budget)
            schema_objects[name]=dict(objects=count,sha256=checksum)
        result['schema_objects']=schema_objects
        result['constraints']=schema_objects['constraints']
        result['foreign_key_count']=conn.execute("""
            SELECT count(*) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND k.contype='f'
        """).fetchone()[0]
        # JSON columns are part of every row fingerprint above. This marker is
        # deliberately limited to content inclusion; it does not claim external
        # application identifiers embedded in JSON are semantically resolvable.
        result['json_payloads_included_in_row_fingerprints']=True
    return result


