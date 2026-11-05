# Comment collection and preservation

`dls_video_downloader.comments.CommentStore` stores **yt-dlp-extracted comment
objects**, not raw YouTube API responses. An independent manual collector retrieves
one explicit video's comments and submits them to this store. A bounded runner
can select and execute due work when explicitly requested; no unattended collection
or scheduling is installed. The existing media runner does not invoke comment collection. Offline tests use
synthetic objects and disposable stores. A pure offline refresh policy is also
implemented; it evaluates supplied history but does not collect or schedule work.

## Manual single-video collection

From the checkout:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader comments collect 20jeos2MJLY
```

The argument must be an 11-character YouTube video ID, not a URL or playlist.
The video need not exist in the media inventory or have been downloaded. The
command dispatches before opening the video database; the global `--db` setting
does not affect it. `--store-root PATH` overrides the dedicated comment root for
isolated use. The production default remains `state/comments/`.

Defaults are 1,000 comments total, newest first, unlimited parent/reply/depth
sub-limits within that total, one second between requests, a 30-second socket
timeout, zero retries, and a 600-second subprocess deadline. `--max-comments N`
accepts 1–1000; `--timeout-seconds N` accepts 1–600. Configuration uses yt-dlp's
documented five-part `youtube:max_comments=1000,all,all,all,all;comment_sort=new`.
The explicit executables are `/usr/local/bin/yt-dlp-new` and
`/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node`; no shell initialization,
ambient yt-dlp config, plugins, installation, or upgrades are used.

Video/audio downloading and subtitle/thumbnail/metadata file writing are disabled.
The full extraction JSON is held only in a bounded memory buffer (32 MiB); only
comment objects and selected provenance are persisted. stderr is bounded at
256 KiB, with at most 64 KiB of sanitized diagnostics retained. URLs and sensitive
diagnostic lines are omitted; comment text and its source fields are unaffected.
Output overflow, timeout, and interruption kill the process group, reap the
parent, and record a failure or usable partial result. Truncated JSON fragments
are never salvaged. A separate nonblocking `.collector.lock` prevents overlapping
collectors for the same root; the subprocess inherits ownership until cleanup.
No video acquisition lock is held and no media acquisition status is changed.

CLI output is JSON with the attempt ID, retrieval state, coverage, counts, target
matches, snapshot path/hash/size, portable collection-record path and catalog path.
Exit codes: 0 for a preserved retrieval (including uncertain coverage) or explicit
disabled status; 2 for partial/failed/invalid retrieval; 130 for interruption;
75 for comment-collector overlap; 1 for storage/internal failure. Invalid CLI
arguments exit 2 before retrieval. No rate-limit bypass or automatic retry occurs.

Run the same command again only when another manual collection is desired.
Identical comment objects reuse the snapshot while each invocation records a new
attempt and portable record. Source timestamp estimates or likes may change and
produce new evidence even when text is unchanged. No previous snapshot is replaced.

## API and layout

Construct `CommentStore(root)` with an explicit dedicated directory. The intended
operational root is `state/comments/`, already covered by `/state/` in `.gitignore`:

```text
state/comments/comments.sqlite3
state/comments/<YouTubeID>/snapshots/<sha256>.json
state/comments/<YouTubeID>/collections/<attempt-id>.json
```

For example, a future caller can submit an already retrieved list:

```python
with CommentStore(dedicated_root) as store:
    result = store.record(
        video_id, comment_objects,
        outcome="success",
        coverage={"assessment": "unknown", "reason": "Traversal not verified"},
        cap=1000,
        tool={"name": "yt-dlp", "version": installed_version},
        configuration={"comment_sort": "new", "max_comments": "1000,all,all,all,all"},
    )
