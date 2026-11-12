# DLS Video Downloader

The DLS Video Downloader preserves selected official show videos and their comment and original-caption records. Stable YouTube IDs identify videos; titles and dates are evidence, not identifiers.

## Operations Quick Start

**Current operation:** forward discovery and media acquisition are scheduled and active. The installed acquisition limit is one video per run. Comment and caption collection remain paused. The independent Pushover dispatcher runs every 15 minutes. Historical backfill is not installed.

Forward collection covers eligible videos dated on or after **October 26, 2026**. Its bounded discovery automatically recovers missed forward work after outages; manual catch-up is optional acceleration, not required for recovery. Historical backfill is a separate future workflow.

Media and acquisition manifests are stored under `state/acquisitions/<video-id>/` (`media.mkv` and `acquisition.json`). The main inventory is `state/inventory.sqlite3`; comment and caption catalogs/assets are under `state/comments/` and `state/captions/`. Shared YouTube safety evidence is under `state/youtube-safety/`; notification delivery history is in `state/notifications/deliveries.sqlite3`.

Use these read-only commands to check current inventory, pending work, and shared safety:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader status --json
/opt/homebrew/bin/python3 -B -m dls_video_downloader plan --json
/opt/homebrew/bin/python3 -B -m dls_video_downloader --db state/inventory.sqlite3 youtube-safety status --json
/opt/homebrew/bin/python3 -B -m dls_video_downloader --db state/inventory.sqlite3 youtube-safety events --json
```

Recent run summaries and sanitized diagnostics are in `state/runs/`; scheduled output is in `state/scheduled/`. To investigate a failure, inspect `status`, the matching run summary/events, and its bounded stderr log. A read-only review does not need another YouTube request; do not rerun collection just to reproduce an error.

Normal scheduled forward acquisitions are intended to rise to **up to two per invocation** after a separately approved cron update. Manual forward catch-up may explicitly use the existing runner with a maximum of **three** acquisitions when an operator wants to accelerate a backlog:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 3 --start-date 2026-10-26
```

The command above is a manual example, not the installed schedule. It uses the same shared safety gate, operation lock, forward boundary, metadata limits, and per-video retry policy. Each download subprocess may run for up to two hours; later scheduled runs defer safely if the shared lock is occupied. Historical backfill is separate and is initially intended to proceed at approximately one acquisition per day.

See [runner usage and operational limits](RUNNER.md), [comment collection](COMMENTS.md), [caption preservation](CAPTIONS.md), [shared YouTube safety](YOUTUBE-SAFETY.md), and [project handoff](HANDOFF.md) for detailed operating guidance.

## Requirements and usage

Python 3.10+ and its standard library are sufficient. Run directly from this checkout without installing anything:

```sh
python3 -B -m dls_video_downloader --help
python3 -B -m dls_video_downloader import \
  --live recent-live-inventory.json \
  --videos recent-videos-inventory.json \
  --full sample-video.json
python3 -B -m dls_video_downloader status
python3 -B -m dls_video_downloader plan --json
python3 -B -m dls_video_downloader decide Zvvx0Lcavkk approve --reason 'Original reaction broadcast'
python3 -B -m dls_video_downloader reclassify
python3 -B -m unittest discover -s tests -v
```

Optional installation in your own virtual environment: `python -m pip install -e .` (packaging tools may require network access). This provides `dls-video-downloader`. Runtime Python dependencies: none.

The default database is `state/inventory.sqlite3`; override it before the subcommand with `--db /path/to/inventory.sqlite3`. `status` and `plan` open an existing database in SQLite read-only mode and never initialize one. Both support `--json`, `--details`, `--start-date YYYY-MM-DD`, and optional inclusive `--end-date YYYY-MM-DD`. Plan lists every item and its classification/eligibility reasons. JSON includes all records and per-item warnings. Exit codes are 0 for success, 2 for partial failure/usage errors, 1 for fatal failure, 75 for overlap, and 130 for interruption. Import and decision updates are transactional; a failed multi-file import rolls back its inventory changes.

