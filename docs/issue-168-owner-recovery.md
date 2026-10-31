# Issue #168 — owner-run full recovery, with all-row parity

## Actual state / gates

This is a separate draft stacked on PR #162, reusing its snapshot, encrypted
chunking, private Releases, remote re-download and PostgreSQL 18 restore pipeline.
It is **not evidence of an existing production backup**. No source data was
exported, uploaded, restored or deleted during this implementation.

Read-only Render check on 2026-10-31 confirmed the pinned source:
PostgreSQL 18, 37 user tables, 22 sequences, zero large objects,
802,936,511 database bytes. This is an inventory check, not a consistent backup.
Local preflight finds Python/libraries available, Docker Linux engine unavailable,
and no authenticated local GitHub CLI session. No source connection URL is
available to the local process; the Render read-only query connector cannot
hold an exported snapshot or supply pg_dump authentication. No new secret is
requested in chat or written to GitHub Actions by this change.

The owner can use the local launcher below to enter the existing source URL
securely at their own terminal. Its successful final Release URL/manifest/receipt
must exist before Phase 2; all cleanup/reset remains a separately approved Phase 3.

## What changed from #162

Counts alone cannot detect a restored wrong match ID, modified JSON, changed odds
or different frozen pick when row count stays constant. `inventory` now streams
**every row of every user table** as PostgreSQL row_to_json text, hashes each row,
locally sorts the 32-byte row hashes and hashes the ordered multiset. Duplicate
rows count separately. Sorting uses a temporary local SQLite spool, not ORDER BY
on the nearly-full production database. Session timezone/date/interval/float/bytea
output settings are normalized. No application-level JSON/float conversion can
round exact numeric values. Large-object pages are hashed too, beyond LO counts.

These per-table content hashes enter the encrypted manifest and restored inventory.
Comparison checks every row's contents and IDs via cryptographic multiset parity,
not just a sample. Source counts/content are captured inside the same read-only
REPEATABLE READ snapshot passed to pg_dump. Sequence states are checked before
and after dumping because sequences are not MVCC snapshot data. The second source
inventory does not rehash the same snapshot; it compares counts/sequences/LO counts.

The independent empty verifier must be PostgreSQL 18. A successful pg_restore
remains mandatory for all dumped schema, constraints, triggers and data. The
logical application backup still excludes global roles/passwords/owner ACLs and
external systems; it must not be called a physical cluster backup.

The receipt now calls its restored SQL probes `sql_smoke`. Both receipt and public
summary explicitly set `application_read_through_verified=false`: SQL readability
in the verifier is **not** proof the deployed site reads from GitHub. A successful
summary includes the reconstructed dump SHA-256 and row_content_verified=true.
No eligibility rules are weakened to claim backup/read-through equivalence.

## Owner-operated Windows fallback

Requirements: Windows PowerShell, Python with project dependencies (libpq >=17
for system trust), Docker Desktop running Linux containers, and an existing
GitHub CLI login that can read/write the PRIVATE archive repository. No paid plan
or new Render database. At least 8 GiB free in the local workspace drive, plus
sufficient Docker backend space for the PostgreSQL restore/image. The 2 GiB dump
and 4 GiB archive budget from #162 remain hard limits. A source export failure,
changed sequence, missing privilege, corrupted remote byte or any parity mismatch
leaves the Release incomplete/draft. Never reset to get around a failed check.

Run from a private local terminal, without Start-Transcript/screen sharing. Use
paths outside all Git repos and synced/public folders. These examples contain no
credentials or key material; adapt only the paths and unique backup ID.

```powershell
# Read-only dependency checks. No source prompt, dump, restore or upload.
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode Preflight

# Create a NEW dedicated private folder; never replaces an existing key.
# Skip this step if an existing, independently retained recovery key validates.
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 `
  -Mode InitKey -KeyFile "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key"

# Independently copy the key offline first. Never paste it into this chat,
# a PR, an issue, a workflow log or a Git commit. Losing it loses the archive.
# KeyRetainedOffline is the owner's attestation, not an automated offline proof.
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 `
  -Mode Backup -BackupId "vv755-owner-20261031-a" `
  -KeyFile "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key" `
  -KeyRetainedOffline
```

