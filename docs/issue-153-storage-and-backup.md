# Issue #153: disk exhaustion, backup coverage and staged remediation

## Evidence and verification status (27 October 2026)

User confirmed `Deniz's workspace` (`tea-db1u8a8m7kps73csvh70`). Read-only Render
discovery identifies free web service `srv-db2hvoflk1mc73bgbhrg` and active free
Postgres 18 `dpg-dbehainb9u1s739nem0g-a` / `Kuponanaliziivv7`.

| Evidence | Result | Provenance / limit |
| --- | --- | --- |
| Database size | 895,538,879 bytes (854 MiB rounded) | Issue #153's earlier successful query; **not remeasured** |
| board_editions | 692,502,528 bytes (~660 MiB), 5,195 editions / 15 leagues | Issue report; not independently remeasured |
| Other large relations | match_events ~79 MiB; odds ~23 MiB | Issue report; approximate |
| New read-only DB connection | Rejected with `53100`, could not write init file, no space left | Direct connector attempt in this task |
| Runtime logs | Repeated DiskFullError at ~10-second intervals, 16:14–16:16 UTC | Read-only Render logs |
| Live deployment | `60efd582`, `dep-dbgco0vb9u1s7387v7bg`, live since 16:07 UTC | Deployment status only |
| `/health` | HTTP 200, `status=ok` | Process liveness, **not schema readiness** |
| prediction_publications exists | Earlier query returned NULL | Current connection cannot verify; **not claimed operational** |
| Archive repository | `donozoyout-ux/kuponanalizii-data-archive`, private, push allowed | GitHub connector metadata verified |
| Production encrypted manifest / restore | **NOT VERIFIED** | No validated production manifest obtained; DB currently cannot connect |
| Existing owned-audit | Filtered forecasts/decisions/coupon/report metadata | Excludes board_editions and most operational data; not a full backup |

No database deletion, row rewrite, index drop, guard removal, VACUUM, plan change,
production export, Telegram/NVIDIA call or deployment was performed in this task.
No production encryption key or dedicated archive reader credential is available
in this session. An empty private repository is not a backup.

## Cause and safe startup change

`supervise_database()` resets `ready=False` after every exception and calls
`Database.initialize()` again after ten seconds. `create_all` precedes guard and
seed setup. Disk exhaustion during CREATE TABLE rolls the transaction back;
retrying cannot create capacity. Current service configuration uses a blank HTTP
health-check path (TCP liveness); the repository's `/health` also always returns
200. Both explain why Render can mark this deploy live while DB initialization fails.

The staged change recognizes SQLSTATE 53100 through SQLAlchemy/driver exception
wrappers, latches the background supervisor in a cancellable no-query state, and
rejects mutating API requests. It preserves transient-error retry behavior for
other errors. Recovery requires reviewed capacity remediation and an intentional
restart; no automatic DDL probes are used while blocked.

New `/ready` returns 503 until initialization is complete, checks the actual
publication table on a bounded connection, and never creates it. `/health` stays
process liveness. Do not switch Render's health check to `/ready` while disk is
full: automatic restarts would reset the circuit and cause more failed attempts.
The PR does not change the active service or Render plan.

## Can archiving/compression fix capacity without touching history?

An external compressed copy preserves evidence but reclaims **zero source bytes**.
PostgreSQL already supports transparent TOAST compression for large values; the
measured table size must not be treated as an uncompressed JSON size. Changing a
column compression policy is not evidence that old tuples have shrunk. Rewriting
old values violates this task's no-mutation rule; VACUUM FULL also rewrites the
table and needs extra space. Neither is implemented or recommended here.

`board_heads` and `board_read_publications` reference edition versions. Historical
editions remain audit evidence even when not current. Deleting them after export
would require a separately reviewed retention decision, a durable archive lookup
path, verified restore, FK analysis, and explicit authorization. This PR never
turns a verified archive into permission to prune.

Under the current no-delete/no-rewrite/no-plan-change constraints, **a safe immediate
capacity recovery is not established**. Provider assistance to recover connection
headroom without changing user tables is the next operational dependency; no
support message was sent. Repeated deployment is not a capacity remedy.

After connectivity is restored, run `scripts/storage_retention_report.sql` using
a SELECT-only role. It reports relation/index sizes, estimated row/dead-tuple
counts, edition date ranges, current-pointer coverage, FK dependencies and guards.
It contains no cleanup commands. Results must be recorded as new observations,
not substituted for the earlier issue's measurements.

