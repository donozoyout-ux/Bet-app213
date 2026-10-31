# Full recovery backup (active Kuponanaliziivv755)

This is a complete application-database logical recovery backup, not the
eligibility-filtered history archive or board-only export. It builds on PR #161
and preserves #154/#156/#157. PR #146's PostgreSQL client/isolated restore design
was reviewed; its old host pin, single-table minimum count and Actions-artifact
output are deliberately replaced. No existing workflow or PR is merged/deployed.

## What is preserved and proved

- Every non-system ordinary/partitioned/materialized table, across all user
  schemas, is enumerated dynamically. Unknown future tables are not omitted.
  Schema objects, data, constraints, triggers, sequences and large objects are
  included by full PostgreSQL 18 custom-format pg_dump (no table/date filter).
  Foreign tables reject the operation because external contents are not a dump.
- One REPEATABLE READ READ ONLY transaction exports a snapshot used by pg_dump
  and exact table row counts. Sequence states are compared before/after dump to
  reject non-MVCC changes while writers should be paused. Existing app writers
  are not enabled. SELECTs / ACCESS SHARE locks only on production.
- No source-side DDL, role creation, new Render PostgreSQL, VACUUM, deletion or
  threshold override. Existing credentials may be owner-capable but the exporter
  and pg_dump are forced read-only. A dedicated all-table reader is preferable.
- This is not a physical cluster backup: owner/ACL assignments, role passwords,
  tablespace files, server configuration and external systems are not restored.
  Restore uses the isolated verifier role. Partition parents and their leaves
  each get their own counts (sum is a comparison total, not distinct-row count).
- TLS verify-full and system CA trust; exact active host/database pin. The old
  suspended database is rejected before connecting. Source command stderr and
  credential values are never printed; no DSN in process command arguments.
- Custom dump is split into at most 128 x 16 MiB plaintext chunks, gzip compressed
  and independently AES-256-GCM authenticated with backup ID/name context. SHA-256
  covers plaintext, encrypted assets and reconstructed whole dump. Manifest and
  source/restored per-table counts are encrypted too. 2 GiB dump / 4 GiB target
  repository budget: exceeding a limit fails, never truncates or prunes.
- Private Release stays draft until the uploaded manifest/chunks are downloaded,
  authenticated, reconstructed and restored into an EMPTY loopback-only existing
  CI verifier (localhost:5433/postgres). pg_restore exits on any error and uses
  one transaction; source/restored complete inventories and sequence states must
  match. SQL reads original match/board/prediction/settlement/lesson JSON as smoke
  checks. A checksum-verified encrypted receipt is uploaded before publication.
- No plaintext artifacts or logs. Transient files live in a restrictive temporary
  runner directory and are removed on normal/error exit; runner disposal also
  removes them. No promise of forensic secure erasure is made.
- Interrupted runs preserve draft assets. Do NOT overwrite or combine snapshots;
  restart with a new backup ID. Failed drafts are not verified backups and are
  not automatically deleted. AES key must be independently retained by owner.

## Secret names checked (30 October 2026; values never printed)

Main Bet-app213 repository: ARCHIVE_ENCRYPTION_KEY, ARCHIVE_GITHUB_TOKEN,
RENDER_DATABASE_URL, SCRAPER_API_TOKEN. Archive repository:
ARCHIVE_DATABASE_URL, ARCHIVE_ENCRYPTION_KEY, ARCHIVE_GITHUB_TOKEN.
No repository Actions variables were present. Secrets do not automatically cross
repositories: this runner uses MAIN repository secrets only.

- RECOVERY_ENCRYPTION_KEY (REQUIRED for full recovery): a dedicated
  cryptographically random 32-byte AES key encoded in Base64, kept separately
  by the owner. A correct Base64 value typically occupies 44 characters.
  Decode and confirm **exactly 32 bytes locally** before saving as a MAIN
  Bet-app213 Actions repository secret. The full-recovery job refuses missing
  or invalid values instead of silently falling back to legacy keys.
- ARCHIVE_ENCRYPTION_KEY: existing legacy archive key for the older daily audit.
  Never rotate or overwrite this value to fix full recovery; older archives may
  depend on it. Neither key value is printed by the runner.
- ARCHIVE_GITHUB_TOKEN: existing token restricted to private archive repo,
  Contents read/write. No admin/delete/Render permission is needed. Runner checks
  private repo identity and API rights at use; metadata presence is not validation.
- ARCHIVE_DATABASE_URL (recommended main-repo secret): active database external
  URL with all-table SELECT, sequence SELECT and large-object access required
  for pg_dump. If absent, existing RENDER_DATABASE_URL is used. Either way exact
  host dpg-dbgdegou01pc738m3mmg-a.frankfurt-postgres.render.com and database
  kuponanaliziivv755 are mandatory. No secret is copied from the other repository.
  If guard rejects, owner sets this name under Settings > Secrets and variables >
  Actions > Repository secrets using the active DB external connection string.
  Never paste it into a chat, issue, PR or workflow log.

No new token or key should be generated when existing ones validate. Missing or
incorrect settings must be reported by NAME only. GitHub does not reveal secret
values through its metadata API; only the protected runner validates them.

## Running without merge/deploy

The existing `owned-audit.yml` workflow has an explicit manual-only full_recovery
input (default false). Its original scheduled filtered-audit job is unchanged,
and excluded when full_recovery=true. Dispatch it on the reviewed feature branch:
full_recovery=true, backup_id=<unique lowercase letters/digits/hyphens>.
It calls `full-recovery.yml` from that SAME branch with no app deployment. No
schedule activates complete backups. Existing daily/Telegram gates remain off.
Use CI-validated source commit. Report Actions run, commit, private release tag,
manifest and receipt asset IDs/SHA, exact table/count match and restore status.
A green unit test or encrypted upload alone is not a verified production backup.

To recover later, download the pinned manifest asset, authenticate via
recovery_format.unseal using its backup ID/name; for every chunk verify ciphertext
SHA, decrypt, verify plaintext size/SHA, concatenate in manifest order and verify
whole dump SHA. On a separate empty isolated PostgreSQL 18 test service use
full_recovery.restore_verify against manifest inventory. Never run pg_restore
against the production source. This does not activate archive read-through: full
recovery and eligible match-history formats have distinct purposes.

## Controls retained

SCRAPER_WORKER_ENABLED=false; AUTO_BACKFILL_ON_EMPTY=false; Telegram/NVIDIA empty
in runner; no Render plan change. PR161 high-watermarks stay unchanged. Its nested
cutoff review concern remains a separate release review item; backup preparation
does not reinterpret or rewrite any existing board, prediction or settlement.
