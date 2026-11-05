> Current safety status: all six DLS cron commands remain commented out. Shared
> YouTube admission/recovery is implemented offline; no recovery or cron
> restoration has been run. Historical activation schedules below are not active.
> See [YOUTUBE-SAFETY.md](YOUTUBE-SAFETY.md) for state, inspection and recovery.

# Scheduled forward acquisition runner

Cron was activated on October 29, 2026 at 1:01 a.m. Pacific with one labeled entry and four daily checks. Use the same command for a manual run:

```sh
cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 2 --quiet
```

Replace `run` with `discover` for discovery-only. Quiet mode suppresses routine output, including acquisition progress, but reports failures to stderr. Structured evidence remains local. No video ID is needed; `--only-id` remains an optional supervised restriction. This schedule authorizes ordinary forward acquisition, not historical backfill.

## Acquisition naming contract

The [acquisition naming contract](ACQUISITION-NAMING.md) intentionally keeps paths such as `state/acquisitions/MbO7y_90zUI/media.mkv` independent of mutable titles. This is an internal verified acquisition store. Future archive presentation and indexing use stable IDs and metadata; they do not rename or reinterpret these acquisition paths. No acquisition path, manifest, or receipt was changed for naming or presentation.

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

## Installed cron schedule

```cron
0 11,14,18,21 * * * cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader run --streams-limit 25 --videos-limit 75 --max-enrich 8 --max-downloads 2 --quiet
```

The entry is enclosed by `# BEGIN dls-video-downloader (verified forward acquisition)` and `# END dls-video-downloader`. It was verified present exactly once, with all six previous jobs and every original crontab byte preserved. The confirmed system timezone is `America/Los_Angeles`: **11 a.m., 2 p.m., 6 p.m. and 9 p.m. Pacific**, including weekends for catch-up. No timezone environment line was added. A child `TZ` setting must not be assumed to change cron's schedule timezone. DST keeps these wall-clock hours; they are outside the early-morning missing/repeated hour. No existing task environment changed.

Read-only power checks: AC `sleep 0`, display sleep 10 minutes, standby off, automatic restart on, no scheduled sleep/wake events. Automatic system sleep is disabled; display sleep does not prevent execution. Keep the Mac on AC and connected. Manual sleep, shutdown, power or network loss can still miss runs. [Apple documents that cron does not replay asleep/off jobs until the next designated time](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html). Catch-up is provided by this runner's known backlog and subsequent bounded discovery. No power setting changed.

## Operations, Friday checks and rollback

Use the manual command at the top of this document, or `status` / `status --json` above to inspect recent success/failure, pending work and retry delays. Detailed per-run records are under `state/runs/`; fatal diagnostics are in the run directory or `state/runner-fatal.log`. Error delivery before Python starts still depends on cron's stderr/mail handling; verify a real scheduled firing and continue checking status. No indefinitely appended cron-output file is used.

Friday October 30 is the first full unattended day. The daily livestream and The Club are selected/enriched independently. Incomplete live broadcasts are deferred. The late Club upload can be picked up at 6 p.m., 9 p.m., or a subsequent run; an upload after 9 p.m. is caught by the next day's 11 a.m. check. Missing strict AV1/Opus formats cause a visible retry delay, never a fallback. All automatic acquisition requires verified dates on or after October 26. Missed runs catch up through the inventory backlog and bounded channel scans; coverage gaps are reported rather than silently ignored. Keep the Mac on AC, awake and connected.

To disable only this task, use `crontab -e` and remove only the labeled BEGIN/END block and its one DLS command. Preserve every other entry, comment and line. This does not delete acquired files or change inventory decisions. Confirm no active DLS entry remains before doing any manual recovery work.

The complete pre-activation crontab is backed up privately outside Git:

```text
/Users/alejandroveloz/.local/state/dls-video-downloader/cron-backups/crontab-before-dls-20261029T075615522038Z.txt
```

The backup is mode `0600` in a `0700` directory and contains sensitive existing configuration. Do not display it in logs or commit it. To restore that entire snapshot, use stdin (which avoids the long-filename handling issue observed during installation):

