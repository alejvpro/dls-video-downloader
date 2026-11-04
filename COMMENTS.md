# Offline comment preservation

`dls_video_downloader.comments.CommentStore` stores **yt-dlp-extracted comment
objects**, not raw YouTube API responses. This phase implements offline persistence
only. No command, network retrieval, video importer integration, retry execution,
or scheduling is installed. The existing media runner does not import this module.
Tests use synthetic objects and disposable directories; the investigation's real
comments have not been imported into an operational store.

## API and layout

Construct `CommentStore(root)` with an explicit dedicated directory. The intended
operational root is `state/comments/`, already covered by `/state/` in `.gitignore`:

```text
state/comments/comments.sqlite3
state/comments/<YouTubeID>/snapshots/<sha256>.json
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

## Independent readers and future work

`load_snapshot(video_id, snapshot_hash)` verifies the hash and returns the saved
envelope. Future Indexer code can read these JSON files independently and open the
catalog using SQLite `mode=ro`. Join attempts to snapshots and `attempt_comments`
to `comment_versions` to inspect provenance, ordered sightings, and history.
No searchable content index, chapters, segment metadata, episode relationships,
or comment summaries are created here. Indexer owns interpretation; Archiver owns
long-term preservation, backup, and any future handoff.

Next phases may add a bounded, manually invoked single-video collector and then
independent retry/refresh execution. Neither is implemented. No production video
database, acquisition receipt, classifier, media lock, or cron configuration is
used by this storage module. The explicit store root also makes tests independent
of the operational comment tree.

Run the synthetic regression tests with:

```sh
/opt/homebrew/bin/python3 -B -m unittest discover -s tests -p test_comments.py -v
```
