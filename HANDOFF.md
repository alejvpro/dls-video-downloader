# DLS video downloader handoff

## Caption and YouTube safety Git checkpoint

Caption preservation and shared YouTube safety are captured together because
caption modules, controlled recovery, CLI, cycle integration and tests depend on
each other. Earlier milestone sections retain their historical implementation
and validation status. All six DLS cron commands remain paused; captions remain
opt-in. This checkpoint performs no live request, legacy-state import, recovery,
production migration, credential access, activation or push. Next operational
steps still require separate supervised authorization as described below.

## Shared YouTube safety milestone — offline, cron still paused

All six DLS cron command lines remain commented out; unrelated entries are
unchanged. The November 5 caption challenge and original cooldown/catalog/portable
record are preserved. No shared production state import, network request,
recovery, credential access, cron restoration, commit or push is performed here.

New `youtube_safety.py` and bounded `youtube_recovery.py` coordinate one local
state/event journal under ignored `state/youtube-safety/`. Both network subprocess
launchers and their workflow/planning entry points participate, including the
independent video runner. Bot verification defaults to 24h; explicit 429 honors
Retry-After or 1h/6h/24h backoff. Bare 403/timeouts/unavailable assets are not global
blocks. Legacy evidence is projected without rewriting or extending it. Expiry
requires explicit coordinated recovery, never one probe per routine job. Repeated
bot challenge during recovery requires manual review; no job is auto-resumed.

Read-only commands: `youtube-safety status --json`, `youtube-safety events --json`,
and existing planners. Explicit import/review/probe capabilities require separate
operational authorization; see [YOUTUBE-SAFETY.md](YOUTUBE-SAFETY.md). No notification
transport is implemented. Future Pushover may consume immutable event IDs and
alertable transitions; no notification credential file is accessed.

Selection, AV1/Opus acquisition, receipts, comment cutoff and caption confirmation
policy remain unchanged. Network work defers without clearing asset history.
Already admitted subprocesses are not destructively interrupted; admission/launch
and internal yt-dlp request races remain documented. Next: review checkpoint,
then separately authorize legacy import and one controlled recovery observation;
validate a bounded asset operation before considering any cron restoration.
Verification: 55 new shared-safety tests and all 524 offline tests pass with
network sockets and production yt-dlp/FFmpeg launches guarded; zero unexpected
launches. All 93 protected production files, three SQLite integrity checks and
paused crontab bytes match. No `state/youtube-safety/` directory was initialized.

## Caption LOGIN_REQUIRED diagnosis — offline follow-up

One authorized production `captions collect MbO7y_90zUI` attempt at November 5
12:25 PST was blocked in fresh metadata extraction, before caption payloads.
Failure attempt `069f9229f7774eb8b1252e4e32d672f7`, portable JSON, independent
catalog and shared cooldown remain unchanged. Cooldown's original expiry is
November 5 18:25:59 PST. Existing cron still does not enable captions.

Offline comparison found no demonstrated networking defect causing the challenge:
the earlier direct payload experiment reused existing metadata, while production
had to obtain a fresh listing. Same yt-dlp release/build and explicit Node path;
no forced client/cookies. Request volume, IP/session state and effective earlier
client behavior cannot be established causally from retained logs. See CAPTIONS.md.

Two small prospective failure-provenance corrections retain tool version before
raising on failed extraction and fill cooldown attempt ID at record allocation.
No authentication, extractor, acquisition/comment policy or production state
changes. Actual retained error diagnostics replay correctly with mocked metadata
execution and zero payload calls. All 469 offline tests pass, including two new
failure-provenance regressions, with network/production-tool guards reporting
zero unexpected launches. Next single live attempt and unattended rollout
remain separately supervised decisions; no cached-URL workaround is implemented.

## Caption preservation milestone — offline only

Original-English JSON3 and VTT preservation is implemented but uncommitted and
not activated. See [CAPTIONS.md](CAPTIONS.md). New independent modules provide an
exact-byte immutable store/portable schema-1 records, track identity filtering,
per-format recovery/revisions, pure retry planning and bounded execution. The
existing selection view is reused without coupling caption planning to comment
history. Full completed-status evidence is required; no MKV is needed.

`captions plan`, `captions run` and default `cycle` are read-only. Manual
`captions collect ID` and `captions run --execute` are live capabilities requiring
separate supervised authorization. The supplemental cycle requires both
`--execute --captions` to execute captions; the installed cron commands lack that
flag and keep existing behavior. No production caption retrieval, catalog,
source migration, cron changes, live requests or backfill occur in this milestone.

