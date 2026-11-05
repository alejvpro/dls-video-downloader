# DLS Pushover alerts — offline-tested, not activated

One supervised connectivity test was accepted by Pushover on November 5, 2026,
and the user independently confirmed visible receipt on the iPhone. Private
configuration was provisioned during that authorized test. All six DLS cron
commands remain paused. This sender is explicitly invoked; neither
archival operations nor the supplemental cycle calls it automatically. No new
scheduler or notification service is installed.

## Responsibilities and supported messages

The YouTube safety journal remains incident evidence. `notifications.py` maps its
supported events to fixed DLS alert templates, independently determines delivery
eligibility, and maintains notification history. `pushover.py` implements only
the documented HTTPS message transport. Notification failure cannot block media,
comment or caption preservation and cannot change a YouTube cooldown.

| Existing safety event | Level | Deduplication scope |
| --- | --- | --- |
| New explicit authentication/access challenge | Critical | Initial alert per incident |
| New rate limit or unknown imported legacy restriction | Warning | Initial alert per incident |
| Imported legacy evidence establishing authentication challenge | Critical | Initial alert per incident |
| Repeated challenge requiring manual review | Critical | First escalation per incident |
| Unresolved incident past its review point | Warning | Review alert per incident |
| Verified controlled recovery | Informational | Recovery observation per incident |
| Rate-limited or incomplete controlled recovery | Warning | Distinct recovery event |

Routine blocking observations and recovery authorization do not generate alerts.
Cooldown expiry alone is not recovery and produces no success message. Unknown
future events are not mapped. Bodies contain fixed language, a shortened incident
ID and observation time, never full diagnostics or local paths. They describe
historical observations and direct the operator to current safety status.

Critical/warning/informational are DLS categories. All currently use Pushover
normal priority **0**. Sender selection prioritizes critical, warning, then
informational work, with oldest-due ordering within each level. No high-priority quiet-hours override or repeating emergency
alerts are enabled. Serious integrity failures, backfill stalls and weekly archive
summaries have no implemented producer/mapping here. The future Indexer owns the
Saturday morning completeness report.

## Read-only inspection and explicit execution

```sh
python3 -B -m dls_video_downloader notifications plan --json
python3 -B -m dls_video_downloader notifications status --json
python3 -B -m dls_video_downloader notifications run --json
```

These never access credentials, initialize delivery state or contact a provider.
`status` includes operator connectivity tests and the latest 50 attempt records;
`plan`/normal `run` exclude connectivity tests. A missing safety journal is shown
explicitly and does not mean YouTube access is healthy; legacy evidence is not
automatically imported merely to generate an alert. Existing CLI `--db PATH` determines
the operational state directory without opening the inventory database.

Capabilities for a **separately authorized** live milestone:

```sh
python3 -B -m dls_video_downloader notifications run --execute --max-messages 1 --runtime-seconds 60 --json
python3 -B -m dls_video_downloader notifications test --test-id first-validation-20261105 --execute --runtime-seconds 30 --json
```

`test` is a fixed operator connectivity message, not a synthetic archival incident.
It sends only that test ID, even if real incidents are pending. Reusing an already
accepted ID never sends it again. Omit `--execute` to inspect without network or
credentials. Test delivery writes only notification history, not safety events.

Normal sender defaults: one message, 60 seconds total; configurable 1–5 messages
and 5–120 seconds. Requests are sequential, at most 20 seconds each plus bounded
child cleanup, and do not start with fewer than five seconds remaining. One
attempt per selected notification per invocation; no inline retry loop.

## Delivery state and concurrency

Ignored operational state is independent of archive assets:

```text
state/notifications/.sender.lock
state/notifications/deliveries.sqlite3
```

SQLite schema 1 has `notifications`, `event_links`, `attempts`, and a single-row
`provider_state` rejection hold. Stable safety event IDs link to a deterministic
incident/stage notification ID. Overlapping workers cannot issue duplicate sends:
a nonblocking sender lock covers the batch, with its descriptor inherited by the
network child. If the parent dies, the surviving child retains ownership until
it exits. SQLite uses transactional updates, foreign keys and one-second waits.

Before network work, the attempt and `inflight` transition are committed. Only
HTTP 200 with JSON integer `status: 1` becomes `accepted`. This means provider
acceptance, **not delivery to or acknowledgment on the iPhone**. A sanitized
provider request UUID may be retained; recipient identifiers are not stored.

- `pending`: eligible, not attempted.
- `inflight`: durable attempt started; response not committed yet.
- `accepted`: confirmed API acceptance; never automatically resent.
- `retryable`: known temporary provider/worker-launch failure, with next due time.
- `rejected`: provider/input/quota rejection, requiring operator correction.
- `uncertain`: response lost, timeout/interruption, invalid response, or abandoned
  in-flight attempt; operator attention required.
- `review`: automatic retry budget exhausted.

Temporary failures retry no sooner than 1m, 5m, 25m and 60m, with review after the
fifth failure. There is no rapid automatic retry. HTTP 4xx/provider rejection pauses
further Pushover delivery in this catalog; fix configuration/quota/input and
explicitly requeue the rejected notification to release that hold. It never
pauses YouTube or changes safety state.

