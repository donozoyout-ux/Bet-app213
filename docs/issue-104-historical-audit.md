# Historical match and prediction accuracy audit — Issue 104

## Scope, evidence and safety

Read-only Render PostgreSQL audit on 18 October 2026, starting 19:47 UTC
(22:47 Europe/Istanbul). This is separate from speed PR #105, archive #101
and capacity #103. Base master e45307b. No runtime settlement logic, database
records, job checkpoints, deployment or infrastructure configuration changed.
No Goaloo scrape, historical import, Telegram or NVIDIA call was made.

Core prediction evidence was extracted in pages of 50 through approximately
19:52 UTC. Connector calls use separate read-only transactions: these observations
are **not one atomic database snapshot**. The running application added two
snapshots during collection (236 to 238); the final bundle contains 238 and 493
selections. The supplied CLI improves reproducibility with a single PostgreSQL
REPEATABLE READ READ ONLY transaction, 50-row pages and five-second query limits.

At 19:52:48 UTC: **14,796 matches; 238 snapshots; 227 settlement revisions;
238 coupons; zero queued/running jobs; 20 paused_disabled jobs**.
Application size 700,012,223 bytes; aggregate size 723,412,556 bytes.
Both exceed 700,000,000 bytes. No import or remediation was attempted.
Paused checkpoint digest: `0eb4fcf8d7868a3a754fa59f627071de`.

## Prediction outcomes — separate from source data quality

Using original captured selections and current finalized database evidence:

* 493 selections: **172 won, 54 lost, 1 void, 229 pending, 37 awaiting data**.
* Hit rate: **172 / 226 = 76.11%**. Pending, missing evidence and voids are excluded.
* Brier score: **0.18833**, n=226; mean predicted probability **81.15%** versus
  observed 76.11%. Nominal Wilson 95% interval: **70.14–81.20%**. Selections on
  the same match are correlated; this interval is not a cluster-adjusted guarantee.
* Only **2** resolved snapshot selections have usable captured, timestamped prices.
  Their one-unit simulation is -0.54 units / 2 = -27%; this sample is too small
  for profitability conclusions and is not realized betting profit.
* Earliest stored prediction: 2026-10-08 20:55:41 UTC. The database's 2024 match
  history does **not** establish a 2024 prediction track record.

The recorded ledger itself says **174 won, 52 lost, 1 void** (174/226=76.99%).
Ten ledger outcomes disagree with newer statistical evidence. This is a
comparison of two evidence versions, **not a before/after correction result**.
No ledger revision was inserted or rewritten.

League, market, combined league/market, month, confidence and bookmaker tables
are in [accuracy-summary.json](issue-104-accuracy-summary.json). Denominators,
missing/pending counts, Brier, nominal uncertainty and price sample counts are
included for each group. Empty groups have null rates, never invented zero wins.

## Disputed and unresolved records

All 227 stored decision hashes matched the original snapshot and frozen evidence.
Independent settlement arithmetic agreed with all frozen decisions. Ten current
evidence disagreements comprise five corners and five yellow-card selections:
six previously won now evaluate lost; four previously lost now evaluate won.
See [old/new evidence](issue-104-settlement-evidence.json) for scores, counts,
timestamps, revisions and hashes. This supports source-drift review, not a claim
that the original evaluator calculated its recorded evidence incorrectly.

No missing terminal ledger decision was found among currently decidable picks.
Thirty-seven prediction selections on finished matches await evidence: 25 lack
valid HT-dependent scores, 8 lack final corners and 4 lack final yellow-card data.
They remain unknown, not losses. The report retains all selection-level rows in
[prediction-audit.csv](issue-104-prediction-audit.csv), including timestamps,
captured probability/price, actual value, stored outcome and reason.

[Discrepancy plan](issue-104-discrepancy-plan.json) contains deterministic plan IDs
and evidence digests. Re-running unchanged evidence produces identical proposals.
These are review suggestions only. Source verification and an explicitly approved
append-only correction PR are prerequisites to applying a correction. The script
has no apply mode and never calls the application's settlement writer.

