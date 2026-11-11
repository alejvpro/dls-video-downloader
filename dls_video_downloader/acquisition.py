"""Explicit single-video acquisition: strict formats, local staging, verified publication."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import signal
import uuid
from contextlib import contextmanager

from . import youtube_safety as safety
from .classification import CHANNEL, DEFAULT_START, publication
from .inventory import FIELDS, encoded, now, report
from .operations import OperationBusy, retry_failure, retry_pending, retry_success, safe_text, write_log

PROJECT = Path(__file__).resolve().parent.parent
YT_DLP = "/usr/local/bin/yt-dlp-new"
NODE = "/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
MAX_HEIGHT = 720
MAX_WIDTH = 1280
_operation_fd = None


def _is_drm(value):
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"))


def _language_tag(value):
    if not isinstance(value, str):
        return None
    value = value.strip().replace("_", "-").lower()
    return value or None


def _language(value):
    tag = _language_tag(value)
    return tag.split("-", 1)[0] if tag else None


def _audio_identity(row, metadata):
    """Return conservative source-language evidence for a yt-dlp audio format."""
    fmt_id = str(row.get("format_id", ""))
    note = " ".join(str(row.get(key, "")) for key in ("format_note", "format", "audio_track"))
    text = (fmt_id + " " + note).lower()
    if re.search(r"\b(dub(?:bed)?|translated|translation|auto[- ]?dub)\b", text):
        return None, "explicitly_dubbed_or_translated"
    lang = _language_tag(row.get("language"))
    source = _language(metadata.get("language") or metadata.get("original_language"))
    explicit_original = (re.search(r"\boriginal\b", note, re.I)
                         or re.search(r"(?:^|-)orig(?:$|-)", fmt_id, re.I))
    if explicit_original and lang and source and _language(lang) != source:
        return None, "source_language_conflict"
    if explicit_original:
        return lang or source or "unknown", "explicit_original_track"
    if lang and source and _language(lang) == source:
        return lang, "matches_video_source_language"
    # yt-dlp's unsuffixed 251 is the primary audio-only rendition used by the
    # established workflow. Preserve compatibility when its language label is
    # omitted, but do not extend this assumption to suffixed/dub variants.
    if fmt_id == "251" and not lang:
        return source or "unknown", "legacy_primary_audio_format"
    return None, "original_language_unverified"


def _audio_codec(row):
    codec = str(row.get("acodec") or "").lower()
    if codec == "opus":
        return "opus"
    if codec == "aac" or codec.startswith("mp4a."):
        return "aac"
    return None


def _audio_rank(row, codec, language_evidence, duration=None):
    """Stable preference: non-DRC, quality evidence, efficient codec, size, ID."""
    drc = bool(re.search(r"(?:^|[-\s])drc(?:$|[-\s])",
                         str(row.get("format_id", "")) + " " + str(row.get("format_note", "")), re.I))
    abr = row.get("abr")
    abr = float(abr) if isinstance(abr, (int, float)) and abr > 0 else None
    estimated = False
    size = row.get("filesize") or row.get("filesize_approx")
    if abr is None and isinstance(size, (int, float)) and size > 0 and isinstance(duration, (int, float)) and duration > 0:
        abr = float(size) * 8 / float(duration) / 1000
        estimated = True
    quality = row.get("quality")
    quality = float(quality) if isinstance(quality, (int, float)) else None
    size = int(size) if isinstance(size, (int, float)) and size > 0 else None
    # If bitrate evidence is comparable (within 15%), favor Opus for storage
    # efficiency. Otherwise prefer the better evidenced bitrate.
    primary = 0 if str(row.get("format_id")) == "251" else 1
    return drc, abr, quality, 0 if codec == "opus" else 1, primary, size if size is not None else float("inf"), str(row.get("format_id", "")), estimated


def select_formats(metadata):
    """Select strict format 398 plus confidently original Opus/AAC audio."""
    metadata = metadata if isinstance(metadata, dict) else {}
    rows = metadata.get("formats", []) if isinstance(metadata, dict) else []
    if not isinstance(rows, list):
        rows = []
    formats = {str(row.get("format_id")): row for row in rows if isinstance(row, dict)
               and row.get("format_id") is not None}
    video = formats.get("398")
    reasons = []
    if video is None:
        reasons.append("video_398_missing")
    else:
        if video.get("width") != MAX_WIDTH:
            reasons.append("video_398_width_mismatch")
        if video.get("height") != MAX_HEIGHT:
            reasons.append("video_398_height_mismatch")
        if video.get("fps") != 30:
            reasons.append("video_398_fps_mismatch")
        if not str(video.get("vcodec", "")).startswith("av01"):
            reasons.append("video_398_codec_mismatch")
        if _is_drm(video.get("has_drm")):
            reasons.append("video_398_drm")
    candidates = []
    rejected = {"not_audio_only": 0, "unsupported_codec": 0, "drm": 0, "size_unknown": 0,
                "dubbed_or_translated": 0, "language_unverified": 0}
    for row in formats.values():
        codec = _audio_codec(row)
        if row.get("vcodec") != "none":
            rejected["not_audio_only"] += 1
            continue
        if codec is None:
            rejected["unsupported_codec"] += 1
            continue
        if _is_drm(row.get("has_drm")):
            rejected["drm"] += 1
            continue
        language, evidence = _audio_identity(row, metadata)
        if language is None:
            key = "dubbed_or_translated" if evidence == "explicitly_dubbed_or_translated" else "language_unverified"
            rejected[key] += 1
            continue
        size = row.get("filesize") or row.get("filesize_approx")
        if not isinstance(size, (int, float)) or size <= 0:
            rejected["size_unknown"] += 1
            continue
        candidates.append((row, codec, language, evidence))
    if candidates:
        # Prefer non-DRC first. Among alternatives within 15% bitrate, Opus is
        # favored; otherwise bitrate determines quality. Remaining ties use size
        # and stable format ID for reproducible selection.
        duration = metadata.get("duration")
        rank = lambda item: _audio_rank(item[0], item[1], item[3], duration)
        non_drc = [item for item in candidates if not rank(item)[0]]
        pool = non_drc or candidates
        best_bitrate = max((rank(item)[1] or 0) for item in pool)
        if best_bitrate:
            comparable = [item for item in pool
                          if (rank(item)[1] is None or rank(item)[1] >= best_bitrate * 0.85)]
            pool = comparable or pool
        else:
            qualities = [rank(item)[2] for item in pool if rank(item)[2] is not None]
            if qualities:
                best_quality = max(qualities)
                pool = [item for item in pool if rank(item)[2] == best_quality]
        audio_row, audio_codec, language, evidence = min(
            pool, key=lambda item: (0 if item[1] == "opus" else 1,
                                    -(rank(item)[1] or 0),
                                    -(rank(item)[2] or 0),
                                    rank(item)[4],
                                    rank(item)[5],
                                    str(item[0].get("format_id", ""))))
        audio_rank = _audio_rank(audio_row, audio_codec, evidence, duration)
        selected_drc = audio_rank[0]
        size = audio_row.get("filesize") or audio_row.get("filesize_approx")
        abr = audio_rank[1]
        fallback = audio_codec == "aac"
        rationale = ("DRC fallback: no suitable non-DRC original-language rendition"
                     if selected_drc else "selected original-language rendition; non-DRC preferred")
        if fallback:
            rationale += "; AAC selected because no comparable suitable Opus rendition was selected"
        audio_evidence = {"codec": audio_codec, "language": language,
                          "language_evidence": evidence, "drc": selected_drc,
                          "drm": audio_row.get("has_drm") if isinstance(audio_row.get("has_drm"), bool) else "unknown",
                          "abr_kbps": abr if isinstance(abr, (int, float)) else None,
                          "abr_estimated_from_size_and_duration": audio_rank[7],
                          "quality": audio_row.get("quality") if isinstance(audio_row.get("quality"), (int, float)) else None,
                          "size_bytes": size if isinstance(size, (int, float)) else None,
                          "rationale": rationale}
        audio_id = str(audio_row["format_id"])
    else:
        audio_id = None
        audio_evidence = None
        reasons.append("original_audio_unavailable_or_unverified")
        if rejected["size_unknown"]:
            reasons.append("audio_size_unknown")
    if candidates:
        sizes = [(video.get("filesize") or video.get("filesize_approx")) if video else None,
                 audio_row.get("filesize") or audio_row.get("filesize_approx")]
        if not all(isinstance(size, (int, float)) and size > 0 for size in sizes):
            reasons.append("selected_format_size_unknown")
    if candidates and selected_drc:
        reasons.append("drc_audio_fallback")
    return {"available": not [reason for reason in reasons if reason != "drc_audio_fallback"],
            "video_format_id": "398" if video else None, "audio_format_id": audio_id,
            "audio_codec": audio_codec if candidates else None,
            "audio_selection": audio_evidence,
            "video_selection": ({"format_id": "398", "codec": "av1", "width": video.get("width"),
                                 "height": video.get("height"), "fps": video.get("fps"),
                                 "drm": video.get("has_drm") if isinstance(video.get("has_drm"), bool) else "unknown"}
                                if video else None),
            "rejected_audio_counts": rejected,
            "reasons": reasons}


# Kept as a compatibility alias for callers/tests from the strict-format era.
assess_required_formats = select_formats


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, log, timeout):
    gate=safety.current()
    request=gate.begin(Path(log).name) if safety.is_network(args) else None
    # A terminated yt-dlp must not leave its FFmpeg/Node children running after
    # the parent releases the operation lock.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True,
                          pass_fds=(_operation_fd,) if _operation_fd is not None else ()) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, stderr = process.communicate()
            write_log(log, stderr)
            if request:gate.observe(stderr,component=safety.component('video'),operation=Path(log).name,observation_id=request)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ValueError("External command timed out; partial output retained") from exc
            raise
    # Verbose merger evidence is useful; playback and other request URLs are not.
    write_log(log, stderr)
    if request:
        blocked=gate.observe(stderr,component=safety.component('video'),operation=Path(log).name,observation_id=request)
        if blocked:raise safety.Blocked('shared_youtube_'+blocked['status']+': '+blocked['reason'])
    if process.returncode:
        errors = [line for line in safe_text(stderr).splitlines() if 'ERROR' in line]
        reason = errors[-1][:500] if errors else f"External command failed (exit {process.returncode})"
        raise ValueError(f"{reason}; see {log.name}")
    return stdout


def verify(path, duration, probe_log, expected_audio_codec="opus"):
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError("Missing or empty regular media output")
    data = json.loads(run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], probe_log, 120))
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if "matroska" not in data.get("format", {}).get("format_name", "").split(",") or path.suffix != ".mkv":
        raise ValueError("Output is not Matroska/MKV")
    if len(streams) != 2 or len(video) != 1 or len(audio) != 1:
        raise ValueError("Expected exactly one video and one audio stream")
    v, a = video[0], audio[0]
    if (v.get("codec_name") != "av1" or v.get("width") != 1280
            or v.get("height") != MAX_HEIGHT or a.get("codec_name") != expected_audio_codec
            or expected_audio_codec not in ("opus", "aac")):
        raise ValueError("Output does not contain required 720p AV1 and selected Opus/AAC audio")
    numerator, denominator = map(int, v.get("avg_frame_rate", "0/1").split("/"))
    if not denominator or abs(numerator / denominator - 30) > 0.1:
        raise ValueError("Video is not approximately 30fps")
    actual = float(data["format"]["duration"])
    if not duration or abs(actual - duration) > max(10, duration * 0.001):
        raise ValueError("Media duration differs from expected duration")
    return {"container": "mkv", "duration_seconds": actual, "video_codec": "av1",
            "audio_codec": a["codec_name"], "width": v.get("width"), "height": v["height"], "fps": numerator / denominator}


def record_complete(db, manifest, media, receipt):
    with db:
        db.execute("INSERT OR IGNORE INTO acquisitions VALUES(?,?,?,?,?,?)", (
            manifest["video_id"], manifest["completed_at"], str(media.relative_to(PROJECT)),
            str(receipt.relative_to(PROJECT)), manifest["size"], manifest["sha256"]))


def cleanup_completed(db, manifest, media, root):
    """Called under the acquisition lock, after final-file verification/commit.

    Unlink only the extra name for the verified inode. Never recursively sweep
    staging: partial attempts and legacy manifest-referenced evidence survive.
    Cleanup problems are observational and cannot relabel a verified acquisition.
    """
    try:
        record = db.execute("SELECT * FROM acquisitions WHERE video_id=?", (manifest["video_id"],)).fetchone()
        if not record or record["sha256"] != manifest["sha256"] or record["size"] != manifest["size"]:
            return "deferred: no matching verified database receipt"
        attempt = manifest["attempt_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            return "deferred: invalid attempt identity"
        work = root / ".work" / manifest["video_id"] / attempt
        evidence = media.parent / "attempts" / attempt
        for directory in (work, evidence):
            if directory.resolve() != directory.absolute():
                return "deferred: unsafe staging/evidence path"
            staged = directory / "media.mkv"
            if staged.exists() or staged.is_symlink():
                if staged.is_symlink() or not staged.is_file() or not os.path.samefile(staged, media):
                    return "deferred: staged file is not the verified media hard link"
                staged.unlink()
        # Only empty directories are removable; legacy evidence stays referenced.
        for directory in (work, work.parent):
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        return "complete; legacy referenced evidence retained where applicable"
    except OSError as exc:
        return f"deferred: {type(exc).__name__}; verified acquisition retained"


@contextmanager
def operation_lock(root):
    global _operation_fd
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".acquisition.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationBusy("Another acquisition or discovery operation is running") from exc
        _operation_fd = lock.fileno()
        try:
            # Network-capable child workflows pass this descriptor through so
            # the exclusion remains held if the parent is interrupted.
            yield lock.fileno()
        finally:
            _operation_fd = None


@safety.scoped(lambda a:safety.state_from_db(a['db']),'video-acquisition')
def acquire(db, video_id, start=DEFAULT_START, *, root=None, _lock_held=False,
            retry_base_minutes=60, retry_max_hours=24, _forward_only=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video ID")
    selected = next((i for i in report(db, start)["items"] if i["id"] == video_id), None)
    if not selected or selected["eligibility"] != "eligible":
        raise ValueError("Video is not an eligible effective Download candidate")
    root = (root or PROJECT / "state" / "acquisitions").resolve()
    if not root.is_relative_to(PROJECT):
        raise ValueError("Acquisition output must remain inside the project")
    safety.current().ensure('acquisition')
    root.mkdir(parents=True, exist_ok=True)
    def perform():
        verified = db.execute("SELECT 1 FROM acquisitions WHERE video_id=?", (video_id,)).fetchone()
        pending = retry_pending(db, video_id, "acquisition")
        if pending and not verified:
            raise ValueError(f"Retry deferred until {pending['next_retry']}: {pending['category']}")
        try:
            result = _acquire_locked(db, video_id, root, forward_start=start if _forward_only else None)
        except safety.Blocked:
            raise
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            retry_failure(db, video_id, "acquisition", exc, base_minutes=retry_base_minutes, max_hours=retry_max_hours)
            raise
        retry_success(db, video_id, "acquisition")
        return result
    if _lock_held:
        return perform()
    with operation_lock(root):
        return perform()


def _acquire_locked(db, video_id, root, forward_start=None):
    final = root / video_id
    final.mkdir(exist_ok=True)
    if final.is_symlink() or final.resolve().parent != root:
        raise ValueError("Unsafe video output directory")
    media, receipt = final / "media.mkv", final / "acquisition.json"
    if receipt.exists():
        manifest = json.loads(receipt.read_text())
        if manifest["video_id"] != video_id or sha256(media) != manifest["sha256"] or media.stat().st_size != manifest["size"]:
            raise ValueError("Existing acquisition failed identity/size/checksum verification; refusing replacement")
        verify(media, manifest["expected_duration"], final / "repeat-ffprobe.stderr.log",
               manifest.get("audio_codec", "opus"))
        record_complete(db, manifest, media, receipt)
        cleanup = cleanup_completed(db, manifest, media, root)
        return {"status": "already_verified", **manifest, "path": str(media), "cleanup": cleanup}
    if media.exists() or db.execute("SELECT 1 FROM acquisitions WHERE video_id=?", (video_id,)).fetchone():
        raise ValueError("Incomplete or inconsistent publication; manual recovery required")
    attempt = uuid.uuid4().hex
    work = root / ".work" / video_id / attempt
    work.mkdir(parents=True)
    with db:
        db.execute("INSERT INTO acquisition_attempts VALUES(?,?,?,NULL,?,?,?)", (
            attempt, video_id, now(), "running", "Strict AV1 398 with selected original Opus/AAC acquisition", str(work.relative_to(PROJECT))))
    common = [YT_DLP, "--ignore-config", "--no-cache-dir", "--no-playlist", "--js-runtimes", "node:" + NODE,
              "--no-write-comments", "--no-write-subs", "--no-write-auto-subs", "--no-write-thumbnail",
              "--no-write-info-json", "--socket-timeout", "30", "--retries", "0", "--fragment-retries", "0",
              "--extractor-retries", "0", "--ffmpeg-location", FFMPEG]
    url = "https://www.youtube.com/watch?v=" + video_id
    try:
        print(f"{video_id}: checking current metadata and selecting preserved formats", flush=True)
        metadata = json.loads(run(common + ["--skip-download", "--dump-single-json", url], work / "preflight.stderr.log", 180))
        if (metadata.get("id") != video_id or metadata.get("channel_id") != CHANNEL
                or metadata.get("live_status") not in ("was_live", "not_live") or metadata.get("is_live")):
            raise ValueError("Unexpected source identity or video not ready/completed")
        if forward_start:
            published, _ = publication(metadata)
            if not published or published < forward_start:
                raise ValueError("Current source metadata lacks a verified forward-window date")
        selection = select_formats(metadata)
        if not selection["available"]:
            raise ValueError("Required AV1 398 and verified original Opus/AAC audio unavailable: "
                             + ", ".join(selection["reasons"]))
        formats = {str(f["format_id"]): f for f in metadata.get("formats", []) if f.get("format_id") is not None}
        video, audio = formats[selection["video_format_id"]], formats[selection["audio_format_id"]]
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in (video, audio)]
        if not all(isinstance(s, (int, float)) and s > 0 for s in sizes):
            raise ValueError("Cannot establish expected storage requirement")
        required = int(sum(sizes) * 3) + 1024**3
        free = shutil.disk_usage(root).free
        if free < required:
            raise ValueError(f"Insufficient local storage: need {required} bytes, have {free}")
        (work / "metadata.json").write_text(encoded({k: metadata[k] for k in FIELDS if metadata.get(k) is not None}))
        fmt = f"{selection['video_format_id']}+{selection['audio_format_id']}"
        print(f"{video_id}: downloading only {fmt}; storage budget {required} bytes, free {free}", flush=True)
        output = work / "media.mkv"
        run(common + ["--verbose", "--newline", "--no-overwrites", "--fixup", "never", "-f", fmt,
                      "--merge-output-format", "mkv", "-o", str(work / "media.%(ext)s"), url], work / "download.stderr.log", 7200)
        merger_log = (work / "download.stderr.log").read_text()
        if "-c copy" not in merger_log and "-c:v copy" not in merger_log:
            raise ValueError("Missing FFmpeg stream-copy evidence; refusing completion")
        verified = verify(output, metadata.get("duration"), work / "ffprobe.stderr.log", selection["audio_codec"])
        evidence = final / "attempts" / attempt
        manifest = {"schema_version": 1, "video_id": video_id, "attempt_id": attempt,
                    "completed_at": now(), "expected_duration": metadata["duration"],
                    "video_format": selection["video_format_id"], "audio_format": selection["audio_format_id"],
                    "audio_selection": selection["audio_selection"], "transcoded": False,
                    "size": output.stat().st_size, "sha256": sha256(output), **verified,
                    "metadata_relative": str((evidence / "metadata.json").relative_to(PROJECT)),
                    "merger_log_relative": str((evidence / "download.stderr.log").relative_to(PROJECT)),
                    "storage_budget_bytes": required}
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(output, media)  # Atomic no-replace publication on the same filesystem.
        evidence.parent.mkdir(exist_ok=True)
        work.rename(evidence)  # Preserve small evidence before publishing its manifest.
        with db:
            db.execute("UPDATE acquisition_attempts SET work_relative=? WHERE attempt_id=?", (str(evidence.relative_to(PROJECT)), attempt))
        with receipt.open("x") as stream:
            stream.write(encoded(manifest)); stream.flush(); os.fsync(stream.fileno())
        record_complete(db, manifest, media, receipt)
        with db:
            db.execute("UPDATE acquisition_attempts SET finished_at=?,status='complete',detail=? WHERE attempt_id=?", (now(), "Verified MKV, streams, duration, size and SHA-256", attempt))
        cleanup = cleanup_completed(db, manifest, media, root)
        return {"status": "complete", **manifest, "path": str(media), "cleanup": cleanup}
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        with db:
            db.execute("UPDATE acquisition_attempts SET finished_at=?,status=?,detail=? WHERE attempt_id=?", (now(), status, safe_text(exc)[:1000] or type(exc).__name__, attempt))
        raise