The documented message API does not provide a caller idempotency key. A server
may accept a message before the acknowledgment is lost. **Exactly-once delivery
cannot be promised across that boundary.** On restart, abandoned in-flight
attempts become uncertain only while the new worker owns the sender lock; they
are not automatically resent. Records remain visible, rather than silently lost
or falsely marked successful. Operator-approved retries may cause a duplicate:

```sh
# Inspect first; this command only requeues, never sends:
python3 -B -m dls_video_downloader notifications retry NOTIFICATION_ID --json
# After correcting a known rejection, explicitly allow another attempt:
python3 -B -m dls_video_downloader notifications retry NOTIFICATION_ID --execute --json
# For uncertain/inflight outcomes, additional explicit risk acknowledgment:
python3 -B -m dls_video_downloader notifications retry NOTIFICATION_ID --execute --acknowledge-duplicate-risk --json
```

Accepted notifications cannot be requeued. A separate authorized send command is
still necessary after retry authorization. Alert history never edits safety
incident history. No background attempt or cron job is created by these commands.

## Credentials and supervised provisioning reference

Default configuration is **outside Git**:

`~/.config/dls-video-downloader/pushover.json`

The parent must be owned by the current user and mode `0700`; the regular file
must be owned by that user, mode `0600` (or read-only `0400`), and not a symlink or
hard link. Config locations resolving inside the repository are rejected. Only
`token`, `user`, and optional `device` fields are allowed. Tokens/user keys must
match the provider's 30-character alphanumeric format. No credential validation
API request is made. `--config PATH` selects another equally private external file.

**The temporary `~/pushover.txt` is reserved and is not supported by the loader.**
The offline implementation did not access it. The subsequent authorized
provisioning read it privately and retained it unchanged; secure cleanup still
requires separate authorization. Routine planning/checkpoint verification does
not access either credential file. No automatic credential discovery or migration
is implemented.

Reference procedure for a separately authorized provisioning/test milestone
(the first test below is already accepted and must not be resent):

1. Authorize creation of the private canonical configuration and exactly one
   connectivity-test POST. Keep cron paused and YouTube recovery unperformed.
2. Have the operator enter the DLS Archive application token and user key locally
   using hidden prompts/editor input. Do not put values in command arguments,
   shell history, chat, source control, screenshots or diagnostic output. If the
   temporary source file is needed, authorize that access separately first.
3. Create the private directory/file with restrictive permissions and refuse to
   overwrite any existing configuration without review. A normal layout is JSON
   containing `token` and `user`; no device field is necessary for the first test.
4. Run the read-only test command first:
   `notifications test --test-id first-validation-20261105 --json`.
   It should select exactly one fixed test message without reading credentials.
5. With explicit live authorization, run the `test --execute` command above once.
   This makes one message-API POST; no YouTube, validation, quota or receipt API
   calls occur. Stop on any failure/uncertainty; do not force success with retries.
6. Inspect `notifications status --json`: exactly one test attempt should be
   accepted, with the same notification ID and optional provider UUID. Ask the
   operator to confirm receipt on the iPhone independently. Inspecting the same
   test ID without `--execute` must not send another message.
7. Compare production artifact/cron baselines. Keep unattended notification
   scheduling and all DLS jobs paused pending a separate rollout decision.

Credentials travel to the killable provider worker only on stdin, never argv or
logs. HTTPS certificate verification is enabled, the endpoint is fixed, redirects
are not followed, provider responses are bounded, and raw provider errors are
never persisted or printed. Worker timeouts/crashes become uncertain, without
exposing exception text. Notification reports contain only static error codes.

## Future boundaries

Reliable media/supplemental integrity or repeated-failure event producers may
later gain mappings to this DLS sender. No ad hoc inspection of archive assets or
manufactured integrity incidents is added. Healthchecks.io should independently
monitor missed scheduled jobs, rather than be treated as message delivery. The
Indexer should generate its own Saturday report. AlejMusic may use a separate
application/recipient configuration in its own project; nothing there is changed.