The three root research JSON files remain exploratory inputs, not bundled application fixtures or complete channel inventories. Inputs are not modified or copied. `--other` imports another local extraction; `--full` uses source `other`. Metadata depth is determined independently from playlist structure / `_type`. No yt-dlp executable is invoked.

## Inventory schema (version 3)

SQLite `user_version` guards schema compatibility. Writable opens migrate older schemas to version 3, adding acquisition and operational tables without changing original records. Read-only reporting accepts versions 1–3; unknown versions are rejected.

| Table | Meaning |
| --- | --- |
| `videos` | Unique video ID; application first/last import sightings; initial/latest titles; merged known metadata and per-field observation time/detail provenance |
| `observations` | Immutable unique sanitized metadata payloads by ID, detail (`flat`/`full`), and content hash; source observation and import times |
| `sources` | Distinct discovery sources (`live`, `videos`, `other`) and application sighting times per ID |
| `imports` | Exact input-byte digest plus source, filename, import time, and entry count |
| `classifications` | Automatic result/reason, rule version, evidence digest, evaluation time; latest row is current |
| `decisions` | Append-only manual decisions and reasons by stable ID; latest row is current |
| `acquisition_attempts` | Per-video attempt identity, working directory, start/finish times, running/failed/interrupted/complete state, and outcome summary |
| `acquisitions` | One verified acquisition per video ID, with media/manifest paths, size, SHA-256, and completion time |
| `retry_state` | Persistent per-video/operation failure count, reason, category, and next retry time |
| `runner_runs` | Bounded recent run outcomes and summaries |
| `scan_frontiers` | Previous scan head per tab for continuity checks |
| `cleanup_events` | Bounded history of safe stale-file removals |

Channel identity is stored in metadata. Allowlisted metadata preserves titles, full descriptions, source date/timestamp fields, durations, live/availability flags, author/channel fields, counters, tags, categories, and language. Operational tables intentionally omit formats, thumbnails, and temporary playback URLs. Original research files remain the raw evidence; this is not a lossless yt-dlp dump archive. Database observation snapshots are not yet Archiver artifacts.

Identical input bytes/source are a complete no-op, including sighting times. Changed input files can update sightings without creating identical metadata payloads again. Returning to a previously observed payload reuses its content record; the merged latest values can still change. Observation rows represent distinct content, not every polling event. Acquisition attempts are recorded separately in `acquisition_attempts`.

Initial title means first title imported by this application. Latest values follow source observation time (`epoch`, falling back to import time). Missing/null fields never erase known values. Older observations do not overwrite newer values. Later flat observations can update titles and counters, but cannot downgrade full-extraction descriptive/status fields. Changed full descriptions remain in observations. A future explicit-null/deletion policy must be developed from real refresh evidence.

Source publication dates are separate from application discovery/import times and metadata observation times. Reliable `release_date` precedes `upload_date`; differing values produce a warning. Title dates and discovery dates never establish publication. Timestamp fields are preserved but are not currently converted into publication dates. Flat research entries therefore remain explicitly date-unknown.

Back up the local SQLite database when the application is stopped; logs are not a substitute for it. Import/classification/decision evidence is persistent. Explicit acquisition retains per-attempt sanitized tool diagnostics; bounded runner logs are described in RUNNER.md.

## Controlled single-video acquisition

The ID-based storage layout is intentional; see the [acquisition naming contract](ACQUISITION-NAMING.md). Titles and archive presentation never determine acquisition paths.

```sh
python3 -B -m dls_video_downloader download MbO7y_90zUI
```

Scheduled forward acquisition is authorized from October 26 onward; see RUNNER.md for its limits. The command takes exactly one existing ID and optional `--start-date`; it never enumerates or queues other videos. Effective selection and eligibility must allow acquisition. Manual approval can select a Review/Skip item; manual rejection always blocks it. The video requirement remains format **398 (AV1 1280×720, 30 fps, non-DRM)**. Audio is selected from confidently identified original-language Opus or AAC tracks, preferring non-DRC and then Opus when quality evidence is comparable. The established unsuffixed 251 primary rendition remains accepted if yt-dlp omits its language label. Otherwise ambiguous source language, translated/dubbed tracks, DRM, or missing size evidence stop preflight for review. Output remains MKV with stream copying; there is no transcoding or within-run retry loop. The resolution ceiling remains 720p.