Completion observation anchors missing-caption thresholds 1h/6h/24h/3d/7d/14d/30d;
ordinary uploads prefer verified upload availability evidence. First complete
preservation anchors +24h/+7d confirmations. Missed checkpoints are not replayed;
revisions/manual refresh do not restart terminal monitoring. Technical errors use
1h/6h/24h backoff and review after five consecutive failures. Missing listings,
partial formats, technical failure and monitoring completion stay distinct.

Caption defaults: one video/180 seconds, 16 MiB per representation, at most four
English tracks and zero retries, within the unchanged 600-second shared cycle
budget. Comments retain priority. Shared blocking cooldowns and separate locks
apply; the existing acquisition-probe race remains documented. Portable JSON and
checksummed source files are readable without SQLite. No interpretation,
transcript generation, universal asset abstraction or Archiver integration exists.

Verification: 84 new caption tests and all 467 offline tests pass. A guarded full
suite also blocked network sockets and production external-tool launches without
unexpected attempts. Read-only production planning created no caption state; all
89 baseline-protected production files and installed cron bytes matched.

Next step requires Level 1 approval for one bounded live caption validation and
separate approval before adding the caption flag to unattended cycle commands.

## Production supplemental rollout — November 5, 2026

Unified supplemental cycles are committed and scheduled alongside the unchanged
11/14/18/21 Pacific video runner. Discovery-enabled cycles run Friday 08:35 and
Monday–Friday 09:25/10:15; inventory-only cycles run weekends 10:15 and daily 19:30.
The Mac is America/Los_Angeles; system-local cron follows PST/PDT without changing
other job environments. Exact installed lines, backup/disable instructions and
operational inspection are in RUNNER.md. Only one unified cycle command is used.

Defaults/explicit cron bounds: two comment videos, 1,000 comments each, shared 600s,
metadata cap 180s, Streams 10, Videos 0, emergency 20 full-metadata requests. Default
`cycle` is still read-only; live work requires `--execute`, with `--discover` for
metadata. Components preserve independent phases/assets and remain separate from
MKV acquisition. Existing classification/comment policy, 24h confirmation and
publication +15d hard cutoff are unchanged. No captions or backfill are implemented.

The corrected supervised live test refreshed November 5 `tNzL9c4FmXM` first and
observed full `was_live` completion, enriched all three pending forward IDs, kept
historical undated backlog out, and preserved 159 October 28 comments including two
IrishWeegee comments. Independent JSON checks passed; no media/decision changes.
All 383 offline tests pass. That was supervised evidence, not unattended evidence.

Rollout does not force another live cycle or wait for a scheduled firing. Inspect
new `state/supplemental/last-cycle.json` timestamps and both phase outcomes after
the first scheduled invocations, plus coverage, cooldowns and portable artifacts.
Reports are bounded/latest; original observations and comment history remain in
their established stores. Stdout goes to /dev/null, stderr to normal cron error
handling; stale reports require investigation. Known media-probe race and bounded
listing coverage remain. Earlier sections retain phase history, superseded by this
rollout summary and the current forward-enrichment contract in RUNNER.md.


## Historical media baseline — November 3, 2026

The downloader is operating through cron on the Apple Silicon Mac mini (macOS Ventura 13.2.1). Read-only status reports **eight verified acquisitions**, 417 video identities, 694 metadata observations and four persistent manual decisions. These counts are a snapshot, not fixed expectations. Acquired content covers the October 26–30 and November 2–3 daily shows, plus October 30's The Club. The latest inspected scheduled run, November 3 at 2 p.m. Pacific, succeeded; no forward downloads or retry records are pending.

Existing checkpoints:

- `28662d696636c12c2b1f9bb95ca13fd84d39ece0`: initial inventory and verified acquisition workflow.
- `139ca8dbf08d81647039a31b023fdbf8c5b4ef39`: unattended-operation safeguards.
- `69ecd6ca0804872f897f7cafdfb1c29ce9c12bbc`: acquisition naming and active cron documentation.

There is no Git remote. The current offline suite contains 383 tests using synthetic data, disposable stores and tiny local subprocesses. Earlier counts below describe historical milestones.

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