```sh
/opt/homebrew/bin/python3 - <<'PY'
from pathlib import Path
import subprocess
backup = Path('/Users/alejandroveloz/.local/state/dls-video-downloader/cron-backups/crontab-before-dls-20261029T075615522038Z.txt')
subprocess.run(['/usr/bin/crontab', '-'], input=backup.read_bytes(), check=True)
PY
```

If other jobs or credentials have changed since backup, disable only the DLS block instead of restoring the old snapshot. Backup checksums and the installed-entry receipt are recorded in ignored `state/cron-activation.json`; it contains no full crontab copy. `state/` (SQLite, acquisitions, manifests, snapshots, staging, logs and validation records), `.test-tmp/`, and local research extracts remain outside Git. The private backup is outside this repository entirely.

Strict 398+251 remains required. Missing AV1 is delayed and reported, never silently substituted. Receipt and coverage problems require operator review. A future launchd migration is outside scope.

## Activation validation — October 29

The exact runner command was invoked manually in a minimal noninteractive environment at 12:56 a.m. Pacific. Run `2026-10-29T075640.198004+0000-87fb1953` completed successfully at 12:59 a.m.: 25 Live entries, 75 Videos entries, no new IDs, eight full enrichments, zero acquisition attempts and zero failures. No eligible missing Thursday broadcast was identified in those bounded results at that early hour; no ID was forced and there was no polling loop. The three existing acquisitions were skipped and the historical approval was explicitly deferred. A future scheduled check will handle Thursday's show when it is discovered, dated and completed.

Cron installation was verified at 1:01 a.m. Pacific. The initial file-based attempt failed without changing the original crontab; the final stdin installation and verification succeeded. All previous bytes are preserved. The manual runner test is **not** evidence that cron fired: no actual scheduled execution had occurred when activation was checked at 1:02 a.m.; the first scheduled check is Thursday at 11 a.m. Inspect its result before departure.

After the manual run, the inventory has 400 IDs, 437 observations, four unchanged manual decisions, and the same three acquisitions. New observations reflect bounded discovery and enrichment, not additional media. Existing media SHA-256, sizes, inodes, manifests and acquisition receipts match the pre-activation baseline. No acquisition naming logic, source code, format or classification rule was changed in this activation step. This activation and naming documentation is recorded in a separate documentation checkpoint.

## Prior readiness validation (before activation)

All **82 offline tests passed**, including persisted cooldown/recovery, repeated-failure backoff, stale/active/linked/evidence cleanup, real concurrent processes, inherited lock lifetime, coverage gaps, Club enrichment behind 70 clips, strict scheduled dates, manual historical exceptions, per-run limits, bounded logs, and exit reporting. Final safety-review tests additionally cover atomic migration rollback/retry, retry visibility in a separate process, UTF-8-safe log truncation, fatal programming errors and secondary reporting failures, quiet pre-initialization failure diagnostics, and explicit FFmpeg/Node paths for discovery.

A minimal-environment real discovery-only run (`--streams-limit 25 --videos-limit 75 --max-enrich 0`) completed successfully at `2026-10-29T06:59:22Z`: 25 Live entries, 75 Videos entries, zero new IDs, zero acquisitions, zero failures. Its result is `state/runs/2026-10-29T065857.588008+0000-87b68be1/summary.json`. It established both scan frontiers. Existing historical approval was visibly deferred by forward-only policy. Six changed flat payloads added observations (417 → 423); all previous observations, initial titles, decisions, classification history and known metadata fields remain intact. Classification totals remain 84 daily shows, 19 The Club, 92 Skip and 205 Review.

There are still 400 IDs, four manual decisions, and three verified acquisitions. October 26–28 media sizes/inodes/SHA-256 and manifest bytes match the pre-change baseline; acquisition/attempt receipts are identical. No production files were deleted. The existing crontab checksum matches. Source classification rules, format requirements, forward boundary, system/runtime settings and sibling projects were not changed. The final safety review performs no additional live discovery or acquisition; the readiness checkpoint is committed separately from cron activation.

