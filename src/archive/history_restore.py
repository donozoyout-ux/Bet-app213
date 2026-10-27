"""Isolated restore only. Never connects to or initializes the active database."""
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine, text, DateTime, JSON, Float, Integer, Boolean, String
from src.models import Base
from .history_format import TABLES, parse, sha, eligibility, timestamp

MAX_RESTORE_BYTES = 256 * 1024 * 1024


def restore(path, catalog, chunks):
    """Exact archived bytes plus typed relational read model, with FK validation.

    Caller owns a fresh temporary directory; existing files are never replaced.
    All schema comes from application code, never SQL supplied by the archive.
    """
    path = Path(path)
    path.touch(exist_ok=False)
    engine = create_engine('sqlite:///' + path.as_posix())
    try:
        Base.metadata.create_all(engine)
        counts = dict.fromkeys(TABLES, 0)
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE archive_objects (name TEXT PRIMARY KEY, raw BLOB NOT NULL)'))
            for meta, raw in zip(catalog['chunks'], chunks, strict=True):
                if len(raw) != meta['raw_bytes'] or sha(raw) != meta['raw_sha256']:
                    raise ValueError('Plaintext integrity mismatch')
                records = parse(raw)
                if not isinstance(records, list) or len(records) != meta['rows']:
                    raise ValueError('Chunk row count mismatch')
                for record in records:
                    if set(record) != {'table', 'row'} or record['table'] not in TABLES:
                        raise ValueError('Unapproved table')
                    table = Base.metadata.tables[record['table']]
                    row = record['row']
                    if set(row) != set(table.columns.keys()): raise ValueError('Incomplete row schema')
                    values = {}
                    for col in table.columns:
                        value = row[col.name]
                        if value is None:
                            if not col.nullable: raise ValueError('Missing required value')
                        elif isinstance(col.type, DateTime):
                            value = timestamp(value)
                        elif isinstance(col.type, Boolean):
                            if type(value) is not bool: raise ValueError('Boolean type')
                        elif isinstance(col.type, Integer):
                            if type(value) is not int: raise ValueError('Integer type')
                        elif isinstance(col.type, Float):
                            if type(value) not in (int, float): raise ValueError('Number type')
                        elif isinstance(col.type, String):
                            if not isinstance(value, str): raise ValueError('String type')
                        elif not isinstance(col.type, JSON): raise ValueError('Unsupported column type')
                        values[col.name] = value
                    if table.name == 'matches':
                        if eligibility(row, catalog['eligibility'].get(str(row['id']), {}), timestamp(catalog['created_at'])):
                            raise ValueError('Ineligible match')
                        cover = catalog['coverage']
                        if not timestamp(cover['from']) <= timestamp(row['kickoff_at']) < timestamp(cover['to']) or row['league_id'] not in cover['league_ids']:
                            raise ValueError('Match outside declared coverage')
                        if row['odds_complete'] is not True: raise ValueError('Incomplete odds')
                    if table.name == 'match_statistics' and row['is_final'] is not True:
                        raise ValueError('Nonfinal statistics')
                    conn.execute(table.insert().values(**values))
                    counts[table.name] += 1
                conn.execute(text('INSERT INTO archive_objects VALUES (:name, :raw)'), {'name': meta['name'], 'raw': raw})
                recovered = conn.scalar(text('SELECT raw FROM archive_objects WHERE name=:name'), {'name': meta['name']})
                if recovered != raw: raise ValueError('Exact roundtrip failed')
                pages = conn.scalar(text('PRAGMA page_count'))
                page_size = conn.scalar(text('PRAGMA page_size'))
                if pages * page_size > MAX_RESTORE_BYTES: raise ValueError('Restore disk budget exceeded')
            if counts != catalog['counts']: raise ValueError('Table counts differ')
            if conn.execute(text('PRAGMA foreign_key_check')).first(): raise ValueError('Missing dependency')
            if conn.scalar(text('PRAGMA integrity_check')) != 'ok': raise ValueError('Restore integrity')
            matches = {str(row[0]) for row in conn.execute(text('SELECT id FROM matches'))}
            if matches != set(catalog['eligibility']): raise ValueError('Eligibility identity mismatch')
            # These provenance references predate SQL foreign keys. Validate them
            # explicitly rather than silently discarding publication/report history.
            versions = set(conn.scalars(text('SELECT version FROM board_editions')))
            for table in ('prediction_publications', 'published_match_analyses'):
                if not set(conn.scalars(text(f'SELECT board_version FROM {table}'))) <= versions:
                    raise ValueError('Missing publication provenance')
            reports = Base.metadata.tables['learning_reports']
            settlements = Base.metadata.tables['prediction_settlements']
            decisions = {(r.match_id, r.pick_index, r.revision, r.snapshot_hash, r.evidence_hash)
                         for r in conn.execute(settlements.select())}
            for manifest in conn.scalars(reports.select().with_only_columns(reports.c.source_manifest)):
                for ref in manifest:
                    identity = tuple(ref[k] for k in ('match_id', 'pick_index', 'revision', 'snapshot_hash', 'evidence_hash'))
                    if identity not in decisions: raise ValueError('Missing lesson source evidence')
            # An attestation cannot hide a missing settlement for a saved pick.
            for mid, picks in conn.execute(Base.metadata.tables['prediction_snapshots'].select().with_only_columns(
                    Base.metadata.tables['prediction_snapshots'].c.match_id,
                    Base.metadata.tables['prediction_snapshots'].c.picks)):
                settled = set(conn.scalars(text('SELECT DISTINCT pick_index FROM prediction_settlements WHERE match_id=:mid'), {'mid': mid}))
                if not set(range(len(picks))) <= settled: raise ValueError('Unsettled saved prediction')
    except BaseException:
        engine.dispose()
        path.unlink(missing_ok=True)
        raise
    finally:
        engine.dispose()
    return path
