# Private historical archive pilot — no-delete protocol

## Approved architecture
- Live application remains on existing Render PostgreSQL. Never substitute archive quotes for 900-second live odds.
- Historical partition: one league and one season of finished fixtures dated 2024+.
- Archive output: deterministic `.jsonl.gz` and `.manifest.json` with SHA256, match/quote counts; includes 1X2/AH/OU opening+closing and a subset of match statistics. This is a **pilot subset**, not a full DB backup (does not include all raw payloads, events, odds snapshots, saved predictions/coupons, or every statistic).
- Source transaction uses `REPEATABLE READ READ ONLY`; no production insert/update/delete.
- Restore into isolated temporary SQLite is a logical integrity demonstration only. A full, isolated PostgreSQL schema/restore parity test is required before any retention decision.

## Separate private repository prerequisites
1. Create a new **private** repository `donozoyout-ux/kuponanalizii-archive` using GitHub UI, with access limited to intended maintainers. The source app repository is public; **do not** attach archive artifacts/releases to it.
2. Review Goaloo source licensing/redistribution and GitHub terms. Private visibility is not a license to redistribute third-party data.
3. Use narrow-scoped token/installation with write access to Releases **only** for the private archive repository. Never print/store the token in app repo, logs, or release assets.
4. Keep the archive repo private and confirm visibility through GitHub API before **any** archive upload. If missing/wrong visibility, abort.
5. For first pilot, export locally on a controlled authenticated runner, verify checksums and isolated logical restore; do not automatically upload until review.

## Commands after configuring an isolated runner
```bash
python -m scripts.archive_readonly_pilot --league 6 --season 2025-2026 --output /tmp/kupon-archive/poland-2025-2026.jsonl.gz
python -m scripts.archive_readonly_pilot --league 7 --season 2025-2026 --output /tmp/kupon-archive/denmark-2025-2026.jsonl.gz
```

Make sure `DATABASE_URL` is the existing read-only PostgreSQL credential (a dedicated SELECT-only role is preferred). The script does not initialize a schema or upload files. Keep any generated files off publicly accessible build artifacts.

## Gates before Release upload
- Verify private destination, permissions, licensing, expected season/league and SHA256.
- Check sample export/restore parity and inspect missing/null odds and statistics as factual data gaps.
- Ensure credentials/personal data are absent from the archive.
- Encrypt at rest if required by data classification, and maintain an independent recovery copy.
- Never remove archive source rows from PostgreSQL in this pilot.

## Current scope
PR #99 contains code and unit tests only. No archive repo was created, no production export executed, and no real historical database restored yet. This document does not authorize destructive actions.