## Coupons: avoid counting versions as independent bets

238 immutable coupon versions contain 363 legs but only **16 distinct matches
and 19 distinct match/market/selection combinations**. Version-weighted outcomes
are 214 won, 73 lost, 42 awaiting data and 34 pending: 214/287=74.56%.
This is a descriptive version-weighted rate, **not 287 independent wagers**.
One-unit price simulation is +93.72/287=32.66%; no actual stakes or wager placement
exist in this evidence, and repeated versions make this unsuitable as a claimed
betting return. Original coupon IDs, root IDs and ownership hashes are excluded
from [coupon-audit.csv](issue-104-coupon-audit.csv); row keys use opaque hashes.

Coupon payload `status=pending` is an immutable capture-time field. The existing
coupon-detail reader derives current outcome from match evidence. Its difference
from a final result is **not** treated as a stale persisted settlement requiring
an overwrite. Original coupon records must remain intact.

## Historical match data quality

The initial inventory covers **11,483 finished matches from 2024 onward**:
0 missing FT, 32 missing HT, 0 negative/inconsistent HT/FT pairs; 11,200 with
final non-null corner counts and only **1,953 with all four final yellow/red
card fields**. Missing red cards must not be assumed zero. Source quality and
prediction success use different denominators and are reported separately.

[League/season/date inventory](issue-104-history-inventory.json) shows actual
first/last dates; no league is claimed complete against an external schedule.
For example, several new leagues still have only a 25-match 2024 sample,
Switzerland/Greece 35, and EPL coverage starts August 2024 rather than January.

The later identity query saw 11,484 finished matches as live processing continued.
All 11,484 had stored Goaloo schedule IDs, league IDs and home/away external IDs
matching relational fields. Zero duplicated Goaloo IDs or exact
league/home/away/kickoff keys were found. This is stored-source consistency,
**not independent live Goaloo verification**.

33 latest stored schedule score records differ from the relational live result;
[row evidence](issue-104-score-evidence-differences.json) exposes blank/older
schedule scores and live-observation timestamps. A schedule/live lag can explain
these differences; do not overwrite live results from the older schedule.
Source kickoff correctness, authoritative final card counts and external fixture
completeness remain independently unverified.

Across Crown/Bet365/Sbobet 1X2/AH/OU tables: 97,609 historical price rows,
97,330 with all opening/closing prices >1 and required lines present; 279 partial
rows. [539 league/season/bookmaker/market groups](issue-104-history-odds.json)
separate row presence from field completeness. Missing entire rows are not part
of that row denominator. Observation-metadata presence does not prove timely
historical observation, price accuracy or source availability. No price was filled.

## Safe priorities

1. Review the ten changed count-evidence decisions against independent final
   source evidence; retain both original and reviewed revisions.
2. Resolve missing HT/count evidence only after capacity and separately approved
   collection policy permit it. Do not convert unknowns to losses or zero cards.
3. Keep the 2024 fixture inventory separate from the October 2026 prediction
   track record; show denominators and incomplete seasons in future reporting.
4. Do not score immutable coupon `pending` fields as an operational backlog.

## Reproduction and tests

```sh
# AUDIT_DATABASE_URL supplied securely through the environment (never CLI/logs).
python scripts/audit_history.py --output private-audit.json
# Offline replay of collect() output, with no database access:
python scripts/audit_history.py --input private-bundle.json --output replay.json
python -m pytest tests/test_history_audit.py -q
```

The report contains no source credentials or owner identifiers; review any new
local evidence before publication. JSON numeric lexemes are preserved using
`picks::text` and `evidence::text`, because converting 1.0 to 1 changes historical
SHA-256 even though the numeric values compare equal.

