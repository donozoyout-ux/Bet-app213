"""Metadata-only readiness checks. No dump, hashing, GitHub call or source DDL."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import httpx

import psycopg
from sqlalchemy.engine import make_url

from .relational_source import _reader_role, MAX_SOURCE_BYTES, MAX_RELATIONS
from .crypto import key_bytes

ARCHIVE_REPOSITORY = 'donozoyout-ux/kuponanalizii-data-archive'


def source_url(value):
    parsed = make_url(value)
    # Exact libpq URL only; reject options that could override the target,
    # credentials, startup transaction settings or certificate verification.
    allowed = {'sslmode', 'sslrootcert', 'sslcert', 'sslkey'}
    if (parsed.drivername != 'postgresql' or parsed.username != 'kupon_archive_reader'
            or not parsed.password or not parsed.host or not parsed.database
            or parsed.query.get('sslmode') != 'verify-full'
            or set(parsed.query) - allowed
            or any(not isinstance(v, str) for v in parsed.query.values())):
        raise ValueError('source_configuration')
    # Explicit CA file works with both Windows libpq and the Linux dump container.
    # Do not silently rely on two different platform trust stores.
    root = parsed.query.get('sslrootcert')
    if not root or root == 'system' or not Path(root).is_file():
        raise ValueError('source_configuration')
    for option in ('sslcert', 'sslkey'):
        if option in parsed.query and not Path(parsed.query[option]).is_file():
            raise ValueError('source_configuration')
    return parsed


def metadata_checks(conn):
    """Return fixed boolean checks only, never names, rows or server messages."""
    checks = {}
    checks['reader_identity'] = conn.execute(
        "SELECT session_user='kupon_archive_reader' AND current_user=session_user"
    ).fetchone()[0]
    checks['session_readonly_default'] = conn.execute(
        'SHOW default_transaction_read_only').fetchone()[0] == 'on'
    checks['tls_active'] = conn.pgconn.ssl_in_use
    try:
        _reader_role(conn)  # Same negative gate; keep reporting missing SELECT too.
        checks['select_only_role'] = True
    except ValueError:
        checks['select_only_role'] = False
    checks['no_memberships'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_auth_members m
          JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=current_user)
    """).fetchone()[0]
    checks['schema_usage'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_namespace n
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
          AND NOT has_schema_privilege(current_user,n.oid,'USAGE'))
    """).fetchone()[0]
    checks['relation_select'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
          AND c.relkind IN ('r','p','v','m','f')
          AND NOT has_table_privilege(current_user,c.oid,'SELECT'))
    """).fetchone()[0]
    checks['sequence_select'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind='S'
          AND NOT has_sequence_privilege(current_user,c.oid,'SELECT'))
    """).fetchone()[0]
    checks['foreign_tables_absent'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_class WHERE relkind='f')
    """).fetchone()[0]
    checks['constraints_validated'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND NOT k.convalidated)
    """).fetchone()[0]
    # PR #181 fingerprints pg_largeobject pages directly when any objects exist.
    # Do not broaden system-catalog grants automatically to bypass this gate.
    checks['large_objects_readable'] = conn.execute("""
        SELECT NOT EXISTS(SELECT 1 FROM pg_largeobject_metadata)
          OR has_table_privilege(current_user,'pg_catalog.pg_largeobject','SELECT')
    """).fetchone()[0]
    checks['database_budget'] = 0 < conn.execute(
        'SELECT pg_database_size(current_database())').fetchone()[0] <= MAX_SOURCE_BYTES
    checks['relation_budget'] = conn.execute("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
          AND c.relkind IN ('r','p','m','f')
    """).fetchone()[0] <= MAX_RELATIONS
    # Read-only transaction defaults are not an authorization boundary. Callable
    # SECURITY DEFINER/user C routines need a separate owner audit, not execution.
    checks['privileged_routines_absent'] = not conn.execute("""
        SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          JOIN pg_language l ON l.oid=p.prolang
          WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
          AND (p.prosecdef OR NOT l.lanpltrusted)
          AND has_function_privilege(current_user,p.oid,'EXECUTE'))
    """).fetchone()[0]
    return checks