```

`record` owns its transaction and returns `attempt_id`, effective `outcome`,
`snapshot_hash` (possibly null), and matching comment IDs by target author ID.
Optional arguments include timezone-aware ISO `collected_at`, a list of warning
strings, an error string, and `target_author_ids`. Collection time is normalized to
UTC separately from the local recording time and untouched source timestamp fields.
Tool/configuration/coverage objects must be finite JSON. Callers must supply only
relevant provenance and sanitized diagnostics, never full extraction documents,
playback URLs, cookies, or credentials. The comments argument accepts only a list
of individual comment objects, not an entire video metadata document.

Every object needs a unique nonempty comment `id` and string `text`; IDs use the
YouTube alphanumeric/underscore/hyphen form, allowing dot-separated reply suffixes.
Parent IDs, when present, follow the same form. Missing author/display name/parent
fields and unknown source fields are preserved. Non-JSON values, malformed IDs,
non-string text, and duplicate IDs invalidate the whole collection; no arbitrary
subset wins. The attempt records `invalid` and a diagnostic without retaining the
malformed payload. Invalid API parameters (video ID, outcome, provenance, time,
targets, or cap) raise before creating an attempt. Storage errors raise and roll
back rather than masquerading as extraction failures.

## Snapshots and hashes

Snapshot format version 1 is an object containing `format_version`, `video_id`,
and `comments`. Comments are sorted by ID; every object's fields and array values
remain intact. Serialization uses UTF-8 JSON, sorted object keys, compact separators,
and no trailing newline. Non-finite numbers and lossy Python types are rejected.
No text, whitespace, Unicode normalization, timestamp range parsing, or content
interpretation is performed.

Three SHA-256 identities serve different purposes:

- Snapshot hash: canonical bytes of the entire snapshot envelope.
- Payload hash: canonical bytes of one complete comment object.
- Text hash: the exact comment text encoded as UTF-8, without JSON quoting.

Reordered comments or object keys reuse a snapshot. Collection order remains in
`attempt_comments.position`; tool configuration belongs to the attempt and does
not change snapshot identity. Every call records another attempt and its sightings,
even when all content hashes already exist. Changes to likes, names, flags, or
estimated times create new payload/snapshot evidence with the same text hash.
An exact text change also changes its text hash. Returning to a previous payload
reuses that version and retains the intervening attempts.

Source time estimates can drift between retrievals even without an edit. Such
changes remain evidence; a changed payload hash alone must not be called a text
edit. `_time_text` may contain an edited label, but neither that label nor yt-dlp's
estimated `timestamp` establishes an exact original publication or edit time.

## Catalog schema

The independent catalog uses application ID `0x444C5343` and schema version 1.
Unrelated databases and unknown versions are refused without migration.

| Table | Purpose and relationships |
| --- | --- |
| `snapshots` | Unique `(video_id, snapshot_hash)` and saved count; path is derived from these values |
| `collection_attempts` | Append-only attempt identity, collection/recording times, tool/configuration, cap, requested/effective outcomes, warnings/error, coverage, counts, optional snapshot FK, and target matches |
| `comment_versions` | Unique `(video_id, comment_id, payload_hash)`, exact text hash, observed author/parent identity, first/latest collection times |
| `attempt_comments` | Attempt/version FKs and original position; records every sighting and reconstructs changing collections |
| `refresh_state` | Per-video last attempt, last complete successful attempt, latest nonempty snapshot, failure count, and reserved next-due/suspended/review fields |
| `target_presence` | Per-video/configured-author first/latest positive observation; null means not yet observed |

Comment payloads live in snapshots rather than being duplicated as SQLite blobs.
All catalog writes for an attempt use one short `BEGIN IMMEDIATE` transaction and
foreign keys. A complete snapshot is fsynced and published with an atomic no-replace
hard link before commit; existing bytes are verified, never replaced. A crash or
catalog rollback can leave a valid unreferenced snapshot. Retry verifies/reuses it.
Catalog rollback and filesystem publication are not one atomic transaction. No
automatic orphan deletion or damaged-file repair is implemented. These guarantees
assume a trusted local directory and filesystem, as does the acquisition store.

Older collection times do not regress first/latest observation bounds or the last
attempt/success pointers. Missing comments in a later collection are **unobserved**,
not deleted. Empty and failed results retain prior nonempty snapshots and versions.
An empty complete result can become the latest successful attempt, while the
separate latest nonempty pointer still preserves access to earlier evidence.

## Outcomes, coverage, and presence

| Effective outcome | Meaning |
| --- | --- |
| `success` | Nonempty result with explicit caller-supplied complete-coverage evidence |
| `empty` | Empty list with explicit complete-coverage evidence |
| `disabled` | Caller reports explicit disabled comments; no snapshot inferred |
| `partial` | Known partial/capped result, or failed extraction with usable comments |
| `uncertain` | A returned list whose coverage has not been established |
| `failed` | Extraction failure without usable comments; no empty snapshot inferred |
| `invalid` | Caller-reported invalid result or malformed submitted collection |

Coverage accepts `assessment` of `complete`, `partial`, or `unknown`, plus arbitrary
relevant JSON evidence. The catalog retains that supplied assessment and adds
`effective_assessment` and `cap_reached`. Reaching or exceeding the configured
total cap overrides a complete claim to `partial`. Counts or exit status alone
never establish completeness. A future collector must assess reply/depth caps,
page failures, warnings, and traversal exhaustion too; this offline module does
not interpret extractor logs. Unknown coverage is the default.

Usable partial/uncertain collections receive snapshots and comment sightings.
Only complete `success`/`empty` attempts update `last_successful_attempt_id`.
Failed, invalid, and partial attempts increment `failure_count`; other outcomes
reset it when they are the latest collection. Invalid input sets `needs_review`.
Existing suspension, next-due, and review values are otherwise preserved; no
retry delays or automatic scheduling are calculated. Missing target matches and
failed attempts never clear positive target-presence history.

The default target is IrishWeegee channel ID `UCbzm6UmzlMA-Yq6v3hvEipA`.
`target_author_ids` supports multiple configurable channel IDs; display names are
never used to infer a match. Each attempt records all matching comment IDs per
configured target, including an empty list when none were observed. Positive
matches from incomplete collections are still valid sightings. All authors'
comments are preserved regardless of matching, and missing matches do not prove
absence. This is identity presence tracking only.

### Current collector coverage limits

The manual collector never infers `complete` from exit status, returned count,
or a matching displayed estimate. A normal result has outcome `uncertain` and
retrieval state `returned_nonempty` or `returned_empty`. These separate successful
retrieval from coverage certainty. Consequently the store's complete-success
pointer can remain unset after a successful manual retrieval; inspect the attempt
and its retrieval state instead.

Reaching the configured cap, interrupted/looping/incomplete diagnostics, or a
conflicting/null extracted count marks usable results partial. Nonzero exit,
timeout, and interruption preserve a valid complete JSON document's usable
comments as partial evidence, when available. Without usable JSON they record
failure. Malformed comment objects are recorded as invalid by the store. Explicit
disabled/turned-off diagnostics plus a null comment list establish `disabled`;
a missing/null list alone does not. No missing match proves absence. YouTube may
filter comments, omit replies, change counts, or return incomplete continuations.
The collector does not claim exhaustive visibility into the site.

### Portable collection records

Snapshot format and catalog schema remain version 1. Each manual attempt also
publishes an immutable `collections/<attempt-id>.json`. This small record carries
source identity, asset type `comments`, collection/recording times, tool version,
limits, warnings/errors, effective outcome and coverage, counts, matching author
IDs and comments, and original comment order. Its snapshot reference contains a
path relative to the video's directory, SHA-256, byte size, and format version.
`preservation_state` distinguishes a saved snapshot from an attempt without one;
preservation of bytes is separate from completeness of the retrieved comment set.

These two JSON artifacts can be read with ordinary JSON tools without SQLite or
write access to downloader databases. A future Archiver can ingest new comment
assets independently of unchanged media. This is a local collection record, not
a shared manifest framework or Archiver integration.

Portable export follows catalog commit and uses the same immutable publication
mechanism as snapshots. If export fails, the command reports the committed attempt
ID. Repair the export offline without repeating retrieval:

```python
with CommentStore(dedicated_root) as store:
    path = store.export_attempt(attempt_id)