1. **Preserve YouTube comments.** The independent [manual collector, store and offline refresh policy](COMMENTS.md) support `comments collect VIDEO_ID` and pure deterministic `evaluate_refresh` decisions. The collector retains its 1,000-comment cap, newest-first sorting, zero retries, explicit runtimes, bounded pipes/timeouts and comment-only lock. Snapshots/schema remain version 1. Forward age uses original YouTube publication evidence. Until IrishWeegee is observed, collect initially, daily through day 7, then once during day 14. First positive observation instead schedules one +24-hour confirmation, then early completion; no text edits or later absence restart monitoring. All automatic work stops before day 15, including bounded recovery. Read-only planning and explicitly requested bounded execution are now implemented (see below); supplemental scheduling is installed as documented above. Historical comment backfill remains a separate future workflow; do not weaken the cutoff or parse comments into chapters.
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

## Morning metadata discovery milestone (pre-rollout history)

`discover-morning [--json]` is implemented and tested offline. It is an explicit
network/inventory-writing command; it has NOT been invoked on production.
Defaults: Streams head 5, Videos disabled, full enrichment 2, total budget 180s.
Configurable bounds: Streams 1–25, Videos 0–25, enrichments 0–4, runtime 30–180s.

Discovery now exposes shared metadata-command, channel-validation and candidate
priority helpers; regular video workflow behavior is preserved. The morning path
uses those helpers and the existing importer/classifier, omits flat live-status
claims, and verifies completion using full metadata. Unknown/contradictory status
is explicit; source timestamps and prior observations remain preserved. A completed
selected daily show becomes visible to the existing read-only comment planner
without an MKV. Club uploads remain uploads, and short Friday shows have no fixed
minimum duration. No comment or media acquisition is invoked.

The existing shared operation lock is acquired nonblockingly before opening the
inventory for writes. Known video cron firings have a runtime+60s pre-start guard
and 60s post-firing guard; the video runner and cron are unchanged. Morning holds
the shared lock for its bounded metadata pass. A manual acquisition started after
that lock is taken can still defer; stronger priority coordination is not added.
No cleanup, retry-state/frontier updates, decision seeding, overlap receipts or
regular run-history pruning occurs. Morning status checks are bounded independently
of old media enrichment retries. Blocking stops additional requests; no retries.

Latest bounded summaries/diagnostics live under `state/morning/` when the command
is eventually executed. Existing source observations remain in inventory. See
RUNNER.md for flags, logs, exit codes, timing assumptions and proposed schedules.
Do not run live discovery, write production inventory or install cron without
separate approval. Comment planning/execution and morning discovery remain separate.
Historical backfill, captions, admin UI and Archiver integration remain out of scope.

The supervised comment runner validation preceding this milestone preserved 140
comments for `MbO7y_90zUI`, including IrishWeegee; its next confirmation is eligible
November 6 at 07:22:31.604536 UTC (November 5, 11:22 PM Pacific). This milestone does
not collect it or change any comment artifacts. Earlier offline-only validation
counts above describe their respective historical checkpoints.

Morning milestone verification: 32 new offline tests and all 325 total tests pass,
including existing regular discovery/acquisition coverage. All 72 baseline protected
inventory/acquisition/comment files retain their hashes; cron is unchanged and no
production `state/morning/` directory was created. No live discovery, collection,
media download, staging or commit was performed. One supervised live metadata pass
still needs separate authorization and should validate that the small channel-head
and enrichment budgets reach the current show before scheduling is considered.

## Unified supplemental cycle milestone (pre-rollout history)

Preserves all earlier uncommitted morning-discovery work. `cycle` defaults to
read-only planning, `cycle --execute` performs inventory-only comment execution,
and `cycle --execute --discover` authorizes bounded metadata refresh followed by
independent due comments. `--discover` without execution is rejected. No live
cycle execution is authorized or performed during this milestone.

The thin orchestrator delegates to morning discovery, the comment planner/policy
and bounded comment runner. Reports retain separate named phase outcomes, budgets,
durations and artifact references; a transient discovery failure can coexist with
successful known-inventory comments. Blocking/cooldown or interruption stops later
network work. No phase rewrites asset histories, publication policy or selections.
Future captions can be another named phase; no caption implementation is present.

One 600-second default absolute deadline spans phases (discovery cap 180 seconds,
comments cap 600 seconds, reduced to remaining allowance). Existing per-phase
process limits and source-age cutoff remain. A nonblocking supplemental lock and
existing component locks prevent overlapping work; the media lock is not held
through comment collection or the whole cycle. Acquisition stays independent.

