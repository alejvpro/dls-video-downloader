# Shared YouTube blocking and controlled recovery

**All six DLS cron command lines remain paused. No recovery, live request, safety
migration or cron restoration was performed during this implementation.** Caption
execution remains an additional explicit opt-in. This mechanism changes network
admission, not selection, formats, comment cutoff or caption confirmation policy.

## State and immutable incident evidence

The single repository-local safety root is `state/youtube-safety/`, ignored by
Git. `events/000000000001-EVENT_ID.json` and subsequent immutable version-1 JSON
events form the authoritative journal. Each contains `event_id`, UTC `at`, event
kind, `alertable`, and the complete `state_after`. The latest numbered committed
event is the current state; there is no separately mutable cache to diverge.

State includes incident ID/count, total and related blocking observations, first
and latest observation, component/operation, reason, retry eligibility, manual
review requirement, policy interval, authorized recovery token/status, last
successful recovery observation, and hashes/provenance of acknowledged legacy
restrictions. Shared evidence contains classified labels, not raw diagnostics,
URLs, tokens, cookies, credentials or response headers. Full sanitized diagnostic
evidence stays in existing asset/run records. One request ID is deduplicated in a
bounded recent-ID set; merely deferred jobs do not create more incidents.

Updates take a short advisory file lock with a one-second bound. Event bytes are
written to a private temporary file, flushed/fsynced and atomically linked under
a unique immutable name, then the directory is fsynced. Interrupted uncommitted
temporary files are ignored. A completed link is a complete committed event.
Concurrent readers see a complete prior or new event; concurrent writers serialize.
Invalid state/configuration fails closed. If persistence fails after confirmed
blocking, a process-local latch prevents that invocation from issuing more work;
operator review is required. A disk failure cannot communicate an unwritten event
to another process: preserve fatal diagnostics and keep jobs paused in that case.

## Classification and cooldown policy

- Explicit `LOGIN_REQUIRED`, bot/human verification, CAPTCHA, authentication
  requirements or HTTP 401: authentication challenge. Confirmed IP/request access
  restrictions also block. Initial local delay is **24 hours**, not a claim about
  YouTube's restriction duration.
- Explicit HTTP 429 / too-many-requests / rate-limit evidence: rate limiting.
  Valid positive Retry-After seconds or HTTP date are honored (minimum 60 seconds,
  supported range up to 30 days). Otherwise related observations use bounded
  **1h, 6h, then 24h** backoff. Invalid/past/unreasonable values use that fallback.
- Bare HTTP 403, 402, timeout/reset, 5xx, individual unavailable video and malformed
  caption payload: no automatic global block. Existing local bounded retries
  continue. A 403 accompanied by explicit bot/access restriction evidence is
  classified using that evidence.

`DLS_YOUTUBE_BOT_COOLDOWN_HOURS` optionally configures the local bot interval
(valid integer 1–168; default 24). Invalid configuration prevents network admission.
This implementation does not change deployed environment or cron configuration.

The states exposed to operators are `normal`, `cooldown`, `recovery_eligible`,
`recovery_in_progress`, and `manual_review`. Expiry changes cooldown to recovery
eligible in the read-only view; **it never grants routine network access**.
Recovery eligibility still requires explicit manual authorization. A repeated bot
challenge during recovery requires a further manual-review acknowledgment before
another controlled recovery. Repeated rate limiting retains bounded cooldown and
manual authorization without incorrectly labeling it a bot challenge.

## Inspection and future authorized actions

Read-only, no runtime initialization:

```sh
python3 -B -m dls_video_downloader youtube-safety status --json
python3 -B -m dls_video_downloader youtube-safety events --json --limit 50
python3 -B -m dls_video_downloader cycle --json
```

The following are implemented capabilities, **not authorization to run them now**:

```sh
# Explicitly journal original component-local restrictions without rewriting them.
python3 -B -m dls_video_downloader youtube-safety import-legacy --execute --json
# One durable review-due event per unresolved incident; no network.
python3 -B -m dls_video_downloader youtube-safety review-events --execute --json
# One coordinated, bounded metadata-only probe after retry eligibility.
python3 -B -m dls_video_downloader youtube-safety recover VIDEO_ID --execute --authorize --json
# Only after reviewing a repeated challenge; does not bypass an active cooldown:
# ... recover VIDEO_ID --execute --authorize --reviewed --json
```

