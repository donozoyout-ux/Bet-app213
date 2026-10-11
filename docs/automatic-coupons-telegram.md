# Automatic coupons and Telegram groups

The existing prediction quality gates and maximum of two strong recommendations
per fixture remain in effect. Automatic generation consumes fresh precomputed
league boards, refreshes the exact database quotes in batches, and chooses at most
one qualified selection per upcoming fixture. There is no fixed fixture-count cap.
The source horizon is the existing seven-day prediction board and enabled leagues.
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
draft cap is removed. Each match still contributes at most one coupon selection.
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

Synthetic histories through real SQLite/PostgreSQL tables exercise unlimited
fixtures, stale/weak rejection, immutable edits, both time windows, daily limits,
simultaneous claims, new database connections after restart, interrupted sends,
HTTP failures and secret redaction. All Telegram tests inject MockTransport;
test fixtures mask inherited real credentials, including subprocess environments.
Chromium and JavaScript runtime tests exercise automatic generation, editing,
saving/history and source/copy/error controls. No live group send or deployment
is part of implementation validation.

## Daily shortlist and controls

The default automatic coupon is **balanced, up to four different matches**, for the current `Europe/Istanbul` calendar date. It never pads weak or unpriced selections to reach the target. Candidates remain subject to the existing prediction quality and verified current-price checks. Each fixture contributes at most one selection. The UI offers optional targets of 2, 3, 4, 5, 6, 8 or 10 matches and balanced/cautious/value ranking. The daily filter runs before creating the immutable saved automatic coupon. Manual additions and removals are unaffected. Telegram's 12:00 and 18:00 scheduler also uses the default daily four-match balanced shortlist and retains durable at-most-once sending and 15-minute odds freshness validation.
