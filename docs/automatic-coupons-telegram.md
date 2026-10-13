# Automatic coupons and Telegram groups

The existing prediction quality gates and maximum of two strong recommendations
per fixture remain in effect. Automatic generation consumes fresh precomputed
league boards, refreshes the exact database quotes in batches, and chooses at most
one qualified selection per upcoming fixture, up to four different fixtures on
the current Istanbul calendar date. Manual editing uses the existing seven-day
prediction board and enabled leagues.
Only Crown, Bet365 and Sbobet quotes with the exact fixture/market/period/line and
a verified unexpired price qualify. Empty results are not saved or sent.

Products use decimal arithmetic; extremely large products use scientific notation
instead of JSON Infinity. Mixed bookmakers are explicitly a draft product rather
than a price offered by one betting operator. No bets are placed or paid, and no
success or profit guarantee is made.

## Saved versions and editing

`saved_coupons` stores immutable versions and their observed prices. Automatic
versions are canonical and reused only when the meaningful selection/model/price
and observation content is unchanged. User edits create separate manual versions;
they never mutate or disable automatic generation. An unchanged save reuses the
existing version. Concurrent edits of a superseded version return HTTP 409.

The UI can generate, add/remove suitable selections, save, load paginated history,
copy and view settlement from stored scores/final statistics. The earlier two-match
manual draft cap is removed. The daily automatic shortlist has a four-match cap.
Each match still contributes at most one coupon selection.
Browser-owned manual history uses a cryptographically random browser key, stored
locally; only its hash is kept server-side. This is anonymous browser ownership,
not a cross-device login account. Clearing browser storage loses access to that
private history, without deleting PostgreSQL records. Automatic history is public;
editing it creates a browser-owned fork.

Saved prices are historical observations. Results are calculated from real match
records; missing final fields remain awaiting-data, including unknown red cards.
Past coupons cannot be rewritten using retrospectively invented predictions.

## Telegram configuration and access

Set **only on the existing Render service**:

- `TELEGRAM_BOT_TOKEN`: the Bot API token.
- `TELEGRAM_CHAT_ID`: a negative numeric group/supergroup chat ID.

Add the bot to the group and permit it to post text and documents. The application
continues functioning if settings are absent or invalid. `/api/telegram/status`
returns configuration presence, schedule and safe delivery states; no token or
chat ID. Configuration presence does not prove group permission.

Manual group sharing requires the existing `SCRAPER_API_TOKEN` Bearer authorization
and browser ownership for private coupons. The UI keeps that management key only
in the password field, never in localStorage. The Telegram credentials never enter
the browser. HTTP logging redacts bot-token URLs; provider response descriptions
and transport exception text are not stored or returned.

## Scheduling, durability and cost

The existing scraper subprocess runs a lightweight independent Telegram coroutine.
No new Render service, paid cron or external queue is created. Outside the due
windows there is no prediction scan. The loop checks every 60 seconds; a committed
window requires only a ledger lookup. Empty results back off for five minutes.

Europe/Istanbul windows are 12:00–18:00 and 18:00–local midnight. Startup catches
the current window, with newly verified prices. It does not replay an expired
morning window in the evening or yesterday's window. The status view identifies
an entirely missed morning window. Target send times are 12:00 and 18:00; catch-up
is deliberately visible through actual creation/observation/delivery timestamps.

`.github/workflows/telegram-coupons.yml` wakes the web service at 09:00/15:00 UTC
(12:00/18:00 Istanbul) and calls the authenticated durable tick endpoint. Configure
repository secret `SCRAPER_API_TOKEN` to match Render, and optional variable
`BETAPP_URL`. **Do not put Telegram credentials in GitHub secrets.** The workflow
has no blind POST retry; repeated executions still obey PostgreSQL claims.

Free Render sleep/cold starts, database outages, source coverage and GitHub schedule
delays mean exact-time or guaranteed 24/7 delivery cannot be promised. The worker
cannot run while its instance is asleep; the optional wake workflow reduces this
dependency but also requires correct repository configuration.

## Delivery semantics

`telegram_deliveries` uniquely identifies each coupon version and each automatic
date/window. PostgreSQL advisory transaction locks serialize short reservations
across schedulers, web calls and workers. No database transaction is held during
Telegram HTTP. At most two automatic network attempts can be reserved per local
day; explicit authorized manual sends are separate, but cannot repeat the same
coupon version.

