# DLS video downloader handoff

## Current baseline — November 3, 2026

The downloader is operating through cron on the Apple Silicon Mac mini (macOS Ventura 13.2.1). Read-only status reports **eight verified acquisitions**, 417 video identities, 694 metadata observations and four persistent manual decisions. These counts are a snapshot, not fixed expectations. Acquired content covers the October 26–30 and November 2–3 daily shows, plus October 30's The Club. The latest inspected scheduled run, November 3 at 2 p.m. Pacific, succeeded; no forward downloads or retry records are pending.

Existing checkpoints:

- `28662d696636c12c2b1f9bb95ca13fd84d39ece0`: initial inventory and verified acquisition workflow.
- `139ca8dbf08d81647039a31b023fdbf8c5b4ef39`: unattended-operation safeguards.
- `69ecd6ca0804872f897f7cafdfb1c29ce9c12bbc`: acquisition naming and active cron documentation.

There is no Git remote. The offline suite currently contains 112 tests, including 30 synthetic comment-store tests.

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

1. **Preserve YouTube comments.** The [offline persistence layer](COMMENTS.md) now preserves submitted yt-dlp comment objects, deduplicated snapshots, observation history, coverage/outcome evidence and stable author-ID sightings in a separate store. It has only been exercised against synthetic temporary test data. No live collector or refresh execution is implemented. The Mac mini investigation retrieved 121 comments for `20jeos2MJLY`, including one IrishWeegee comment; ignored evidence remains under `state/comment-investigation/`. Next, add a bounded manually invoked collector after review. Comments can appear hours or days later: eventual independent refresh should prioritize this author without disrupting media acquisition. Do not interpret comments into chapters.
2. **Local admin interface.** Build a localhost-only browser interface showing recent acquisitions with titles, verified publication dates, YouTube IDs, acquisition timestamps and verification status, plus failures, retries and pending work. Include ambiguous-video Review queues with Keep, Skip and Clear Decision; retain reversible decision history and support search, filtering and past-decision review. Eventually show comment collection status. Direct YouTube-hosted thumbnail URLs are acceptable; no local image cache is necessary. No interface exists yet.
3. **Historical backfill.** Work backward before October 26, 2026 toward the channel's earliest uploads around June 2021, using bounded date ranges. Reuse inventory, classifications and decisions; review uncertain historical material before downloading. Keep backfill independent from ongoing cron acquisition and never overwrite verified forward acquisitions. Historical publishing formats need evidence-based rules; no backfill workflow exists yet.

Deferred: comment interpretation/chapter generation (Indexer), long-term drive replication (Archiver), possible cron-to-launchd migration, possible private GitHub remote, and refined classification informed by accumulated decisions.

## Development conventions

Make incremental, reviewable changes and implement one major feature at a time. Run offline tests, preserve production SQLite and downloaded media, and use explicit executable paths for unattended processes. Do not change cron or production acquisition behavior without authorization. Do not stage runtime data, credentials or extraction dumps. Commit at sensible checkpoints, then stop and report after each authorized phase. Start a new session by checking Git and read-only status; historical validation documents describe past snapshots, not necessarily current counts.
