"""Disposable PostgreSQL 18 only; no production URL or GitHub credential is read."""
import os
from pathlib import Path
import subprocess
import psycopg
import pytest
from src.archive.offline_recovery import ephemeral_pg18,_restore,local_inventory
from src.archive.relational_source import capture_source
from src.archive.source_preflight import metadata_checks

pytestmark=pytest.mark.skipif(os.getenv('RUN_OFFLINE_RECOVERY_DOCKER')!='1',reason='explicit isolated Docker PostgreSQL 18 test')


def test_same_snapshot_readonly_capture_restores_every_relation(tmp_path):
    owner='relational-source-fixture'
    with ephemeral_pg18(5433,owner) as (container,params):
        with psycopg.connect(**params,autocommit=True) as admin:
            admin.execute('CREATE DATABASE archive_source')
            admin.execute("CREATE ROLE archive_reader LOGIN PASSWORD 'fixture-password'")
        source_params={**params,'dbname':'archive_source'}
        with psycopg.connect(**source_params) as admin:
            admin.execute('CREATE TABLE matches (id bigint NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(id)) PARTITION BY RANGE (id)')
            admin.execute('CREATE TABLE matches_low PARTITION OF matches FOR VALUES FROM (0) TO (100)')
            admin.execute("INSERT INTO matches VALUES (1,'{\"history\":[{\"revision\":1}]}'),(2,'{}')")
            admin.execute('CREATE TABLE prediction_results (id serial PRIMARY KEY, match_id bigint REFERENCES matches(id), record jsonb)')
            admin.execute("INSERT INTO prediction_results(match_id,record) VALUES (1,'{\"match_id\":1,\"board_version\":\"frozen\"}')")
            admin.execute("CREATE FUNCTION archived_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            admin.execute('CREATE TRIGGER keep_history BEFORE UPDATE ON prediction_results FOR EACH ROW EXECUTE FUNCTION archived_trigger()')
            admin.commit()
        with psycopg.connect(**params) as admin:
            admin.execute('GRANT CONNECT ON DATABASE archive_source TO archive_reader')
            admin.commit()
        with psycopg.connect(**source_params) as admin:
            admin.execute('GRANT USAGE ON SCHEMA public TO archive_reader')
            admin.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO archive_reader')
            admin.execute('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO archive_reader')
            admin.commit()
        url='postgresql://archive_reader:fixture-password@127.0.0.1:5433/archive_source?sslmode=disable'
        dump=tmp_path/'same-snapshot.dump'
        result=capture_source(url,dump,allow_local_test_source=True,timeout=180)
        assert result['dump_bytes']>0 and result['source_relation_count']>=3
        assert result['source_inventory']['foreign_key_count']>=1
        assert result['source_inventory']['schema_objects']['json_columns']['objects']>=2
        with ephemeral_pg18(5434,owner) as (container2,params2):
            _restore(container2,dump)
            restored=local_inventory(params2)
        assert restored==result['source_inventory']
        assert restored['tables']


def test_owner_setup_sql_and_reader_privilege_gates():
    # Executes the actual psql setup artifact only in a disposable PG18 fixture.
    with ephemeral_pg18(5433, 'reader-setup-fixture') as (container, params):
        with psycopg.connect(**params, autocommit=True) as admin:
            admin.execute('CREATE DATABASE reader_fixture')
        source = {**params, 'dbname': 'reader_fixture'}
        with psycopg.connect(**source, autocommit=True) as admin:
            admin.execute('CREATE TABLE history(id serial PRIMARY KEY, payload jsonb)')
        script = (Path(__file__).resolve().parents[1] /
                  'scripts/sql/kupon-archive-reader.sql').read_text()
        script = '\n'.join(line for line in script.splitlines()
                           if not line.startswith('\\password'))
        result = subprocess.run(['docker', 'exec', '-i', container, 'psql', '-X',
                                 '-U', 'postgres', '-d', 'reader_fixture'],
                                input=script.encode(), capture_output=True, timeout=30)
        assert result.returncode == 0
        with psycopg.connect(**source, autocommit=True) as admin:
            admin.execute("ALTER ROLE kupon_archive_reader PASSWORD 'fixture-only'")
        reader = {**source, 'user': 'kupon_archive_reader', 'password': 'fixture-only'}
        with psycopg.connect(**reader) as conn:
            conn.execute('BEGIN READ ONLY')
            checks = metadata_checks(conn)
            # Disposable fixtures intentionally use loopback without TLS.
            assert not checks.pop('tls_active')
            assert all(checks.values()), checks
            assert conn.execute("SELECT has_sequence_privilege(current_user,'history_id_seq','USAGE,UPDATE')").fetchone()[0] is False
            conn.rollback()
        # A missing SELECT blocks; granting USAGE would allow nextval and must fail.
        with psycopg.connect(**source, autocommit=True) as admin:
            admin.execute('REVOKE SELECT ON history_id_seq FROM kupon_archive_reader')
        with psycopg.connect(**reader) as conn:
            conn.execute('BEGIN READ ONLY')
            assert not metadata_checks(conn)['sequence_select']
            conn.rollback()
        with psycopg.connect(**source, autocommit=True) as admin:
            admin.execute('GRANT USAGE ON history_id_seq TO kupon_archive_reader')
        with psycopg.connect(**reader) as conn:
            conn.execute('BEGIN READ ONLY')
            assert not metadata_checks(conn)['select_only_role']


def test_optional_reader_creation_is_idempotent(monkeypatch):
    from src.archive import source_preflight as p
    from sqlalchemy.engine import make_url
    with ephemeral_pg18(5433, 'reader-bootstrap-fixture') as (container, params):
        with psycopg.connect(**params, autocommit=True) as admin:
            admin.execute('CREATE DATABASE bootstrap_fixture')
        source = {**params, 'dbname': 'bootstrap_fixture'}
        with psycopg.connect(**source, autocommit=True) as admin:
            admin.execute('CREATE TABLE history(id serial PRIMARY KEY)')
        # Only this disposable fixture bypasses TLS URL validation. The public
        # entry point always requires verify-full and an existing CA file.
        owner = make_url('postgresql://postgres@127.0.0.1:5433/bootstrap_fixture?sslmode=disable').set(password=params['password'])
        reader = owner.set(username='kupon_archive_reader', password='fixture-only')
        monkeypatch.setattr(p, 'source_url', make_url)
        assert p.prepare_reader(owner.render_as_string(hide_password=False), reader.render_as_string(hide_password=False)) == 'reader_created'
        assert p.prepare_reader(owner.render_as_string(hide_password=False), reader.render_as_string(hide_password=False)) == 'reader_existing_unchanged'
        with psycopg.connect(**{**source, 'user': 'kupon_archive_reader', 'password': 'fixture-only'}) as conn:
            conn.execute('BEGIN READ ONLY')
            checks = metadata_checks(conn)
            checks.pop('tls_active')
            assert all(checks.values()), checks