The `sending` claim commits before network I/O. Success stores `sent` and message
ID; explicit 4xx rejection stores `failed`. Timeout, 5xx, malformed success or
an interrupted sender produce `uncertain` (or a recoverable precommitted `sending`
record that becomes uncertain). These records never automatically retry. A crash
after claiming but before transmitting can therefore lose a delivery. This is
at-most-once attempt semantics, not exactly-once delivery. Inspect the group before
any deliberate replacement after ambiguity. Known rejected attempts also require
operator attention rather than an unbounded retry loop.

Long coupons use one UTF-8 document with a summary caption, preserving every match
instead of splitting into extra automatic messages. See the official
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage) and
[Render free service limitations](https://render.com/docs/free#free-web-services).

## Validation

Synthetic histories through real SQLite/PostgreSQL tables exercise the daily
four-match cap, stale/weak rejection, immutable edits, both time windows, daily limits,
simultaneous claims, new database connections after restart, interrupted sends,
HTTP failures and secret redaction. All Telegram tests inject MockTransport;
test fixtures mask inherited real credentials, including subprocess environments.
Chromium and JavaScript runtime tests exercise automatic generation, editing,
saving/history and source/copy/error controls. No live group send or deployment
is part of implementation validation.

## Daily shortlist and controls

The default automatic coupon is **balanced, up to four different matches**, for the current `Europe/Istanbul` calendar date. It never pads weak or unpriced selections to reach the target. Candidates require .70 probability, .80 reliability, .80 quality and exact fresh prices. Each fixture contributes at most one selection. The UI offers optional targets of 2, 3 or 4 matches and balanced/cautious/value ranking. The daily filter runs before creating the immutable saved automatic coupon. Manual additions and removals are unaffected. Telegram's 12:00 and 18:00 scheduler also uses the default daily four-match balanced shortlist and retains durable at-most-once sending and 15-minute odds freshness validation.

The browser stores manual draft edits across reloads. The candidate list offers
both independent recommendations per fixture; missing/unverified prices are
hidden unless explicitly requested. Fresh priced alternatives are preferred
without lowering evidence thresholds. Empty automatic responses include the
analysis/day/quality/price reason and `/api/odds-coverage` exposes measured market
coverage. The worker refreshes relevant upcoming quotes separately from historical
backfill and backs off failures by fixture/market in PostgreSQL.

Refreshing a quote can create a new saved version, but does not authorize sending
the same match/selection set twice on the same local day. This duplicate check
also covers uncertain deliveries. The ledger still commits before network I/O.
See `critical-repair-audit.md` for measured coverage and saved-prediction metrics.


## Issue 46: uncapped totals and separate manual mode

Both coupon modes now enforce probability >= .70, reliability >= .80 and score
>= .80, in addition to the existing qualified-model, exact market/period/line and
15-minute observation checks. There is no maximum selection price or combined
coupon price. The four-fixture limit applies only to the daily automatic shortlist.
Both verified alternatives per fixture participate in strategy ranking; one wins
per fixture. Strategy/target are part of the automatic version identity, preventing
one control setting from returning another setting's stored metadata.

Kuponum loads the daily automatic result before the optional manual candidate
request. A dedicated daily panel stays visible while a separate manual draft is
edited, saved or restored. Returning to automatic mode preserves the manual draft;
opening manual mode restores it. Candidate-list failure does not hide a successful
daily coupon. Empty daily results explain the quality, date, analysis or price
failure. The home API prioritizes freshly priced recommendations and fixtures
before its page limit; stale prices are never displayed as current offers.

Telegram status distinguishes missing and invalid configuration without exposing
credentials. Configuration presence does not verify bot identity or group access.
The UI translates rejection, uncertainty, missed windows and duplicate prevention
into Turkish. Malformed JSON success responses remain uncertain and never trigger
a blind retry. The 12:00/18:00 Istanbul schedule, daily two-attempt limit and durable
claims remain unchanged. Tests use injected MockTransport only.

Read-only production observation, 13 October 2026 at 19:10 Europe/Istanbul:
`/api/predictions/best?date=2026-10-13&limit=100&market=all` returned six daily
recommendations above the strict quality floor, with one qualifying fresh price
and no stale league boards. Three recommendations had mismatched source lines;
two had unavailable source markets. At that observation a one-match daily coupon
was possible; four selections could not be justified. Coverage is time-dependent.
The aggregate `/api/odds-coverage` request returned HTTP 504 during the audit, so
these counts come from the date-filtered prediction response rather than invented
coverage totals.

Read-only `/api/telegram/status` showed configured settings, a rejected noon
attempt (`telegram_400`) and evening duplicate-selection suppression. Existing
configuration is not proof that @BKupon8Bot can post to the intended group. The
operator must inspect bot/group configuration and permission; this task neither
sent a real test message nor reset the ledger, changed secrets or deployed.
