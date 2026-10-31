# Issue #166: isolated append-only coupon persistence review

## Status and forbidden rollout

This PR is stacked on #165. It is a **test-only design and implementation**.
No application route, worker, startup migration, Telegram delivery or UI imports
this store. Production connection / DDL / DML / backfill / deployment is forbidden.
The existing production backup and capacity problems remain unresolved.

`IsolatedEditionStore(..., enabled=False)` fails before creating an engine.
Even explicit `enabled=True` accepts only PostgreSQL asyncpg, a loopback host,
no URL query overrides, database `betapp_test`, and a generated
`coupon_test_<32 hex>` schema. It never reads DATABASE_URL or application settings.
These are misuse guards, not authorization to point a local proxy at production.
The only supported execution is the disposable PostgreSQL CI service; never use
a production tunnel, restore containing personal data, or production credentials.

## SQL draft and migration behavior

[SQL review draft](sql/coupon-edition-review.sql) is not an executable production
migration. No objects are registered with `Base.metadata`; application startup's
`create_all` cannot create them. The test fixture alone creates a random schema,
loads this draft in a transaction and drops only that schema at cleanup.
Production deployment of this source would still not install or enable storage.
There are no reads/writes to SavedCoupon, BoardEdition or historical 2024+ records.

`coupon_edition` stores a separate UUID storage PK, Istanbul calendar day, kind,
revision, original ce1 ID/digest, previous ID, effective first ID, capture/model
metadata, and at most **16,384 canonical UTF-8 content bytes**. Only the redundant
ID/digest are omitted from content; reconstruction returns exactly the #165
canonical document. Immediate revision diff is retained; no recursive history,
board inputs or giant prediction JSON is stored. Oversized editions fail closed;
they are never truncated and the byte cap does not weaken selection thresholds.

`coupon_head` contains only day, kind and edition ID. It has a (day,kind) PK
and a scoped FK into the edition ledger. A missing head is absent, persisted
empty content is no_eligible, budget-limited content is incomplete, and stale
is a read-time label on unchanged frozen selections.

Constraints and triggers:

- Unique digest plus ce1-prefix identity check; unique (day,kind,revision) and
  scoped (day,kind,edition_id) key. Four allowed product kinds only.
- SHA-256 of the exact stored content, UTF-8 byte cap, metadata/content equality,
  Istanbul capture date, and scoped previous/first references.
- Revision 1 self-identifies as first (the original document still has
  first_edition_id=null); subsequent entries must reference previous revision+1,
  same first ID/scope and nondecreasing capture time.
- Edition UPDATE/DELETE/TRUNCATE rejected. Head DELETE/TRUNCATE rejected;
  UPDATE must follow its current edition within the same scope, never rewind.

The DB checks integrity/lineage, not statistical truth. The writer revalidates
an incoming contract with #165 `propose` at its capture time and compares the
entire canonical document (including diff, total and revision). This rejects even
rehashed low-quality, fake-price, duplicate-match and forged-lineage documents.
New inserts must still be within their Istanbul day and before price/kickoff
expiry at admission. Exact stored retries bypass re-evaluation and can return
old editions after expiry/day rollover, without moving a newer head backwards.

## Atomic protocol and concurrency

1. Own a fresh transaction; check for the exact stored ID/document (retry).
2. Resolve the explicit expected previous digest and validate the complete proposal.
3. Insert the immutable edition, subject to unique scope/revision and FKs.
4. First edition inserts a head with ON CONFLICT DO NOTHING; revisions UPDATE
   the head only WHERE its edition_id equals the expected ce1 digest.
5. Require one returned head; otherwise raise PublicationConflict (HTTP 409
   equivalent) and roll back the entire transaction, including the edition.
6. If a concurrent insert loses on a unique constraint, rollback first and read
   in a fresh transaction. Return only an exact identical committed edition;
   all differing proposals conflict. Never silently retry changed content.

Two independent connections are synchronized after their initial absence reads
in the race tests. Both then compete on the real PostgreSQL unique index/CAS.
Different initial/revision proposals yield one success and one conflict; identical
proposals yield two identical replies and only one stored revision. Tests inject
failure after the edition INSERT and before head CAS, then restart and retry,
proving no half-publication survives. Reads use SELECT only and do not run DDL,
model scans, odds fetches, autoflush or repair. There is no HTTP GET route yet.