Morning status priority now includes unresolved branded daily-show IDs from known
inventory even outside the small channel head. The existing classifier supplies
priority hints only, never fabricated completion. Successful live refreshes move
behind less recently checked daily candidates. Full verified status is still
required for daily comment eligibility. Regular video acquisition/discovery rules
and defaults are unchanged.

Inventory connection API now accepts an optional timeout, retaining the existing
five-second default for the video runner. Supplemental inventory and read-only
comment planning use one-second busy waits. Existing comment-store writes retain
their five-second bound. Contention tests verify committed reads, deferred writes,
rollback/integrity and independent comment writes. No schema/journal migration or
shared database is added; morning discovery requires existing schema 3.

Execution-only latest reports live under `state/supplemental/`; default planning
creates no operational files. See RUNNER.md and COMMENTS.md for modes and safety
contracts. No cron changes, new live requests, captions, UI, historical backfill,
Archiver integration or commits are part of this milestone. Next step requires
separate approval for one supervised bounded cycle test.

Cycle verification: 42 new cycle tests and all 367 offline tests pass. This includes
real SQLite reserved/exclusive contention, bounded waits, rollback/integrity, safe
retry, failure isolation, phase deadlines, cancellation and preservation fixtures.
Read-only production planning reported eight due IDs, initially selecting
`wUrudRrP-OY` and `IX9Dv4cBUKg`; cached `tNzL9c4FmXM` remained live/Review and
excluded. No fresh status was claimed. All 75 protected production files retained
their hashes, no supplemental directory was created, and cron was unchanged.
These are validation observations, not fixed future candidate counts.


## Comprehensive forward metadata refinement (pre-rollout history)

Supersedes the older morning/cycle slot and priority descriptions above. The live
cycle found 69 priority candidates, including old undated flat `was_live` rows;
two historical IDs consumed the slots and November 5 was not freshly checked.
That live test's observations/artifacts are preserved; no rollback occurred.

Supplemental metadata now attempts every pending forward ID once per pass, without
hypothetical-classification ranking or routine small enrichment quotas. Forward
scope uses reliable recent publication (15 days bounded by the forward start),
current/recent bounded-head presentation, or recent full active status when undated.
Legacy first_seen/title dates/flat completed labels do not admit historical rows.
Known old publication wins over a recent listing. Old incomplete records remain
preserved historical backlog with count/sample reporting, not automatic backfill.

The existing sources table records `morning:live` / `morning:videos` provenance,
alongside original Live/Videos sources. This is presentation evidence, not a queue
or publication timestamp, and keeps unfinished current work recognizable after head
changes. The optional importer provenance argument leaves video-runner calls and
classification/manual decisions unchanged. No schema or database is added.

Defaults: Streams 10, Videos 0, emergency 20 full-metadata requests, per-request 60s,
metadata total 180s, cycle total 600s. `--max-metadata-requests` replaces supplemental
`--max-enrich`; regular video commands retain their existing flag and behavior.
All four current videos in the November 5 + three mini-broadcast fixture are checked
within normal bounds, with historical undated rows untouched. Completed adequately
verified metadata is reused; live/upcoming/post-live status refreshes next cycle.

`forward_coverage` reports incomplete work after deadline/emergency/backoff/failure.
Failure-only atomic retry state is under `state/morning/retry.json`: 1h/6h/24h
backoff, six-hour blocking cooldown, no same-cycle retry. Cycles honor metadata and
comment cooldowns, including inventory-only execution. Safety-state errors suppress
further network work. No video retry state, source artifacts or comment policy is
changed. See RUNNER.md for the complete current command/scope contract.

This refinement is offline only. No YouTube execution, production writes, cron,
commits, caption work, backfill, UI or Archiver integration are authorized here.

Forward-refinement verification: all 383 offline tests pass (16 net new regression
cases plus updated slot-era tests). Fixtures use explicit clocks for inventory
imports as well as policy evaluation. They cover the November 5 + three minis,
historical flat-only separation, all four checks within normal bounds, status
transitions, idempotence, partial failure/recovery, backoff/blocking, emergency
coverage/resume, head displacement and cross-tab deduplication. Read-only retained-
inventory scope analysis finds only `tNzL9c4FmXM` pending, with historical metadata
backlog separate. This is cached evidence, not a fresh live-status check.

All 80 protected production files retained their baseline hashes, cron remained
unchanged, and no production metadata retry file was created. No live requests,
production writes, staging or commits occurred. The corrected forward path still
requires separately authorized supervised live validation before scheduling.
