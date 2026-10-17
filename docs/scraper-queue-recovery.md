# Queue recovery audit, 17 October 2026

Read-only production inspection before changes found 26 queued/running jobs.
Twenty were legacy jobs for disabled leagues; they were already excluded from
worker selection. Austria job 11 was still labelled running: 251 completed,
266 queued, one interrupted running item and two parser issues (520 total).
Both issues are the old flat playoff tie shape now supported by the parser.
No error evidence or historical scores are removed by this repair.

Eredivisie job 2005 was genuinely progressing: the reported 474/523 had reached
503/523 and then 505/523 during inspection. PostgreSQL advisory lock 213002 was
held by the live worker. An old started_at is not evidence that a job is dead.
Job items had no timestamps; the additive checkpoint_at migration supplies a
durable observation of subsequent worker activity.

Albania 315/348 had zero matches because PR90 intentionally prevented empty
pilots from automatically starting unbounded history imports. Their queued
statistics jobs had no input matches. Albania second division's bookmaker
coverage is weak; no quality threshold or production promotion is relaxed.
An explicit Albania observation mode allows only IDs 315/348, maximum five
finished matches each, using their previously reviewed identity report and live
identity validation per season. This is not admission to the quality-approved
European archive scope or automatic prediction promotion.

## Recovery rules

Only the owner of advisory lock 213002 reconciles durable work. A competing
worker cannot reclaim an active owner's job. Interrupted running items become
queued; completed/failed items, source issues, discovery checkpoints, original
started_at and counters survive. Disabled work becomes paused_disabled and
resumes on re-enablement. Explicit europe_bounded jobs use another lock and
are never adopted by the ordinary worker. No jobs or match records are deleted.

Daily update jobs remain first. Other jobs rotate by checkpoint_at after at most
25 items or 180 seconds between items. A source request is not cancelled halfway
through its transaction. A long history job cannot keep all other history behind
it solely because its status says running. Existing match/domestic_history job
kinds remain supported.

History capacity checks run before discovery and before every item on PostgreSQL;
at 700,000,000 aggregate database bytes work becomes paused_capacity and requires
explicit review to resume. Updates retain their normal path. Supervised Europe
imports additionally check the live health endpoint every five matches and stop
between commits on a five-second timeout or HTTP error. No new storage or worker
service is provisioned.

Tests cover checkpoint/error preservation, disabled pause/resume, fair rotation,
daily priority, manual-import isolation, bounded slices, real PostgreSQL lock
contention, and stopping supervised imports on unhealthy API responses. Existing
prediction and coupon code is unchanged; no Telegram/NVIDIA calls are made by
the supervised workflow.