API reference: [Pushover message API](https://pushover.net/api). Its required HTTPS
POST, success response, priority and retry guidance informed the transport; the
conservative uncertainty policy and durable history are DLS operational decisions.

## Offline verification

All 587 regression tests pass, including 63 notification tests. Coverage includes
incident mapping/deduplication, successful API acceptance, provider rejection,
bounded retries, uncertain delivery, crash/restart, concurrent send/retry workers,
permission/ownership/link checks, safe stdin transport, sanitization, test-message
isolation and read-only planning. External sockets and production downloader/
provider launches were blocked, with zero unexpected accesses. The reserved
credential source was guarded and never accessed. Production hashes, databases
and paused cron remained unchanged; no production notification state was created.

## Verified production connectivity test and checkpoint

Test ID `first-validation-20261105` has exactly one accepted attempt. Pushover
returned HTTP 200 at 2026-11-05 23:20:08 UTC, and the user confirmed receipt on the
iPhone. The catalog retains the accepted operator-test notification separately
from safety incidents. Reusing that accepted test ID does not deliver again.
This is evidence of one supervised test, not unattended incident delivery.

The Git checkpoint includes only source, tests and documentation. Configuration,
notification history and temporary credential source stay outside Git and remain
untouched during checkpoint verification. Before enabling unattended alerts,
separately review incident intake, sender invocation/activation and bounded retry
operations; do not restore video/supplemental cron or perform YouTube recovery as
part of notification activation implicitly.

## Unattended incident dispatcher — implemented, not scheduled

`notifications dispatch` is a finite incident-only poll of the existing sender.
It shares the ledger, templates, retry policy, nonblocking sender lock and child
lock inheritance with `notifications run`. There is no additional database,
queue, daemon, automatic safety probe, or callback into archival workflows.

```sh
# Default: no credentials, network, catalog initialization or operational writes
/opt/homebrew/bin/python3 -B -m dls_video_downloader notifications dispatch --json
# Capability only; requires separately authorized production activation
/opt/homebrew/bin/python3 -B -m dls_video_downloader notifications dispatch --execute --max-messages 2 --runtime-seconds 60 --json
```

The report includes `operation: incident_dispatch`, attempt/acceptance/due/review
counts, selected IDs, per-attempt outcomes and sanitized errors. Empty execution
polls make no provider request and do not load credentials. Provider acceptance
is not phone receipt. Exit codes: 0 for completed/empty poll, 2 for unsuccessful
attempt, 75 for sender contention/provider hold, 1 for configuration/operational
error, 130 for interrupted delivery. Uncertain records remain held for explicit
duplicate-risk review, never silently resent by the next five-minute poll.
Confidently failed temporary attempts follow the existing bounded retry policy.

Proposed **not installed** Mac-local cron (the dispatcher has no time-of-day policy):

```cron
*/5 * * * * umask 077; cd /Users/alejandroveloz/Sites/dls-video-downloader && /bin/mkdir -p state/notifications && /opt/homebrew/bin/python3 -B -m dls_video_downloader notifications dispatch --execute --max-messages 2 --runtime-seconds 60 --json > /Users/alejandroveloz/Sites/dls-video-downloader/state/notifications/dispatch-last.json 2>&1
```

The command creates the notification directory before shell redirection is
opened; if directory creation fails, sending does not start. `umask 077` keeps
new directories and reports private (it does not repair existing permissions).
Review existing ownership/permissions during supervised installation.
This overwrites the last operational report instead of appending an unbounded
log; durable evidence remains in `deliveries.sqlite3`. An overlapping invocation
may replace this last report with a busy result; consult the ledger for delivery
evidence. No secrets are in argv
or report. Keep the directory private. The batch is sequential, with bounded
provider timeouts and no inline retries; overlap defers through the existing
sender lock. To pause notifications, comment only this future dispatcher line,
independently of the six currently paused YouTube jobs. No GUI/login session
is required by this HTTPS sender; normal cron host availability still applies.

Inspect `notifications plan --json` for pending/due items and `notifications
status --json` for accepted, retryable, rejected, review and uncertain records,
provider hold and recent attempts. Fix configuration/provider rejection before
explicit retry; review uncertainty before acknowledging duplicate risk. Do not
repeat the already accepted connectivity-test ID as a validation strategy.

### Legacy evidence and rollout

An absent shared journal is an empty source, not proof of healthy YouTube access.
The dispatcher does not inspect/import component-local cooldowns, initialize
shared safety state, emit review events, or manufacture a notification for the
November 5 legacy caption failure. That original evidence remains authoritative.
A future authorized `youtube-safety import-legacy --execute` can create an
alertable import event: **the next executed dispatcher may deliver it**. Decide
intentionally whether that historical alert should be sent before authorizing
import and scheduling. Do not clear history to suppress an alert. The first real
shared event will be picked up on the next authorized poll even while YouTube
remains blocked. Cooldown expiry alone produces no recovery notification.

Next supervised milestone: inspect the read-only dispatcher plan, agree on
legacy-import/initial-alert handling, authorize one bounded real-incident send
if an eligible event exists, verify its ledger/API outcome and phone receipt,
then separately install the proposed schedule. Do not fabricate a production
incident or resume acquisition/recovery implicitly. Check subsequent empty polls
and failure reports before declaring unattended delivery validated.

Future Archiver/Indexer producers could reuse the sender only through explicitly
reviewed stable event mappings; their asset databases remain independent. No
Healthchecks or weekly summary producer is implemented here.

Dispatcher diagnostics cap each item/due/review list at 50 and expose omitted
counts; summary counts cover the full selection. This affects reporting only,
not ingestion, deduplication or the ledger. Full inspection remains available
through the existing plan/status commands.

Offline milestone verification: 607 regression tests (20 new dispatcher tests),
with external networking, real provider/downloader launches and production
credential-file reads guarded. All 95 protected files remained unchanged; four
operational catalogs passed integrity checks and cron remained byte-identical.