### Post-activation check — October 29, 1:14 a.m. Pacific

The installed crontab checksum still matches the activation receipt, the original pre-activation content remains an exact prefix, and the DLS command occurs exactly once. Only the two known manual run records exist; no scheduled invocation has occurred yet. The first check remains due at 11 a.m. Pacific. The latest manual run recorded no new IDs, no downloads and no failures, with no pending retry state. No additional discovery or acquisition was triggered for this check.

## Forward metadata discovery and supplemental cycle

```sh
# Read-only plan: no network, locks, logs, stores or operational writes.
python3 -B -m dls_video_downloader cycle --json

# Explicit comments from existing inventory, without discovery.
python3 -B -m dls_video_downloader cycle --execute --json

# Explicit forward metadata discovery followed by independent due comments.
python3 -B -m dls_video_downloader cycle --execute --discover --json

# Metadata only; writes inventory when explicitly invoked.
python3 -B -m dls_video_downloader discover-morning --json
```

`--discover` alone is rejected. Global `--db PATH` precedes the command;
`--store-root PATH` selects the separate comment store. None of these commands
starts or waits for media acquisition. The established video `run` and `discover`
commands, their `--max-enrich` option, cleanup, retries and cron are unchanged.
The supplemental schedule below is installed. The corrected forward path passed
383 offline tests and one supervised live cycle; unattended success is not yet
verified.

### Comprehensive forward enrichment

Supplemental discovery no longer has a two/four-slot enrichment quota, content
priority queue, or hypothetical completed-classification ranking. It attempts
**every pending forward candidate once** unless safety conditions defer it. Oldest
forward source evidence goes first, so a known current broadcast precedes newer
mini-broadcasts without reserving a small number of slots.

Forward scope uses a 15-day recent-evidence window, bounded by the existing forward
start date, and includes:

- IDs currently presented in the bounded channel heads, if publication is unknown.
- IDs with reliable recent publication/release evidence, including those needing
  first full metadata outside the current head.
- IDs recently presented by this bounded forward workflow whose metadata is still
  incomplete, even if newer mini-broadcasts displace them from the head.
- Undated videos with recently observed *full* upcoming/live/post-live status.

Known old publication evidence always wins over a recent presentation. Known dates
more than 15 days ahead are deferred outside the forward horizon. Legacy inventory
`first_seen`, mutable counters, title dates and flat `was_live` labels are not proof
of publication age. In particular, old undated flat-only records outside the recent
heads do not enter forward enrichment. They remain preserved historical backlog,
with a count/sample in reports and full evidence still available in inventory.
No historical backfill command or automatic history scan is added.

Bounded-head provenance is recorded in the existing `sources` table as
`morning:live` / `morning:videos`, alongside the original `live` / `videos` source.
Alias times describe extraction observations, not publication. This lets unfinished
current work survive head changes without a second database or persistent work
queue. Original first-seen values, immutable observations and manual decisions are
preserved. The importer accepts this optional provenance; existing video-runner
calls retain their original behavior. Classification semantics are unchanged.

Full metadata is requested for all scoped IDs lacking adequate verified metadata,
including unselected mini-broadcasts. Explicit manual Skip/Reject decisions exclude
metadata enrichment and are reported separately. Successful upcoming/live/post-live
metadata is refreshed on later cycles until it resolves. Adequately complete means
matching official identity, full consistent `was_live` or `not_live` evidence, and
usable original publication evidence. These completed IDs are not routinely
re-enriched unless the current listing contradicts their status. Missing status or
publication evidence remains unresolved, never invented.

Flat status is only a hint. It cannot establish daily-show completion. Full
validated `was_live` metadata can make the existing comment planner select a daily
show without an MKV. The Club remains a normal-upload case. No duration threshold,
classification rule, comment cadence, confirmation rule or publication cutoff has
changed. A first check of an undated current listing can reveal an old publication;
that evidence is preserved and the ID then leaves forward scope. This is not an
invitation to enrich other historical IDs.