```

Repeated export verifies/reuses existing bytes. Damaged records are refused rather
than silently replaced. Failed attempts also have portable records, without a
snapshot reference when no source data was preserved.

### Controlled validation — November 4, 2026

After all 137 offline tests passed, exactly one authorized live invocation collected
`20jeos2MJLY` at 12:07 p.m. Pacific. It completed in about 56 seconds with exit 0,
121 comments (69 top-level, 52 replies), no warnings/errors, and IrishWeegee comment
`Ugx6U2-39QsqzNgc6Eh4AaABAg`. The configured Node 22 runtime was recognized.
Outcome is `uncertain`, retrieval state `returned_nonempty`, cap not reached.

Attempt `3e94c28cf42f4d7880999117ec6970d0` is recorded in the separate catalog and
portable collection JSON. The immutable snapshot is 70,560 bytes with SHA-256
`818b0d7de99be43ce87f70532a53c7099d1a40e4e5f55d3b2fdde56535c8aabf`.
Both JSON artifacts were independently opened and the checksum verified without
SQLite. These operational artifacts remain ignored under `state/comments/`.
No second live collection was performed; repeat/deduplication testing used
synthetic disposable data.

## Offline deterministic refresh policy

`dls_video_downloader.comment_refresh.evaluate_refresh` is pure decision logic for
one video's complete history. It does not open databases/files, read the clock,
collect comments, update `refresh_state`, or schedule anything. The independent
bounded runner below consumes these decisions; no comment cron entry or historical
backfill workflow is operational.

```python
from dls_video_downloader.comment_refresh import evaluate_refresh, RefreshPolicy

