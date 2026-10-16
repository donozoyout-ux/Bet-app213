# API publication latency investigation — 2026-10-16

Production observations after merged #83/#84 (read-only, no deploy):

- Concurrent GET predictions limit=8: 504 / 4474 ms; best limit=8: 504 / 4959 ms. Daily coupon: 200 / 978 ms; revision: 200 / 1822 ms; Telegram status: 200 / 1259 ms. These include external network time.
- Server logs for these requests: board_read 4014.5 / 4431.7 ms, only one completed SQL (metadata, 437.8 / 300.5 ms). The next payload read never completed before the existing four-second budget. No current_quotes stage was reached.
- A second isolated live GET returned 504 / 5343 ms. A concurrent pg_stat_activity sample caught betapp-web actively executing publication_json_cast for 3055 ms, no wait event and zero blocking PIDs (query hash 1cd347708c821750a8349cac2d36f0d4). This directly identifies the in-flight expensive SQL, rather than attributing the timeout from averages.
- Eleven valid boards total roughly 9.7 MB uncompressed JSON. Per-board payloads: 0.12–1.25 MB; normal strip items are only 6.7–67.5 KB. Former HTTP query cast the entire evidence document to JSONB before projecting small fields. Coupon and engine evidence were parsed even for a list read.
- Worker profiles: board checkpoints 8.0–11.4 seconds / 25–91 SQL, SQL time 1.5–2.4 seconds; global input_revision 646–1176 ms. Most checkpoint wall time is outside completed SQL; this is a shared-resource contributor, not proof of a database lock.
- Connection sample: web one idle, scraper two idle + one idle-in-transaction. A second sample found scraper idle-in-transaction 538 ms, ClientRead, zero blocking PIDs. No connection exhaustion or blocker was observed in these samples; they cannot exclude intermittent saturation.
- Telegram scheduler logged a deferred TimeoutError during worker activity. GET status is safe; no getMe/sendMessage or NVIDIA request was made.

- Render 18:10–18:25 UTC minute averages: service CPU 0.088–0.139 of 0.15 CPU (59–93%); RAM 243–252 MB of 537 MB. PostgreSQL CPU 0.021–0.037 of 0.1 CPU; active connections 4–8. No memory exhaustion was observed. Minute averages do not measure short CPU bursts.
- Existing indexes include board/head league primary keys, match (league_id,status,kickoff_at,id), price match IDs and market (match_id,market). Read-only EXPLAIN of the former projection shows a materialized CTE over 11 board rows; another index cannot remove per-row whole-document JSONB conversion. EXPLAIN ANALYZE is restricted to the isolated PostgreSQL test schema.

## Change

Worker publishes `board_read_publications` in the same transaction as the immutable edition/head, with separate items, availability, evaluated, coupon records and engine columns. PostgreSQL list HTTP reads select the first three directly, without JSONB casts or loading coupon/engine JSON. Primary key league_id bounds the current table to one row per league. Version equality with the current head and current board validity are mandatory. Existing installations warm at normal worker checkpoints; before projection availability, the response explicitly reports warming/partial and never performs a legacy heavy fallback. There is no HTTP-side backfill or model calculation.

Unchanged evidence retains the version, generation time, ordering and projection JSON; missing projections are rebuilt by the writer. Expired publications, kickoff/status/rescheduling checks and fresh bookmaker validation still run. Missing projection versions are excluded before cache lookup, so warming cannot be cached for ten minutes under the eventual valid key. The new board_payload stage logs duration safely without IDs, SQL parameters or secrets.

No timeout, pool size, quality threshold, 900-second freshness, independence rule, Telegram schedule or model math was changed. #84 per-key single-flight remains intact.

## Verification and limits

SQLite regression tests cover unchanged/no JSON rewrite, existing-edition warming and changed-version identity. PostgreSQL CI compares the old cast and new direct query on the same isolated ~10MB synthetic evidence publication, checks identical output, uses EXPLAIN ANALYZE in its isolated schema only, verifies concurrent cold readers and live freshness/status guards. Fixtures are test data, never production display data.

This PR is not deployed. The structural improvement and isolated timings do not establish production recovery. Repeat concurrent GET probes and inspect board_payload/current_quotes phase profiles under worker load after an authorized deployment; do not report the live 504 as resolved before that check. Further worker revision/backfill tuning must preserve cross-league UEFA evidence and be measured separately.