The key uses the Windows cryptographic RNG, exactly 32 decoded bytes, owner-only
ACLs, and exclusive creation. Existing key files and legacy Actions keys are not
rotated/overwritten. The launcher validates the key without printing its value,
length, fingerprint or a partial prefix. It uses `gh auth token` in memory; if
CLI login is absent, sign into the existing account locally with normal `gh auth
login`. It does not ask for a PAT in chat or change GitHub Actions secrets.

Only Backup mode prompts for the current external Render URL via AsSecureString;
source guard and private repository access checks happen before source export.
The URL/key/token stay in the child process environment, never command arguments.
Environment values are restored on exit. Transient plaintext and hash-sort files
are under a new owner-only LocalAppData run directory. Only sanitized stages and
the safe final summary are retained in `verification.log`; the key is separate.
No promise of forensic erasure is made after a crash; keep the machine protected
and treat leftover private run folders as confidential.

The launcher creates one newly labelled `postgres:18` verifier bound only to
127.0.0.1:5433. Existing containers/volumes are not selected or reset. Windows
pg_dump uses Docker's ordinary network (no Desktop host-network toggle); pg_restore
runs via docker exec only after checking the exact newly created container ID,
postgres:18 image and this run's label. The restore file is copied into that
verifier; the source DSN/token/key are never passed into the verifier container.
Finally it removes only that exact labelled container and its anonymous volume.
On process termination before finally, inspect the run label manually; never
use a broad docker prune or delete a pre-existing database.

## Success evidence required (not yet produced for this source)

- Unique published **private** Release URL and backup ID, source snapshot time.
- Authenticated manifest/receipt asset IDs and hashes, whole-dump SHA-256.
- Exactly 37 current tables / 22 sequences plus any dynamically discovered future
  objects: counts **and all-row hashes** equal after REMOTE download and restore.
- LO count/page digest equality; pg_restore single-transaction success on empty PG18.
- source_deleted_rows=0; eviction_authorized=false; application_read_through_verified=false.
- Independently retained decryption key; verify receipt using that copy separately.

A green synthetic CI, this inventory inspection, a draft upload, or an original
local dump alone is not a successful offsite backup.

## Ordered Phase 2 and Phase 3 — not executed here

After the real backup receipt exists, review #156/#157 and build a separate
archive-backed application PR. The recovery dump is not an application query API.
Test actual site routes from authenticated remote archive bytes with the hot
fixture deliberately missing; compare original match IDs, FT/HT, opening/closing
1X2/AH/OU for Crown/Bet365/Sbobet, corners/cards, frozen publications,
prediction snapshots/settlements, lessons and relational/JSON dependencies.
A missing/mismatched archive must yield an explicit incomplete/error state,
not silently regenerate a pick or fall back to a reused numeric match ID.

The hot retention proposal is Istanbul today through the next three calendar
days, **plus** all unfinished/postponed/unsettled/contradictory/missing-evidence
records regardless of age. Keep dependency closure (FKs AND JSON references),
including first frozen selections and disputed results. BoardEdition compaction
requires a separate reference-aware plan; never TRUNCATE it.

Only after backup + restored parity + real application read-through proof:
prepare exact per-table before/candidate/retained/after counts and immutable IDs
from the verified snapshot, recheck source changes, stage an authenticated archive
catalog, test atomic catalog cutover and rollback, and present the bounded plan
for **separate explicit owner approval**. No executable cleanup/reset is supplied
or authorized by #168. Rollback preserves original source IDs and archived
lineage; it never points old references at newly reused match IDs.

The current free Render instance expires on 2026-11-26 regardless of deleting
rows. Plan an eventual new free-database cutover only after preserved offsite
backup and explicit approval; only one free database can be active per workspace.
See current [Render free limits](https://render.com/docs/free) and
[free logical export guidance](https://render.com/docs/postgresql-backups).
No new instance, paid plan, writer/scraper restart, master merge or deployment.