decision = evaluate_refresh(
    history,
    now=explicit_now_utc,
    published_at=original_verified_publication,
    first_observed_at=first_discovered_at,
    cooldown_until=collector_cooldown_until,
    policy=RefreshPolicy(),
)
```

### Publication-age limit and absent-target fallback

Age always uses the video's original verified YouTube publication timestamp,
never its media download/archive time. Date-only `datetime.date` evidence uses UTC
midnight with `anchor_source='publication_date'`, explicitly retaining its day
resolution. Only when authoritative publication evidence is unavailable may an
explicit first-observed timestamp be used (`anchor_source='first_observed'`). A
later discovery/backfill encounter never replaces a known original publication
anchor. Without either anchor, automatic work stops with `missing_time_anchor`.
Target detection and estimated comment timestamps cannot supply the age anchor.

Days are elapsed 24-hour UTC periods, unaffected by DST. The final day-14 slot is
exactly `[anchor + 14 days, anchor + 15 days)`, inclusive at its start and exclusive
at its end. Every automatic due time is strictly before `deadline_at = anchor +
15 days`, regardless of target sightings, failed checks, cooldowns or manual edits.
The cutoff cannot be extended by policy configuration. The bounded runner
rechecks it before acting and caps process execution to the remaining window.

Until IrishWeegee has been observed:

| State | Forward automatic decision |
| --- | --- |
| Never collected, before cutoff | Initial collection immediately once eligible |
| Days 1–7 | Routine checks at least 24 hours after the previous collection finishes |
| Days 8–13 | No routine refreshes; wait for day 14 |
| Day 14 | One final check, with at most one bounded recovery if unsuccessful |
| At/after day 15 | Suspend all forward automatic collection |
| Explicit disabled/unavailable state | Limited day-7/day-14 checks within the same deadline |

A video first discovered during days 8–13 can still receive its first collection;
this does not replay missed intervals. Later routine checks wait for day 14.
Existing collection history prevents a duplicate initial collection. A video first
discovered at/after day 15 cannot start forward automatic collection. Unknown or
empty results do not prove absence or deletion.

### Early completion after one confirmation

Only stable channel ID `UCbzm6UmzlMA-Yq6v3hvEipA` (IrishWeegee) triggers early
completion. Display names never establish a match. Other configurable target IDs
remain available for presence reporting; they do not initiate separate cycles.
All successfully retrieved authors' comments remain preserved by the store.

The first collection containing one or more IrishWeegee comments establishes one
confirmation due **24 elapsed hours after that collection completes** (or after its
collection timestamp if no finish time was recorded). Eligibility still prevents
collection before the publication anchor. Multiple matching comments create one
confirmation, not one per comment. A usable positive sighting in a partial result
also switches to this cycle; it does not add an earlier retry before confirmation.
The existing five-unsuccessful-attempt safety guard can still suspend work for review.

The first subsequent collection starting at/after that due time and before the
publication cutoff satisfies the confirmation opportunity. This can be a manual
collection and need not contain IrishWeegee again. An earlier manual collection
does not consume the opportunity or reset its original clock. Cooldowns may defer
it, but cannot extend the publication deadline. Confirmation is a bounded exception
to the routine days 8–13 pause: first detection on day 7 permits confirmation on
day 8; first detection on day 13 permits confirmation on day 14.

After a usable confirmation (`success`, `empty`, or ordinary `uncertain`) routine
forward collection is complete. Explicit disabled/unavailable confirmation also
closes the opportunity without claiming permanent unavailability. Later edits,
metadata changes, new target comments, or missing target comments never resume
collection or create another confirmation. A delayed confirmation consumes the
one opportunity; there is no catch-up series. No long-term edit milestones or
text-triggered follow-ups exist.

If the first positive observation is so late that +24 hours reaches or exceeds
day 15, no confirmation is scheduled (`confirmation_exceeds_window`). For example,
first detection at the beginning of day 14 cannot receive a full +24-hour check
before the exclusive cutoff. Source evidence remains preserved; this opportunity
never guarantees corrections will be captured.

### Failures, recovery and cooldowns

Failed, invalid, partial/capped, timed-out and interrupted results are unsuccessful.
Backoff is +1 hour after the first, +6 hours after the second, then +24 hours,
measured from completion when available. Five consecutive unsuccessful attempts
suspend pending review. Ordinary successful uncertain or empty retrieval is not a
failure and resets the streak. No failed attempt erases earlier positive sightings.

A failed/partial confirmation permits at most **one recovery attempt**, governed
by that backoff and the original cutoff. It remains the same confirmation cycle;
no new +24-hour sequence starts. Confirmation recovery, like confirmation itself,
can occur in the routine pause. After a usable recovery, monitoring is complete.
After an unsuccessful recovery, automatic work stops with
`confirmation_recovery_exhausted` and review status. A later manual collection
cannot cause another automatic retry cycle. If recovery cannot fit before the
cutoff, it is not scheduled. Final absent-target checks have the same maximum-one
recovery bound, within the final day-14 slot.

Before any positive target sighting, transient retries respect the days 8–13 pause.
Disabled/unavailable day-7 checks that fail wait for day 14 rather than initiating
hourly polling. Explicit unavailability must be provided as
`retrieval_state='unavailable'`; the policy does not guess it from error text.

A supplied collector-wide `cooldown_until` is a lower bound for initial, routine,
confirmation and recovery work. Work moved to/past the cutoff suspends. Rate-limit
cooldowns never reopen or extend the window. Global cooldown lookup/persistence
belongs to the independent runner; this evaluator creates no shared state.

### Evidence, outputs and completion meanings

`now` is explicit and timezone-aware. Frozen `CollectionEvidence` inputs contain
attempt ID, collection/optional finish times, outcome, retrieval state and
`CommentEvidence(comment_id, author_id, text_hash)` sightings. The evaluator does
not interpret source text or source comment timestamps, and does not compare text
to generate work. Stable UTC sorting and validation make evaluations reproducible.

`history_from_rows(attempt_rows, comment_rows)` adapts already-read rows without
opening SQLite. Supply the full `collection_attempts` history for one video and an
`attempt_comments`/`comment_versions` join exposing `attempt_id`, `comment_id`,
`author_id`, `text_hash`. Coverage JSON supplies retrieval/finish evidence. Full
history is essential to retain the first positive observation and completed or
exhausted confirmation cycle; using only the latest snapshot could restart work.
The existing schema, observations, snapshots and portable records are unchanged.

`RefreshDecision` reports due/deferred time, `deadline_at`, reason, suspension,
manual eligibility, review status, anchor/source, failure streak, phase
(`ordinary`, `disabled`, `confirmation`), last attempt/outcome and `TargetPresence`
metadata. `confirmation_due_at` is the original eligible +24-hour time, before any
cooldown; it is null when no confirmation can fit. The primary/recovery attempt
IDs identify which evidence consumed the opportunity.

`monitoring_complete=True` means the policy's confirmation/recovery or final
fallback opportunity has been fulfilled. It does **not** mean the retrieved comment
set is exhaustive: an ordinary uncertain result can complete the monitoring cycle.
An expired or exhausted unsuccessful cycle is suspended for manual review, without
a false completeness claim. Neither suspension nor monitoring completion implies
comments are permanently unavailable. Missing matches never establish deletion.

Manual collection remains available for videos of any age, subject to YouTube
availability and rate limits. It may preserve corrected text as a new immutable
snapshot, but cannot restart an exhausted confirmation cycle or extend the age
window. Original and changed text, other authors and historical positive sightings
remain preserved. The bounded window and early stop reduce downstream archival
churn without freezing later explicitly requested source observations.

### Future backfill compatibility: separate intent, not a cutoff override

Historical backfill is not implemented. A future explicitly authorized workflow
may collect an old video's comments once even though its **forward refresh**
decision is suspended. That eligibility belongs outside this evaluator; there is
no backfill intent flag that weakens the publication cutoff. Manual collection is
already independent of this automatic policy.

A backfilled video still inside its original publication-age window can use this
same policy after its initial collection. Read existing YouTube-ID history first:
`last_attempt_id` identifies a prior attempt; its outcome plus catalog snapshot
references and source artifacts establish whether source data was actually saved.
A failed attempt alone must not be labeled a completed archival asset. Encountering
an already collected ID again must not enqueue another initial/backfill collection.
A legitimate due forward refresh is a different reason to collect, not a replay
of backfill. Do not substitute acquisition/download time or the backfill encounter
for a known original publication date. Tests cover the separate-intent boundary;
no backfill commands, queues or scheduling are provided.

The independent runner selects candidates, re-reads history under the collector
lock, enforces per-run budgets/cooldowns and remaining execution time, and records
attempts through the existing collector. `automatic_suspended=True` is an explicit
operator pause, not a cached derived result. No policy state is written here.

## Independent readers and future work

`load_snapshot(video_id, snapshot_hash)` verifies the hash and returns the saved
envelope. Future Indexer code can read these JSON files independently and open the
catalog using SQLite `mode=ro`. Join attempts to snapshots and `attempt_comments`
to `comment_versions` to inspect provenance, ordered sightings, and history.
No searchable content index, chapters, segment metadata, episode relationships,
or comment summaries are created here. Indexer owns interpretation; Archiver owns
long-term preservation, backup, and any future handoff.

Offline retry/refresh decisions, read-only planning and explicit bounded execution
are implemented. Unattended scheduling and the admin interface remain future
work. No production video
database, acquisition receipt, classifier, media lock, or cron configuration is
used by this storage module. The explicit store root also makes tests independent
of the operational comment tree.

Run the synthetic regression tests with:

```sh
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comments.py -v
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comment_collection.py -v
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comment_refresh.py -v
```

## Independent read-only comment planning

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader comments plan
/opt/homebrew/bin/python3 -B -m dls_video_downloader comments plan --json
```