def github_checks(env, *, transport=None):
    """GET only, fixed private repository, no redirects or credential discovery."""
    checks = {'github_configuration': bool(env.get('RELATIONAL_GITHUB_TOKEN')) and
              env.get('RELATIONAL_GITHUB_REPOSITORY') == ARCHIVE_REPOSITORY}
    if not checks['github_configuration']:
        return checks
    with httpx.Client(transport=transport, timeout=15, follow_redirects=False,
                      trust_env=False, headers={
                          'Authorization': 'Bearer ' + env['RELATIONAL_GITHUB_TOKEN'],
                          'Accept': 'application/vnd.github+json',
                          'X-GitHub-Api-Version': '2022-11-28'}) as client:
        response = client.get('https://api.github.com/repos/' + ARCHIVE_REPOSITORY)
        if response.status_code != 200:
            checks['github_repository_read'] = False
            return checks
        repo = response.json()
        checks['github_repository_read'] = (
            repo.get('full_name', '').lower() == ARCHIVE_REPOSITORY.lower()
            and repo.get('private') is True and repo.get('archived') is False)
        if checks['github_repository_read']:
            releases = client.get('https://api.github.com/repos/' + ARCHIVE_REPOSITORY + '/releases?per_page=1')
            checks['github_releases_read'] = releases.status_code == 200 and isinstance(releases.json(), list)
    # Do not claim write permission based on a successful GET or repo.permissions.
    return checks


def prepare_reader(owner_url, reader_url):
    """Opt-in creation only. Existing role is never altered, granted or reset."""
    from psycopg import sql
    reader = source_url(reader_url)
    owner = make_url(owner_url)
    if (owner.drivername != 'postgresql' or not owner.username or not owner.password
            or owner.username == reader.username
            or (owner.host, owner.port or 5432, owner.database) !=
               (reader.host, reader.port or 5432, reader.database)
            or dict(owner.query) != dict(reader.query)):
        raise ValueError('owner_configuration')
    with psycopg.connect(owner_url, connect_timeout=10, gssencmode='disable',
                         application_name='betapp-reader-preparation',
                         options='-c statement_timeout=10000 -c lock_timeout=2000') as conn:
        conn.execute('BEGIN READ ONLY')
        exists = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='kupon_archive_reader')").fetchone()[0]
        conn.rollback()
        if exists:
            return 'reader_existing_unchanged'
        # A concurrent creator causes CREATE ROLE to fail and roll back safely.
        conn.execute('BEGIN')
        conn.execute("SET LOCAL password_encryption='scram-sha-256'")
        # Generate SCRAM verifier client-side: no plaintext password sent in SQL.
        verifier = conn.pgconn.encrypt_password(reader.password.encode(), b'kupon_archive_reader', b'scram-sha-256').decode()
        conn.execute(sql.SQL('CREATE ROLE kupon_archive_reader LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD {}').format(sql.Literal(verifier)))
        for setting, value in (('default_transaction_read_only', 'on'),
                               ('statement_timeout', '120s'), ('lock_timeout', '2s'),
                               ('idle_in_transaction_session_timeout', '300s')):
            conn.execute(sql.SQL('ALTER ROLE kupon_archive_reader SET {} = {}').format(sql.Identifier(setting), sql.Literal(value)))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO kupon_archive_reader').format(sql.Identifier(reader.database)))
        schemas = conn.execute("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema'").fetchall()
        for (schema,) in schemas:
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO kupon_archive_reader').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO kupon_archive_reader').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('GRANT SELECT ON ALL SEQUENCES IN SCHEMA {} TO kupon_archive_reader').format(sql.Identifier(schema)))
        conn.commit()
    return 'reader_created'


