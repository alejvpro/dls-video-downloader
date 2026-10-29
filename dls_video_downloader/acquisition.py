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

from .classification import CHANNEL, DEFAULT_START
from .inventory import FIELDS, encoded, now, report

PROJECT = Path(__file__).resolve().parent.parent
YT_DLP = "/usr/local/bin/yt-dlp-new"
NODE = "/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
MAX_HEIGHT = 720


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, log, timeout):
    # A terminated yt-dlp must not leave its FFmpeg/Node children running after
    # the parent releases the operation lock.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, stderr = process.communicate()
            log.write_text(re.sub(r"https?://[^\s'\"]+", "[URL omitted]", stderr))
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ValueError("External command timed out; partial output retained") from exc
            raise
    # Verbose merger evidence is useful; playback and other request URLs are not.
    diagnostic = re.sub(r"https?://[^\s'\"]+", "[URL omitted]", stderr)
    log.write_text(diagnostic)
    if process.returncode:
        raise ValueError(f"External command failed (exit {process.returncode}); see {log.name}")
    return stdout


def verify(path, duration, probe_log):
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
    if v.get("codec_name") != "av1" or v.get("width") != 1280 or v.get("height") != MAX_HEIGHT or a.get("codec_name") != "opus":
        raise ValueError("Output does not contain required 720p AV1 and Opus")
    numerator, denominator = map(int, v.get("avg_frame_rate", "0/1").split("/"))
    if not denominator or abs(numerator / denominator - 30) > 0.1:
        raise ValueError("Video is not approximately 30fps")
    actual = float(data["format"]["duration"])
    if not duration or abs(actual - duration) > max(10, duration * 0.001):
        raise ValueError("Media duration differs from expected duration")
    return {"container": "mkv", "duration_seconds": actual, "video_codec": "av1",
            "audio_codec": "opus", "width": v.get("width"), "height": v["height"], "fps": numerator / denominator}


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
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".acquisition.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another acquisition or discovery operation is running") from exc
        yield


def acquire(db, video_id, start=DEFAULT_START, *, root=None, _lock_held=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Invalid YouTube video ID")
    selected = next((i for i in report(db, start)["items"] if i["id"] == video_id), None)
    if not selected or selected["eligibility"] != "eligible":
        raise ValueError("Video is not an eligible effective Download candidate")
    root = (root or PROJECT / "state" / "acquisitions").resolve()
    if not root.is_relative_to(PROJECT):
        raise ValueError("Acquisition output must remain inside the project")
    root.mkdir(parents=True, exist_ok=True)
    if _lock_held:
        return _acquire_locked(db, video_id, root)
    with operation_lock(root):
        return _acquire_locked(db, video_id, root)


def _acquire_locked(db, video_id, root):
    final = root / video_id
    final.mkdir(exist_ok=True)
    if final.is_symlink() or final.resolve().parent != root:
        raise ValueError("Unsafe video output directory")
    media, receipt = final / "media.mkv", final / "acquisition.json"
    if receipt.exists():
        manifest = json.loads(receipt.read_text())
        if manifest["video_id"] != video_id or sha256(media) != manifest["sha256"] or media.stat().st_size != manifest["size"]:
            raise ValueError("Existing acquisition failed identity/size/checksum verification; refusing replacement")
        verify(media, manifest["expected_duration"], final / "repeat-ffprobe.stderr.log")
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
            attempt, video_id, now(), "running", "Strict 398+251 acquisition", str(work.relative_to(PROJECT))))
    common = [YT_DLP, "--ignore-config", "--no-cache-dir", "--no-playlist", "--js-runtimes", "node:" + NODE,
              "--no-write-comments", "--no-write-subs", "--no-write-auto-subs", "--no-write-thumbnail",
              "--no-write-info-json", "--socket-timeout", "30", "--retries", "0", "--fragment-retries", "0",
              "--extractor-retries", "0", "--ffmpeg-location", FFMPEG]
    url = "https://www.youtube.com/watch?v=" + video_id
    try:
        print(f"{video_id}: checking current metadata and strict formats", flush=True)
        metadata = json.loads(run(common + ["--skip-download", "--dump-single-json", url], work / "preflight.stderr.log", 180))
        if (metadata.get("id") != video_id or metadata.get("channel_id") != CHANNEL
                or metadata.get("live_status") not in ("was_live", "not_live") or metadata.get("is_live")):
            raise ValueError("Unexpected source identity or video not ready/completed")
        formats = {f["format_id"]: f for f in metadata.get("formats", [])}
        video, audio = formats.get("398", {}), formats.get("251", {})
        if (video.get("height") != MAX_HEIGHT or video.get("fps") != 30
                or not str(video.get("vcodec", "")).startswith("av01") or audio.get("acodec") != "opus"
                or video.get("has_drm") or audio.get("has_drm")):
            raise ValueError("Required 398 AV1 720p30 + 251 Opus formats unavailable")
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in (video, audio)]
        if not all(isinstance(s, (int, float)) and s > 0 for s in sizes):
            raise ValueError("Cannot establish expected storage requirement")
        required = int(sum(sizes) * 3) + 1024**3
        free = shutil.disk_usage(root).free
        if free < required:
            raise ValueError(f"Insufficient local storage: need {required} bytes, have {free}")
        (work / "metadata.json").write_text(encoded({k: metadata[k] for k in FIELDS if metadata.get(k) is not None}))
        print(f"{video_id}: downloading only 398+251; storage budget {required} bytes, free {free}", flush=True)
        output = work / "media.mkv"
        run(common + ["--verbose", "--newline", "--no-overwrites", "--fixup", "never", "-f", "398+251",
                      "--merge-output-format", "mkv", "-o", str(work / "media.%(ext)s"), url], work / "download.stderr.log", 7200)
        merger_log = (work / "download.stderr.log").read_text()
        if "-c copy" not in merger_log and "-c:v copy" not in merger_log:
            raise ValueError("Missing FFmpeg stream-copy evidence; refusing completion")
        verified = verify(output, metadata.get("duration"), work / "ffprobe.stderr.log")
        evidence = final / "attempts" / attempt
        manifest = {"schema_version": 1, "video_id": video_id, "attempt_id": attempt,
                    "completed_at": now(), "expected_duration": metadata["duration"],
                    "video_format": "398", "audio_format": "251", "transcoded": False,
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
            db.execute("UPDATE acquisition_attempts SET finished_at=?,status=?,detail=? WHERE attempt_id=?", (now(), status, type(exc).__name__, attempt))
        raise