`--details` includes excluded videos in human output; JSON includes all known
inventory IDs. All displayed times are UTC. Global `--db PATH` must precede
`comments`; `--store-root PATH` selects a separate comment tree. `--start-date`
defaults to the existing forward boundary, 2026-10-26. `--now` accepts an explicit
timezone-aware ISO timestamp for reproducible evaluation. Without it, only the
CLI reads the current UTC clock. The Python API is
`comment_planning.plan(inventory_path, comment_root, now=..., start=...,
cooldown_until=...)`; the cooldown is supplied data, not persistent scheduler state.

Both databases are opened read-only, without migrations, importer calls,
reclassification, source JSON reads, locks, or runtime logs. An absent comment
catalog means no prior collection; it is not initialized. An existing invalid
catalog fails closed. Each database is read in its own consistent transaction;
the two databases do not form a shared atomic snapshot. Planning never discovers
IDs or calls the collector. It works with newer inventory observations without
requiring media, acquisition receipts, or changes to the video runner.

Selection reuses the current stored classifications, persistent decisions, and
existing eligibility function. Skip/reject and unresolved content remain excluded;
explicit approval can select content but cannot bypass the forward boundary or
hard age cutoff. Daily shows require `was_live`; upcoming, live and processing
(`post_live`) videos are deferred from selection. The Club can be a normal upload.

