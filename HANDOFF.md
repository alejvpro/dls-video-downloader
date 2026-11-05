# DLS video downloader handoff

## Current baseline — November 3, 2026

The downloader is operating through cron on the Apple Silicon Mac mini (macOS Ventura 13.2.1). Read-only status reports **eight verified acquisitions**, 417 video identities, 694 metadata observations and four persistent manual decisions. These counts are a snapshot, not fixed expectations. Acquired content covers the October 26–30 and November 2–3 daily shows, plus October 30's The Club. The latest inspected scheduled run, November 3 at 2 p.m. Pacific, succeeded; no forward downloads or retry records are pending.

Existing checkpoints:

- `28662d696636c12c2b1f9bb95ca13fd84d39ece0`: initial inventory and verified acquisition workflow.
- `139ca8dbf08d81647039a31b023fdbf8c5b4ef39`: unattended-operation safeguards.
- `69ecd6ca0804872f897f7cafdfb1c29ce9c12bbc`: acquisition naming and active cron documentation.

There is no Git remote. The offline suite currently contains 226 tests, including 30 comment-store, 25 manual-collector and 89 refresh-policy tests using synthetic data, disposable stores and tiny local subprocesses.

## Architecture and operations

Python 3.10+ CLI, using the standard library, with SQLite schema version 3 at `state/inventory.sqlite3`. See [README](README.md) for usage and schema, [RUNNER](RUNNER.md) for operational details, and [runtime documentation](NODE-RUNTIME.md) for Node configuration and rollback.

- `inventory.py` preserves video identities, source provenance, partial/full metadata observations, initial/latest observed titles, classification history and manual decisions. Missing metadata does not erase known values. Source publication/release dates are separate from discovery and observation times; title dates are not authoritative.
- `classification.py` produces **Download / Skip / Review** selection. Internal Download types are daily show and The Club. Persistent ID-based approvals/rejections/skips take precedence; clearing a decision restores automatic selection while retaining history. Four seeded decisions remain intact.
- `discovery.py` scans bounded recent results from the official channel's Live/streams and Videos tabs. Defaults: 25 Live, 75 Videos, eight full enrichments, two acquisition attempts per run. Daily shows and The Club receive enrichment priority; coverage gaps are reported.
- Automatic acquisition requires selected content, verified official-channel identity, a verified date on or after **October 26, 2026**, and completed/ready media. Unknown-date, pre-window, Skip and unresolved Review items are excluded. Historical manual approvals remain preserved but cannot bypass the scheduled forward window; explicit manual acquisition can handle approved exceptions.
- `acquisition.py` requires format **398 (1280×720 AV1, 30 fps) + 251 (Opus)**, merges to MKV with FFmpeg stream copying, and refuses silent fallback or transcoding. ffprobe verification, duration checks, file size and SHA-256 precede completion. Manifests and SQLite receipts identify verified results; duplicates are protected.
- `operations.py` maintains persistent retry delays, bounded logs and safe staging retention. Default retries begin at one hour for transient failures, six hours for format/storage failures, and 24 hours for unavailable/verification failures, with exponential delay capped at 24 hours. Failures consume bounded attempt slots; other items can proceed.
- A shared process lock prevents overlapping discovery/acquisition/cleanup. Subprocess timeouts and inherited lock ownership protect interrupted operations. Retries currently start fresh, not from partial files. Cleanup requires established ownership/age, protects verified media and referenced evidence, and defaults to 72-hour stale retention with bounded removals. Inspect `cleanup` without `--execute` first.

Acquisitions intentionally live at `state/acquisitions/<YouTubeID>/media.mkv`, with `acquisition.json` and attempt evidence. **Do not rename these paths for human readability or title changes.** See the [naming contract](ACQUISITION-NAMING.md). This is an internal verified store; future archive presentation belongs to the Archiver.

Cron is installed exactly once, at **11 a.m., 2 p.m., 6 p.m. and 9 p.m. America/Los_Angeles daily**:

```cron
0 11,14,18,21 * * * cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 2 --quiet
```

The runner uses explicit `/usr/local/bin/yt-dlp-new`, `/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node`, `/opt/homebrew/bin/ffmpeg` and `/opt/homebrew/bin/ffprobe`; it does not rely on interactive nvm or shell initialization. Preserve the old `/usr/local/bin/node` and unrelated scheduled jobs. System sleep was disabled on AC at activation; the Mac must remain awake, powered and connected. Later invocations catch up through bounded discovery and known eligible backlog.

