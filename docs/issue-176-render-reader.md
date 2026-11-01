# Issue 176 / PR 181: Render reader preparation

Status: preparation implemented; live source parity **unverified**;
`cleanup_authorized=false`. No live SQL, capture, archive upload, merge or deploy
was executed during this preparation. PR 181 continues to stack on PR 177;
no base change or additional PR is required.

## Owner steps

1. Review `scripts/sql/kupon-archive-reader.sql`. From a protected owner-operated
   `psql -X` session connected to the intended source database, use
   `\i scripts/sql/kupon-archive-reader.sql`. Do not put a connection URL or password
   on the command line or record this session. This SQL is a proposed owner action,
   not part of the automated preflight. It creates only the separate
   `kupon_archive_reader` role, role settings, and its SELECT/CONNECT/USAGE grants.
   A role name collision aborts the transaction; do not alter an existing account
   to make the script work. `\password` prompts for the new password without
   including it in the file. If that prompt fails, the created login remains
   without a password until the owner sets it interactively.
2. Use the Render **external direct database hostname**, not an IP substitution or
   pooler, and a trusted PEM CA bundle from a verified trust source. Provide the
   new login URL only as the process secret `RELATIONAL_SOURCE_DATABASE_URL` via
   your secret manager. Format (placeholders only):
   `postgresql://kupon_archive_reader:<encoded-password>@<external-host>:5432/<database>?sslmode=verify-full&sslrootcert=<encoded-absolute-CA-file-path>`.
   Percent-encode password and Windows certificate paths. Optional `sslcert` and
   `sslkey` must also be existing file paths. Do not change `DATABASE_URL`, Render
   default credentials, Blueprint references or production application settings.
   This preflight deliberately requires an explicit CA file so the Windows client
   and Linux dump container can use the same bundle; `sslrootcert=system` is not
   accepted here. No TLS downgrade is offered.
3. Start Docker Desktop's Linux engine; make `postgres:18` available locally using
   your normal image preparation process. The preflight never pulls an image.
   Install this checkout's Python requirements in your protected environment.
4. Run the single Windows PowerShell 5.1 entry point from this checkout:

   ```powershell
   .\scripts\test-relational-source.ps1
   .\scripts\test-relational-source.ps1 -CheckSource
   ```

   The first command is local only. The second explicitly opens a bounded,
   read-only catalog session and, if all gates pass, one containerized `psql`
   metadata probe using the CA mounted read-only. It does not start pg_dump,
   inspect table payloads, fingerprint rows, export snapshots, require GitHub
   credentials, call GitHub or write a receipt. Output is a fixed allowlisted
   `PASS/BLOCKED/PENDING <check>` protocol; raw SQL errors, URLs, passwords,
   certificate contents, object names and native stderr are suppressed. Exit 1
   means a check failed; exit 0 means only the checks actually run passed.
   `PENDING` stages remain outstanding even with exit 0. Share only these fixed
   status lines if reporting an error. Do not share full environment or reports.

## Permission gates and limits

- The new role has no memberships, ownership, elevated role attributes, grant
  options, database/schema CREATE or application write grants. Its default
  transaction mode is read-only, but that setting can be overridden by the login;
  SQL authorization checks are the real gate. The preflight reuses PR 181's
  negative gate and verifies all relation SELECT, schema USAGE and sequence
  SELECT privileges separately. Sequence SELECT reads `last_value/is_called`;
  never grant USAGE or UPDATE (which can permit `nextval`/`setval`).
- All existing non-system schemas and relations are covered, including partitions,
  views and materialized views. No broad built-in read role or default privileges
  are granted. New objects need owner-reviewed SELECT grants and another check.
- PUBLIC privileges and existing users are untouched. PostgreSQL has no per-role
  DENY: inherited PUBLIC CREATE or unsafe callable SECURITY DEFINER/untrusted
  routines can block this design. Do not fix that by silently revoking PUBLIC or
  changing existing routines. Resolve through a separately reviewed access design.
  PUBLIC TEMP/normal function execution may still exist; this is not a sandbox
  against arbitrary SQL supplied by the login. The tooling executes fixed queries
  inside read-only transactions, never application functions.
- RLS, foreign tables and unvalidated constraints block. Any existing large
  objects require direct page readability for PR 181's complete fingerprint;
  the script reports a block rather than granting catalog-wide privileges.
- Checks are bounded by 10-second connection/statement and 2-second lock timeouts;
  the Docker probe has a 30-second process deadline. Catalog checks do not prove
  that an 8-GiB/1,000-relation source fits the full capture's five-minute budget,
  or that its dump/stream/spool budgets fit. A live schema can change after a
  successful check; rerun close to any future authorized capture.

## Evidence still required before real parity

- Owner application of the reviewed SQL and secure password provisioning.
- Real external host, trusted CA file, access allowlist and successful Windows plus
  container `verify-full` connections with the new login.
- All real metadata/privilege gates passing; Docker PG18 fixture tests running.
- A quiet capture window: sequence values are not MVCC-snapshotted and must be
  unchanged before/after pg_dump. Metadata-only checks cannot prove stability.
- Owner-reviewed resource/output-directory capacity and bounded snapshot plan.
- Separate authorization before the existing archive executor: that command
  uploads and publishes a private GitHub Release. **Do not run it under this
  preparation task.** Private repository permissions, key provisioning, encrypted
  upload/readback, independent restore and full source fingerprints remain
  unverified. Local ZIP restores and synthetic tests do not substitute for these.
- JSON/JSONB contents are fingerprinted, but semantic resolution of application
  identifiers inside JSON is not established by PR 181. Roles/ACLs are excluded.

## Validation in this preparation session

Windows PowerShell 5.1 wrapper and unit tests are exercised without live secrets.
The targeted suite completed with **24 passed, 3 skipped**; all three skips are
Docker integration tests. `git diff --check` passed. The new unit suite is also
included in the existing PR 181 recovery CI workflow (remote result pending).
Disposable PG18 tests are included but require an available Docker Linux engine.
The observed local engine was unavailable (`dockerDesktopLinuxEngine` pipe
missing). No live source credential or CA was provisioned or tested by this work.
Actual source parity and GitHub readback stay unverified; cleanup stays forbidden.

## Documentation checked

- [Render credentials](https://render.com/docs/postgresql-credentials): users
  created with SQL do not replace the default user; Dashboard/API credentials do.
- [Render external connections](https://render.com/docs/postgresql-creating-connecting).
- [PostgreSQL TLS verification](https://www.postgresql.org/docs/current/libpq-ssl.html):
  `verify-full` validates both the certificate chain and hostname.
