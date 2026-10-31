# Separate plan after verified full recovery — NOT an eviction authorization

This document is a proposal only. No source deletion, update, truncate, vacuum or
history migration is executed by this PR. Current database capacity remains used.

1. Require published private recovery Release + authenticated manifest/receipt,
   successful independent PostgreSQL restore and complete per-table count equality.
   Keep encryption key separately. Record source snapshot time and exact code SHA.
2. Re-measure DB/relation sizes and physical headroom read-only; backup uploads
   reclaim zero source bytes. Keep workers/backfill/schedulers disabled at current
   high-watermarks. Render expiry needs its own continuity decision.
3. Inventory candidate historical records by league/season/source match identity.
   Finished >=24h is necessary, not sufficient: unresolved settlement, missing or
   conflicting provider result, incomplete FT/HT markets, live/postponed matches
   remain in PostgreSQL. Current missing finality evidence is not waived by a full
   recovery backup. Preserve first predictions, publication revisions and lessons.
4. Complete genuine provider evidence, then publish eligible dependency-complete
   match-history generations through #157. Restore/read-through each generation;
   pin the reviewed root and demonstrate hot/archive identity collision handling.
   A full pg_dump archive alone cannot serve these application read paths.
5. Board editions are the largest potential recovery target, but keep every edition
   referenced by current heads, frozen coupons, delivery/audit/lesson provenance
   or unpublished generations. Map FK AND embedded JSON references before proposing
   bounded archival batches. Full recovery copy is an extra safeguard, not enough
   to declare referenced rows safe to remove.
6. Present exact candidate IDs/counts/dependency closure and retained reasons to
   owner in a separate PR, with archive receipts and rollback drill. Only explicit
   new owner approval may authorize any later eviction. No generic retention age
   or automatic deletion follows from this plan or from successful backup CI.
