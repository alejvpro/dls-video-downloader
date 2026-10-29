# October 28 supervised discovery/acquisition validation

Two bounded live operations completed: discovery-only, followed by discovery + acquisition restricted to `IX9Dv4cBUKg`. Both scanned 10 Live entries and 10 Videos entries, enriched at most three records, and reported no failures. No cron job was installed; its checksum remains unchanged.

No new identities were found: all 20 encountered IDs already existed in the 400-video research inventory. Final classifications among encountered IDs were seven daily shows, one The Club, eight Skip, and four unresolved Review. No unresolved Review was acquired. There are no active forward-window Reviews with verified dates in this sample; unknown-date Reviews remain outside the active queue.

Full metadata refreshed October 28 and enriched the October 23 daily show `35d9CIWjSkM` and The Club `GbncAA6bkx4`. Both October 23 uploads remain out of the automatic forward window. October 28 reports the official channel, release/upload date `20261028`, completed-live status, regular daily-show classification, and forward eligibility. No title-derived or discovery dates were used for eligibility.

Two ordinary uploads changed automatic classification during polling under unchanged rules. `9Cbm2FOXfPk` temporarily changed from Skip to Review when its title changed, then returned to Skip; `HIR-PAGyCkE` changed from Review to Skip with the title “Are the Miami Heat in Trouble Already? (ft. Joy Taylor)”. Title histories are retained. This illustrates the existing topical-clip heuristic's sensitivity to wording; no rule refinement was attempted here.

| Inventory/state | Before | After |
| --- | ---: | ---: |
| Video identities | 400 | 400 |
| Distinct metadata observations | 404 | 417 |
| Daily-show candidates | 84 | 84 |
| The Club candidates | 19 | 19 |
| Skip | 91 | 92 |
| Review | 206 | 205 |
| Missing publication dates | 397 | 395 |
| Manual decisions | 4 | 4 |
| Verified acquisitions | 2 | 3 |

Repeated real discovery did not add duplicate identities or identical observations. It added three observations on the second pass because returned metadata changed; both daily-show full payloads were unchanged and reused, while the Club payload changed. Exact unchanged discovery is additionally covered by offline tests. Poll timestamps and temporary URLs do not enter metadata observation hashes. Database uniqueness checks found zero duplicate `(video_id, detail, digest)` observations.

## October 28 media

| Field | Verified value |
| --- | --- |
| ID | `IX9Dv4cBUKg` |
| Media | `state/acquisitions/IX9Dv4cBUKg/media.mkv` |
| Manifest | `state/acquisitions/IX9Dv4cBUKg/acquisition.json` |
| Streams | Format 398, 1280×720 AV1, 30 fps; format 251, Opus |
| Container | MKV |
| Duration | 11405.041 seconds (3:10:05.041) |
| Size | 1165175892 bytes |
| SHA-256 | `f31cf876c4971854a1adedb579e2950a6d325a758660508d9938a82ed6a8a43e` |
| Encoding | FFmpeg `-c copy`; no re-encoding or fallback |

Preflight required 4573289824 bytes with 875157794816 bytes available. ffprobe, duration, size, SHA-256, manifest and SQLite receipt checks passed; no merger/extraction warnings were reported. Completed staging was cleaned, final link count is one, and metadata/log evidence remains in the final video's attempt directory.

A second direct acquisition invocation returned `already_verified` after checksum and stream checks, with no network request or new attempt. October 26 and 27 were skipped by the runner and independently confirmed unchanged by hash, size, inode, manifest bytes, and database receipts. Only October 28 was newly acquired. All four manual decisions remain byte-for-byte unchanged.

All 47 offline tests passed after the supervised acquisition. Shared locking, process-group timeout handling, bounded scans/enrichment/download attempts, isolated failures, and existing publication/cleanup safety are implemented. [Runner documentation](RUNNER.md) describes syntax and remaining cron blockers: stale-attempt retention, cross-run retry delay, coverage beyond scan caps, and operational log/failure reporting. Unattended downloading remains unauthorized.

Run summaries and timestamped events:

- `state/runs/2026-10-29T053628.276847+0000-719093cb/`
- `state/runs/2026-10-29T053850.500495+0000-ebf53bcc/`
