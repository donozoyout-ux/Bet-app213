# Issue 176 / PR 181: single-command Windows setup and preflight

Run in Windows PowerShell 5.1 from this checkout:

```powershell
.\scripts\test-relational-source.ps1 -StartDocker -InstallDependencies -PromptForSecrets
```

This starts the installed Docker Desktop hidden if its Linux engine is unavailable,
waits at most 90 seconds (configurable up to 180), installs minimal Python packages
in the ignored `venv/archive-setup` environment, and checks the source and GitHub
independently. It does not install Docker, elevate, switch engines, pull images,
change services or alter execution policy. Prepare `postgres:18` locally if the
image check reports it missing. Python 3.12 must already be installed.

Secret prompts use hidden input. Empty input is allowed and reports a missing
setting. Existing process secrets are reused without displaying them. Prompted
values are restored/removed when the script ends; nothing is saved to disk or the
user/machine environment. Do not paste secrets into shell commands, logs, chats or
transcripts. Use your secret manager instead of prompts in a noninteractive run.
The wrapper emits fixed status codes plus fixed Turkish guidance, never raw native
stderr, exception text, table names, passwords, tokens, keys or connection URLs.

## Settings

- `RELATIONAL_SOURCE_DATABASE_URL`: direct external Render PostgreSQL URL for
  `kupon_archive_reader`, with `sslmode=verify-full` and an explicit trusted CA PEM
  file in URL-encoded `sslrootcert`. Percent-encode the password and Windows file
  paths. Optional client certificate/key paths must exist. Obtain the CA from a
  verified trust source. No TLS downgrade or IP hostname substitution is offered.
- `RELATIONAL_GITHUB_TOKEN`: private archive repository read access. The script
  uses only GET requests to the fixed GitHub API origin, without redirects. It
  does not discover or reuse CLI credentials, tokens from production, or proxies.
- `RELATIONAL_GITHUB_REPOSITORY`: defaults to
  `donozoyout-ux/kuponanalizii-data-archive`. Another value blocks the check.
- `RELATIONAL_ENCRYPTION_KEY`: existing archive key, base64 encoding of 32 bytes.
  Validation only; no key is generated, printed or changed.
- `RELATIONAL_OWNER_DATABASE_URL`: optional, only for explicit `-PrepareReader`.
  It must use a different login but the exact same database, host, port, CA and
  TLS options as the reader URL. Never replace production `DATABASE_URL` or
  Render/Blueprint default credentials.

## Existing or missing reader

Default operation connects as the reader and audits privileges without changing
roles, grants, passwords, ownership, PUBLIC privileges or application settings.
Successful login establishes that the reader exists. Authentication failures are
reported as an access/credential gate, not falsely classified as a missing role.

For automatic creation of an absent reader only, add `-PrepareReader` to the same
command. This is an explicit owner action that performs role/grant DDL:

```powershell
.\scripts\test-relational-source.ps1 -StartDocker -InstallDependencies -PromptForSecrets -PrepareReader
```

The owner connection first performs a read-only existence query. If the role
already exists, it rolls back and leaves it entirely unchanged, then normal reader
checks report any missing or excessive permissions. Otherwise it creates the
separate role atomically, with a client-generated SCRAM password verifier,
connection limit 3, no role memberships or elevated attributes, read-only defaults,
bounded timeouts and SELECT grants on existing application tables/sequences plus
schema USAGE and database CONNECT. A concurrent role creation fails closed and
rolls back; it never resets an existing password. Plaintext passwords are not
embedded in setup SQL. The manual psql artifact remains available at
`scripts/sql/kupon-archive-reader.sql`, with hidden `\password` input and collision
abort. Do not run that non-idempotent manual artifact on an existing role.

No live reader creation was performed by the development tests: creation tests
use disposable loopback PostgreSQL 18 containers only.

## Separate checks

- Source configuration: exact reader identity, explicit CA file and verify-full.
- Windows TLS connection and active SSL, plus a separate containerized read-only
  `psql` probe with the same CA mounted read-only. No payload reads or pg_dump.
