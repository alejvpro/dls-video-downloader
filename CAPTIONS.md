# Independent original-English caption preservation

Implemented and offline-tested. One supervised production collection was authorized
but YouTube blocked its metadata lookup; no production caption payloads have been
retrieved. **Unattended caption activation is not authorized.** Existing scheduled cycle
commands omit `--captions` and continue metadata/comment work only. No caption
cron entries or database migrations are required.

## Commands and authorization

Read-only commands (no network, database initialization, runtime logs or locks):

```sh
python3 -B -m dls_video_downloader captions plan --json
python3 -B -m dls_video_downloader captions run --json
python3 -B -m dls_video_downloader cycle --json
```

Commands implemented for a separately authorized supervised validation:

```sh
python3 -B -m dls_video_downloader captions collect VIDEO_ID --json
python3 -B -m dls_video_downloader captions run --execute --max-videos 1 --json
python3 -B -m dls_video_downloader cycle --execute --captions --max-caption-videos 1 --json
# --discover additionally authorizes the existing metadata phase.
```

`collect` explicitly requests a manual single-video refresh, at any age. `run`
uses inventory eligibility and history, defaults to dry-run, and rechecks before
network work. `--store-root` selects an independent caption tree. Global `--db`
selects inventory. `cycle --captions` without `--execute` is rejected; default
planning already displays caption decisions. **Do not add the flag to production
cron until Level 1 validation and rollout approval.**

## Eligibility and source identity

The planner reuses the existing inventory selection/provenance view in one
read-only SQLite transaction, without reading the comment catalog. Manual
Keep/Skip decisions retain existing semantics; automatic historical acquisitions
outside the forward start-date boundary remain excluded. No MKV is required.
Daily/live-origin shows require full completed `was_live` evidence; ordinary
selected uploads require full `not_live` evidence. Fresh extraction must again
establish completion. Live, upcoming, post-live and unknown states do not qualify.

Track selection inspects source URL language, `kind=asr`, and absence of `tlang`;
an English display key alone is insufficient. Manual/non-ASR and automatic
English tracks are distinct. Identity hashes use type, source language, source
name and source identifier when exposed. Equivalent aliases share that source
identity when per-format URLs agree; conflicting URLs require review.
`en-orig` and `en` do not cause duplicate downloads. Distinct named
tracks remain distinct. Only HTTPS YouTube timedtext endpoints are accepted.
Non-English/translated listings are retained as availability evidence, without
payload downloads. Live chat is excluded. Conflicting/missing English identity
evidence creates an explicit ambiguity and automatic review suspension.

## Exact-byte assets and catalog

The independent, Git-ignored layout is:

```text
state/captions/captions.sqlite3
state/captions/.collector.lock
state/captions/runner/.runner.lock
state/captions/runner/last-run.json
state/captions/runner/cooldown.json                 # only after blocking evidence
state/captions/VIDEO_ID/tracks/TRACK_HASH/SHA256.json3
state/captions/VIDEO_ID/tracks/TRACK_HASH/SHA256.vtt
state/captions/VIDEO_ID/collections/ATTEMPT_ID.json
```

JSON3 and VTT are independent required representations of the same source track.
Neither represents the creator's original uploaded file necessarily: they are
YouTube-served caption response bodies. The supervised experiment established
JSON3 event/run timing, append/window information and speaker-change flags that
VTT does not fully retain. VTT remains independently useful for interoperability.
Acquisition validates containers/timing syntax without normalizing text, converting
timestamps, identifying speakers, or creating a transcript. Structural validity
and temporal extent do not establish transcription accuracy, exhaustive coverage
or permanent finality. Cue times are relative playback positions, not verified
absolute broadcast times.

HTTP bodies are retained byte-for-byte, without conversion, decompression or
subtitle postprocessing. Unexpected content encoding is rejected. Each portable
version-1 JSON record contains video/track identity, aliases, safe availability
and completion evidence, tool/version, configuration, collection and per-format
retrieval times, outcomes/errors, sizes, SHA-256 and paths relative to the caption
root. URLs and tokens are transient only; full extraction JSON is discarded.
Records contain no transcript copy. A reused artifact references prior observation
evidence and retains its original retrieval time.

SQLite schema 1 is independent from inventory and comments:

- `attempts`: one immutable portable record per actual collection attempt.
- `assets`: unique `(video_id, track_id, format, sha256)` and portable path/size.
- `observations`: attempt-to-representation links, with foreign keys to both.

Retry state is derived from attempt history rather than a second mutable state
machine. Planning opens catalogs read-only with one-second SQLite timeouts.
Execution serializes through independent runner/collector locks. Artifact and JSON
publication is atomic and never overwrites distinct bytes; catalog writes are
transactional. A database failure can leave an orphan immutable artifact/record
as genuine evidence: it is not silently adopted, deleted or counted as a committed
catalog attempt. Stop for review; prior history remains intact. Existing files
are checked before reuse, and corruption fails closed.

A future Archiver can read portable records and verify paths/sizes/checksums
without database access. The Indexer may derive text/timing representations later;
original files remain untouched. No universal asset model or integration exists.

## Partial preservation and revisions

Per-format states distinguish not advertised, retrieval failure, validation
failure, preserved, reused, unchanged and revised. Collection outcomes distinguish
missing tracks, missing formats, ambiguity, partial preservation, failure, and
complete/unchanged/revised format preservation.

If JSON3 succeeds and VTT fails, retain JSON3 and retry VTT only; the symmetric
case works identically. Retry of a partially successful confirmation also reuses
its successful representations. Normal recovery does not redownload verified
formats. Manual refresh and scheduled confirmations intentionally retrieve both
to observe changes. Identical bytes reuse immutable artifacts; different bytes
create a new checksum revision. Every attempt has its own portable observation.
Missing tracks in later listings never imply deletion of earlier source data.

## Independent bounded lifecycle

For livestreams the anchor is the earliest retained **full completed observation's
`imported_at`**: when this inventory first knew reliable completion. The external
extraction `observed_at` is retained separately. Neither claims actual broadcast
end. Original release/start time remains provenance, not the caption retry anchor.
For ordinary uploads, a verified full observation's numeric YouTube `timestamp`
provides upload/availability evidence; otherwise first full `not_live` observation
is the explicit fallback. Unknown/future completion evidence defers collection.

Initial collection is immediately due. Missing tracks or required formats use
thresholds relative to that anchor: **+1h, +6h, +24h, +3d, +7d, +14d, +30d**.
One observation consumes elapsed checkpoints; late invocations never replay a
burst. After the final successful missing-caption check, monitoring ends and
manual refresh remains possible. This is not permanent caption unavailability.

The first complete preservation schedules **+24h and +7d confirmations**, anchored
to its completion time. A late invocation after day 7 performs the final check
without replaying day 1. A successful listing can complete a confirmation even
if a formerly available track is now missing; that fact remains explicit. Revised
bytes do not restart monitoring. Manual refresh after terminal monitoring does
not restart its cadence. These intervals are initial operational policy, separate
from the comments' publication-age cutoff, and can be reviewed using observations.

Technical failures use **1h, 6h, then 24h** progressive backoff; five consecutive
failures suspend automatic work for review. Successful empty listings are not
technical failures. Failure recovery can defer an elapsed checkpoint, but is
bounded by the consecutive-failure suspension; there is no indefinite retry loop.
Confirmed blocking stops the batch under the [shared policy](YOUTUBE-SAFETY.md):
24-hour default for bot verification, Retry-After or bounded rate-limit backoff.
Bare 403 is not automatically global blocking. The cycle shares
blocking cooldown evidence across metadata/comments/captions. History and artifacts
are never cleared after failure. Historical mode is a pure policy extension point
that refuses forward automatic execution; no historical command/backfill is built
and no historical attempt replays this forward timeline.

## Supplemental budgets, outcomes and inspection

Order: requested metadata, comments, then **opt-in captions**. Defaults: one
caption video per cycle (configurable 1–2), 180 caption seconds (30–300), at most
four original English tracks per video, 16 MiB per representation, zero retries,
20-second sockets, at most 60 seconds per metadata/payload subprocess. A shared
600-second cycle deadline spans every phase; remaining time is rechecked before
each request. Less than 30 seconds prevents starting a caption video; less than
five seconds prevents starting another request. The emergency track bound reports
ambiguity/incomplete coverage and requires review rather than claiming success.

