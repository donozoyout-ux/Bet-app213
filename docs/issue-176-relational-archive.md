# Issue 176: full relational GitHub archive and source parity

This workflow extends the owner-operated encrypted local recovery tools from PR #177. It captures one bounded PostgreSQL snapshot, compares every in-scope relation against two separate PostgreSQL 18 restores, then stores only authenticated encrypted dump chunks and an encrypted manifest in the exact private repository `donozoyout-ux/kuponanalizii-data-archive`. The second restore is made from assets downloaded back from the private GitHub Release. The Release stays a draft until that restore matches the source inventory.

## What the inventory covers

- All ordinary, partitioned, materialized, and partition child relations are discovered from PostgreSQL catalogs; there is no application table allowlist. Foreign tables stop the run because their remote contents are outside this database snapshot.
- Every row is represented in an order-independent multiset hash with duplicate rows preserved. This includes every value in JSON and JSONB columns. Catalog hashes cover columns, defaults, indexes, constraints and validation state, triggers, functions, views, partitions, row security policies, types, extensions, and sequence ownership. Sequence definitions and current values, foreign key definitions, and large-object metadata and content are compared too.
- The archive is a full database dump, so it does not prune rows by foreign key or JSON references. PostgreSQL foreign keys are restored and validated. JSON/JSONB payload bytes are part of every row hash; this does not interpret application-specific identifiers embedded inside JSON as foreign keys or assert that those identifiers resolve.
- Database roles and ACLs are deliberately excluded (`pg_dump --no-owner --no-acl`). The workflow never authorizes source cleanup; `cleanup_authorized` is always false. Do not use this archive workflow as approval to delete or alter a source database.

## Source and resource limits

Use a dedicated source login that is transaction read-only and has SELECT access to all application relations and sequences. The workflow rejects superuser/privileged or write-capable logins, source row-level security, and foreign tables. The connection must use `sslmode=verify-full`; custom `sslrootcert`, `sslcert`, or `sslkey` paths in the URL are mounted read-only into the local `postgres:18` container used for `pg_dump`.

The source database cap is 8 GiB; relation cap is 1,000; inventory streams and local hash spool each have 8 GiB and 2 GiB caps; a single row is capped at 64 MiB; the custom dump cap is 2 GiB; a snapshot is capped at five minutes with 120-second SQL timeout and two-second lock timeout. The source transaction is `REPEATABLE READ READ ONLY`, exports a snapshot to `pg_dump`, and is cancelled at the deadline. Sequence state is checked both before and after `pg_dump` because PostgreSQL sequence changes are not MVCC-snapshotted; any change aborts the run. A long snapshot can delay vacuum cleanup, so run during a quiet window and let the bounded command fail closed if it cannot finish.

## Owner-run procedure

1. On the machine with protected source access, inspect the existing PR #177 `verification.json` locally. Print only its status flags; do not send the ZIP, encryption key, database URL, GitHub token, or full report. For example:

   ```powershell
   $report = Get-Content -Raw 'C:\private\recovery\verification.json' | ConvertFrom-Json
   $report | Select-Object independent_restore_verified, source_parity_verified, github_readback_verified, cleanup_authorized
   ```

   If the older receipt has no `source_parity_verified` or `github_readback_verified` fields, treat those states as false. A local-only restore is not proof of source parity or GitHub readback.

2. Supply these process environment variables through the owner's secret manager: `RELATIONAL_ARCHIVE_ACTIVATED=true`, `RELATIONAL_SOURCE_DATABASE_URL`, `RELATIONAL_GITHUB_TOKEN`, `RELATIONAL_GITHUB_REPOSITORY=donozoyout-ux/kuponanalizii-data-archive`, and `RELATIONAL_ENCRYPTION_KEY`. The encryption key must be accepted by the existing recovery `key_bytes` format. The token needs private repository release and asset write/read access. The output parent must already exist and be private to the owner.

3. Ensure Docker already has `postgres:18` locally; the script does not pull an image. Run from the repository checkout:

   ```powershell
   .\scripts\verify-relational-archive.ps1 -OutputRoot 'C:\private\relational-archive-run-<new-id>'
   ```

   This creates a new owner-only output folder. It writes `verification.json` only after source capture, both independent restores, encrypted upload, and private GitHub readback all pass. The console emits only a stage and fixed boolean summary. On failure, the private draft Release is retained for manual review; no archive deletion or retention cleanup is attempted.

4. Read only the receipt status booleans locally. Do not attach the receipt, source dump, ZIP, encryption key, or credentials to the issue or PR. `source_parity_verified=true` is emitted only when the complete inventory matches both the first local restore and the GitHub-downloaded independent restore. It is not a result of CI's synthetic fixture.

## CI and current verification

The repository workflow runs synthetic unit tests and disposable PostgreSQL 18 source/dump/restore parity tests. It does not use the live source database or archive-repository credentials, publish a Release, or delete data. Live source parity remains pending until the owner runs the bounded procedure and privately confirms the sanitized receipt flags.
