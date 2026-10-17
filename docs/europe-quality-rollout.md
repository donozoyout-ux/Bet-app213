# Reviewed Europe rollout (2026-10-17)

Live Goaloo catalog identities, all published seasons overlapping 2024, and deterministic early/middle/late finished odds samples per season. Counts include only unique fixture IDs; finished counts exclude future kickoffs. Sampling does not establish full-population odds completeness. No source values are synthesized.

| Goaloo ID | League | Finished 2024+ | Dated fixtures | Complete odds samples | Initial rollout |
|---|---|---:|---:|---:|---|
| 3 | Austrian Bundesliga | 532 | 615 | 12/12 | Import only; disabled |
| 5 | Belgian Pro League | 857 | 1091 | 12/12 | Enable existing history |
| 6 | Poland Ekstraklasa | 840 | 1056 | 12/12 | Import only; disabled |
| 7 | Denmark Superliga | 538 | 609 | 12/12 | Import only; disabled |
| 22 | Norway Eliteserien | 658 | 722 | 9/9 | Import only; disabled |
| 26 | Swedish Allsvenskan | 665 | 720 | 9/9 | Import only; disabled |
| 27 | Switzerland Super League | 642 | 714 | 12/12 | Import only; disabled |
| 29 | Scottish Premiership | 621 | 771 | 12/12 | Enable existing history |
| 32 | Greece Super League A | 649 | 789 | 12/12 | Import only; disabled |
| 37 | England Championship | 1491 | 1923 | 12/12 | Import only; disabled |
| 133 | Croatia 1.Division | 493 | 628 | 11/12 | Import only; disabled |
| 137 | Czech Republic Gambrinus Liga | 773 | 933 | 12/12 | Import only; disabled |

All final schedule checks had zero parse errors and zero final request failures; sampled odds requests had zero failures. These are observed run rates, not long-term source reliability estimates. Croatia has one incomplete historical sample; missing fields remain NULL.

The Championship/Belgian/Austrian/Swiss/Norwegian playoff feed includes flat two-leg rows. The parser expands only fixture arrays whose teams match their enclosing tie. Old 2022-2023 club archives do not overlap 2024; skip these, preserving 2023-2024. Czech 2024+ schedules and all 12 odds samples then pass.

Production baseline: Belgium 689 finished matches and Scotland 504. Each bookmaker has opening/closing 1X2 and AH/OU lines for all these rows. Austria has 428 finished matches but only 251 priced matches; keep disabled. Existing rows are never re-created.

New reviewed rows are seeded disabled. Only Belgium (5) and Scotland (29), with existing history, enter the explicit production scope. The existing quality/sample-size/fresh-price gates remain in force. Albania 315/348 retain PR #90 pilot scope and cannot auto-enqueue empty historical jobs.

## Capacity and execution

Confirmed existing Render database: dpg-db39f66k1f9s738tgug0-a, free plan, fixed 1 GB (https://render.com/docs/free). SQL baseline: betappv33 417109695 bytes; postgres 7853759 bytes. These database-size measurements exclude WAL and host filesystem overhead. No disk metric is exposed by the connected MCP, so the importer reserves at least 300 MB by stopping at 700000000 aggregate database bytes. Capacity checks fail closed and run before each network/write boundary. No storage resize, new cloud service, or production deletion.

Explicit workflow: `.github/workflows/europe-bounded-history.yml`. It has no schedule. Select 1-3 reviewed IDs; maximum 25 matches per league per invocation. Repeat manually after inspecting results. Existing external_match_id uniqueness plus durable job items prevent duplicates; retries do not rewrite existing scores. Source failures are retried up to three invocations, then retained as source_unavailable with NULL fields. The job does not execute in the web process or hold the live-worker lock; source requests run outside SQL transactions. Import-only leagues are excluded from the regular worker.

Workflow uses the existing RENDER_DATABASE_URL secret and explicitly clears Telegram/NVIDIA credentials. It calls only Goaloo and PostgreSQL; no Telegram or NVIDIA entry points. Per-match transactions preserve progress across timeout/restart. Job output reports actual populated opening/closing fields and measured capacity.

Initial live checks: /health 200 (0.91 s), /api/diagnostics 200 (2.30 s). Accessible request logs from 10:00 UTC to check time contained no HTTP 504; HTTP metrics returned no data, so this is not a comprehensive availability claim.

Validation: source/parser/admission tests; PostgreSQL CI integration exercises bounded imports, source-failure resume, disabled league state, populated real-fixture odds, and preservation of corrected scores. Existing UI files are untouched.