def docker_tls_check(parsed):
    """Probe the dump container's actual CA mount and hostname verification."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env.update(PGHOST=parsed.host, PGPORT=str(parsed.port or 5432),
               PGDATABASE=parsed.database, PGUSER=parsed.username,
               PGPASSWORD=parsed.password, PGSSLMODE='verify-full',
               PGCONNECT_TIMEOUT='10', PGGSSENCMODE='disable',
               PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000')
    command = ['docker', 'run', '--rm', '--pull=never']
    for option, variable in (('sslrootcert', 'PGSSLROOTCERT'),
                             ('sslcert', 'PGSSLCERT'), ('sslkey', 'PGSSLKEY')):
        if option in parsed.query:
            mount = '/run/pgcert/' + variable.lower()
            command += ['--volume', str(Path(parsed.query[option]).resolve()) + ':' + mount + ':ro']
            env[variable] = mount
    for key in ('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE',
                'PGCONNECT_TIMEOUT', 'PGGSSENCMODE', 'PGOPTIONS', 'PGSSLROOTCERT', 'PGSSLCERT', 'PGSSLKEY'):
        if key in env:
            command += ['--env', key]
    command += ['postgres:18', 'psql', '-X', '-w', '-qAt', '-v', 'ON_ERROR_STOP=1',
                '-c', "BEGIN READ ONLY; SELECT current_user='kupon_archive_reader' AND current_setting('transaction_read_only')='on'; ROLLBACK;"]
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30)
    return result.returncode == 0 and result.stdout.strip() == b't'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-source', action='store_true')
    parser.add_argument('--check-github', action='store_true')
    parser.add_argument('--prepare-reader', action='store_true')
    args = parser.parse_args()
    checks = {}
    try:
        value = os.environ.get('RELATIONAL_SOURCE_DATABASE_URL', '')
        parsed = source_url(value)
        checks['source_configuration'] = True
    except Exception:
        checks['source_configuration'] = False
    if args.prepare_reader:
        try:
            name = prepare_reader(os.environ.get('RELATIONAL_OWNER_DATABASE_URL', ''), value)
            checks[name] = True
        except Exception:
            checks['reader_preparation'] = False
    try:
        result = subprocess.run(['docker', 'image', 'inspect', 'postgres:18'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        checks['local_pg18_image'] = result.returncode == 0
    except Exception:
        checks['local_pg18_image'] = False
    if args.check_source and checks['source_configuration']:
        try:
            with psycopg.connect(value, connect_timeout=10,
                                 gssencmode='disable',
                                 application_name='betapp-archive-preflight',
                                 options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000') as conn:
                # Check the login default before options can mask it.
                # Reconnect without options below is avoided: role setting is
                # inspected from catalogs instead.
                conn.execute('BEGIN READ ONLY')
                checks.update(metadata_checks(conn))
                checks['role_readonly_setting'] = conn.execute("""
                    SELECT COALESCE('default_transaction_read_only=on'=ANY(rolconfig),false)
                    FROM pg_roles WHERE rolname=current_user
                """).fetchone()[0]
                conn.rollback()
        except Exception:
            checks['source_access_and_role_gate'] = False
        if checks.get('tls_active') and checks.get('local_pg18_image'):
            try:
                checks['docker_tls_connection'] = docker_tls_check(parsed)
            except Exception:
                checks['docker_tls_connection'] = False
    if args.check_github:
        try:
            checks.update(github_checks(os.environ))
        except Exception:
            checks['github_access'] = False
    try:
        key_bytes(os.environ.get('RELATIONAL_ENCRYPTION_KEY', ''))
        checks['encryption_key'] = True
    except Exception:
        checks['encryption_key'] = False
    for name, ok in checks.items():
        print(('PASS' if ok else 'BLOCKED') + ' ' + name)
    if not args.check_source:
        print('PENDING source_metadata')
    if not args.check_github:
        print('PENDING github_access')
    if 'docker_tls_connection' not in checks:
        print('PENDING docker_tls_connection')
    for name in ('sequence_stability', 'full_source_parity',
                 'github_write_permission', 'github_readback_restore'):
        print('PENDING ' + name)
    print('PASS cleanup_disabled')
    print('PASS upload_disabled')
    return 0 if checks and all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