For completed livestreams, age uses `release_timestamp` (YouTube livestream
start/release evidence), then `release_date`/`upload_date`. For other uploads,
`timestamp` (availability/upload evidence) precedes `release_timestamp`, then
source dates. Invalid fields are skipped; title dates and download times are never
used. Numeric evidence supplies its UTC date to the existing eligibility rules.
Date-only evidence retains date precision and uses the policy's UTC-midnight
anchor. The chosen original field, raw timing values and inventory provenance
remain visible. `timestamp` is never labeled broadcast end.

Without usable publication evidence, the policy's explicit `first_seen` fallback
is shown, but automatic selection fails closed because the forward date boundary
cannot be verified. An approval does not remove this requirement. A known old
publication date is never replaced by discovery time.

The planner adapts full collection-attempt and comment-version relationships to
`history_from_rows()` and `evaluate_refresh()`. It preserves the committed daily,
final-day, retry, cutoff and single-confirmation semantics, including successful
uncertain-coverage confirmation. It does not rely on a latest-success cache or
infer deletion from absent comments. Output includes first/last collections,
positive author observations, confirmation attempt/time, first observed completed
status (source observation and local import separately), next due time, cutoff,
phase, monitoring completion and exclusion reasons. Monitoring completion is not
exhaustive retrieval or permanent comment unavailability.