- PR 181's negative privilege/RLS gate, role membership, schema USAGE, relation
  SELECT, sequence SELECT, role read-only default, unsafe callable privileged
  routines, foreign tables, unvalidated constraints, large-object page access,
  database size (8 GiB) and relation count (1,000).
- GitHub private repository identity/status and Releases list read access. A GET
  does not prove token write access or release asset download/decryption.
- Archive key format independently of source and GitHub checks.

Missing source settings do not prevent GitHub checks, and GitHub failures do not
hide source SELECT/sequence results. Output `PASS`, `BLOCKED` and `PENDING` lines
identify each gate. Exit 1 indicates failed required checks. Exit 0 covers only
checks actually run, never the remaining PENDING evidence. `-LocalOnly` skips all
source/GitHub network checks; it cannot be combined with `-PrepareReader` or
`-CheckSource`. The old `-CheckSource` switch is accepted for compatibility; normal
operation now checks both source and GitHub. Omit `-PromptForSecrets` for automation
with process secrets. Omit `-InstallDependencies` to reuse the prepared environment.

No missing/excess privilege is fixed on an existing account. Owner review must
resolve reported SELECT/USAGE/default or excess-privilege issues separately. Never
grant sequence USAGE/UPDATE (nextval/setval), broad read roles or ownership to make
checks pass. PUBLIC rights cannot be denied per role; a conflicting PUBLIC grant
or callable privileged routine requires a separate access design. Normal PUBLIC
TEMP/function privileges may remain: this login is not an arbitrary SQL sandbox.
New objects need reviewed SELECT grants and another preflight. Large objects are
not skipped if direct page access is missing. JSON bytes are fingerprinted in the
archive, but semantic JSON identifier closure remains outside PR 181's proof.

## Upload remains separately gated

The setup script has **no archive execution option or code path**. It never calls
the archive executor, even if `RELATIONAL_ARCHIVE_ACTIVATED=true` was inherited.
`PASS upload_disabled` and `PASS cleanup_disabled` are unconditional safeguards.
Do not invoke `scripts/verify-relational-archive.ps1` until the user has separately,
explicitly authorized a real archive run. That existing executor requires explicit
activation and a new private output directory and can publish a private Release.
This preparation is not that approval.

## Real evidence still outstanding

- Actual owner provisioning (if absent), real credentials/CA/allowlist and all real
  Windows, container TLS, SELECT, sequence and privilege gates passing.
- Real private repository/token read access. Write permission is deliberately
  PENDING; it cannot be established safely by pretending a GET proves a write.
- Quiet source window, stable sequence state around pg_dump, complete bounded
  same-snapshot fingerprint/dump comparison and resource/time budgets.
- Encrypted private upload, re-download/authentication and independent restore
  matching every source relation/schema/sequence/large-object fingerprint.
- Application-level JSON reference semantics and excluded roles/ACLs.

Until those proofs exist: `source_parity=unverified`,
`github_readback_restore=unverified`, `cleanup_authorized=false`. Synthetic/local
fixture success and old ZIP restore results are not real source parity. No live
capture, upload, deletion, trigger change, merge or deploy was performed here.

## Validation

Targeted local suite: 36 passed, including disposable PG18 recovery, actual manual
setup SQL, optional creation/idempotence, missing sequence SELECT, excessive
sequence USAGE, Windows PowerShell wrapper, redaction and GET-only GitHub mocks.
The local Docker Desktop Linux engine was started successfully and the existing
postgres:18 image was used. The bootstrap command is also exercised locally.
The existing recovery CI includes these checks and an added Windows setup job;
see the PR check results for the latest remote run. PR 181 still bases on PR 177;
no new PR or dependency rebase was introduced.

References checked: [Render credentials](https://render.com/docs/postgresql-credentials),
[external connections](https://render.com/docs/postgresql-creating-connecting),
[PostgreSQL verify-full](https://www.postgresql.org/docs/current/libpq-ssl.html).
SQL-created users do not replace Render's default credential; Dashboard/API-added
credentials do. Keep the production credential unchanged.