From the project directory, inspect without triggering network access:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader status
/opt/homebrew/bin/python3 -B -m dls_video_downloader status --json
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -v
```

Inspect detailed run records under `state/runs/` and fatal diagnostics there or in `state/runner-fatal.log`. Quiet mode retains errors. Exit codes: 0 success, 2 partial failure/usage, 1 fatal, 75 overlap, 130 interruption. Operational SQLite, acquisitions, manifests, staging, logs, snapshots and local research extracts are intentionally ignored by Git: they are large, mutable, potentially sensitive production data, not source fixtures. A code checkout alone does not restore them. The sensitive pre-activation crontab backup is outside the repository; use RUNNER.md for rollback without exposing its contents.

## Responsibilities

The downloader acquires media and preserves source observations without interpreting their content. Future `dls-indexer` work will interpret timestamp comments, identify chapters and connect video/audio episodes. `dls-archiver` owns long-term organization, integrity, backups and eventual descriptive archive names. Neither integration is implemented. Sibling repositories remain independent; do not modify them for downloader work.

## Next priorities — separate authorized phases

1. **Preserve YouTube comments.** The independent [manual collector, store and offline refresh policy](COMMENTS.md) support `comments collect VIDEO_ID` and pure deterministic `evaluate_refresh` decisions. The collector retains its 1,000-comment cap, newest-first sorting, zero retries, explicit runtimes, bounded pipes/timeouts and comment-only lock. Snapshots/schema remain version 1. Forward age uses original YouTube publication evidence. Until IrishWeegee is observed, collect initially, daily through day 7, then once during day 14. First positive observation instead schedules one +24-hour confirmation, then early completion; no text edits or later absence restart monitoring. All automatic work stops before day 15, including bounded recovery. Read-only planning and explicitly requested bounded execution are now implemented (see below); no unattended scheduling is installed. Historical comment backfill remains a separate future workflow; do not weaken the cutoff or parse comments into chapters.
2. **Local admin interface.** Build a localhost-only browser interface showing recent acquisitions with titles, verified publication dates, YouTube IDs, acquisition timestamps and verification status, plus failures, retries and pending work. Include ambiguous-video Review queues with Keep, Skip and Clear Decision; retain reversible decision history and support search, filtering and past-decision review. Eventually show comment collection status. Direct YouTube-hosted thumbnail URLs are acceptable; no local image cache is necessary. No interface exists yet.
3. **Historical backfill.** Work backward before October 26, 2026 toward the channel's earliest uploads around June 2021, using bounded date ranges. Reuse inventory, classifications and decisions; review uncertain historical material before downloading. Keep backfill independent from ongoing cron acquisition and never overwrite verified forward acquisitions. Historical publishing formats need evidence-based rules; no backfill workflow exists yet.

Deferred: comment interpretation/chapter generation (Indexer), long-term drive replication (Archiver), possible cron-to-launchd migration, possible private GitHub remote, and refined classification informed by accumulated decisions.

### Manual comment validation — November 4, 2026

After 137 offline tests passed, one authorized `comments collect 20jeos2MJLY`
invocation created the first operational comment collection: 121 comments,
69 top-level and 52 replies, with one IrishWeegee match and no warnings/errors.
The outcome correctly remains coverage-uncertain. Attempt
`3e94c28cf42f4d7880999117ec6970d0`, its portable record and a verified 70,560-byte
snapshot are under ignored `state/comments/`; see [validation details](COMMENTS.md).
Snapshot/catalog schema versions remain unchanged. No automatic refresh or
comment schedule has been activated.

### Offline refresh-policy phase

`comment_refresh.py` accepts explicit current time, original verified publication
timestamp/date (UTC midnight for date-only evidence), explicit first-observed
fallback only when publication evidence is unavailable, full supplied history,
target IDs and collector cooldown. It returns decisions without IO, clock reads,
collection or persistence. Existing catalog/snapshot schemas are unchanged.

Forward fallback: initial collection immediately before the cutoff (including
first discovery during the days 8–13 routine pause), daily through day 7, and one
final check in `[anchor +14 days, anchor +15 days)`. At/after day 15 no automatic
collection is due. Discovery/download/backfill dates never replace a known original
publication anchor, and missed intervals are not replayed.

The first collection observing IrishWeegee channel `UCbzm6UmzlMA-Yq6v3hvEipA`
schedules one confirmation 24 elapsed hours after collection completion (or its
collection time if finish time is unavailable), only if it fits before the cutoff.
This is a one-off exception to the routine pause. A confirmation can preserve
corrections but does not guarantee observing them. Multiple matching comments do
not create multiple schedules. After the confirmation opportunity succeeds, stop
routine collection; later edits, metadata changes, or absence never restart it.
A failed/partial confirmation permits at most one recovery with progressive
backoff, within the same original age window; then complete or suspend for review.
There is no long-term monitoring, text-edit budget or new confirmation cycle.

`monitoring_complete` means the policy opportunity was fulfilled, not exhaustive
comment retrieval or permanent unavailability. Uncertain successful retrieval can
complete confirmation. Presence/history remains preserved. Manual refresh stays
available at any age. The policy itself does not enforce locks, budgets or persistence. The bounded
runner described below now supplies that orchestration.

Historical comment backfill remains separate: a future explicitly authorized
workflow may collect an old video once, while forward automatic eligibility stays
suspended. Existing YouTube-ID history prevents duplicate initial collection;
backfilled videos inside the original age window can enter normal refresh policy.
No backfill flag bypasses the cutoff. No backfill command, queue, cron change,
unattended comment scheduling, admin interface or Archiver integration is implemented.

The existing November 4, 2 p.m. Pacific video cron metadata updates were retained
(`2026-11-04T220001.162208+0000-512ea43b`). Each policy refinement uses a fresh
preservation baseline and never reverts legitimate scheduled inventory changes.

## Development conventions

Make incremental, reviewable changes and implement one major feature at a time. Run offline tests, preserve production SQLite and downloaded media, and use explicit executable paths for unattended processes. Do not change cron or production acquisition behavior without authorization. Do not stage runtime data, credentials or extraction dumps. Commit at sensible checkpoints, then stop and report after each authorized phase. Start a new session by checking Git and read-only status; historical validation documents describe past snapshots, not necessarily current counts.

## Read-only comment planner phase

`comments plan [--json] [--details] [--now ISO]` now reads existing inventory and
comment catalog without network access or operational writes. Global `--db` and
local `--store-root` support disposable fixtures. Missing comment catalogs are not
initialized; invalid existing catalogs fail closed. No discovery, collection,
reclassification, media eligibility mutation or scheduler installation occurs.

`comment_planning.plan(..., now=...)` reuses inventory report/eligibility and the
committed pure refresh evaluator with full per-video comment history. Due IDs are
ordered by due time, initial work winning ties. Completed daily shows use retained
release/start evidence; normal Club uploads use availability/upload timestamps.
Source fields/provenance and date precision are exposed. Forward date boundaries
and the day-15 cutoff apply even to approvals; unknown publication evidence cannot
authorize forward collection. The first-observed fallback is visible but does not
bypass that selection restriction. No downloaded media is required.

Human output labels UTC and summarizes exclusions; JSON/details include them.
Confirmation completion remains distinct from retrieval completeness. See
COMMENTS.md for source precedence, timing evidence and CLI/API details. The bounded runner below now provides execution-time revalidation, bounded
budgets, independent logs/lock and global cooldown persistence, with the documented
nonblocking acquisition-probe limitation. Morning discovery and historical backfill
remain separate future work.

## Bounded comment runner milestone

`comments run` defaults to read-only dry-run; `--json` provides structured output.
`--execute` enables independent, sequential collection using the existing planner,
policy, collector and store. Defaults: two videos, 1,000 comments/video, one total
600-second invocation deadline. Limits: 1–10 videos, 1–1,000 comments, 30–600 seconds.
No discovery, reclassification, media existence requirement, video-state writes,
backfill or unattended scheduling was added. The planner is included in the same checkpoint.

The runner holds a separate comment-runner lock and revalidates under the existing
collector lock immediately before extraction through a small optional collector
preflight hook. Shared remaining-time/publication budgets reduce the collector
process timeout; insufficient time defers. An existing busy acquisition lock defers
comments nonblockingly; it is never held over network work. Acquisition can begin
after the probe: this conservative strategy does not guarantee strict exclusion,
and the video runner's locking semantics are unchanged.

`state/comments/runner/cooldown.json` retains sanitized blocking evidence and a
six-hour minimum global cooldown (extended by reliable Retry-After evidence).
`runner/last-run.json` is a bounded atomically replaced latest report; permanent
per-attempt history remains in the established catalog and portable records.
Malformed cooldown state or unexpected persistence errors fail closed. Ordinary
recorded failures allow the next selected video; blocking/interruption stops the
batch. Dry-run creates none of these files. See COMMENTS.md for commands, limits,
exit codes, timing evidence, lock limitations and report details.

Execution acceptance tests use only disposable stores and mocked extraction.
Production validation for this milestone is dry-run only; no additional live
YouTube collection is authorized or performed. Before one live runner test, review
this implementation and explicitly authorize the production comment-state writes.
Morning discovery, cron, captions, backfill, admin UI and Archiver integration
remain outside this milestone. This checkpoint includes no live runner validation.

Offline milestone verification: 38 new runner acceptance tests and all 293 total
tests pass. Read-only production dry-run selected the oldest two of nine due IDs
and deferred seven by budget. All 68 baseline inventory/acquisition/comment files
retained their SHA-256 hashes, no production runner tree was created, and the cron
hash was unchanged. No live extraction occurred. These are validation observations,
not fixed candidate counts for future invocations.