Executables are explicit: `/usr/local/bin/yt-dlp-new`, the versioned Node path documented above, and existing `/opt/homebrew/bin/ffmpeg` / `ffprobe`. Ambient yt-dlp configuration is ignored. Preflight metadata stays in memory except for sanitized allowlisted metadata; expiring playback URLs are omitted from retained diagnostics and manifests. Format sizes must be available; free-space budget is three times combined advertised stream size plus 1 GiB for download/merge headroom. This estimate cannot prevent unrelated processes consuming space later.

Outputs stay inside this project at `state/acquisitions/<video-id>/media.mkv` and `acquisition.json`. Unique in-progress attempts live under `state/acquisitions/.work/<video-id>/<attempt-id>/`. New verified attempts move their small metadata/log evidence to `<video-id>/attempts/<attempt-id>/` before publishing the manifest. The manifest associates this evidence, selected formats, stream details, expected/actual duration, file size, and SHA-256 with the permanent video ID. Publication uses an atomic no-replace hard link, temporarily creating two names for one inode rather than copying the media.

Completion requires a nonempty MKV file, exactly one 720p30 AV1 video stream and one selected Opus or AAC audio stream, duration within the larger of 10 seconds or 0.1%, merger stream-copy evidence, and SHA-256. New manifests record selected audio format, codec, language evidence, DRC status, quality/size evidence, and rationale. Existing manifests without these optional fields remain valid; their recorded codec is used for repeat verification. A subsequent invocation verifies the existing hash, size, and streams before returning `already_verified`; it performs no YouTube request or download. Existing damaged/inconsistent final files are never replaced automatically. Interrupted attempts keep isolated partial output and cannot establish completion. SIGKILL/power loss may leave an attempt marked running; incomplete publication requires explicit recovery review. A completed manifest can reconcile a missing database receipt after re-verification.

A local advisory lock prevents concurrent acquisitions using the same output root. After successful verification and database recording, cleanup under this lock unlinks only an attempt's extra `media.mkv` name when it demonstrably shares the final media inode, then removes only empty staging directories. It never removes final media or recursively sweeps failed, interrupted, or unrelated attempts. Cleanup failure is reported as deferred and does not relabel a completed acquisition. A repeat invocation can retry cleanup after reverification. Legacy October 26 metadata/logs remain at their original `.work` paths because its unchanged manifest references them; only its extra media hard link is removed.

Interrupted attempts retain partial data. A retry after the persistent delay starts a new isolated attempt rather than resuming. A final file without a valid completed manifest requires manual recovery. SIGKILL can leave status `running`; this never proves completion. `cleanup` provides a dry-run plan for old owned staging files; the runner applies conservative bounded retention under its lock. See [retry/retention policy](RUNNER.md). Guarantees assume cooperative processes and trusted local directories. This is acquisition evidence, not the Archiver's preservation/backup receipt; no external-drive handoff exists. Offline tests use tiny disposable fixtures.

The first October 26 acquisition and duplicate-protection check succeeded; see [pilot results](PILOT-ACQUISITION.md) for the verified streams, size, hash, and limitations.

## Classification and decisions

Rule version 2 uses channel identity plus combinations of branding, source, completed-live status, and contextual metadata. Regular daily candidates require completed-live metadata, Live discovery, and regular daily branding. Previously established daily-show identity survives descriptive retitling. Classification history and manual decisions survive reevaluation.

The Club is recognized on the official channel's Videos source by explicit Club branding, without a minimum duration or Friday requirement. Excluded programs are Mystery Crate, Pitch Clock, Because Miami, MMA Hangout/UFC watchalongs, Football America, Canes Insight, and identified Suey award/nomination compilations. Rules examine titles for series branding, not incidental description mentions.

Confirmed program exclusions also include The Hockey Show, MORALLY ABHORRENT (including previews), and The Alley Oop. Because Miami recognizes case-insensitive `#BecauseMiami`, `BecauseMiami`, and `Because Miami` leading branding. These four program rules require a leading name followed by an episode separator or the end of the title; Alley Oop also accepts the observed trailing `| Alley Oop` / `| The Alley Oop` label. Incidental mentions and description text do not match. Established daily-show identities, newly recognized regular completed daily shows, and The Club are evaluated before these four rules. Older exclusion policies and clip/reaction heuristics are unchanged; the proposed `SEGMENT` and “joins the show” rules remain unimplemented.

