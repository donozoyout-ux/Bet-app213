# Issue 176: application integration, first narrow draft

Based on clean `origin/master` (`c6f79860`), after reviewing #175 and #178.
Includes the two reader commits from #178; does not merge #174 or rewrite history.
This is the first application integration slice permitted by the latest issue
comment, **not completion of either full application coverage or live-source parity**.

## Connected surfaces

| Surface | This draft | Remaining |
|---|---|---|
| Existing dashboard match detail | Cold fallback on `/api/matches/{id}`; score, identity and recorded odds; archive revision and unverified-source notice | List discovery/paging across hot+cold; full `/match/{id}` dossier |
| Existing dashboard odds | Cold fallback on `/api/matches/{id}/odds` | Full relational odds export and original provider payload preservation in private backup |
| Separate `/history-archive` view from #178 | Encrypted records, date/match/board/revision filters, pinned paging, explicit outage | Real private release evidence |
| Tahminler | Not connected | Immutable board/publication/analysis records and revision-aware merge |
| Kuponum | Not connected | Owner isolation, immutable legs, board/settlement JSON closure |
| Performans | Not connected | Verified settlement history and aggregate identity deduplication |
| İstatistikler | Not connected | Published pre-match analysis; never regenerate historical predictions |
| Dersler | Not connected | Learning report/lesson/slice dependencies and immutable evidence |

The current dashboard panel is distinct from the standalone `/match/{id}` dossier.
Do not describe the full match screen or any unconnected surface as complete.

## Read contract and safety

Disabled by default via #178's `HISTORY_ARCHIVE_ENABLED` configuration. Ordinary
hot hits never contact GitHub, including during an archive outage. A hot miss
falls through only when enabled. A database outage remains a database error:
it is not mistaken for proof that a hot row does not exist. Existing disabled
league rows remain inaccessible. No rows are inserted, edited or removed.

Both routes accept optional `board_version`, `revision`, and `snapshot`. An
explicit selector reads the immutable archive instead of returning an unrelated
current hot value. With no board selector, multiple matching boards fail closed;
within one board the highest revision wins even if kickoff was corrected.
Hot/cold records are never concatenated in these singular responses. Existing
collection endpoints are unchanged; combined collection pagination is not claimed.

The authenticated manifest, SHA256 and AES-GCM checks from #178 are retained.
Reader errors, corrupt/missing assets, version ambiguity, timeout or ineligible
records produce 503, not a successful empty history. A verified index with no
matching identity produces 404. Tokens, keys and raw provider payloads never
enter public output. Empty `raw` objects satisfy the existing public contract;
they explicitly do **not** mean private raw backup data was preserved by this
projection. Archive headers carry snapshot/board/revision, source parity
`unverified` and cleanup authorization `false`. The dashboard shows the archive
revision and source limitation, and names archive outages explicitly.

Fallback rejects current Istanbul day through day+3, all later dates, unresolved
status, absent scores, absent/pending evidence, mismatched evidence revision or
scores. Such records must remain hot. This is read eligibility only, never a
retention authorization. A recorded terminal result is not independent source
parity evidence. Archive requests share the reader concurrency limit and have a
10-second total bound within the database request deadline.

## Full relational inventory required for subsequent drafts

No projection-only count can prove a full backup. Include every non-system
relation discovered from the source catalog, including unknown future tables.
At minimum account for these groups and dependencies:

| Group | Relations / dependencies |
|---|---|
| Match identity | matches, teams, leagues, seasons, referees, bookmakers; original internal and external identifiers |
| Prices | odds_1x2, asian_handicap, asian_totals, market_odds, odds_snapshots; match/bookmaker, period, observation time, original JSON |
| Match facts | match_events, match_statistics; original source keys, event revisions, result evidence |
| Immutable predictions | board_editions, board_heads, board_read_publications, prediction_boards, prediction_snapshots, prediction_publications, published_match_analyses, prediction_settlements |
| Coupons | saved_coupons, daily_coupon_publications, coupon legs/owner/version/settlement links in JSON; telegram delivery references |
| Learning | learning_reports, learning_heads, learning_slices, learning_lessons and all learning_ai_* tables |
| Operational references | scraper_jobs, scraper_job_items, collector_retries, scraper_issues, worker_checkpoints and newly discovered relations |
| Non-table state | constraints and validation state, indexes, triggers/functions, sequences, large-object identities/metadata/content, schema/extension versions |