Reference semantics: PostgreSQL 16 documents concurrent UPDATE predicate
re-evaluation under [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html)
and built-in [sha256(bytea)](https://www.postgresql.org/docs/16/functions-binarystring.html).
No pgcrypto extension or extension installation is needed.

## Capacity calculation (synthetic, not a production free-space assessment)

The #165 fixtures produce these canonical content sizes:

| Kind | First content bytes |
| --- | ---: |
| banko | 810 |
| balanced | 3,507 |
| combined_6_7 | 1,895 |
| combined_8_10 | 1,894 |
| All four | 8,106 |

A changed Banko revision with its immediate before/after diff is 2,091 bytes.
Per-row limit is 16,384 bytes, including diff. Metadata is bounded separately
(model <=100 characters; fixed-size identifiers/date/revision). There is no claim
that a 16 KiB document means 16 KiB of total physical PostgreSQL usage.

| Scenario | Rows/year | Fixture-sized content/year* | Max allowed content/year |
| --- | ---: | ---: | ---: |
| Four products, one edition/day | 1,460 | 2,958,690 B (2.82 MiB) | 23,920,640 B (22.81 MiB) |
| Four products, eight editions/day | 11,680 | 23,669,520 B (22.57 MiB) | 191,365,120 B (182.50 MiB) |

*Fixture-sized is illustrative, not a bound: revision diffs increase sizes.
Head growth is up to 1,460 rows/year (day+kind). Four ledger indexes and the head
PK, heap tuple/page overhead, TOAST, WAL, replicas/backups, dead head tuples and
free-space reserve all add cost. A conservative planning allowance of 2x the
max content alone is **45.63 / 365.00 MiB/year**, plus metadata/index/WAL/backup
budgets; 2x is not a measured upper bound. Actual physical measurements from
`COUPON_CAPACITY` in CI cover a tiny fixture only; extrapolation requires a larger
representative workload. Index EXPLAIN disables seqscan only to prove access-path
availability; it is not a production latency benchmark.

Eight/day is a sensitivity scenario matching 3-hour checks, **not a policy cap**.
Arbitrary explicit revisions mean unbounded lifetime growth. Any production
review must approve a rate budget, head retention, disk high-water stop, per-period
monitoring, and an archive policy before enabling writes. The 16 KiB per-row cap
alone cannot solve an already-full database. No retention/eviction runs here.

Alternative: immutable content objects in an independently controlled encrypted
archive, retaining only digest/UUID/lineage/head references in hot PostgreSQL.
That reduces hot payload growth but adds object request cost, durable upload/DB
ordering, checksum/restore verification, missing-object handling, access control,
and a measured read-latency tradeoff. It requires its own reviewed two-phase
publication and restore design. No external archive is created or paid for here.

## Security and rollout/rollback gate

Review findings before any production proposal:

- The fixture uses a disposable privileged CI DB user. Production needs a
  separate owner/migrator; writer gets SELECT/INSERT ledger and SELECT/INSERT plus
  edition_id-only UPDATE on head. Reader gets SELECT only. No runtime DDL,
  ledger UPDATE/DELETE/TRUNCATE, trigger ownership or disabling permissions.
- Digests detect corruption, not authorship. Accept only trusted validated
  candidate records; a DB owner can forge content or disable triggers. No secrets,
  user owner hashes or personal payloads belong in this ledger.
- Raw administrative INSERT can create an unreferenced row; normal writes are
  transactionally coupled to the head. Only the reviewed writer should receive
  runtime insertion rights; an integrity audit must flag orphan lineages.
- The test target guard is intentionally not a production configuration switch.
  A new reviewed implementation/rollout is required to lift the restriction.

**Mandatory independent gates:** verified snapshot plus independent restore to
an isolated database; recorded checksum/count/revision-chain comparison; measured
production headroom and capacity plan; schema/permissions/security review; then
explicit user approval for the specific migration and activation. None of these
production gates is satisfied or executed by this PR. Do not request secrets or
assume the old backup is valid. No merge/deploy in this task.

Rollback before activation: leave feature disabled and the draft unapplied.
After a separately approved rollout: stop writer, keep all immutable editions
and heads, retain read access, return existing application flows to prior behavior.
Do not delete editions, rewind heads, drop tables or rewrite historical data as
an automatic rollback. Restore/reconciliation would be a separately approved
procedure from verified snapshots. Fixture schema cleanup is not a production
rollback script.

## Test evidence

Local pure contract/persistence validation and inherited contract/product tests
run without PostgreSQL or network access. PostgreSQL-specific tests use only the
CI service's betapp_test database and disposable schema. New CI step runs these
before the full existing pytest/Chromium/Node suite. PR description records the
exact final CI run and outcomes, including CAS and physical byte measurements.

No changes to UI, #159 CSS, Home/Kuponum route wiring, Telegram transport,
NVIDIA, scraper enablement, Render configuration or production records.
