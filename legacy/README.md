# Archived collectors

These modules and their old tests are retained for reference only. Production
does not import them. The assumed NowGoal API/Next.js schemas were not verified,
the Playwright code includes fake helper classes and invalid browser methods,
and old tests can accidentally make real network requests or delete local caches.
SofaScore is outside the current Goaloo-only collection scope.

The active test suite is `tests/`, selected by `pytest.ini`. Archived tests are
not evidence that Goaloo collection works. Do not add pandas, Prefect, Redis,
Playwright or Docker dependencies back to the production requirements to run
these historical modules. Master's `src/etl/nowgoal_clean.py` is retained here
under `legacy/etl/nowgoal_clean.py`, superseded by `src/scrapers/goaloo/`.