`due_ids` orders due work by oldest due time, with initial collections winning
ties. This is a plan, not execution authorization. The bounded runner described
below refreshes inputs, checks deadlines, applies budgets and honors its independent
cooldown. Morning discovery, unattended scheduling and backfill remain unimplemented.
Manual collection remains available independently.

## Independent bounded comment runner

```sh
# Default: read-only dry-run. Does not create locks, catalogs, reports or snapshots.
python3 -B -m dls_video_downloader comments run
python3 -B -m dls_video_downloader comments run --json

# Explicit execution capability; live validation requires separate approval.
python3 -B -m dls_video_downloader comments run --execute --max-videos 2 \
  --max-comments 1000 --runtime-seconds 600 --json
```

Global `--db PATH` precedes `comments`; `--store-root PATH` and `--start-date`
work as for the planner. The runner does not offer an artificial CLI clock for
execution. Dry-run reports the initial selection under the video-count budget,
remaining due count, exclusions, publication anchors, history and cooldown. It is
not a promise of execution: current eligibility, locks, timing and history can
change before execution. `comments plan` remains separately read-only and detailed.

Defaults are two videos, 1,000 comments each, sequentially, with one 600-second
invocation budget. Allowed limits are 1–10 videos, 1–1,000 comments and 30–600
seconds. The monotonic deadline starts before planning and never resets between
videos. Each extraction timeout is the smaller of the remaining invocation budget
and original publication-age window, rounded down with a one-second reserve.
At least 30 seconds of extraction allowance must remain; otherwise defer instead
of launching another request. Cleanup, JSON persistence and local reporting can
finish after the network budget; the collector kills/reaps its subprocess group.
No internal network retries were added; socket timeout remains 30 seconds.