### Protective limits and retry state

Defaults: **10 Streams entries**, Videos disabled, a **180-second metadata budget**,
60 seconds per extractor process, 30-second socket timeout, and zero internal
retries. `--streams-limit` accepts 1–25; `--videos-limit` accepts 0–25 (for example,
10 when upload discovery is wanted). An **emergency bound of 20 full-metadata
requests** protects abnormal workloads, separately from the one/two listing calls.
`--max-metadata-requests` accepts 1–100; it is not intended as a routine small quota.
`--max-enrich` is removed from supplemental commands, but remains on video commands.

The cycle defaults to one shared 600-second network deadline, metadata cap 180,
comments cap 600 reduced to remaining time, two comment videos and 1,000 comments
per video. Options remain `--runtime-seconds` (30–600), `--discovery-seconds`
(30–180), `--comment-seconds` (30–600), `--max-videos` (1–10),
`--max-comments` (1–1,000), and `--start-date`. Standalone metadata runtime accepts
30–180 seconds. Components receive absolute deadlines so allowances cannot reset.
Local bounded SQLite waits, persistence and process cleanup can finish afterward;
network work cannot start without sufficient allowance.

`state/morning/retry.json` stores **failure evidence only**, not a candidate queue.
Failures/unusable metadata back off 1 hour, then 6 hours, then 24 hours; success
clears that ID's failure counter. Valid active status does not create failure
backoff. Blocking stops remaining requests and establishes a six-hour metadata
cooldown. Cycles honor metadata and comment cooldowns even in inventory-only mode.
Invalid or unpersistable safety state fails closed; do not repeatedly invoke after
blocking. Retry state is atomic, size-bounded to 256 KiB, and independent from the
video runner's SQLite retry state. It is not a new database or preservation layer.

`forward_coverage.complete` certifies only that the requested bounded heads and
known forward candidates were checked, not exhaustive channel coverage or terminal
video status. Backoff, failed listings, failures, emergency limits and deadline
deferrals explicitly make coverage incomplete, with unattempted/failed IDs.
Discovery does not report clean success while known current candidates remain
unchecked. Subsequent cycles recompute remaining work from source evidence.

The November 5 regression includes its previously live daily show, three new mini-
broadcasts, and the old undated rows that previously consumed both slots. All four
current videos now receive full metadata under normal bounds; historical rows are
not requested. A 25-candidate test reaches the emergency bound, reports five
unattempted IDs, and completes those five on the next cycle.

### Phase isolation, concurrency and reports

Reports retain separate discovery/comments outcomes, reasons, durations, allocated
budgets, attempts and artifact references. Transient discovery failure or partial
coverage does not discard independently eligible known-inventory comments. Blocking,
shared cooldown, unsafe persistence or interruption suppresses later network work.
A successful comment phase does not turn failed discovery into success, and comment
failure does not invalidate imported metadata. Operational success does not mean
exhaustive comment retrieval; all existing coverage/confirmation semantics remain.

Execution takes `state/supplemental/.cycle.lock` nonblockingly. Discovery uses the
existing shared inventory/media operation lock; comments retain their own locks.
The media lock is released between phases and never held throughout comment
retrieval. Active acquisition defers without waiting for an MKV. Morning discovery
retains its runtime+60-second guard before known 11/14/18/21 Pacific cron firings,
and 60 seconds afterward. The comment acquisition probe still cannot exclude
acquisition beginning after the probe. No video lock semantics or cron changed.

Read-only planners use independent SQLite read transactions with one-second busy
waits. Supplemental inventory writes also use one second; video connections retain
their original five-second default and comment-store writes their five-second
bound. Contention defers or fails visibly without unbounded retries. No schema,
journal or shared-transaction changes are introduced; metadata discovery requires
an initialized schema-3 inventory.

