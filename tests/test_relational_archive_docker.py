"""Disposable PostgreSQL 18 only; no production URL or GitHub credential is read."""
import os
import psycopg
import pytest
from src.archive.offline_recovery import ephemeral_pg18,_restore,local_inventory,CutoverBlocked
from src.archive.relational_source import capture_source

pytestmark=pytest.mark.skipif(os.getenv('RUN_OFFLINE_RECOVERY_DOCKER')!='1',reason='explicit isolated Docker PostgreSQL 18 test')


def test_same_snapshot_readonly_capture_restores_every_relation(tmp_path,monkeypatch):
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
        import src.archive.relational_source as source
        original_run=source.subprocess.run;failure=[]
        def checked_run(*args,**kwargs):
            kwargs.pop('capture_output',None)
            kwargs['stdout']=__import__('subprocess').PIPE
            kwargs['stderr']=__import__('subprocess').PIPE
            try:return original_run(*args,**kwargs)
            except __import__('subprocess').CalledProcessError as exc:
                failure.append((exc.returncode,exc.stderr.decode(errors='replace')))
                raise
            except Exception as exc:
                failure.append((type(exc).__name__,str(exc)))
                raise
        monkeypatch.setattr(source.subprocess,'run',checked_run)
        try:result=capture_source(url,dump,allow_local_test_source=True,timeout=180)
        except ValueError:
            if failure:raise AssertionError(failure[-1])
            raise
        assert result['dump_bytes']>0 and result['source_relation_count']>=3
        assert result['source_inventory']['foreign_key_count']>=1
        assert result['source_inventory']['schema_objects']['json_columns']['objects']>=2
        try:
            with ephemeral_pg18(5434,owner) as (container2,params2):
                _restore(container2,dump)
                restored=local_inventory(params2)
        except CutoverBlocked:
            if failure:raise AssertionError(failure[-1])
            raise
        assert restored==result['source_inventory']
        assert restored['tables']
