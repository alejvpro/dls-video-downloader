"""Bounded, explicitly invoked single-video comment collection, independent of media."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from .comments import CommentStore, VIDEO_ID, timestamp


PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = PROJECT / "state/comments"
YT_DLP = "/usr/local/bin/yt-dlp-new"
NODE = "/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
STDOUT_LIMIT = 32 * 1024 * 1024
STDERR_LIMIT = 256 * 1024
DIAGNOSTIC_LIMIT = 64 * 1024


class CommentBusy(ValueError):
    pass


def safe_diagnostic(value):
    text = re.sub(r"https?://[^\s'\"]+", "[URL omitted]", str(value))
    lines = ["[sensitive diagnostic omitted]" if re.search(
        r"(?i)(?:authorization|set-cookie|cookie|password|access_token)\s*[:=]", line)
        else line[:1000] for line in text.splitlines()]
    raw = "\n".join(lines).encode("utf-8")
    if len(raw) > DIAGNOSTIC_LIMIT:
        half = (DIAGNOSTIC_LIMIT - 40) // 2
        raw = raw[:half] + b"\n[diagnostic truncated]\n" + raw[-half:]
    return raw.decode("utf-8", errors="ignore")


@contextmanager
def collection_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".collector.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommentBusy("Another comment collector is active") from exc
        # Close releases ownership; children inherit the FD until cleanup finishes.
        yield lock.fileno()


@dataclass
class Execution:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int | None = None
    termination: str | None = None
    diagnostics_truncated: bool = False


def run_extractor(argv, timeout, lock_fd):
    """Drain bounded pipes without writing the video payload or allowing deadlock."""
    output = bytearray()
    diagnostics = bytearray()
    result = Execution()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               pass_fds=(lock_fd,))
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.termination = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stdout:
                        if len(output) + len(chunk) > STDOUT_LIMIT:
                            result.termination = "output_limit"
                            break
                        output.extend(chunk)
                    else:
                        diagnostics.extend(chunk)
                        if len(diagnostics) > STDERR_LIMIT:
                            half = STDERR_LIMIT // 2
                            diagnostics[:] = diagnostics[:half] + diagnostics[-(STDERR_LIMIT - half):]
                            result.diagnostics_truncated = True
                if result.termination:
                    break
            if not result.termination:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    result.termination = "timeout"
    except KeyboardInterrupt:
        result.termination = "interrupted"
    finally:
        # Also kill descendants that closed their pipes but outlived the parent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        finally:
            process.stdout.close()
            process.stderr.close()
    result.stdout = bytes(output)
    result.stderr = bytes(diagnostics)
    result.returncode = process.returncode
    return result


def command(video_id, max_comments):
    return [YT_DLP, "--ignore-config", "--no-plugin-dirs", "--no-cache-dir",
            "--no-playlist", "--no-js-runtimes", "--js-runtimes", "node:" + NODE,
            "--ffmpeg-location", FFMPEG, "--skip-download", "--write-comments",
            "--no-write-info-json", "--dump-single-json", "--no-write-subs",
            "--no-write-auto-subs", "--no-write-thumbnail", "--no-write-description",
            "--no-write-playlist-metafiles", "--no-ignore-errors",
            "--socket-timeout", "30", "--retries", "0", "--extractor-retries", "0",
            "--fragment-retries", "0", "--sleep-requests", "1", "--extractor-args",
            f"youtube:max_comments={max_comments},all,all,all,all;comment_sort=new",
            "--verbose", f"https://www.youtube.com/watch?v={video_id}"]


def assess(video_id, execution):
    """Assess only extractor structure/diagnostics, never comment content."""
    diagnostic = safe_diagnostic(execution.stderr.decode("utf-8", errors="replace"))
    warnings = [line for line in diagnostic.splitlines() if "WARNING:" in line][:32]
    errors = [line for line in diagnostic.splitlines() if "ERROR:" in line][:32]
    version = re.search(r"\[debug\] yt-dlp version ([^\n]+)", diagnostic)
    runtime = re.search(r"\[debug\] JS runtimes: ([^\n]+)", diagnostic)
    tool = {"name": "yt-dlp", "executable": YT_DLP,
            "version": version.group(1) if version else None,
            "node_executable": NODE, "js_runtimes": runtime.group(1) if runtime else None}
    truncated = (execution.diagnostics_truncated or len(execution.stderr) > DIAGNOSTIC_LIMIT
                 or any(len(line) > 1000 for line in execution.stderr.splitlines()))
    coverage = {"assessment": "unknown", "retrieval_state": "failed",
                "returncode": execution.returncode, "termination": execution.termination,
                "diagnostics_truncated": truncated,
                "diagnostics": diagnostic,
                "reason": "CLI JSON does not certify continuation exhaustion"}
    failure = execution.termination or (f"yt-dlp exited {execution.returncode}"
                                      if execution.returncode else None)
    if errors:
        failure = failure or errors[-1]
    comments = None
    outcome = "failed"
    try:
        data = json.loads(execution.stdout)
        if not isinstance(data, dict) or data.get("id") != video_id:
            raise ValueError("Extraction did not identify the requested video")
        if "comments" not in data:
            raise ValueError("Extraction omitted the comments field")
        reported = data.get("comment_count")
        coverage["reported_comment_count"] = reported if type(reported) is int else None
        comments = data["comments"]
        disabled = re.search(r"(?i)\bcomments (?:are |have been )?(?:disabled|turned off)\b", diagnostic)
        if comments is None:
            if disabled and not failure:
                outcome = "disabled"
                coverage["retrieval_state"] = "disabled"
            else:
                raise ValueError("No comment list; disabled status was not established")
        elif not isinstance(comments, list):
            outcome = "invalid"
            coverage["retrieval_state"] = "invalid"
        else:
            coverage["retrieval_state"] = "returned_nonempty" if comments else "returned_empty"
            incomplete = re.search(r"(?i)incomplete|looping|stopping comment|rate.?limit|too many requests|HTTP Error|interrupted by user", diagnostic)
            count_uncertain = "comment_count" in data and (reported is None or (
                type(reported) is int and reported != len(comments)))
            if failure:
                outcome = "failed"  # Store promotes usable failed output to partial.
            elif incomplete or count_uncertain or (disabled and comments):
                outcome = "partial"
            else:
                outcome = "uncertain"
            if outcome == "partial":
                coverage["assessment"] = "partial"
    except (ValueError, UnicodeError) as exc:
        # Never salvage fragments of JSON or associate another video's objects.
        comments = None
        failure = failure or str(exc)
    if execution.termination:
        coverage["retrieval_state"] = execution.termination
    if failure:
        coverage["failure"] = failure
    return comments, outcome, coverage, tool, warnings, failure


def collect(video_id, *, root=DEFAULT_ROOT, max_comments=1000, timeout=600):
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Expected an 11-character YouTube video ID")
    if type(max_comments) is not int or not 1 <= max_comments <= 1000:
        raise ValueError("Comment limit must be 1..1000")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("Process timeout must be 1..600 seconds")
    root = Path(root).resolve()
    if root == PROJECT / "state" or root.is_relative_to(PROJECT / "state/acquisitions"):
        raise ValueError("Comment store must be separate from video operational data")
    with collection_lock(root) as lock_fd:
        with CommentStore(root) as store:
            started_at = timestamp()
            argv = command(video_id, max_comments)
            try:
                execution = run_extractor(argv, timeout, lock_fd)
            except OSError as exc:
                execution = Execution(stderr=safe_diagnostic(exc).encode(), termination="launch_failed")
            except KeyboardInterrupt:
                execution = Execution(termination="interrupted")
            comments, outcome, coverage, tool, warnings, error = assess(video_id, execution)
            coverage["finished_at"] = timestamp()
            configuration = {"max_comments": f"{max_comments},all,all,all,all", "comment_sort": "new",
                             "socket_timeout_seconds": 30, "process_timeout_seconds": timeout,
                             "retries": 0, "sleep_requests_seconds": 1,
                             "stdout_limit_bytes": STDOUT_LIMIT, "stderr_limit_bytes": STDERR_LIMIT}
            result = store.record(video_id, comments, outcome=outcome, coverage=coverage,
                                  cap=max_comments, tool=tool, configuration=configuration,
                                  warnings=warnings, error=error, collected_at=started_at)
            try:
                portable = store.export_attempt(result["attempt_id"])
            except (OSError, ValueError) as exc:
                raise ValueError(f"Attempt {result['attempt_id']} committed, but portable export failed; "
                                 "retry CommentStore.export_attempt offline") from exc
            record = json.loads(portable.read_bytes())
            attempt = record["collection"]
            snapshot = record["snapshot"]
            return {**result, "video_id": video_id, "retrieval_state": coverage["retrieval_state"],
                    "retrieved_count": attempt["retrieved_count"], "top_level_count": attempt["top_level_count"],
                    "reply_count": attempt["reply_count"], "coverage": attempt["coverage"],
                    "warnings": warnings, "error": attempt["error"], "snapshot": snapshot,
                    "snapshot_path": str(root / video_id / snapshot["path"]) if snapshot else None,
                    "collection_record": str(portable), "catalog": str(root / "comments.sqlite3"),
                    "exit_code": 130 if execution.termination == "interrupted" else
                    2 if execution.termination or result["outcome"] in ("failed", "partial", "invalid") else 0}
