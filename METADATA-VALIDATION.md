# October 26–28, 2026 metadata-only validation

Executed with `/usr/local/bin/yt-dlp-new`, reporting version `2026.10.26`. The older Homebrew executable was not used or modified. No media, comments, captions, or thumbnails were downloaded. No dependencies or system configuration changed.

## Verified records

All three identify channel `UCAIZ314ffmfM4PynCR8vElw`, `LeBatardShow`, public availability, and `live_status=was_live`. Upload and release dates agree. Current titles are `LIVE at 9am ET | <date> | The Dan Le Batard Show` with dates `10/26/26`, `10/27/26`, and `10/28/26`, respectively.

| Date | ID | Duration | Release timestamp (UTC) | Publication timestamp (UTC) |
| --- | --- | --- | --- | --- |
| October 26 | MbO7y_90zUI | 3:11:45 | 2026-10-26 12:51:50 | 2026-10-26 16:19:01 |
| October 27 | wUrudRrP-OY | 3:27:45 | 2026-10-27 12:51:22 | 2026-10-27 16:27:22 |
| October 28 | IX9Dv4cBUKg | 3:10:05 | 2026-10-28 12:51:52 | 2026-10-28 16:08:46 |

Release and publication timestamps differ by hours, but not calendar dates here. Existing precedence remains source `release_date`, then `upload_date`; both fields/timestamps are preserved. A future cross-day discrepancy should be surfaced rather than hidden. For livestream acquisition, release date is the appropriate proposed broadcast-date boundary; no timestamp conversion or new date policy was implemented.

Descriptions were inspected and imported in full: October 26 and 28 contain regular daily-show promotional text (1,608 characters); October 27 also has a sponsorship/legal preamble (2,570 characters). Incidental Mystery Crate links in descriptions do not change daily-show classifications. Initial and latest titles remain distinguishable fields, although these three titles did not change.

## Listed formats, not verified downloads

All three list AV1 `398`, 720p30, `av01.0.05M.08`, video-only MP4 over HTTPS. Lower-resolution AV1 is also listed at 144/240/360/480p. Actual video resolutions span 144–1080p; storyboard heights are not video formats. At 720p, HTTPS alternatives include VP9 `247` and H.264 `136`, plus HLS alternatives. No DRM is reported on the listed candidate streams.

| Date | AV1 398 bytes | VP9 247 bytes | H.264 136 bytes | Opus 251 bytes |
| --- | ---: | ---: | ---: | ---: |
| October 26 | 818733591 | 1659552182 | 667554931 | 168361142 |
| October 27 | 1100334996 | 1476639879 | 1617322392 | 182411036 |
| October 28 | 996698570 | 1338481576 | 1502689112 | 169817430 |

Preferred later test candidate: `398` plus non-DRC Opus `251` (approximately 117–119 kbps). Retain original streams and test muxing before choosing a container. At equal resolution, VP9 `247` or H.264 `136` are reasonable fallbacks; October 26 advertises smaller H.264 than AV1, so codec alone does not guarantee smallest size. Do not sacrifice substantial resolution simply to obtain AV1.

Other listed audio includes AAC `140` (~129 kbps), low-rate AAC `139`, Opus `249`/`250`, DRC variants, and HLS audio `233`/`234`. Suitable audio is advertised for all three, but URLs, sizes, and actual access must be revalidated in a separately authorized download test. No stream access or complete media integrity has been verified.

## JavaScript readiness

The findings below describe the initial extraction before Node 22 installation. A subsequent authorized installation and metadata-only test resolved the runtime warning and exercised Node challenge solving; see [Node runtime validation](NODE-RUNTIME.md).

Every extraction emitted the same warning: no supported JavaScript runtime was found; only Deno is enabled by default, and some formats may be missing. No extraction errors occurred. Node exists at `/usr/local/bin/node` but reports `v16.14.1`; Deno is absent from PATH. FFmpeg and FFprobe paths exist, but versions and actual muxing capability were not validated in this step.

The official [yt-dlp EJS guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS) currently lists Deno >=2.3.0 (recommended) and Node >=22.0.0 (explicit enabling required). Official bundled executables include EJS scripts. Merely enabling existing Node 16 would not satisfy the documented requirement. A later approved setup should verify a Ventura-compatible supported runtime and pass its explicit path, then repeat metadata extraction before controlled downloading. No runtime was installed, upgraded, or enabled here.

## Inventory verification

- 400 unique identities unchanged; observations increased from 401 to 404.
- All existing observations preserved; unrelated video records and four decisions unchanged.
- Automatic classifications unchanged: 84 daily shows, 19 The Club, 91 Skip, 206 Review.
- All three target videos remain daily-show Download candidates and are eligible under default `2026-10-26`.
- Eligible total increases from one under the former October 28 default to four: three daily shows plus the existing manually approved reaction.
- Missing publication dates decrease from 399 to 397; selected/date-unknown decreases from 102 to 100.
- Repeating all three full-metadata imports adds no observations or classification records; the complete report is unchanged.
- Sanitized operational records contain no formats, thumbnails, or playback-URL fields. Full extraction JSON and stderr are ignored local research evidence under `state/phase2-metadata/`, not a permanent inventory format or reusable stream-address cache.
- All 22 offline tests passed, including the new default boundary, configurable overrides, enrichment, date discrepancy reporting, and import idempotency.

Extraction used `--ignore-config --no-cache-dir --no-playlist --skip-download --no-write-comments --no-write-subs --no-write-auto-subs --no-write-thumbnail --no-write-info-json --dump-single-json`, with bounded retries/socket timeout. Stdout JSON and stderr warnings were captured separately. The sandbox prevented the packaged executable from initializing a semaphore, so the specifically authorized executable ran outside the sandbox. No general discovery command was added.