Without `--execute`, these commands only inspect status. Recovery additionally
requires `--authorize`; it never collects assets or changes cron. The recovery
lock is nonblocking/bounded and held only over that one probe, with the descriptor
inherited by the extractor so a surviving child retains ownership. Routine jobs
remain blocked while recovery runs. At most one extractor invocation is admitted;
it has a 60-second process budget and zero retries. Returned video identity,
completed status, successful extraction and absence of challenge evidence are
required before recording recovery. Probe timeout/interruption/malformed metadata
keeps admission closed, with a one-hour delay before a newly authorized attempt.
A killed process leaves a closed in-progress state; a later explicit recovery can
reclaim it only after acquiring the OS lock. There is no automatic recovery loop.

A successful metadata probe is an observation about that request, not proof that
all media/comment/caption endpoints work. Validate one controlled asset operation
next, then review before restoring jobs. No cookies, proxies, client overrides,
CAPTCHA workarounds or authentication setup are implemented.

## Legacy restrictions

Known comment/caption runner `cooldown.json` and morning `retry.json` restrictions
are projected read-only even before shared state is initialized. Original files,
observed times, hashes and deadlines are retained. Still-active restrictions are
honored; original November 5 caption evidence is not retroactively extended to
24 hours. Even an expired legacy restriction requires coordinated recovery, not
silent reset to normal. Explicit import or recovery journals the source evidence.
Successful authorized recovery acknowledges exact legacy hashes without deleting
or modifying them. A changed/new local restriction is honored again. A new
legacy restriction arriving during a probe prevents that probe from clearing
admission; it requires a new authorization after the restriction. Component
files remain compatibility evidence; the shared journal controls admission.

## Enforcement and reporting

Both network launchers consult the shared policy: acquisition/discovery's external
command helper and the bounded comment/caption subprocess helper (including the
caption timedtext worker). Their callers establish the same inventory-state scope;
custom asset output directories do not bypass it. Outside a scope, low-level
network launchers use this repository's state root. Existing per-runner locks,
acquisition priority and per-asset histories remain separate.

Coverage includes both channel tabs, full enrichment, morning status refresh,
media preflight and AV1/Opus download, comments, caption listings, caption response
bodies and controlled recovery. FFprobe/local verification is not network-gated.
New operations check admission again at practical subprocess/request boundaries.
Already admitted work is not destructively killed when another process blocks;
completed local verification/publication may finish. There is an unavoidable gap
between an admission read and process launch. Internal requests made by an already
running yt-dlp process are not individually intercepted. No global lock is held
across an MKV transfer; stronger exclusion would require different coordination.

Blocked video CLI commands defer before inventory initialization/migration. Video
runs interrupted by a new block retain independent run/attempt evidence, stop
before the next tab/enrichment/acquisition, and avoid treating global deferral as
an asset-local retry failure. Supplemental phases distinguish shared deferral,
ordinary failure, partial coverage and success. Plans expose `youtube_safety`,
`deferred_due_ids` and `policy_due` so due-but-blocked work is not confused with an
empty queue. Caption execution still requires `--captions`; no schedule is resumed
by successful recovery or any safety command.

To pause execution without source edits, leave/remove only the DLS executable
cron lines as already authorized; preserve unrelated jobs and backups. Recovery
is not a cron-management operation. Future historical enumeration/backfill must
enter the same inventory-state safety scope and use the guarded launchers; it
must not build a second safety store or bypass recovery admission.

## Alert adapter boundary

Events identify new incidents/cooldown activation, recovery authorization,
controlled recovery success, repeated block/manual review, repeated rate limiting,
incomplete recovery and unresolved review points. New incidents/recovery/manual
review transitions are alertable; related observations do not repeatedly alert.
`review_overdue` is available without writes; an explicit `review-events` tick
materializes one deduplicated review event. A future adapter can retain its own
last-delivered sequence/event IDs and consume immutable records. No notification
transport, delivery credentials, Pushover, Healthchecks, email or webhook exists.