Execution logs remain `state/morning/last-run.json`, bounded diagnostic slots,
`state/comments/runner/last-run.json`, and `state/supplemental/last-cycle.json`.
Metadata diagnostic slots are `enrich-0..99.stderr.log` at most, according to the
emergency bound; used slots are replaced and unused slots may be older. These are
latest operational reports, not archive manifests. Immutable source observations,
comment snapshots and portable collection records remain authoritative. Planning
creates none of these files. Captions can later be another named phase; no caption
schema, collector, shared manifest or cross-project integration is implemented.

Cycle exit codes: 0 planning/success/ordinary safety deferral, 2 incomplete or failed
phases, 75 supplemental overlap, 130 interruption, 1 setup/reporting failure. Phase
results retain their own codes. Unchecked forward work cannot yield clean success.

The installed unified-cycle jobs below replace the earlier proposed separate
morning/comment timings. Confirmation eligibility is measured from collection
completion, not schedule slots: the next scheduled cycle may run later than +24h,
but never early or after the original cutoff. Archiver owns backup; Indexer owns
interpretation.


## Installed supplemental schedule — November 5, 2026

The existing video cron remains exactly unchanged at 11/14/18/21 Pacific. The five
entries below use only the unified cycle: discovery Friday 08:35 and weekdays
09:25/10:15; inventory-only weekends 10:15 and daily 19:30.

```cron
# BEGIN dls-video-downloader (supplemental cycle)
35 8 * * 5 cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader cycle --execute --discover --max-videos 2 --max-comments 1000 --runtime-seconds 600 --discovery-seconds 180 --comment-seconds 600 --streams-limit 10 --videos-limit 0 --max-metadata-requests 20 --json > /dev/null
25 9 * * 1-5 cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader cycle --execute --discover --max-videos 2 --max-comments 1000 --runtime-seconds 600 --discovery-seconds 180 --comment-seconds 600 --streams-limit 10 --videos-limit 0 --max-metadata-requests 20 --json > /dev/null
15 10 * * 1-5 cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader cycle --execute --discover --max-videos 2 --max-comments 1000 --runtime-seconds 600 --discovery-seconds 180 --comment-seconds 600 --streams-limit 10 --videos-limit 0 --max-metadata-requests 20 --json > /dev/null
15 10 * * 0,6 cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader cycle --execute --max-videos 2 --max-comments 1000 --runtime-seconds 600 --discovery-seconds 180 --comment-seconds 600 --streams-limit 10 --videos-limit 0 --max-metadata-requests 20 --json > /dev/null
30 19 * * * cd /Users/alejandroveloz/Sites/dls-video-downloader && /opt/homebrew/bin/python3 -B -m dls_video_downloader cycle --execute --max-videos 2 --max-comments 1000 --runtime-seconds 600 --discovery-seconds 180 --comment-seconds 600 --streams-limit 10 --videos-limit 0 --max-metadata-requests 20 --json > /dev/null
# END dls-video-downloader (supplemental cycle)
```

The Mac's `/etc/localtime` resolves to `America/Los_Angeles`; cron uses that system
local timezone. No `TZ`/`CRON_TZ` setting or original job environment was changed.
Local times stay Pacific across PST/PDT: 10:15 corresponds to 18:15 UTC in winter
and 17:15 UTC in summer. All supplemental times are outside the 2 AM DST change.
Keep the system timezone Pacific. The 10:15 cycle's 600-second network deadline
ends by 10:25, leaving 35 minutes before the 11 AM media job, plus ample margin for
bounded local persistence/cleanup. Existing acquisition locks/cron guards still
apply; an active long video acquisition causes deferral, not a wait for its MKV.

Each invocation permits at most two comment videos, 1,000 comments each, one shared
600-second network budget, 180 seconds of metadata, 10 Streams entries, no Videos
tab scan and the emergency 20 full-metadata ceiling. Inventory-only lines do not
invoke discovery despite retaining the same explicit limits. State, cooldowns,
phase independence and publication/confirmation policy remain as documented above.