Execution takes `state/comments/runner/.runner.lock` nonblockingly and retains the
existing per-collection `.collector.lock`. After acquiring the collector lock and
initializing its store, the collector invokes an optional runner preflight before
starting extraction. This reopens read-only inventory/history views and reevaluates
current policy, including the hard cutoff, current manual decisions and cooldown.
A manual collection completed since initial planning can therefore consume the due
opportunity without a duplicate network collection. The hook can only reduce the
caller's timeout. No policy/schema changes or importer calls are involved.

Before each request the runner probes the existing acquisition lock nonblockingly.
If busy, it defers; if the lock file is absent it does not create it. It never holds
that lock during extraction or changes the video runner. **This cannot guarantee
strict mutual exclusion:** acquisition can begin after the probe. Video priority
means deferring to already-active acquisition, without making the video cron skip
because comments held its lock. Stronger coordination needs a separate design.

Only explicit execution writes these small independent files:

- `state/comments/runner/cooldown.json`: versioned global cooldown and sanitized
  rate-limit/blocking evidence, written by atomic replacement.
- `state/comments/runner/last-run.json`: latest invocation report, atomically
  replaced, at most 256 KiB. It includes selected candidates, outcome/attempt and
  portable artifact references, deferrals/skips, timings, budgets and stop reason.
  It is not an append-only log or a new observation database. Per-attempt evidence
  remains permanently available through the existing catalog/portable records.

Known HTTP 429/402, HTTP 403 access failures, rate-limit and bot/blocking diagnostics
stop the batch immediately and establish a six-hour minimum cooldown. A recognizable
`Retry-After` header (positive seconds or timezone-qualified HTTP date) can extend
that interval; shorter or malformed values never shorten the conservative default.
Only extractor diagnostics are examined, never source comment contents. Future
invocations read the cooldown, which does not extend publication-age deadlines.
Invalid cooldown state fails closed for manual review. Manual `comments collect`
remains independent and intentionally does not silently adopt runner policy.

Ordinary recorded extraction failures/partial results consume a video slot, retain
all usable evidence and permit the next selected candidate within budget. Future
invocations use the established per-video backoff/suspension policy. Rate limits,
interruption, overlap, exhausted runtime or unknown persistence/programming errors
stop additional network work. Cooldown/report write failures are visible errors,
not reasons to retry extraction. Exit codes: 0 successful/dry/deferred operation,
2 recorded retrieval failure or blocking, 1 fatal error, 75 runner/collector overlap,
130 interruption. These are independent of the media cron's exit status.

Reports preserve source release/start and upload/availability fields, inventory
first-seen/completed observations, prior target sightings and confirmation timing.
Collected work adds collection start/completion and portable-record/snapshot paths.
Target ID lists in runner summaries are limited to 20 IDs plus their full count;
the catalog and snapshots retain all sightings. IrishWeegee's exact source fields
(`timestamp`, relative label, edit indicators) remain in the original portable
snapshot, linked by the report. Those estimates are not exact posting/edit times;
YouTube `timestamp` is not asserted to be broadcast end. No timing analytics,
comment interpretation or duplicate source-data database was added.

This milestone validates execution offline only, using disposable catalogs and
mocked extraction. No production runner execution or additional live comment
collection has been performed. No comment cron, morning discovery, backfill,
Indexing or Archiver integration is installed. A separately approved live bounded
test is the next step.