Discover FK edges from the catalog; extract known match/board/publication/coupon
references from every JSON payload without changing the payload. Unknown JSON
reference shapes, RLS visibility restrictions, missing parents, foreign tables,
unvalidated constraints and permission failures block complete-coverage claims.
Referenced hot parents and all pending/unknown-evidence records remain hot.
No name allowlist may silently exclude a new relation from the full inventory.

## Bounded read-only source parity design (not executed)

1. Use a separately supplied read-only credential and an owner-scheduled window.
   Begin one `REPEATABLE READ READ ONLY` transaction, with 30-second statement,
   2-second lock, 60-second idle-in-transaction and 5-minute client wall limits.
   Fix UTC, ISO dates, bytea hex and float formatting. Verify effective read-only
   mode, role visibility/RLS, PostgreSQL version and schema before scanning.
2. Associate a consistent dump with that exact exported snapshot. A newly taken
   live snapshot cannot certify the old ZIP merely because a few counts match.
   If no same-snapshot dump is available, report comparison/mismatch only and
   leave source parity unverified. Do not keep transactions indefinitely to retry.
3. Stream rows through server cursors (256 rows per fetch). No server-side sort,
   temporary table or aggregate over giant JSON. Hash canonical full rows on the
   client, preserving duplicates, then sort hashes in a bounded local spool.
   Set explicit 1,000-relation, 8-GiB stream and 2-GiB spool caps; exceeding any
   cap aborts as incomplete, never samples and never declares parity. Oversized
   individual rows fail closed. Report only counts/hashes, never row contents.
4. Independently compute the same inventory after downloading authenticated
   GitHub parts and restoring locally. Match schema/constraints, all table row
   multisets, FK/JSON closure, immutable revisions, large-object pages and IDs.
   Sequences are non-MVCC: concurrent sequence advancement invalidates an exact
   full-state claim unless an owner-established quiescent capture proves it.
5. Bind report to source snapshot/time, dump digest, manifest/part ciphertext
   digests, reader/exporter versions and downloaded independent restore digest.
   Always roll back/close the source transaction on success, timeout or failure.
   Logs must contain only stage and sanitized error class, never DSN/SQL/rows.
6. A local restore comparison proves local restore parity only. Real GitHub
   read-through and live-source equality need separate evidence. Even all equal
   values do not authorize cleanup: `cleanup_authorized=false` is unconditional.

No source connection, dump, upload, SQL maintenance, history rewrite, merge or
deploy is part of this draft. This design needs a separate reviewed implementation
before running against Render/Neon. Never use NVIDIA or Telegram for these tests.

## Evidence and owner steps

The issue reports the successful #177 local run. The sanitized `verification.json`
was not supplied at a known path in this chat; it has **not** been independently
inspected here. Original ZIP/key were not opened or uploaded. Actual private
GitHub archive reads and live-source parity remain **unverified**.

1. Provide only the local path of the sanitized #177 `verification.json`. Keep
   ZIP/key local. Check its pinned ZIP/dump digests, complete inventory and false
   source/GitHub/cleanup flags before citing it as local-restore evidence.
2. Review this draft and the remaining coverage checklist. Do not enable in
   production as a replacement for existing history storage.
3. Next narrow draft: typed immutable publication/analysis and dossier projection,
   full relational exporter/index with FK/JSON closure, plus existing Predictions
   and Statistics API integration. Then owner-scoped coupons, performance and
   learning surfaces with deterministic hot/cold paging and identity deduplication.
4. Implement and separately review the bounded source comparison above. Obtain
   same-snapshot source/export evidence and a real private-GitHub independent
   download/restore. Keep issue #176 open and cleanup authorization false.

Local tests use only synthetic encrypted records via `httpx.MockTransport`, empty
SQLite fixtures and the real ASGI app. Chromium runs the existing dashboard and
asserts cold detail/revision rendering and an explicit corruption outage. CI runs
these tests explicitly and retains the repository's full test suite.