Tests cover score/count unknowns, pushes, quarter-line half wins/losses, malformed
selections, stale/future prices, original evidence integrity, data drift,
prematch denominators, coupon semantics, deterministic plans and no input mutation.
A real PostgreSQL CI fixture checks read-only enforcement and unchanged rows;
its intentionally rejected DELETE targets only an isolated synthetic test schema.
Production is never used for write tests. CI links/results are recorded on the PR.

Author review: production modules are unchanged; audit modules have no writer,
scraper or model dependencies. Source rates are explicitly conditional on stored
evidence, unknowns are excluded, plan IDs are stable and coupon identities redacted.
No merge/deploy is included in this task.

## League and market tables (current database evidence)

| league | Won / decided | Hit rate | Missing evidence | Pending |
|---|---:|---:|---:|---:|
| Albania Kategoria e Pare | 0 / 0 | unknown | 0 | 6 |
| Albania Super league | 0 / 1 | 0.00% | 3 | 9 |
| Belgian Pro League | 0 / 0 | unknown | 0 | 16 |
| Denmark Superliga | 0 / 0 | unknown | 8 | 12 |
| English Premier League | 22 / 30 | 73.33% | 6 | 16 |
| France Ligue 1 | 20 / 23 | 86.96% | 3 | 16 |
| German Bundesliga | 22 / 25 | 88.00% | 2 | 12 |
| Holland Eredivisie | 20 / 25 | 80.00% | 1 | 18 |
| Italy Serie A | 14 / 24 | 58.33% | 8 | 24 |
| Liga Portugal 1 | 10 / 14 | 71.43% | 0 | 14 |
| Poland Ekstraklasa | 2 / 3 | 66.67% | 3 | 20 |
| Scottish Premiership | 10 / 11 | 90.91% | 1 | 12 |
| Spanish La Liga | 17 / 26 | 65.38% | 0 | 16 |
| Turkey Super Lig | 18 / 22 | 81.82% | 2 | 8 |
| UEFA Champions League | 8 / 12 | 66.67% | 0 | 20 |
| UEFA Conference League | 4 / 4 | 100.00% | 0 | 6 |
| UEFA Europa League | 5 / 6 | 83.33% | 0 | 4 |

| market | Won / decided | Hit rate | Missing evidence | Pending |
|---|---:|---:|---:|---:|
| asian_handicap | 1 / 2 | 50.00% | 0 | 0 |
| corners | 31 / 41 | 75.61% | 8 | 79 |
| double_chance | 26 / 36 | 72.22% | 0 | 0 |
| first_half_goals | 13 / 16 | 81.25% | 10 | 40 |
| goals | 51 / 64 | 79.69% | 0 | 15 |
| result | 11 / 15 | 73.33% | 0 | 3 |
| second_half_goals | 18 / 25 | 72.00% | 15 | 54 |
| yellow_cards | 21 / 27 | 77.78% | 4 | 38 |

Small denominators (especially n=1, 2, 3, 4 or 6) cannot establish league/market quality. These are recorded selections, not every historical match.

## Ten evidence disagreements

| Match:pick | Market / selection | Frozen actual | Current actual | Stored -> current |
|---|---|---:|---:|---|
| 821:0 | yellow_cards / under_6_5 | 2 | 7 | won -> lost |
| 821:2 | corners / under_11_5 | 2 | 12 | won -> lost |
| 823:0 | yellow_cards / under_5_5 | 3 | 7 | won -> lost |
| 3102:0 | corners / under_11_5 | 11 | 12 | won -> lost |
| 3103:2 | yellow_cards / under_6_5 | 6 | 7 | won -> lost |
| 3104:2 | yellow_cards / under_4_5 | 2 | 5 | won -> lost |
| 5971:0 | corners / over_7_5 | 5 | 19 | lost -> won |
| 9494:1 | corners / over_7_5 | 5 | 8 | lost -> won |
| 9496:0 | corners / over_7_5 | 1 | 8 | lost -> won |
| 11528:0 | yellow_cards / over_2_5 | 2 | 4 | lost -> won |