Caption work never launches media, holds its acquisition lock during network IO,
or delays comments to reserve caption capacity. Nonblocking acquisition probes
happen before requests; acquisition may start after a probe (the existing race).
The cycle/runner/collector locks prevent overlapping supplemental/duplicate caption
work. Independent phase outcomes, attempt counts, deferred/incomplete work,
collection-record references and errors are in the existing cycle report and
caption runner's bounded latest report. Empty successful listings may be
operationally successful while caption preservation is still incomplete.

Inspect `captions plan --json`, `cycle --json`,
`state/supplemental/last-cycle.json`, `state/captions/runner/last-run.json`, portable
collection JSON and any cooldown JSON. Asset checksums can be verified directly
with `shasum -a 256 PATH`, without SQLite. SQLite inspection should use read-only
connections. Invalid catalog or cooldown state fails closed.

Next: authorize one supervised single-video validation, inspect exact bytes,
portable checksums, partial-success behavior and next decisions, then separately
approve caption activation through the existing cycle flags. No separate caption
scheduler, cron installation, captions backfill, analytics, UI, Archiver or Indexer
integration is implemented.

## November 5 LOGIN_REQUIRED diagnosis (offline)

The 10:43–10:44 Pacific listings used the same yt-dlp 2026.10.26 release/build and
explicit Node path as the blocked 12:25 collector. Neither selected a player
client or supplied cookies. The 11:04 payload experiment reused that earlier
listing and made two direct timedtext requests: it did not test a fresh watch-page
or player lookup. The permanent collector correctly requests fresh metadata to
verify current completion, original track identity and availability. It stopped
when tv_samsung, visionos and web player responses required login and the error
explicitly requested bot verification. The normal video runner's metadata flags
also isolate configuration, use Node and disable caption/comment writes; it does
not share the caption listing's exact options or establish the later request's
server-side state.

The successful listing used a 15-second socket timeout and no verbose or explicit
FFmpeg-location flag; the collector uses 20 seconds, verbose diagnostics, explicit
FFmpeg location and disabled playlist metadata writes. These differences do not
demonstrate a cause. No custom client, proxy or authentication setting was found
in the blocked diagnostics; earlier complete environments/client traces were not
retained. Transient server-side anti-bot/session/IP state is plausible; cumulative
request volume and specific client behavior remain unproven. A scheduled video
run occurred between experiments, which establishes chronology, not causation.

The old caption URLs' advertised expiry was later than the failed attempt, but
those URLs were never used by the collector. Expiry cannot explain a challenge
in its earlier fresh-metadata stage. Inventory drops caption fields by design;
research metadata contains temporary signed URLs and old availability. Reuse safe
identity/provenance and verified caption bytes for partial recovery, not cached
URLs as a workaround. Confirmations and missing-format recovery still need fresh
listings. Never weaken translation/original-English verification to avoid a block.

Two demonstrable provenance defects are corrected for **future attempts only**:
extractor version is retained before failure classification, and cooldown evidence
is linked to the attempt ID once allocated. No extractor/network/authentication
behavior changes. Existing production failure JSON, SQLite and cooldown are not
rewritten: the original record retains its historical null fields.

The original shared cooldown expires November 5 at 18:25:59 PST (November 6
02:25:59 UTC). Expiry is only a safety threshold, not proof YouTube will allow
access. Normal scheduled cycles still omit captions; shared blocking evidence can
defer their metadata/comment network phases without changing comment policy or
media cron. Do not clear the cooldown or infer permanent caption unavailability.
Next live work requires a new supervised authorization, a safe schedule window,
free locks, no active cooldown and one bounded attempt. Stop again on blocking;
no cookie/client/proxy workaround or unattended activation is approved.

## Shared blocking safety

The repository-wide guard now covers caption metadata and timedtext subprocesses,
including manual collection. See [YOUTUBE-SAFETY.md](YOUTUBE-SAFETY.md). The existing
November 5 failure and six-hour legacy deadline remain unchanged; expiry does not
prove recovery. New failures follow the shared policy. All DLS cron jobs remain
paused, caption scheduling stays opt-in, and no live recovery was attempted in
this milestone. Caption retry/confirmation and immutable revision rules do not
change. Shared safety state is independent of caption SQLite and portable assets.
