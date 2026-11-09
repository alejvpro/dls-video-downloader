"""One-video, metadata-only format diagnostics behind shared YouTube safety."""
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import uuid

from . import acquisition, youtube_safety as safety
from .classification import CHANNEL

MAX_TIMEOUT = 180
MAX_FORMATS = 500
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
FORMAT_FIELDS = ("format_id", "ext", "format_note", "vcodec", "acodec", "width", "height",
                 "fps", "has_drm", "filesize", "filesize_approx", "availability",
                 "protocol", "language", "quality")


def command(video_id):
    """Return the bounded yt-dlp metadata command; it has no download options."""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube video ID")
    return [acquisition.YT_DLP, "--ignore-config", "--no-cache-dir", "--no-playlist",
            "--js-runtimes", "node:" + acquisition.NODE, "--no-write-comments",
            "--no-write-subs", "--no-write-auto-subs", "--no-write-thumbnail",
            "--no-write-info-json", "--skip-download", "--socket-timeout", "30",
            "--retries", "0", "--fragment-retries", "0", "--extractor-retries", "0",
            "--dump-single-json", "https://www.youtube.com/watch?v=" + video_id]


def _safe_format(row):
    result = {key: row[key] for key in FORMAT_FIELDS if key in row}
    result["has_drm"] = row.get("has_drm")
    return result


def _result(video_id, metadata):
    if not isinstance(metadata, dict) or metadata.get("id") != video_id:
        raise ValueError("yt-dlp metadata identity did not match requested video")
    if metadata.get("channel_id") != CHANNEL:
        raise ValueError("yt-dlp metadata was not from the configured DLS channel")
    rows = metadata.get("formats")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("yt-dlp format metadata was malformed")
    valid_rows = [row for row in rows if isinstance(row, dict)]
    safe_rows = [_safe_format(row) for row in valid_rows[:MAX_FORMATS]]
    assessment = acquisition.assess_required_formats(metadata)
    if not valid_rows:
        assessment["available"] = False
        assessment["reasons"].append("format_list_empty")
    return {
        "schema_version": 1,
        "mode": "executed",
        "video": {key: metadata[key] for key in (
            "id", "title", "channel_id", "live_status", "is_live", "was_live",
            "availability", "upload_date", "release_date", "duration") if key in metadata},
        "format_listing": {"status": "available" if valid_rows else "empty",
                           "count": len(valid_rows), "returned": len(safe_rows),
                           "truncated": len(valid_rows) > MAX_FORMATS},
        "formats": safe_rows,
        "strict_398_251": assessment,
        "exit_code": 0 if valid_rows else 2,
    }


def inspect(video_id, state, *, execute=False, timeout=MAX_TIMEOUT):
    """Plan read-only by default; execute exactly one metadata extraction when opted in."""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube video ID")
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError(f"Timeout must be 1..{MAX_TIMEOUT} seconds")
    state = Path(state).resolve()
    if not execute:
        return {"schema_version": 1, "mode": "plan", "video_id": video_id,
                "network": False, "writes": False, "timeout_seconds": timeout,
                "planned_operation": "one metadata-only format extraction"}

    with safety.scope(state, "format-inspection"):
        safety.current().ensure("format_inspection")
        root = state / "acquisitions"
        with acquisition.operation_lock(root):
            # The shared acquisition subprocess wrapper performs admission immediately
            # before launch and journals any confirmed blocking response.
            log_dir = state / "runs" / "formats"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            log = log_dir / f"{stamp}-{video_id}-{uuid.uuid4().hex[:8]}.stderr.log"
            output = acquisition.run(command(video_id), log, timeout)
            try:
                metadata = json.loads(output)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("yt-dlp returned invalid JSON metadata") from exc
            result = _result(video_id, metadata)
            result["diagnostics"] = {"stderr_log": str(log.relative_to(state)),
                                    "timeout_seconds": timeout, "network_requests": "one yt-dlp extraction"}
            return result
