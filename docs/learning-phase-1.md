# Prediction memory and result audit — phase 1

Branch: `codex/learning-phase-1`, based on master `d291000`.

## Storage and result contract

The existing `prediction_snapshots` table remains the only prediction publication store. Its first model version, original picks (including probability, market, selection and any `verified_odds` source/time/price metadata) are preserved. New publications now record the actual board MODEL_VERSION (including engine flags) rather than the old hardcoded half-goals label. An additive PostgreSQL migration widens model_version from 40 to 100 characters without relabeling existing records. No historical prices or probabilities are filled from today's model or odds tables. PostgreSQL and SQLite reject UPDATE and DELETE of snapshots and settlement rows at the database level. The application database owner can administer its own triggers; these are operational integrity controls, not protection against a malicious database administrator.

`prediction_settlements` adds terminal decisions only, keyed by snapshot match, immutable pick array index, and revision. Unique insert-on-conflict makes concurrent/repeated workers idempotent. Each decision contains actual value, final score/statistics evidence, evidence observation timestamps when available, evaluator version, snapshot/evidence SHA-256 hashes, recorded time, actor and reason. Hashes verify the stored original payload, not the truth of an external provider's data.

Live score, schedule and statistics storage settle in their existing transaction; a rollback also rolls back decisions. A bounded scraper sweep visits up to 100 old snapshot matches per minute and cycles back to retry missing data. No retrospective prediction generation takes place. Deploying later would record the first complete evidence observed by this implementation, not invent a historical settlement time. Missing observations remain unrecorded until sufficient data arrives.

History prefers recorded decisions. During backfill, the existing read-only evaluation remains available with `settlement_basis=unrecorded_evaluation` and no revision; it is explicitly excluded from the Lessons data layer. Recorded results use `settlement_basis=recorded`. History reads do not write to the database. Enabled-league filtering in the existing UI is unchanged.

The five history states are `pending` (Maç bekleniyor), `awaiting_data` (Sonuç verisi bekleniyor), `won`, `lost`, and `void`. Postponement remains pending; cancellation/abandonment is void. Goals require full-time score; first half requires half-time score; second half requires both with consistent differences. Corners require both final corner counts. Combined cards require all four yellow/red counts; yellow-only and red-only require their own two counts. Nulls never become zero. Unknown selections, malformed selections, unsupported quarter lines and negative full-time/count values await data rather than guess a result. Existing impossible half-score validation remains void.

A source correction does not automatically replace an existing terminal result. `correct_settlement` is a trusted internal administrative function, with no public HTTP endpoint. It requires a named actor, nonempty reason and expected revision; it recomputes from current complete final evidence and appends a new revision. Concurrent stale corrections fail. Prior evidence and results remain accessible in the append-only table. Routine workers never issue corrections.

`resolved_predictions(session, after_id=0, limit=200)` is the read-only Lessons boundary: bounded cursor, prematch-only snapshots, recorded terminal decisions, hash validation and replay against recorded evidence, original model/probability/price and copied payloads. It includes explicitly marked voids; training consumers must exclude voids. A cursor follows first-decision IDs and returns the latest correction for each selected prediction. A consumer that needs to discover corrections to already-consumed IDs must re-read that prediction's revision history (the cursor is not a correction event feed).

## Live database coverage (read-only)

Source: Render `betappv33`, PostgreSQL 18, 15 October 2026, queries during 15:25–15:34 Europe/Istanbul. SQL was executed by Render's read-only transaction tool; no schema initialization, settlement writes, scraping, deployment or Telegram sending was performed against production.

There were **129 snapshot matches / 276 selections**, with **0 invalid capture timestamps**. Production had **no durable settlement table**, so **0 persisted terminal decisions** under the new contract. Replaying the existing evaluation rules over exported current source evidence gives:

- Won: **92** selections.
- Lost: **28** selections.
- Void: **1** selection.
- Match pending: **155** selections across **67** matches.
- Result data pending for the selections actually saved: **0**.
- Finished snapshot matches: **61**; another **1** snapshot match is void.

Per-market selection counts (won / lost / match pending / void):

- Goals: **43 / 9 / 11 / 0**.
- Double chance: **23 / 10 / 3 / 0**.
- Result: **10 / 4 / 0 / 0**.
- Corners: **5 / 1 / 54 / 1**.
- First-half goals: **3 / 1 / 17 / 0**.
- Second-half goals: **3 / 2 / 35 / 0**.
- Yellow cards: **4 / 0 / 35 / 0**.
- Asian handicap: **1 / 1 / 0 / 0**.
- No saved combined-card or red-card selections were present; this is not evidence of complete coverage for these markets.

Among the **61 finished snapshot matches**, **43 lack at least one red-card count** and **3 lack at least one yellow-card count**. None lack full-time scores, half-time scores, final statistics flags or corner counts. Those missing card values block the corresponding combined/red/yellow markets if selected. They do not block goals or independently complete yellow-only selections. Missing red/yellow sets may overlap, so do not sum those counts as unique matches.

All **276** exported historical picks lack `verified_odds.has_odds=true`. Their original odds remain unknown; none were reconstructed. Model versions present are `quality-poisson-v1`, `quality-poisson-v2-cards`, and `quality-poisson-v3-half-goals`.

`learning-phase-1-audit-input.json` contains the minimal score/statistics/pick evidence exported for reproducibility, without credentials, team names or raw scraper pages. Its `audited_at_utc` marks the start of the read-only audit window; queries were separate read-only transactions, not one global frozen database snapshot. Reproduce the selection counts with:

```powershell
python -m scripts.audit_prediction_memory --input docs/learning-phase-1-audit-input.json
```

For a fresh authorized database audit, set DATABASE_URL and run `python -m scripts.audit_prediction_memory`. That command starts a repeatable-read, read-only transaction and never calls schema initialization. Its counts describe current-source evaluation, not persisted outcome history. No source odds missing from the original pick are added.

## Validation

Verified on Python 3.12 / Windows, isolated local PostgreSQL 16.4 and Chromium:

- Full Python regression run: **406 passed / 19 skipped** (425 collected at that run). Its 16 PostgreSQL skips were subsequently covered by the separate PostgreSQL run; the other three require POSIX Gunicorn.
- Final phase-1, PostgreSQL storage and browser selection after all source changes: **27 passed**.
- Final complete PostgreSQL selection: **20 passed / 2 skipped** (POSIX Gunicorn only).
- Explicit Chromium UI regression selection: **6 passed** (includes all five history labels).
- JavaScript runtime regressions: **40 scenarios passed** (7 dashboard, 19 predictions, 4 market, 10 coupon); all dashboard JavaScript syntax checks passed.
- Python compileall and git diff whitespace checks passed.
- Read-only audit replay reproduced all 276 outcomes and coverage counts.

These selections overlap; their pass counts must not be summed as unique tests. New coverage includes all seven total-count markets, null versus explicit zero, incomplete final cards, repeated/concurrent settlement, rollback, append-only database protection, explicit audited correction, stable original probability and Lessons integrity/cursor behavior, plus rendered Chromium labels for all five states. Existing PostgreSQL integration, Python, JavaScript and Chromium regressions are also run.

The dynamic coupon engine, model probabilities, thresholds, Goaloo parser, Telegram schedule and NVIDIA integration were not changed. No merge, deployment or real Telegram send is part of this change.
