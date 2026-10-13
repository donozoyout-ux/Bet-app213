# Telegram, daily coupons and exact quote coverage — Issue 49

## Baseline and PR 48

The implementation started from master `eb9c99c` on 13 October 2026. PR 48 was
open; its CI subsequently passed. Its original bot-error allowlist, regression
test and Turkish explanations are retained. This branch extends those changes
without merging PR 48, merging master or deploying Render.

The new connection check calls only `getMe`, `getChat` and `getChatMember`.
It verifies the expected `@BKupon8Bot` identity, exact configured group identity,
membership and separate text/document permissions. Unknown permissions are not
treated as permission to send. These read methods use POST JSON to avoid putting
group/user IDs in HTTP URL logs. Telegram supports POST for these read methods:
[official API documentation](https://core.telegram.org/bots/api#available-methods),
[membership](https://core.telegram.org/bots/api#getchatmember),
[permissions](https://core.telegram.org/bots/api#chatpermissions).

`POST /api/telegram/verify` requires the existing `SCRAPER_API_TOKEN` Bearer key.
The public status endpoint remains network-free and returns only an allowlisted
safe result. Bot credentials, group/user IDs, chat names and raw provider responses
are never returned, persisted or logged by the checker. The result is cached for
five minutes in the checking web process. Credential changes invalidate the
in-memory result; it does not claim that a different process or replica was checked.
An expired result is shown as expired, never as verified. A read failure performs
no immediate retry and creates no delivery record.

## Real before/after evidence

Production measurements used read-only Render PostgreSQL transactions and GET
requests. The production database reported 12,904 matches, 121 enabled scheduled
fixtures within seven days, 37,911 rows in each of the result/totals/handicap
tables and 16,687 extended-market rows: 130,420 stored quote rows in total.

The live baseline `/api/odds-coverage` returned **504 after 12.703 seconds**.
`/api/telegram/status` returned configured settings but no verified connection;
the noon attempt was `telegram_400` and the evening window suppressed duplicate
selections. The original provider description was not retained, so its exact
cause cannot honestly be reconstructed from that old ledger entry.

A single consistent PostgreSQL snapshot was captured at
**13 October 2026, 20:06:21 Europe/Istanbul** (`17:06:21.132484Z`). It contained
121 upcoming fixtures, 11 boards and 1,481 quote rows with their real field
observation and line metadata. The raw export is kept outside Git. The same
snapshot and the same clock were replayed against baseline and changed code.
Neither odds, probabilities, thresholds nor dates were refreshed or invented.

Both versions returned these identical recommendation measurements:

- 126 board recommendations, with 12 exact fresh prices.
- Corners: 49 recommendations; 4 fresh prices, 40 `no_source_rows`, 5 `line_mismatch`.
- First-half goals: 16 recommendations; 2 fresh prices, 14 `line_mismatch`.
- Goals: 4 recommendations; all 4 `line_mismatch`.
- Result: 5 recommendations, all 5 freshly priced.
- Asian handicap: 1 recommendation, freshly priced.
- Second-half goals: 32 recommendations, all `source_market_unavailable`.
- Yellow cards: 19 recommendations, all `source_market_unavailable`.
- `stale`, `unsupported_bookmaker` and `missing_stage_price`: zero for these
  selected recommendations at this captured clock. Zero is a measurement of this
  scope, not a promise about every stored quote or a later point in time.

The default daily coupon was identical before and after: six daily recommendations
passed the strict quality floor; five lacked usable exact prices and one qualified.
The coupon contained match **10475**, `under_9_5_corners`, Bet365 **1.90**, full time,
line **9.5**. Its observed time was **20:03:21** and expiry **20:18:21** Istanbul.
One selection was retained; the target of four did not cause forced filler.
The full stored automatic payload was equal across both replays.

The changed source audit counted 3,248 priced fields, all with observation metadata,
of which 330 were fresh at the frozen clock. These are source-field counts rather
than 330 qualified model picks. Recommendation-level counts remain the 12 above.

The changed HTTP endpoint explicitly bounds source reads to
`enabled_upcoming_7_days`, projects field provenance and defers large raw payloads.
It normalizes rejection counters and keeps the older market counters for clients.
Source reads recognize observation offsets and validate per-side lines; updating
a row alone does not refresh a price. Unsupported bookmakers remain rejected but
are now measured as unsupported rather than incorrectly erased as missing source.

On the real PostgreSQL database, `EXPLAIN ANALYZE` for the bounded totals projection
returned **363 rows in 4.113 ms**, with 3.140 ms planning. This is a database-query
measurement, not the full HTTP endpoint latency. Snapshot replay used an isolated
SQLite database containing only the exported upcoming data: baseline HTTP 200 in
0.2982 seconds, changed HTTP 200 in 0.3857 seconds including coupon evaluation.
Those replay timings do not establish a production speedup. The live endpoint
was not replaced or retested against deployed new code because deployment is
outside this task. Functional after-evidence is the same real input returning
complete diagnostics with unchanged quote/coupon content.

Reproduce the replay against a disposable database:

Export the single JSON column produced by `scripts/export_coupon_snapshot.sql`
using a read-only production connection; the query is one consistent transaction
snapshot. The export deliberately excludes Telegram configuration and deliveries.

```text
python scripts/audit_coupon_snapshot.py <read-only-export.json> --output <audit.json>
```

With `TEST_POSTGRES_URL` it creates and removes a temporary test schema; without
that variable it uses temporary SQLite. Production exports are read-only; replay
must use a disposable test database. No Telegram method is called by replay.

## Daily coupon and UI behavior

The automatic coupon is generated on opening Kuponum and in the existing Telegram
slots. It defaults to balanced ranking and at most four different fixtures on the
current Istanbul date. Combined odds and individual eligible prices have **no upper
cap**. Probability >= .70, reliability >= .80, quality >= .80, exact fixture/market/
period/line, supported bookmaker and <=900-second price age remain mandatory.
The old `feat/auto-coupon-max-five` branch is not merged.

The automatic selections stay visible while a separate manual draft is edited or
restored. Candidate rows are hidden under editing by default. Empty daily coupons
show the reason, actual analysis update, price observation and check time; missing
observations stay explicitly unknown. Stale observations retain their original
time rather than being relabeled as a fresh update. The homepage requests
`priced_only=true`, excluding unpriced, stale-analysis and weak recommendations.
Model-only views remain available in the prediction pages with explicit labels.

Verification is independent of generation and the delivery ledger. The existing
12:00/18:00 Istanbul windows, maximum two automatic attempts, one durable claim per
slot/version and no blind retry after uncertain delivery remain unchanged. Checking
permissions does not send, claim, repair or reset a previously failed delivery.

## Unsupported source markets and actionable setup

Verified source adapters cover result, full-time goals, Asian handicap, first-half
goals and corners. No newly verified price endpoint was found for second-half
goals, yellow/red/combined cards, BTTS or double chance. A statistical estimate or
final match statistic is not a bookmaker quote. Full-time prices cannot be reused
as second-half prices, and a different total line cannot price a model selection.
These markets remain unpriced rather than receiving generated prices.

For a reviewed release installed by the operator:

1. Configure `TELEGRAM_BOT_TOKEN` and a negative numeric `TELEGRAM_CHAT_ID` only on
   the existing Render service. Add `@BKupon8Bot` to the intended group and permit
   text and document messages. Keep the bot credentials out of the browser and DB.
2. Open Telegram management in Kuponum. Enter the **server management key**, not
   the bot token, and use **Bağlantıyı Doğrula · Mesaj Göndermez**. Missing identity,
   membership, permissions, unknown permissions and provider errors produce safe
   help. The control does not invoke `sendMessage` or `sendDocument`.
3. An operator with server-shell access can instead execute
   `python -m src.jobs.telegram_verification`. It prints only the safe result;
   this shell process does not populate another web process's cache.
4. Correct the server configuration/permissions and repeat after cache expiry.
   Do not reset the failed noon ledger or blindly retry an ambiguous delivery.
5. Keep quote collection and board refresh active. Fix source-line coverage only
   by collecting an actually observed exact line, not by substituting model data.

### External limitations retained

The agent environment contained no Telegram credentials. Read-only Render SSH
access was attempted and rejected with `Permission denied (publickey)`. Therefore
**real getMe/getChat/getChatMember responses and production posting permission
could not be verified here**. The implementation and UI flow are tested end to end
with MockTransport; configured production settings are not asserted to prove bot
identity or permission. The operator must perform the safe check after installing
the reviewed release or from an authorized server shell.

There was no real Telegram test message, credential change, provider-price write,
production ledger reset, master merge or Render deployment. Current live coverage
and the old generic 400 remain external production facts until an operator acts.
CI validates Python, real PostgreSQL, Chromium, exact-line/provenance regressions,
the read-only connection workflow and JavaScript runtime behavior. Final counts
and the verified CI run are recorded in the pull request.