Alley Oop metadata remains in the inventory. Its eventual preservation belongs to a separate future acquisition process because episodes appear across two channels; no acquisition or integration for that process exists here. Titles without clear branding separators remain conservative Review candidates unless other existing evidence resolves them.

Short Videos uploads with explicit excerpt/segment branding, or topical sports questions, are treated as ordinary clips only with corroborating channel/source/runtime evidence. Reaction, interview, bonus, exclusive, watchalong, and sit-down markers go to review first. Extended or otherwise ambiguous productions remain review items. These initial heuristics are intentionally limited: they do not prove a segment is contained in a daily show. They are documented, reasoned decisions and should be refined against reviewed examples. Titles, duration, and tab alone are insufficient.

Automatic categories are `daily_show`, `the_club`, `skip`, and `review`. Manual `approve`, `reject`, or `skip` takes precedence for selection. `clear` removes the current override while retaining its history. Decisions can precede discovery. Initial imports seed the four user-provided decisions only if no decision history exists; subsequent imports never overwrite user changes. Repeating an identical decision/reason adds no duplicate. Nothing deletes media or metadata.

The three selection outcomes are **Download**, **Skip**, and **Review**; `daily_show` and `the_club` remain internal Download content types for schema/CLI compatibility. Manual overrides determine effective selection independently of those automatic types.

Automatic content classification and acquisition eligibility are independent:

- Default automatic forward start: **Monday, 2026-10-26**, inclusive. The eventual active review queue must use this same configurable boundary.
- Selected content with missing source dates: `date_unknown`.
- Selected content outside the configured range: `outside_range`.
- Skipped or unresolved content: `not_selected`.
- Manual rejection/skip: `blocked`.
- Explicit approval: `eligible`, including older and date-unknown videos.

`eligible` is a planning authorization, not a claim of completed/available/playable media. Acquisition independently verifies readiness. Dates are parameters of each read-only plan, never permanent historical exclusions. The forward acquisition runner adds a strict verified forward-date requirement even for approvals; explicit manual downloads retain historical approval exceptions. Comments and captions remain paused as noted above.

## Roadmap and project handoffs

The priority is a reliable, continuously growing, searchable archive. Website launch
does not wait for complete historical coverage.

1. **Stabilize forward capture.** Validate the forward schedule and request/runtime
   budgets, resolve the open Club audio-format policy with evidence, then restore
   YouTube jobs through supervised stages. The target is daily full shows and Friday
   Club episodes, with original captions and comments including IrishWeegee timestamp
   evidence. Keep shared YouTube safety, incident preservation, and Pushover alerts
   active. Audit the first scheduled results before treating unattended capture as
   reliable.
2. **Start slow historical collection after forward capture is stable.** The agreed
   historical boundary is June 1, 2026 inclusive; collection remains parked until
   Historical Format Research documents publishing patterns and the practical
   playlist/reconciliation coverage. Work at a deliberately conservative rate; make
   progress resumable, interruptible, and subject to shared YouTube safety.
   Prefer existing inventory evidence or an approved official metadata source for
   dates where that avoids duplicate yt-dlp metadata extraction. Do not let history
   consume forward-capture priority or turn backfill into a second safety system.
3. **Prioritize the main DLS Archiver.** Coordinate storage, portable asset records,
   integrity verification, backup handling, and ingestion contracts across video,
   comments, captions, and the existing podcast assets. Keep operational SQLite
   catalogs distinct from durable archive receipts; do not introduce a universal
   cross-project database as a shortcut.
4. **Build the Indexer and searchable website.** Ingest whatever verified coverage
   exists, including video metadata, captions, comments, podcast metadata, and their
   relationships. Launch with current coverage and add backfilled material
   incrementally; do not wait for the historical project to finish.
