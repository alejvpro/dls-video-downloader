# Comment collection and preservation

`dls_video_downloader.comments.CommentStore` stores **yt-dlp-extracted comment
objects**, not raw YouTube API responses. An independent manual collector retrieves
one explicit video's comments and submits them to this store. No background
collection, retry execution, automatic refresh, or scheduling is installed. The
existing media runner does not invoke comment collection. Offline tests use
synthetic objects and disposable stores.

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

## Independent readers and future work

`load_snapshot(video_id, snapshot_hash)` verifies the hash and returns the saved
envelope. Future Indexer code can read these JSON files independently and open the
catalog using SQLite `mode=ro`. Join attempts to snapshots and `attempt_comments`
to `comment_versions` to inspect provenance, ordered sightings, and history.
No searchable content index, chapters, segment metadata, episode relationships,
or comment summaries are created here. Indexer owns interpretation; Archiver owns
long-term preservation, backup, and any future handoff.

Next phases may add independently tested retry/refresh policy and execution.
Neither is implemented. No production video
database, acquisition receipt, classifier, media lock, or cron configuration is
used by this storage module. The explicit store root also makes tests independent
of the operational comment tree.

Run the synthetic regression tests with:

```sh
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comments.py -v
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comment_collection.py -v
```
