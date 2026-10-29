# Bounded discovery and acquisition runner

No cron entry is installed. Unattended downloading remains unauthorized pending the retention/retry review below.

From this checkout, discovery-only:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader discover --per-tab 10 --max-enrich 3
```

Discovery plus bounded acquisition, after authorization:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader run --per-tab 10 --max-enrich 3 --max-downloads 1
```

The supervised October 28 test additionally uses `--only-id IX9Dv4cBUKg`. This is a run-specific acquisition restriction, never a discovery/classification rule. Normal operation takes no video IDs. `--start-date` overrides the shared default `2026-10-26`. Limits are validated: 1–50 entries per tab, 0–20 full enrichments, 1–3 acquisition attempts. Defaults are 10 / 3 / 1. A download failure consumes one attempt; failures are not retried inside the run.

## Discovery and filtering

The official channel ID is pinned; both `/streams` and `/videos` are flat-enumerated using yt-dlp's playlist limit. Unexpected channel identities, missing lists, and oversized responses are errors. Each valid tab imports transactionally through the existing inventory logic. Source is `live` or `videos`; full observations use `other`. Input provenance identifies channel/tab or full video ID. Allowlisted metadata is persisted directly from memory; no raw extraction dump or playback URLs are retained by discovery.

Repeated identical metadata reuses observations. New counters or actual metadata changes can legitimately add observations. Different poll timestamps/input receipts are not metadata revisions. Existing manual decision history is never reseeded or overwritten by discovery. No classification rules changed.

Full enrichment prioritizes recent positions among selected, unverified videos and unresolved Live entries (including broadcasts that may still be ongoing). Already acquired items and manually rejected/skipped items need no enrichment here. Ordinary Videos clips/ambiguous uploads do not all receive expensive full extraction; Review remains Review pending a later bounded review-enrichment policy. Dates in titles, first-seen dates, and discovery timestamps are never publication evidence.

Source release/upload dates and completion flags establish eligibility. Live/upcoming broadcasts are deferred, even if an earlier daily-show identity survives. Missing dates remain unknown unless an existing explicit manual approval overrides the date boundary. Selected uploads such as The Club can use `not_live`; livestreams require `was_live`. Actual acquisition independently rechecks full metadata and the strict 398+251 formats. Unknown readiness or active broadcasts cannot be acquired.

Acquisition selection also considers eligible previously known records, allowing already-discovered missed items to catch up. Existing verified acquisition receipts are skipped. The run sorts unverified candidates oldest-first by known publication date, with unknown-date explicit approvals last. A finite recent scan cannot guarantee discovery after an arbitrarily long outage; hitting a tab cap generates an explicit coverage warning. No historical crawl or backfill is implied.

## Noninteractive execution and evidence

For eventual cron use, change into the absolute project directory and invoke the absolute Python executable above. The database default is absolute. All tools are explicit absolute paths: yt-dlp, the versioned Node 22 binary, FFmpeg, and ffprobe. No `.zshrc`, PATH ordering, nvm initialization, or shell-built yt-dlp command is required. No cron syntax is installed by this step.

One shared advisory lock covers the entire discovery/enrichment/acquisition operation and direct single-video acquisition. An overlapping invocation fails before network work. Subprocess groups are terminated on timeout or catchable interruption, avoiding orphaned FFmpeg/Node children after the operation lock is released. SIGKILL of the Python parent is still not a recoverable shutdown; uncertain attempts require review.

Run evidence lives under `state/runs/<UTC-and-unique-id>/`: timestamped `events.jsonl`, sanitized per-extraction stderr, and `summary.json`. The summary reports tab counts, newly discovered IDs/classifications, enrichments, eligible items, pending/active reviews, verified receipts, deferred items, acquisitions, coverage warnings, and failures. Each event identifies the operation/video where applicable. A failed tab or item is logged and other work is considered; database/system errors may abort the invocation. CLI exit status is 2 if any recorded operation failed, 130 for interruption, and 0 for a clean bounded run. Data and final reporting can still fail independently on full disks or closed output streams; inspect receipts before retrying.

Socket timeout is 30 seconds, tab extraction 300 seconds, full enrichment 180 seconds, and acquisition subprocess 7200 seconds. yt-dlp retries remain zero for the controlled profile. Work is bounded even when a source is unhealthy. A later invocation can make a fresh attempt; no internal acquisition loop or automatic deletion exists.

## Remaining blockers before cron

- Failed/interrupted partials are retained and retries start fresh. Approve a narrowly bounded retention/cleanup policy before scheduling, with the shared lock, age/attempt checks, byte limits, and exclusion of all verified media and manifest-referenced evidence. Current behavior can otherwise accumulate stale partials and exhaust space.
- Add a modest persistent retry delay/failure budget so scheduled retries cannot continually generate new partials for a persistently failing item. No unbounded loop exists inside one run, but cross-run retry pressure is not yet controlled.
- Review tab/enrichment limits and polling frequency for the expected outage window. Cap warnings currently expose possible gaps; there is no persistent coverage watermark or guaranteed recovery beyond the recent window.
- Review operational failure reporting/log retention and actual cron command/environment before enabling it. Logs currently accumulate and require an explicit retention decision.

The smallest next implementation should address bounded stale-attempt retention and retry delay together, with offline failure/recovery tests. A cron entry remains a separate authorization step. Future launchd migration is outside scope.