5. **Continue historical backfill.** Expand slowly behind the forward workload,
   from the researched recent era toward older and more complicated publishing
   formats. Preserve uncertainty and provenance; let research guide each boundary.

### Archiver and Indexer handoff dependencies

- **Downloader → Archiver:** stable YouTube ID; asset type; original source evidence
  and field provenance; portable relative paths; byte size and SHA-256; retrieval or
  observation times; format/track identity; immutable revision identity; and explicit
  distinction between collection completion and source-data completeness. The
  Archiver owns destination layout, copy/verification, backup policy, and durable
  archive receipts. Downloader operational databases are not the handoff contract.
- **Podcast scraper → Archiver:** the podcast item's stable source identity, original
  media and metadata paths, checksums, and revision/update semantics. Coordinate this
  contract with the existing podcast assets rather than coupling the downloader to
  the sibling repository's internals.
- **Archiver/source assets → Indexer:** stable asset identities, verified locations,
  checksums/revisions, and provenance sufficient to distinguish missing assets from
  unindexed assets. The Indexer owns normalization, transcript/comment interpretation,
  episode/segment relationships, search fields, and query behavior; source-preserved
  caption/comment bytes remain unchanged.
- **Coverage and launch:** Indexer ingestion must be incremental and tolerate partial
  historical coverage. Historical completeness is a continuing improvement, not a
  prerequisite for a useful website.

The current media policy remains strict: acquisition requires yt-dlp format 398
(AV1, 1280×720, 30 fps) with stream-copy verification. The November 6 Club inspection
found 398 but not exact 251; audio selection now permits verified original Opus or AAC
without silently selecting translations or dubbed tracks. The metadata diagnostic
and acquisition use the same selector.

`/usr/local/bin/yt-dlp-new` and the explicit Node 22/FFmpeg/ffprobe paths remain the
configured tools. Do not change dependencies or sibling projects as part of this
roadmap update.

### Future local review interface (not implemented)

Plan a browser interface bound to localhost on the Mac mini, using the existing SQLite inventory. Keep, Skip, and Clear Decision must append reversible manual-decision history and immediately affect subsequent selection. Include active forward reviews, historical reviews, resolved decisions, history inspection, and export for periodic rule refinement. Display YouTube-hosted thumbnails directly from their URLs; a local cache is not required. Those browser requests will contact YouTube image hosting when the future interface is used. Phase 1 stores no thumbnail URLs in its sanitized operational metadata, so providing those URLs remains future UI work. No UI, thumbnail request, or decision export feature is implemented in this refinement.

### Comment preservation

The independent [comment store and manual collector](COMMENTS.md) preserve complete yt-dlp-extracted comment objects in content-addressed JSON snapshots and a separate SQLite history catalog. They retain exact text, identities, reply relationships, timestamp evidence, distinct payload/text hashes, collection outcomes and configurable author-presence sightings. Collect one explicit video with:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader comments collect 20jeos2MJLY
```

Defaults: 1,000 comments, newest first, zero retries, 30-second socket timeout and 600-second process deadline. No media, captions or thumbnails are downloaded. `state/comments/` contains the catalog, snapshots and portable per-attempt JSON provenance. Repeat the same command manually to refresh; identical results reuse snapshots. A successful retrieval normally reports uncertain coverage, not exhaustive completeness. The command does not open the video inventory. Automatic refresh and comment scheduling remain unimplemented. The future Indexer, not this downloader, will interpret comments into structured chapters.

Original-English caption preservation is implemented and offline-tested, but not
activated in production. See [CAPTIONS.md](CAPTIONS.md) for read-only planning,
explicit execution, immutable JSON3/VTT assets and independent retry history.
Installed supplemental jobs do not enable captions.

Forward discovery and acquisition are active; supplemental collection remains paused. [Shared YouTube safety](YOUTUBE-SAFETY.md)
now gates every network workflow; cooldown expiry does not automatically resume
requests or cron. Inspection is read-only and recovery requires explicit approval.

[DLS Pushover alerts](NOTIFICATIONS.md) are implemented for explicit delivery of
supported safety events. Planning is read-only. Private provisioning and one
supervised test succeeded, with iPhone receipt confirmed; unattended incident
delivery and notification scheduling remain inactive.