References checked: [Render health checks](https://render.com/docs/health-checks),
[Render free tier](https://render.com/docs/free),
[PostgreSQL 18 TOAST](https://www.postgresql.org/docs/18/storage-toast.html),
[PostgreSQL 18 VACUUM](https://www.postgresql.org/docs/18/sql-vacuum.html).

## Separate lossless board archive

`python -m src.archive.board_backup` is separate from the existing filtered
`src.archive.cli`. It preserves the PostgreSQL JSON text bytes of every selected
board row, including precise numeric text, timestamps, identity and payload.
It does not recalculate predictions or prices.

- Frozen, encrypted key inventory: schema/version, source identity, keys/count,
  column definitions, chunk size and inventory digest. Default 8 rows/chunk,
  maximum 128; at most 100,000 keys and 16 MiB plaintext per chunk. Oversize rows
  fail closed, never truncate. Use a new ID and smaller chunk size if required.
- Dedicated SELECT-only PostgreSQL credential; TLS `verify-full`; read-only
  repeatable-read transaction; no app initialization. Source identity/schema
  changes and missing/changed historical rows abort resume.
- gzip followed by AES-256-GCM, fresh nonce, authenticated archive ID + asset name.
  Only encrypted `plan.enc`, `chunk-NNNNNN.enc`, and `manifest.enc` go to private
  Release assets. Raw dumps, credentials and decrypted payloads never enter Git.
- SHA-256 digests, byte sizes, exact counts and key ranges in the encrypted final
  manifest. Repository-wide existing asset bytes are counted with pagination
  before mutation. Use one writer (workflow concurrency is repository-wide).
- At most 64 new chunks/run by default. Partial completion exits 2, leaves the
  draft and verified chunks intact, and claims no completed backup. Resume with
  the same ID. Existing chunks are downloaded/authenticated and compared against
  source bytes; uncertain uploads are reconciled on the next invocation.
- Finalization requires read-after-write checks, all expected chunks, exact
  manifest agreement, and isolated SQLite restore/readback of every row byte.
  A completed Release is never extended, overwritten or deleted by this tool.
- Restore produces a **lossless board-row catalog**, not a working application DB.
  A new destination is mandatory. Failure removes only the new incomplete restore.
  Neither decrypted files nor encryption keys are uploaded as Actions artifacts.

The frozen inventory is the archive boundary: later insertions are not silently
added during resume. The next backup ID captures them. Source reads and remote
uploads keep the transaction open; use bounded runs and avoid concurrent writers
until source headroom is recovered. An incomplete report names missing chunks and
never reports `restore_verified=true`.

### Credentials and commands

Create protected GitHub environment `board-backup` on the application repo. Set
environment secrets (never in issues/chat/repository):

- `BOARD_BACKUP_DATABASE_URL`: dedicated SELECT-only reader, `sslmode=verify-full`.
  Existing app-owner credentials are intentionally rejected. Role provisioning
  needs a separate reviewed action after capacity recovery.
- `BOARD_BACKUP_ENCRYPTION_KEY`: independent 32-byte base64 key; keep a recoverable
  copy outside GitHub as well. Losing it makes the archive unrecoverable.
- `BOARD_BACKUP_TOKEN`: fine-grained token scoped only to the private archive repo,
  Contents read/write for Releases (metadata read). Source repo GITHUB_TOKEN stays
  contents:read; no Actions/admin/provider permissions are needed by the exporter.

Set `BOARD_BACKUP_BUDGET_BYTES` to an explicitly reviewed total archive byte budget
and repository variable `BOARD_BACKUP_ENABLED=true` only after these prerequisites.
No credentials or workflow activation were changed by this task.

Manually dispatch `.github/workflows/board-backup.yml` with an ID such as
`render-20261027-01`; choose `export`. Reuse that ID until completed. `report` and
`verify` download/authenticate and restore locally without source DB access.
The workflow has no schedule and no upload-artifact step. Local equivalent:

```text
python -m src.archive.board_backup export --id render-20261027-01
python -m src.archive.board_backup report --id render-20261027-01
python -m src.archive.board_backup verify --id render-20261027-01
python -m src.archive.board_backup restore --id render-20261027-01 --destination NEW_PRIVATE_PATH.sqlite
```

Successful output explicitly says `full_database_backup=false` and
`source_space_reclaimed=0`. Production success requires the actual encrypted
manifest and a successful downloaded restore check; passing unit tests is not that.

## Remaining full-core backup phase

This first implementation covers board_editions only. Prediction snapshots,
settlements, publications, teams/leagues/seasons, matches/events/statistics, all
odds tables, coupons, jobs/checkpoints and learning records remain outside its
coverage. Existing owned-audit covers only an allowlisted subset of some of them.

For a full operational backup, preserve DDL, constraints/indexes/guards, sequences,
all referenced rows, and one consistent database snapshot. Mutable tables cannot
be resumed across independent transactions using the immutable-key protocol.
Use a single consistent pg_dump/snapshot export, or an explicitly managed snapshot
lifetime with a separate recovery design; restarting after snapshot loss requires
a new backup ID. Restore into a new isolated PostgreSQL instance and verify exact
counts, content digests, sequences and FK/guard checks before claiming full coverage.
Do not expand this exporter to mutable tables merely by adding table names.

## Deployment acceptance gate

This is a staged PR, not production remediation. Before deployment, restore DB
connectivity/headroom through an authorized path and validate the archive. After
an approved deployment, independently verify `to_regclass('public.prediction_publications')`,
`/ready` HTTP 200, a read-only audit API request and recent logs free of 53100.
Do not equate `live`, `/health` 200, or a successful CI run with successful migration.
Issue #153 remains open until these operational checks succeed.
