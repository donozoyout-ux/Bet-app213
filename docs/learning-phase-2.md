# Phase 2: versioned statistical learning

Base: master `926436e` (PR #64). Branch: `codex/learning-phase-2`.

## Data and publication contract

The engine consumes `resolved_predictions`, never recreates historical predictions, and reuses the phase-1 snapshot/evidence hashes and evaluator replay. Only original prematch snapshots with recorded terminal decisions are read. Invalid original capture times are excluded. A corrupt hash, unknown evaluator, or decision that does not replay from its evidence fails the refresh closed; an incomplete report is never published. Void, pending and missing/nonfinite/out-of-range probability values are excluded from the scoring denominator. Exact 0 and 1 probabilities are valid; see the numerical log-loss convention below.

Every refresh re-reads the bounded source from the beginning. This is deliberate: the phase-1 cursor follows first settlement IDs, so merely resuming a previous cursor would miss a correction to an already-consumed prediction. Current revisions, settlement IDs, immutable snapshots, kickoff times and coverage form a SHA-256 data fingerprint. PostgreSQL reads use a single repeatable-read, read-only transaction; no result changes midway through an edition.

`LearningReport`, `LearningSlice` and `LearningLesson` are append-only, with database UPDATE/DELETE guards. `(method_version, data_fingerprint)` uniquely identifies an edition. The report, all breakdowns, all lesson observations and the publication pointer commit atomically. Repeated or concurrent work does not duplicate reports or lessons. Corrections produce a new complete edition; an older edition is still reproducible as its original as-of view, rather than being silently rewritten. Its compact, deferred `source_manifest` records exact settlement IDs/revisions, snapshot/evidence hashes, league/model identifiers, frozen kickoff times and outcome availability times. This forensic manifest is not loaded or exposed by ordinary API requests.

`LearningHead` is the only mutable publication state. It points to the currently checked edition, including when the source returns to a previously seen fingerprint. Database source-observation timestamps prevent a slow older analysis from replacing a newer publication. Unchanged input updates the last-checked time without creating a new report. Lesson payloads retain their edition date, source fingerprint and calculation method.

The supervised runner starts a separate learning task after 30 seconds and checks every 15 minutes. It uses its own database pool and releases the read transaction before CPU work runs in a thread. Telegram retains its existing task, pool, dispatch code and 12:00/18:00 schedule. No coupon, scraper, model probability or quality-threshold code is changed. NVIDIA is not connected.

## Statistics and uncertainty

For `n` eligible picks, `y=1` for won and `0` for lost:

- Success rate: `sum(y)/n`.
- Mean original model probability: `sum(p)/n`.
- Observed-minus-expected gap: `mean(y-p)`; expected wins: `sum(p)`.
- Binary Brier score: `mean((p-y)^2)`, range 0–1, lower is better.
- Natural-log loss: `-mean(y*ln(q)+(1-y)*ln(1-q))`, with `q=clip(p,1e-6,1-1e-6)`. Only log loss/logit numerics use this clip; original probabilities and the Brier score are unchanged.
- Ten fixed probability bins, including `p=1` in the last bin; empty bins are omitted. Each reports counts, mean probability, observed frequency, scores and uncertainty.
- Overall, league, market, model-version and league×market scopes. Groups use historical identifiers, including disabled leagues; small groups are not ranked as reliable winners.

Point scores describe selections. Inferential evidence counts match clusters instead of treating related selections as independent trials. If match `m` contributes `n_m` selections, its weight is `w_m=n_m/n` and `n_eff=1/sum(w_m^2)`. Arbitrary within-match dependence is allowed. Repeating the same match 300 times gives effective sample size 1, not 300.

The conservative two-sided Hoeffding radius for bounded success is `r=sqrt(log(2/alpha)/(2*n_eff))`. Success bounds are clipped to `[0,1]`. Gap bounds use `mean(y-p) ± 2r`, clipped to `[-1,1]`. Calibration bins use Bonferroni `alpha/10`. These are conservative concentration bounds, not narrow per-pick binomial or normal intervals.

The assumption is independence **between matches**. Repeated teams, changing seasons and temporal dependence may remain, so reported support is at most moderate and is not a causal finding or a guarantee. Neither Brier nor log loss alone isolates calibration from discrimination. Bounds and multiple-comparison control apply to the fixed edition, not an anytime guarantee across indefinitely repeated looks.

## Lesson observations

Every observation includes title, edition date, league/market/model scope where applicable, examined predictions and matches, expected/observed success, method/version, effective sample size, interval, source fingerprint, confidence and status.

- Below 100 distinct **or effective** matches: `insufficient_sample`, title `Örneklem yetersiz`, confidence low.
- Sufficient sample but no statistically supported gap: `inconclusive`, confidence low.
- Sufficient sample and simultaneous gap interval excludes zero: `supported_candidate`, confidence moderate. It is a candidate, not an automatically verified lesson or live model adjustment.

The tested family includes overall and every reported league/market/model/league×market scope. Each uses `alpha=.05/number_of_scopes` (Bonferroni), accounting for overlapping searches. These are policy gates fixed in method version `learning-v2-cluster-hoeffding-temporal`, not tuned to the current 120 outcomes. Changes to calculations, gates or optimizer require a new method version.

## Offline calibration research and acceptance

Two deliberately small candidates are implemented: intercept-only adjustment of logit(p), and a regularized sigmoid of logit(p) with slope/intercept. Fixed 200 gradient iterations, learning rate .2, L2 .01 around identity, bounded slope/intercept; each match has total training weight 1. Isotonic regression is documented as deferred because its flexible stepwise mapping needs a larger sample.

Calibration cohorts never mix model versions. Dates are sorted by UTC match kickoff and partitioned into the earliest 60%, next 20%, latest 20% of distinct dates; every match date and all selections from one match stay on one side. No random split occurs. A training outcome's latest recorded revision must already have existed before validation begins. Validation outcomes must already have existed before test begins. Late backfills or corrections are excluded from those earlier partitions rather than leaking future truth into them. Test outcomes are never used for fitting or candidate selection.

At least 500 distinct matches are required even to consider fitting, with at least 200 training, 100 validation and 100 test distinct/effective matches after the as-of filters. Training also needs at least 20 matches with each outcome class. Fitting is skipped otherwise.

Both candidates fit training only. A candidate must improve both scores on validation; validation Brier then a fixed name tie-break selects one. The selected frozen candidate is evaluated once on held-out test. Paired loss differences, clustered by match, use one-sided Hoeffding upper bounds. For Brier difference in `[-1,1]`, radius is `sqrt(2*log(1/alpha)/n_eff)`; log-loss difference uses this radius times `-ln(1e-6)`. Family alpha .05 is shared across two scores and all model-version cohorts. Both upper bounds must be negative. Otherwise the recommendation is rejected, even if the point estimate improves.

An accepted result is only `offline_candidate`, with parameters and held-out evidence recorded. There is no activation path, probability replacement, automatic threshold change or external model call.

Sources: [scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html) for scoring, sigmoid/isotonic methods and disjoint calibration data; [Hoeffding (1963), original paper](https://doi.org/10.1080/01621459.1963.10500830) for bounded independent-sum concentration. Match weighting, fixed gates, as-of restrictions and publication rules are explicit conservative implementation choices.

## Read-only API

All routes are GET-only under `/api/learning`:

- `/summary?report_id=...`: stored overall statistics and calibration bins.
- `/coverage?report_id=...`: saved/recorded/eligible/pending/excluded counts.
- `/statistics?kind=league|market|model|league_market|calibration&report_id=...&limit=20&offset=0`.
- `/lessons?report_id=...&limit=20&offset=0`.
- `/reports?limit=20&offset=0`: edition metadata.

Limits are 1–100 and offsets 0–100000, with typed kind/report validation and parameterized queries. A page returns its report ID and fingerprint: clients must pin that `report_id` for subsequent pages to keep one edition throughout pagination. Missing explicit IDs return 404; no materialized report returns `warming`. Metadata distinguishes original edition time from last checked time; `stale=true` after 30 minutes without a successful check. There are no public analysis, training, correction or publication controls. Handlers never call the learning engine.

## Bounded cost

Source verification uses pages of 500, batched settlement lookups and streamed snapshot coverage. Scoring/grouping is linear in eligible rows with fixed ten-bin summaries. Calibration has two fixed 200-iteration fits only after sample gates. Source reads/CPU/publication have a 120-second task budget. There is a deliberate safety cap of 50,000 snapshots/total saved selections/terminal records: above it refresh fails closed, keeps the previous complete edition and eventually reports stale metadata. It does not silently publish a partial or biased sample. Raising this operational cap or supporting incremental aggregation needs a separate scaling change.

Tests cover 20,000 picks / 10,000 match clusters within 20 seconds, plus 2,000 added hash-valid PostgreSQL settlements with fewer than 80 SQL statements and refresh within 20 seconds. API requests load only one small summary or one bounded page; forensic input manifests are deferred. Test timings are environment measurements, not production latency promises.

## Actual production evidence and example output

Read-only Render `betappv33` audit, 15 October 2026, approximately 19:50–19:59 Europe/Istanbul. This branch was not deployed. The starting 276 saved picks have grown to **280 picks / 131 snapshots**. There are still **121 persisted terminal results**, all passing original snapshot/evidence hashes and result replay. There are **92 won, 28 lost and 1 void**; **159 match-pending**, **0 result-data-pending**, **0 invalid capture timestamps** in the saved snapshot cohort.

The scoring denominator is **120** won/lost selections from **61 matches**, effective sample size **60.5042**:

- Success: **76.6667%**; mean model probability: **78.7940%**.
- Observed minus expected: **−2.1273 percentage points**; observed wins **92**, expected **94.5528**.
- Brier: **0.1813255**; natural-log loss: **0.5547625**.
- Conservative 95% match-cluster success bounds: **59.2069%–94.1265%**; the gap interval includes zero.

The 55 scope observations are all `insufficient_sample`. None are validated or supported learning claims. No calibrator was fitted: original model cohorts contain 51, 4 and 6 distinct resolved matches respectively (100, 8 and 12 selections). Those version names are historical labels preserved by phase 1, not retroactively relabeled engine versions.

Examples of market counts / observed rates: goals **52 / 82.69%**, double chance **33 / 69.70%**, result **14 / 71.43%**, corners **6 / 83.33%**, first half **4 / 75%**, second half **5 / 60%**, yellow cards **4 / 100%**, handicap **2 / 50%**. These small-cell rates are descriptive, not reliable rankings. League IDs map to the existing leagues endpoint/database; all league/model/bin details are in the full JSON report.

Reproducible evidence files:

- `learning-phase-2-source.json`: full original snapshot JSON and latest decision evidence as JSON **text**, preserving floating-point representation needed for the existing hashes. No credentials or scraper raw pages are exported.
- `learning-phase-2-example.json`: full verified scores, all breakdowns/calibration bins, structured observations and calibration rejection reasons. It is stored compactly by scope for review.
- `learning-phase-2-export.sql`: the read-only export query; current snapshots/results were read in one statement.

Replay without any database or network access:

```powershell
python -m scripts.learning_report --input docs/learning-phase-2-source.json --output report.json
```

The same command without `--input` reads the configured existing database in a read-only transaction and outputs a report; it never initializes schema, publishes learning rows, scrapes or sends Telegram. The internal scheduled `refresh` is the separate materialization path.

## Validation and delivery

Validation on Python 3.12 / Windows with isolated PostgreSQL 16.4 and Chromium:

- Broad Python/PostgreSQL regression run: **446 passed / 3 skipped**, 449 collected at that run. Skips require POSIX Gunicorn.
- Final phase-2 + phase-1 memory + browser selection after all source changes: **49 passed**. This includes the six additional tests added after the broad run; the current tree collects 455 tests.
- Final complete PostgreSQL selection: **21 passed / 2 skipped** (POSIX Gunicorn).
- Explicit Chromium UI selection: **6 passed**.
- JavaScript runtimes: **40 scenarios passed** (dashboard 7, predictions 19, market 4, coupon 10); dashboard JavaScript syntax checks passed.
- compileall, git diff --check and independent verified source-report replay passed.
- Latest measured cost: 20,000 picks / 10,000 matches **2.094s**; 2,000 added PostgreSQL settlements **20 statements / 0.985s**.

These selections overlap and should not be summed as unique tests. Linux CI runs the final full tree, including Gunicorn checks. Coverage includes independent probability/scoring examples, clustered picks, invalid/void/missing outcomes, all scope gates, temporal ties and late corrections, accepted/rejected offline candidates, correction-triggered editions, repeated/concurrent publication, transactional failure, old-source publication prevention, manifest deferral, bounded pagination and large datasets. The real-data replay is a regression fixture and rejects altered evidence.

No master merge, Render deploy, production writes or real Telegram send is part of this work. The only production access was read-only SQL. Existing JavaScript/Chromium and Python/PostgreSQL regressions are run in addition to the phase-2 tests.


## PR #65 CI correction

The original Linux failure was a last-bit libm difference in nine log-loss fields in `summary`/`slices`, not a different source, fingerprint or outcome. For example, Windows emitted 0.7547635343647574 and Linux 0.7547635343647572. Version v2 evaluates log/exp/sqrt with a fixed 40-digit Decimal context and converts back to full-precision float; it uses fsum reductions and canonical observation/optimizer ordering. It does not round original probabilities or replace exact fixture equality with tolerances. A new method version keeps existing immutable v1 editions intact. The example was regenerated only from the unchanged verified export. All 55 insufficient-sample observations remain.

The tablet push-run failure occurred while the automatic-coupon ASGI route was still acquiring a database connection. The test asserted its four DOM rows after only the default 5s, although that endpoint has a 10s server budget. The other Linux run and local 768px replay passed. The navigation test now waits for its specific response, asserts HTTP 200 and four real selections, and still asserts four rendered rows, editing, mobile/desktop navigation and no overflow. A separate tablet regression deliberately delays that response beyond 5s. UI CSS, coupon selection rules, probabilities and Telegram code are unchanged. The automatic response is consumed before its rendered rows are asserted.

Exact subprocess replay covers PYTHONHASHSEED 0, 1, 7, 42 and random, shuffled snapshot/settlement/input orders, plus reordered fitted-calibration cohorts. The original audited-export equality assertion is retained unchanged. Linux Actions checks the same fixture and these subprocess replays.
