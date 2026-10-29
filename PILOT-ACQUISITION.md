# First controlled acquisition: October 26, 2026

Only `MbO7y_90zUI` was acquired. No other video, comments, captions, or thumbnails were downloaded. No external drives, sibling repositories, system configuration, or scheduled jobs were changed.

| Property | Verified result |
| --- | --- |
| Title | LIVE at 9am ET \| 10/26/26 \| The Dan Le Batard Show |
| Media | `state/acquisitions/MbO7y_90zUI/media.mkv` |
| Manifest | `state/acquisitions/MbO7y_90zUI/acquisition.json` |
| Video | Format 398, AV1, 1280×720, 30 fps |
| Audio | Format 251, Opus |
| Container | MKV / Matroska |
| Duration | 11505.021 seconds (3:11:45.021), expected 11505 seconds |
| Size | 985715504 bytes |
| SHA-256 | `24e8c24ea3cb44a0cbac03b2eeed532592c46211d0cb23a9aa72ae423c551705` |
| Transcoding | None; FFmpeg command log records `-c copy` |
| Attempt | `046aa620224b4e67be9c6aa6b1f7cf40` |

Fresh preflight verified the official channel ID, completed-live status, required 398/251 formats, and advertised stream sizes. Required space was 4035026023 bytes; available space was 877449420800 bytes. The download used the standalone yt-dlp, explicit Node 22 path, existing FFmpeg, no fallback, and zero download/fragment/extractor retries. No extraction or merger warnings were reported.

ffprobe validated the nonempty output, container, exactly two streams, codecs, resolution, frame rate, and duration. The file received a SHA-256 before no-replace publication; the manifest and SQLite acquisition receipt were written only after verification. The staged and published MKV names are hard links to the same inode, consuming media storage once. Attempt metadata and sanitized tool diagnostics remain under `.work` at paths referenced by the manifest; no playback URLs entered operational metadata or retained diagnostics.

A second real CLI invocation returned `already_verified` after hash/size and ffprobe checks. It created no acquisition attempt or additional media artifact and performed no YouTube extraction or download. Existing media was neither overwritten nor removed.

SQLite migrated to schema 2 with `acquisition_attempts` and `acquisitions` tables. There is one completed attempt and one verified acquisition, both tied to the original ID. Exact before/after comparison confirmed that all original inventory tables remained unchanged: 400 video IDs, 404 observations, existing classifications, source/import records, and four manual decisions.

All 31 offline tests passed, including nine acquisition tests with tiny disposable fixture files and mocked external tools. Coverage includes rejected/Review selection, explicit approval, missing formats, insufficient storage, interruption, invalid output, successful completion, repeat invocation, corruption detection, and incomplete publication. No production media is used by tests.

Limits: verification inspects stream/container metadata and hashes the whole file; it is not a full decode audit or an Archiver preservation receipt. Incomplete publication or corruption stops for manual recovery. General format policy, uploaded-program acquisition, discovery, scheduling, and backfill remain future work. October 27 and 28 have not been downloaded.

## Follow-up: staging cleanup and October 27

The paragraphs above record the first pilot's original state. The separately authorized follow-up acquired October 27 only; October 28 remains unacquired.

Inspection confirmed `os.link` publication: the two October 26 MKV names originally shared one inode and one allocation of media bytes. Unlinking the verified staging name does not delete the published file. Its extra media link has now been removed after repeat checksum/stream verification under the acquisition lock. Its unchanged manifest still references small metadata/log evidence in `.work`, so that evidence remains. Original media hash, inode, size, manifest bytes, and database receipt were confirmed unchanged.

New successful attempts move their evidence into the final video's `attempts/<attempt-id>/` before manifest publication. After verified completion and database recording, cleanup removes only a staged/evidence MKV that shares the final inode, then removes empty staging directories. It does not recursively delete interrupted attempts, unrelated files, or legacy evidence. Cleanup failure defers cleanup without invalidating completed media. Explicit retries start a new attempt rather than resume old partials; uncertain final publication requires manual recovery.

| October 27 property | Verified result |
| --- | --- |
| ID | `wUrudRrP-OY` |
| Media | `state/acquisitions/wUrudRrP-OY/media.mkv` |
| Manifest | `state/acquisitions/wUrudRrP-OY/acquisition.json` |
| Formats | 398 + 251; no substitution |
| Streams/container | 1280×720 AV1, 30 fps; Opus; MKV |
| Duration | 12465.021 seconds (3:27:45.021) |
| Size | 1281294295 bytes |
| SHA-256 | `df1050e2510e27c5322e291e2e6f16d07f3875dbb5b1b6a4cc5695dd89aa1d36` |
| Attempt | `2c298a4f706f4b2b95d6d75c26cdad38` |
| Encoding | FFmpeg `-c copy`; no transcoding |

Current formats were checked before downloading, with a 4921979920-byte space budget and 876466860032 bytes available. No extraction/download/merge warnings were reported. ffprobe, whole-file SHA-256, manifest, and SQLite receipt checks passed. The second CLI invocation returned `already_verified` without YouTube access or another attempt/download. There are exactly two verified acquisitions and two completed attempts; inventory metadata, classifications, source/import records, and manual decisions remain unchanged.

October 27's completed `.work` directory is absent. Its metadata/logs survive under `wUrudRrP-OY/attempts/2c298a4f706f4b2b95d6d75c26cdad38/`. Both final MKV files have link count one; no extra staged media remains. All 36 offline tests passed, including focused cleanup, legacy-evidence retention, concurrent locking, interrupted-attempt preservation, independent-file refusal, and cleanup-failure tests. No production media is mutated by tests.
