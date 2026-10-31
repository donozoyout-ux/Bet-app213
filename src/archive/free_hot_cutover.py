"""Strictly local PostgreSQL 18 rehearsal for a future zero-cost cutover.

Only an explicitly owner-labeled disposable Docker verifier is writable here.
NEVER point this operation at Render, Neon or any remote PostgreSQL server.
The source encrypted offsite recovery asset remains immutable and complete.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass

import psycopg
from psycopg import sql


class CutoverBlocked(RuntimeError):
    pass


ALLOWED_REFERENCERS = frozenset({'public.board_heads', 'public.board_read_publications'})
MUST_HAVE_TABLES = frozenset({'public.board_editions', 'public.board_heads', 'public.board_read_publications'})
MAX_ORIGINAL_BOARD_VERSIONS = 100_000


def _qualified(name):
    return name if '.' in name else 'public.' + name


def assert_local_owned_verifier(params, *, docker_run=subprocess.run, environ=None):
    """Fail closed before even starting a potentially destructive local SQL."""
    env = os.environ if environ is None else environ
    if params.get('host') not in {'127.0.0.1', 'localhost'}:
        raise CutoverBlocked('remote_database_forbidden')
    if params.get('port') != 5433 or params.get('dbname') != 'postgres':
        raise CutoverBlocked('isolated_pg18_port_database_required')
    owned = env.get('RECOVERY_VERIFY_CONTAINER', '')
    backup_id = env.get('RECOVERY_BACKUP_ID', '')
    if not re.fullmatch(r'[a-f0-9]{64}', owned):
        raise CutoverBlocked('owned_container_id_required')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', backup_id):
        raise CutoverBlocked('owner_run_id_required')
    try:
        command=['docker', 'inspect', '--format',
                 '{{.Config.Image}} {{index .Config.Labels "kupon.recovery.owner-run"}}',
                 owned]
        result=docker_run(command,capture_output=True,text=True,check=True,timeout=15)
        if result.stdout.strip() != 'postgres:18 ' + backup_id:
            raise CutoverBlocked('owned_container_label_mismatch')
        bound=docker_run(['docker','port',owned,'5432/tcp'],capture_output=True,
                         text=True,check=True,timeout=15)
        if not any(line.strip()=='127.0.0.1:5433' for line in bound.stdout.splitlines()):
            raise CutoverBlocked('local_verifier_port_binding_mismatch')
    except CutoverBlocked:
        raise
    except Exception:
        raise CutoverBlocked('local_container_proof_unavailable') from None
    return True


def board_references(conn):
    """Read PostgreSQL's FK metadata rather than guessing impacted tables."""
    relations = {_qualified(r[0]) for r in conn.execute(
        "SELECT n.nspname || '.' || c.relname "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='r'")}
    if not MUST_HAVE_TABLES <= relations:
        raise CutoverBlocked('expected_board_schema_missing')
    referencers={_qualified(r[0]) for r in conn.execute(
        "SELECT conrelid::regclass::text FROM pg_constraint "
        "WHERE contype='f' AND confrelid='public.board_editions'::regclass")}
    if not referencers <= ALLOWED_REFERENCERS or not referencers:
        raise CutoverBlocked('unknown_board_foreign_key')
    versions = {r[0] for r in conn.execute(
        "SELECT version FROM public.board_heads "
        "UNION SELECT version FROM public.board_read_publications")}
    if not versions or len(versions)>MAX_ORIGINAL_BOARD_VERSIONS:
        raise CutoverBlocked('no_active_board_references_or_budget')
    missing=conn.execute(
        "SELECT count(*) FROM ("
        "SELECT version FROM public.board_heads "
        "UNION SELECT version FROM public.board_read_publications"
        ") required LEFT JOIN public.board_editions b USING(version) "
        "WHERE b.version IS NULL").fetchone()[0]
    if missing: raise CutoverBlocked('dangling_board_versions')
    return versions


def plan_local_hot_seed(conn):
    """Small catalog stats and FK check, no source-row content materialization."""
    versions=board_references(conn)
    total=conn.execute('SELECT count(*) FROM public.board_editions').fetchone()[0]
    kept=conn.execute(
        'SELECT count(*) FROM public.board_editions b '
        'WHERE EXISTS(SELECT 1 FROM public.board_heads h WHERE h.version=b.version) '
        'OR EXISTS(SELECT 1 FROM public.board_read_publications p WHERE p.version=b.version)'
    ).fetchone()[0]
    if kept != len(versions) or total < kept:
        raise CutoverBlocked('board_reference_count_mismatch')
    # pg_column_size only scans the few referenced rows.
    payload_bytes=conn.execute(
        'SELECT coalesce(sum(pg_column_size(b.payload)),0) FROM public.board_editions b '
        'WHERE EXISTS(SELECT 1 FROM public.board_heads h WHERE h.version=b.version) '
        'OR EXISTS(SELECT 1 FROM public.board_read_publications p WHERE p.version=b.version)'
    ).fetchone()[0]
    before_size=conn.execute(
        "SELECT pg_total_relation_size('public.board_editions')").fetchone()[0]
    return dict(stage='local_plan_only',restored_versions=total,
                active_versions=kept,archived_only_versions=total-kept,
                active_payload_bytes=payload_bytes,original_board_relation_bytes=before_size,
                source_database_modified=False,remote_database_modified=False,
                archive_read_through_verified=False,source_row_content_verified=False,
                owner_new_database_created=False,eviction_authorized=False)


def _board_digest(conn, *, retained_only):
    """Optional exact data parity for retained rows, computed in the local verifier."""
    from .full_recovery import row_fingerprint
    query=sql.SQL('SELECT row_to_json(b)::text FROM public.board_editions b')
    if retained_only:
        query += sql.SQL(
            ' WHERE EXISTS(SELECT 1 FROM public.board_heads h WHERE h.version=b.version) '
            'OR EXISTS(SELECT 1 FROM public.board_read_publications p WHERE p.version=b.version)')
    return row_fingerprint(conn,query)


def apply_only_to_owned_local_verifier(params):
    """Only changes the temporary 127.0.0.1:5433 recovery clone, never source.

    This is NOT a production-ready migration, backup or license to drop the source
    board journal. The full original lives only in the protected rescue archive.
    """
    assert_local_owned_verifier(params)
    with psycopg.connect(**params) as conn:
        if not 180000 <= int(conn.execute('SHOW server_version_num').fetchone()[0]) < 190000:
            raise CutoverBlocked('pg18_required')
        plan=plan_local_hot_seed(conn)
        original_kept_count, original_kept_hash=_board_digest(conn,retained_only=True)
        if original_kept_count != plan['active_versions']:
            raise CutoverBlocked('retained_content_count_mismatch')
        # Confirm immutable encrypted recovery package was separately verified
        # by the owner before this branch is used; no source contact here.
        if os.environ.get('OWNER_FULL_RESCUE_VERIFIED') != 'true':
            raise CutoverBlocked('immutable_full_rescue_proof_required')
        removed=conn.execute(
            "DELETE FROM public.board_editions b "
            "WHERE NOT EXISTS (SELECT 1 FROM public.board_heads h WHERE h.version=b.version) "
            "AND NOT EXISTS (SELECT 1 FROM public.board_read_publications p WHERE p.version=b.version)"
        ).rowcount
        if removed != plan['archived_only_versions']:
            raise CutoverBlocked('prune_count_mismatch')
        conn.commit()
        after_kept_count,after_kept_hash=_board_digest(conn,retained_only=False)
        if (after_kept_count,after_kept_hash)!=(original_kept_count,original_kept_hash):
            raise CutoverBlocked('local_retained_board_digest_mismatch')
        # Only VACUUM the isolated owned verifier after logical validation.
        conn.commit()
        conn.autocommit=True
        conn.execute('VACUUM (FULL, ANALYZE) public.board_editions')
        compact=conn.execute(
            "SELECT pg_total_relation_size('public.board_editions')").fetchone()[0]
        return dict(plan,stage='local_copy_pruned_rehearsal',
                    local_removed_versions=removed,kept_content_sha256=after_kept_hash,
                    local_board_relation_bytes=compact,
                    source_database_modified=False,remote_database_modified=False,
                    archive_read_through_verified=False,eviction_authorized=False)


def main():
    """Do not connect to a remote source or accept external URLs as input."""
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply-local',action='store_true')
    args=parser.parse_args()
    # Local-only credentials are supplied through env, never printed.
    params=dict(host='127.0.0.1',port=5433,dbname='postgres',
                user='postgres',password=os.getenv('RECOVERY_LOCAL_PASSWORD',''),
                sslmode='disable',connect_timeout=10)
    try:
        assert_local_owned_verifier(params)
        if args.apply_local:
            result=apply_only_to_owned_local_verifier(params)
        else:
            with psycopg.connect(**params) as conn:
                result=plan_local_hot_seed(conn)
        print(json.dumps(result,sort_keys=True))
    except Exception:
        print('FREE_CUTOVER_INCOMPLETE: local rehearsal only; no source cleanup authorized')
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
