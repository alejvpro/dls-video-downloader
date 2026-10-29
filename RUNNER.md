# Cron-ready bounded runner — not scheduled yet

No cron entry is installed and unattended downloading is not activated. Use the existing CLI and inventory:

```sh
cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 2 --quiet
```

Replace `run` with `discover` for discovery-only. Quiet mode suppresses routine output, including acquisition progress, but reports failures to stderr. Structured evidence remains local. No video ID is needed; `--only-id` remains an optional supervised restriction. Do not enable acquisition until separately authorized.

## Forward policy and coverage

The runner requires a **verified release/upload date on or after 2026-10-26**, even for manually approved videos. Pre-window and undated approvals remain intact but cannot authorize unattended acquisition. The explicit `download VIDEO_ID` command still permits approved historical exceptions, subject to readiness, strict formats and retry delays. No individual ID is hardcoded into this policy. Content rules are unchanged; Skip and unresolved Review are excluded unless effectively approved, and approval cannot bypass the runner's date requirement.

Defaults: **25 Live entries, 75 Videos entries, eight full enrichments, two acquisition attempts**. Limits are configurable: 1–200 entries per tab, 0–20 enrichments, 1–3 downloads. `--per-tab N` overrides both tab limits. Failed attempts consume download slots; remaining candidates are reported as deferred.

Research places successive Club uploads at Videos positions 7, 21, 34, 48 and 63. Ten entries cannot cover even one full observed week. Seventy-five gives several weeks of observed headroom without crawling channel history. Full enrichment reserves the first opportunities for a daily show and The Club; known pre-window, verified and cooldown items do not consume enrichment slots. Ordinary clips are not all enriched. Titles/discovery times never establish publication dates.

Each successful scan stores its head ID. A subsequent bounded scan must contain that head to establish continuity. If missing, a coverage failure is reported and the previous head is retained. Known eligible backlog is still considered. Longer outages or a removed head can require operator review and a larger bounded scan, rather than silently claiming complete coverage. Initial scans report their cap and establish a baseline; no earlier continuity is invented. No historical backfill is implemented.

## Persistent retries

Schema 3 adds `retry_state`, `runner_runs`, `scan_frontiers`, and `cleanup_events`, preserving original inventory/acquisition records. The version-2-to-3 migration is atomic and rechecks the version under its write lock; a failed migration rolls back and can be retried. Failed acquisition attempts record reason and time. Retry state is per video and operation (`enrichment`/`acquisition`) and survives process restarts.

| Category | Initial delay |
| --- | --- |
| Network/unknown transient; interruption | 60 minutes |
| Missing strict formats; insufficient storage | 6 hours |
| Unavailable/private/restricted video; verification/recovery problem | 24 hours |

Consecutive failures double the category delay, capped at 24 hours by default. Configure `--retry-base-minutes` and `--retry-max-hours` when necessary. Categories are conservative diagnostic heuristics. There is no retry loop within a run; a later invocation can retry when due. Success clears that operation's state. Enrichment success does not clear acquisition failures. Verified acquisitions are skipped and never redownloaded. Verification conflicts continue refusing replacement; waiting does not repair them.

## Safe staging retention

