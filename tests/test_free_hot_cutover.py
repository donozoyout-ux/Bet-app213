"""Free-tier cutover rehearsal uses synthetic fixtures and disposable PG18 only."""
import os
import subprocess
import pytest
import psycopg

from src.archive import free_hot_cutover as module


def test_no_remote_or_unowned_postgres_can_be_pruned():
    calls=[]
    def fake_run(command,**kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command,0,stdout='postgres:18 owner-test\n')
    safe=dict(host='127.0.0.1',port=5433,dbname='postgres')
    for params in [
        dict(safe,host='dpg-anything.frankfurt-postgres.render.com'),
        dict(safe,host='neon.tech'),
        dict(safe,port=5432),
        dict(safe,dbname='kuponanaliziivv755')]:
        with pytest.raises(module.CutoverBlocked):
            module.assert_local_owned_verifier(params,docker_run=fake_run,environ={
                'RECOVERY_VERIFY_CONTAINER':'a'*64,'RECOVERY_BACKUP_ID':'owner-test'})
    assert calls == []
    with pytest.raises(module.CutoverBlocked,match='owned_container_id_required'):
        module.assert_local_owned_verifier(safe,docker_run=fake_run,environ={})
    assert calls == []


def test_owned_container_requires_port_and_label_match():
    safe=dict(host='127.0.0.1',port=5433,dbname='postgres')
    env={'RECOVERY_VERIFY_CONTAINER':'b'*64,'RECOVERY_BACKUP_ID':'owner-test'}
    def docker(command, **kwargs):
        if command[1]=='inspect':
            return subprocess.CompletedProcess(command,0,stdout='postgres:18 owner-test\n')
        if command[1]=='port':
            return subprocess.CompletedProcess(command,0,stdout='127.0.0.1:5433\n')
        raise AssertionError('unexpected docker call')
    assert module.assert_local_owned_verifier(safe,docker_run=docker,environ=env)
    def wrong_port(command,**kwargs):
        if command[1]=='port':
            return subprocess.CompletedProcess(command,0,stdout='0.0.0.0:5433\n')
        return docker(command,**kwargs)
    with pytest.raises(module.CutoverBlocked,match='local_verifier_port_binding_mismatch'):
        module.assert_local_owned_verifier(safe,docker_run=wrong_port,environ=env)
    def wrong_label(command,**kwargs):
        if command[1]=='inspect':
            return subprocess.CompletedProcess(command,0,stdout='postgres:18 someone-else\n')
        return docker(command,**kwargs)
    with pytest.raises(module.CutoverBlocked,match='owned_container_label_mismatch'):
        module.assert_local_owned_verifier(safe,docker_run=wrong_label,environ=env)


def test_free_rehearsal_is_not_archival_activation_or_deletion_permission(tmp_path):
    from pathlib import Path
    source=Path('src/archive/free_hot_cutover.py').read_text()
    assert 'DELETE FROM public.board_editions' in source
    assert 'VACUUM (FULL, ANALYZE)' in source
    assert "host='127.0.0.1',port=5433,dbname='postgres'" in source
    assert "'owned_container_label_mismatch'" in source
    assert "archive_read_through_verified=False" in source
    assert "eviction_authorized=False" in source
    assert 'HISTORY_ARCHIVE_TOKEN' not in source
    assert 'ARCHIVE_DATABASE_URL' not in source
    assert 'pg_repack' not in source


@pytest.mark.skipif(not os.getenv('TEST_RECOVERY_VERIFY_URL'),reason='Disposable PG18 verifier absent')
def test_pg18_fixture_retains_current_heads_with_full_row_fingerprint(monkeypatch):
    """This mutates only CI's disposable PostgreSQL; production is never contacted."""
    from src.archive.full_recovery import connection_parameters
    params=connection_parameters(os.environ['TEST_RECOVERY_VERIFY_URL'],source=False)
    monkeypatch.setattr(module,'assert_local_owned_verifier',lambda _: True)
    monkeypatch.setenv('OWNER_FULL_RESCUE_VERIFIED','true')
    with psycopg.connect(**params) as conn:
        # May coexist with ordinary test tables, but namespace below is
        # constrained to its own three public tables and removed afterwards.
        conn.execute('CREATE TABLE public.board_editions(version text PRIMARY KEY,league_id integer,created_at timestamptz,payload json)')
        conn.execute('CREATE TABLE public.board_heads(version text REFERENCES public.board_editions(version))')
        conn.execute('CREATE TABLE public.board_read_publications(version text REFERENCES public.board_editions(version))')
        conn.execute("""INSERT INTO public.board_editions(version,league_id,created_at,payload)
                        VALUES ('current',1,now(),'{"real":true}'::json),
                               ('history',1,now()-interval '3 days','{"real":false}'::json),
                               ('other',1,now()-interval '5 days','{"text":"Türkçe"}'::json)""")
        conn.execute("INSERT INTO public.board_heads(version) VALUES ('current')")
        conn.execute("INSERT INTO public.board_read_publications(version) VALUES ('current')")
    try:
        with psycopg.connect(**params) as conn:
            plan=module.plan_local_hot_seed(conn)
        assert plan['restored_versions']==3 and plan['active_versions']==1
        assert plan['archived_only_versions']==2
        assert not plan['source_database_modified'] and not plan['eviction_authorized']
        result=module.apply_only_to_owned_local_verifier(params)
        assert result['local_removed_versions']==2
        assert not result['archive_read_through_verified']
        with psycopg.connect(**params) as conn:
            assert conn.execute("SELECT version FROM public.board_editions").fetchall()==[('current',)]
            assert conn.execute("SELECT count(*) FROM public.board_heads").fetchone()[0]==1
            assert conn.execute("SELECT count(*) FROM public.board_read_publications").fetchone()[0]==1
            assert conn.execute("SELECT payload::text FROM public.board_editions").fetchone()[0]=='{"real":true}'
    finally:
        with psycopg.connect(**params) as conn:
            conn.execute('DROP TABLE IF EXISTS public.board_read_publications')
            conn.execute('DROP TABLE IF EXISTS public.board_heads')
            conn.execute('DROP TABLE IF EXISTS public.board_editions')
