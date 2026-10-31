# Issue #164 — immutable editions, first reviewable phase

## Scope and rollout boundary

Based on PR #163 (`72b0e36`). This change adds a pure contract/revision gate,
shared serializers and a bounded read-only SavedCoupon adapter. It does **not**
activate a writer, endpoint, UI card or Telegram dispatch path. The issue explicitly
permits this first phase. Existing production behavior is unchanged; end-to-end
application integration is still pending, and #164 must remain open.

No migration, cleanup, live database access or Render deployment is included.
The production backup and board_editions capacity risks remain unresolved.

## Existing writers and readers audited

| Record | Writer | Reader / limitation |
| --- | --- | --- |
| DailyCouponPublication | publications.publish_daily (live updater, explicit automatic route, Telegram claim) | read_daily / revisions; mutable day+strategy head; stable() excludes quote timestamps; cannot prove immutable history |
| SavedCoupon | saved_coupons.automatic / save_manual | detail / accessible; ID and root+revision available; historical automatic payload can lack model version, line, period and capture provenance |
| TelegramDelivery | jobs.telegram.claim / dispatch | unique coupon_id and unique 3-hour slot; durable claim before network; existing checks unchanged |
| PredictionPublication | prediction_audit.record_publication during board publishing | audit; match/board identity, not coupon identity |
| BoardEdition | publications.publish_board | stable board payload omits quote evidence; not a coupon ledger |
| PredictionSnapshot / PredictionSettlement | performance.capture / settlements | first prematch record and separately verified terminal decisions; must never substitute newer picks |

The existing Telegram claim can rebuild a daily head and substitute revalidated
quotes for rendering. The new dry-run deliberately has no connection to it.

## Contract

`coupon_editions.propose` takes one PR #163 candidate product plus an explicit
Istanbul day, aware capture timestamp and model version. All four kinds retain
match/recommendation identities, market, selection, exact line/period, provider,
price and observation/expiry timestamps. Provider is `quote.bookmaker`.
It reuses the quality/risk policy (70% probability, 80% reliability/quality,
900-second price evidence), validates recommendation/quote identity and real
product ranges, and never fills missing legs or trusts a supplied combined total.

Canonical UTF-8 JSON (sorted object keys, compact separators, no NaN/Infinity)
is SHA-256 hashed including day/kind, evidence, capture time and lineage. Item
order is normalized by match/recommendation identity. The ID is `ce1:<digest>`.
The frozen dataclass stores canonical text; each projection returns a fresh copy.
Timestamps normalize to UTC; day scope uses Europe/Istanbul.

Retry against the same previous edition returns its exact canonical document,
including after expiry, without re-evaluating its original eligibility. Any
content/evidence/model change requires `new_revision=True`, the previous edition,
an incremented revision and an explicit diff. Capture cannot predate the prior
revision. Different days start separate lineages. A changed observation stamp
is a content change. A restart loads and verifies the canonical document.

`project` is shared by proposed Home/Kuponum/audit consumers; `project_four`
reports missing kinds as `absent` without computing candidates. Frozen empty
products are `no_eligible`, search-budget exhaustion is `incomplete`; expired
prices, started fixtures and previous days are `stale`, retaining all selections.
The document hash excludes only projection-time status/check timestamps.
`telegram_dry_run` returns the same canonical frozen text and identity tuple,
never calls a transport and marks stale/empty editions unsendable. It is a
review artifact, not a production Telegram message formatter or send reservation.

`read_saved` issues one PK-bound SELECT under no_autoflush. It projects public
automatic rows without rebuilding missing evidence or inventing a new edition
identity. Original ID/root/revision and the detached legacy payload are preserved;
`legacy_unverified` explicitly distinguishes old records. `first_record` means
first revision within that root, **not** earliest publication across the day.
Private/manual rows are excluded. Existing live heads are not silently upgraded.

## Persistence design — requires a separate review

Do not store `ce1:` IDs in SavedCoupon.id (36 characters). Do not append large
JSON history to DailyCouponPublication or repurpose BoardEdition. SavedCoupon
can carry a compact contract under a payload key with its existing UUID as a
storage ID, but its current schema does not enforce unique day+product+revision
or immutable digest identity. Its legacy automatic fingerprint and root revision
constraints alone do not provide the required product-head CAS semantics.

Before wiring a writer, review a dedicated migration/transaction PR that defines:

1. A bounded append-only coupon edition record with day, product kind, storage
   UUID, digest, previous ID, revision, model/capture stamps and compact selections.
2. Unique (day, kind, revision), unique digest identity, and a small day/kind head;
   lock/CAS the expected previous ID and atomically insert+advance. An equal
   digest is a retry; concurrent different edits conflict, never overwrite.
3. Size/retention/capacity estimates and verified restore evidence before DDL;
   do not backfill or rewrite legacy publications. Legacy absence stays explicit.
4. Consumer and settlement references to edition ID plus separate result evidence.
   Missing FT/HT or invalid evidence stays awaiting_data/review, never a loss.
5. A reviewed Telegram transition: retain 3-hour scheduling, durable claims,
   duplicate selection prevention and ambiguous-send at-most-once behavior.
   Dry-run is not authorization to send or populate TelegramDelivery.

The pure revision gate is not a concurrent/durable database implementation.
No migration executable is supplied in this PR.

## Follow-on phases

After persistence review, wire shared readers to Home/Kuponum/audit/performance
and test actual routes for SELECT-only behavior and no model scans. Then add
four published-only cards and 390px/1536px overflow/ARIA checks without touching
PR #159 styling. Finally integrate the reviewed dry-run flow; sending remains
separately controlled. Existing routes are not claimed to use this contract yet.

## Evidence

Synthetic fixture round trip: publication -> canonical restart -> Home projection
-> Kuponum projection -> Telegram dry-run -> audit projection -> existing settle.
Tests assert identical match/recommendation/quote/digest, no alias mutation,
explicit revision diffs, day/product isolation, stale/retry behavior, low-quality,
fake/expired/future prices, same-match conflict, line mismatch, missing FT/HT,
and legacy SELECT-only/no-autoflush reads. No production data is used.

`docs/examples/coupon-edition.json` is a synthetic proposed API projection, not
a deployed endpoint response. Its captured/checked/observed times are fixtures.