Stdout JSON is discarded rather than appended to an unbounded cron log. Latest
bounded operational reports remain under `state/supplemental/`, `state/morning/`
and `state/comments/runner/`; stderr remains available through normal cron error
handling. Inspect `last-cycle.json` for a new `started_at`, overall/phase outcomes,
forward coverage, unattempted IDs, failures, budgets and artifact references.
Inspect `state/morning/retry.json` and `state/comments/runner/cooldown.json` when
present. Catalog attempts and portable comment records retain independent source
history even when later reports replace the latest summary.

Read-only inspection:

```sh
python3 -B -m dls_video_downloader cycle --json
python3 -B -m dls_video_downloader status --json
```

Read the latest execution report directly for actual scheduled results; planning
is not execution evidence. Check report freshness and cron stderr/mail if the
report remains stale. To establish successful unattended operation, observe new
reports starting at installed times, inspect both phases and coverage/cooldowns,
verify any new portable artifacts, and check independent video cron success.
The successful November 5 live cycle was supervised, not scheduled. No future
scheduled firing was awaited or manually triggered during installation.

To disable supplemental work, use `crontab -e` and remove only the exact block
between `# BEGIN dls-video-downloader (supplemental cycle)` and its matching END.
Leave the original verified-forward-acquisition block and every other entry intact.
The private pre-supplemental backup is:

```text
/Users/alejandroveloz/.local/state/dls-video-downloader/cron-backups/crontab-before-supplemental-20261105T181733Z.txt
```

It is mode 0600 and contains unrelated configuration: keep it outside Git and do
not display its contents. Prefer removing only the supplemental block over a full
restore that could discard later unrelated edits. No new daemon/wrapper, caption
collector, backfill, UI, shared archive manifest or cross-project integration was
installed. Future captions will be an independent asset/phase within this cycle.

## Opt-in captions — offline-tested, not activated

The supplemental cycle now plans an independent caption phase after comments.
Default `cycle --json` remains read-only. Existing installed `cycle --execute`
and `cycle --execute --discover` commands **do not collect captions**. Future
supervised execution must explicitly add `--captions`; this milestone does not
change any installed cron line. See [CAPTIONS.md](CAPTIONS.md) for commands, source
identity, immutable formats, retry/confirmation rules and rollout boundaries.

Caption defaults are one video, 180 seconds, two source formats, and the existing
shared 600-second overall deadline. `--max-caption-videos` permits 1–2;
`--caption-seconds` permits 30–300; `--caption-root` selects independent state.
Comments run first without reserving time for captions. Remaining time, cooldowns,
fresh eligibility and acquisition contention are rechecked before requests. All
three phases retain independent outcomes and source histories. The media runner,
its acquisition lock semantics and its cron commands remain unchanged.

Inspect the existing supplemental latest report, the separate caption runner
latest report, portable caption collection JSON, and read-only caption plans.
Do not infer transcription completeness from operational success or completion of
monitoring. Removing the opt-in caption flag later disables only caption work;
there is no separate caption cron job or scheduler to manage.

## Shared YouTube safety

Acquisition remains independent, but now shares network blocking with metadata,
comments and captions. The guard runs before video CLI inventory initialization
and before each network subprocess; global deferral does not change selection,
398+251 formats, manifests or local asset eligibility. Already admitted transfers
are not destructively interrupted. Expired cooldown requires a separately
approved coordinated metadata probe, never automatic per-job probes.
Read `youtube-safety status --json` and the immutable event journal; follow the
[shared operational guide](YOUTUBE-SAFETY.md). Keep cron paused through review.

## Explicit operational alert delivery (not scheduled)

The [DLS Pushover adapter](NOTIFICATIONS.md) consumes supported shared-safety
journal events. `notifications plan/status/run` are read-only by default; delivery
requires `--execute`. No media/supplemental runner invokes it automatically, and
provider failures cannot block archival evidence. All six existing DLS jobs remain
paused. Credentials are external/private and have not been accessed or provisioned
by this milestone. One separately authorized connectivity test precedes rollout.

The first supervised Pushover connectivity test was accepted and independently
confirmed on the iPhone. Private configuration is provisioned; checkpoint/status
operations do not access credentials. No notification schedule or automatic
incident delivery is enabled, and all six DLS cron entries remain paused.
