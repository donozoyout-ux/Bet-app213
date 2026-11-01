-- Owner-reviewed preparation ONLY. Never executed by the preflight.
-- Run using psql -X in the intended source database as its owner.
-- A pre-existing role aborts the whole transaction: do not reuse an app login.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
CREATE ROLE kupon_archive_reader LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
ALTER ROLE kupon_archive_reader SET default_transaction_read_only = on;
ALTER ROLE kupon_archive_reader SET statement_timeout = '120s';
ALTER ROLE kupon_archive_reader SET lock_timeout = '2s';
ALTER ROLE kupon_archive_reader SET idle_in_transaction_session_timeout = '300s';
SELECT format('GRANT CONNECT ON DATABASE %I TO kupon_archive_reader', current_database()) \gexec
-- Existing application relations only, including partition children and sequences.
-- No pg_read_all_data, role memberships, ownership or grant options.
SELECT format('GRANT USAGE ON SCHEMA %I TO kupon_archive_reader', nspname)
FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' \gexec
SELECT format('GRANT SELECT ON TABLE %I.%I TO kupon_archive_reader', n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
AND c.relkind IN ('r','p','v','m','f') \gexec
SELECT format('GRANT SELECT ON SEQUENCE %I.%I TO kupon_archive_reader', n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
AND c.relkind='S' \gexec
COMMIT;
-- New login has no password until the owner sets one interactively (hidden input).
-- Do not place a password in SQL, a command line, shell history or a transcript.
\password kupon_archive_reader
-- No default-privilege or PUBLIC changes: new objects require reviewed SELECT
-- grants and another preflight. PUBLIC CREATE or callable privileged routines
-- can still make this role unsafe; the preflight reports and blocks those cases.