Inspect without deleting:

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader cleanup
```

`cleanup --execute` applies the plan. `--stale-hours`, `--max-files`, and `--max-bytes` configure it. The runner applies the same policy under its shared lock. Defaults: **72 hours**, minimum 24 hours; at most **100 files / 5 GiB per invocation**. Retries still start fresh; partial resumption is not implemented.

Only database-owned `state/acquisitions/.work/<video-id>/<attempt-uuid>/` paths qualify. Incomplete attempts and every file must be old enough according to attempt timestamps and mtime/ctime. Symlinks, subdirectories, unknown media filenames, multi-link files, verified media inodes, completed attempts and manifest-referenced evidence are excluded. Unreadable acquisition receipts fail closed. File identity/stat data is rechecked before unlinking. No tree is recursively deleted.

Failed-attempt metadata and diagnostic logs are retained for 30 days; only known unreferenced filenames then qualify under the same checks. Failure reasons/times remain in SQLite; the latest 5,000 cleanup actions are retained. Stale abandoned `running` attempts can be marked interrupted only while holding the global lock, with a retry delay recorded. Unknown/unsafe leftovers and uncertain publication require review. October 26's referenced legacy evidence is protected.

The production audit found **no incomplete attempts or partial files**. Real cleanup validation was dry-run only and removed nothing. Destructive regression tests use tiny disposable workspace fixtures.

## Locking, timeouts and logs

One lock covers discovery, enrichment, retention and acquisition, including direct downloads. Overlap exits 75 with a timestamped message and a small SQLite receipt when available. Subprocesses inherit the lock descriptor, so surviving yt-dlp processes keep blocking another run if the Python parent disappears. Catchable interruption or timeout terminates the process group, including FFmpeg/Node. SIGTERM is handled as interruption. SIGKILL/power loss still requires subsequent state checks.

Timeouts: socket 30 seconds; tab extraction 300 seconds; enrichment 180 seconds; acquisition subprocess 7,200 seconds. yt-dlp internal retries remain zero. Absolute executables: `/usr/local/bin/yt-dlp-new`, `/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node`, `/opt/homebrew/bin/ffmpeg`, `/opt/homebrew/bin/ffprobe`. Python and the database path are absolute. No interactive initialization or PATH ordering is needed.

Timestamped `state/runs/` events/summaries record start/finish/status, tab counts, new IDs, selections, reviews, receipts, acquisitions, deferrals, retry times and failures. Diagnostics are sanitized and capped at 256 KiB each; event logs are capped near 256 KiB. Registered terminal run directories retain at most 30 days, 100 runs or 64 MiB, plus the current run. Run completion trims SQLite history to 200 terminal summaries; between normal runs, overlap receipts are separately capped at 50 (at most 250 terminal records). Completed acquisition evidence is never pruned as runtime logs. Two legacy supervised-run directories predate registration and remain preserved.

```sh
/opt/homebrew/bin/python3 -B -m dls_video_downloader status
/opt/homebrew/bin/python3 -B -m dls_video_downloader status --json
```

Status shows recent runs, verified acquisition count, forward pending IDs and retry records. Cooling-down/unresolved items can remain on a clean run and are visible as deferred. Exit codes: **0** clean bounded run, **2** partial operation/coverage failure (also argparse usage errors), **1** fatal error, **75** overlap, **130** interruption. Quiet failures go to stderr; inspect status even if cron mail is unavailable.

Unexpected programming/database errors propagate as fatal failures rather than creating transient item retries. Fatal diagnostics include exception type and stack file/line/function locations, without source lines or locals. They are sanitized and retained in the run's bounded `fatal.stderr.log`. Failures before run-log initialization remain visible on stderr and, when possible, overwrite the bounded `state/runner-fatal.log` fallback (at most 256 KiB). `--quiet` suppresses neither. Read-only status/report commands never write this fallback. Use these existing bounded logs plus cron's normal error delivery; do not append stderr indefinitely to a separate file. If local logging fails, stderr still reports the original error.

## Proposed cron schedule — not installed

```cron
0 11,14,18 * * * cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 2 --quiet
```

The confirmed system timezone is `America/Los_Angeles`: **11 a.m., 2 p.m. and 6 p.m. Pacific**, including weekends for catch-up. No timezone environment line is needed or proposed. A child `TZ` setting must not be assumed to change cron's schedule timezone. DST keeps these daytime wall-clock hours; they are outside the early-morning missing/repeated hour. No existing task environment changes.

Read-only power checks: AC `sleep 0`, display sleep 10 minutes, standby off, automatic restart on, no scheduled sleep/wake events. Automatic system sleep is disabled; display sleep does not prevent execution. Keep the Mac on AC and connected. Manual sleep, shutdown, power or network loss can still miss runs. [Apple documents that cron does not replay asleep/off jobs until the next designated time](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html). Catch-up is provided by this runner's known backlog and subsequent bounded discovery. No power setting changed.

## Activation sequence

1. The cron-readiness checkpoint includes the final safety review. Deployment still requires the separate authorizations below.
2. Authorize Thursday October 29's supervised real discovery/acquisition. Confirm the new daily show is recognized, eligible, verified and repeat-safe; inspect reviews/format failures.
3. Separately authorize installation of the single cron entry, preserving all existing entries. Verify a real Thursday cron invocation/environment before departure and confirm access to status/failure reporting.
4. Keep the Mac awake/on AC and connected Friday October 30. Review the 11 a.m., 2 p.m. and 6 p.m. outcomes for the daily show and late Club upload. An upload after 6 p.m. is caught Saturday at 11 a.m.; this is not immediate monitoring.

Strict 398+251 remains required. Missing AV1 is delayed and reported, never silently substituted. Receipt and coverage problems require operator review. A future launchd migration is outside scope.

## Readiness validation

All **82 offline tests passed**, including persisted cooldown/recovery, repeated-failure backoff, stale/active/linked/evidence cleanup, real concurrent processes, inherited lock lifetime, coverage gaps, Club enrichment behind 70 clips, strict scheduled dates, manual historical exceptions, per-run limits, bounded logs, and exit reporting. Final safety-review tests additionally cover atomic migration rollback/retry, retry visibility in a separate process, UTF-8-safe log truncation, fatal programming errors and secondary reporting failures, quiet pre-initialization failure diagnostics, and explicit FFmpeg/Node paths for discovery.

A minimal-environment real discovery-only run (`--streams-limit 25 --videos-limit 75 --max-enrich 0`) completed successfully at `2026-10-29T06:59:22Z`: 25 Live entries, 75 Videos entries, zero new IDs, zero acquisitions, zero failures. Its result is `state/runs/2026-10-29T065857.588008+0000-87b68be1/summary.json`. It established both scan frontiers. Existing historical approval was visibly deferred by forward-only policy. Six changed flat payloads added observations (417 → 423); all previous observations, initial titles, decisions, classification history and known metadata fields remain intact. Classification totals remain 84 daily shows, 19 The Club, 92 Skip and 205 Review.

There are still 400 IDs, four manual decisions, and three verified acquisitions. October 26–28 media sizes/inodes/SHA-256 and manifest bytes match the pre-change baseline; acquisition/attempt receipts are identical. No production files were deleted. The existing crontab checksum matches. Source classification rules, format requirements, forward boundary, system/runtime settings and sibling projects were not changed. The final safety review performs no additional live discovery or acquisition; the readiness checkpoint is committed separately from cron activation.
