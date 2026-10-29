"""Small persistent retry, retention and reporting policies; no scheduler."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
import re
import stat
import traceback

from .inventory import encoded

DEFAULT_STALE_HOURS = 72
LOG_LIMIT = 256 * 1024
RUN_LIMIT = 100
RUN_BYTES = 64 * 1024 * 1024
PROJECT = Path(__file__).resolve().parent.parent


class OperationBusy(ValueError):
    pass


def utc():
    return datetime.now(timezone.utc)


def safe_text(value):
    text = re.sub(r"https?://[^\s'\"]+", "[URL omitted]", str(value))
    return "\n".join("[sensitive diagnostic omitted]" if re.search(
        r"(?i)(?:authorization|set-cookie|cookie|password|access_token)\s*[:=]", line) else line for line in text.splitlines())


def write_log(path, text):
    raw = safe_text(text).encode()
    if len(raw) > LOG_LIMIT:
        head = raw[:LOG_LIMIT // 2].decode('utf-8', errors='ignore').encode()
        tail = raw[-LOG_LIMIT // 2 + 40:].decode('utf-8', errors='ignore').encode()
        raw = head + b"\n[diagnostic truncated]\n" + tail
    path.write_bytes(raw)


def fatal_diagnostic(error):
    """Useful stack locations without source lines or locals containing secrets."""
    lines, seen = [], set()
    while error is not None and id(error) not in seen and len(seen) < 3:
        seen.add(id(error))
        if lines:
            lines.append('  preceding exception:')
        lines.append(f"{type(error).__name__}: {safe_text(error)}")
        for frame in traceback.extract_tb(error.__traceback__):
            lines.append(f"  at {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return '\n'.join(lines)


def failure_category(error):
    text = str(error).lower()
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    if any(s in text for s in ("manual recovery", "checksum", "output does not", "duration differs", "not matroska", "stream-copy evidence")):
        return "verification"
    if any(s in text for s in ("398", "formats", "format is not available")):
        return "formats"
    if any(s in text for s in ("private video", "removed", "unavailable", "not available", "members-only", "sign in", "403")):
        return "unavailable"
    if any(s in text for s in ("storage", "space", "disk full")):
        return "storage"
    return "transient"


def retry_failure(db, video_id, operation, error, *, clock=None, base_minutes=60, max_hours=24):
    at = clock or utc()
    previous = db.execute("SELECT failures FROM retry_state WHERE video_id=? AND operation=?", (video_id, operation)).fetchone()
    count = (previous[0] if previous else 0) + 1
    category = failure_category(error)
    minimum = {"transient": base_minutes, "interrupted": base_minutes,
               "formats": 360, "storage": 360, "unavailable": 1440, "verification": 1440}[category]
    delay = min(max_hours * 60, minimum * 2 ** min(count - 1, 10))
    row = (video_id, operation, count, category, safe_text(error)[:1000] or type(error).__name__,
           at.isoformat(), (at + timedelta(minutes=delay)).isoformat())
    with db:
        db.execute("INSERT INTO retry_state VALUES(?,?,?,?,?,?,?) ON CONFLICT(video_id,operation) DO UPDATE SET failures=excluded.failures,category=excluded.category,reason=excluded.reason,last_failure=excluded.last_failure,next_retry=excluded.next_retry", row)
    return retry_pending(db, video_id, operation, clock=at)


def retry_pending(db, video_id, operation, *, clock=None):
    row = db.execute("SELECT * FROM retry_state WHERE video_id=? AND operation=?", (video_id, operation)).fetchone()
    if row and datetime.fromisoformat(row["next_retry"]) > (clock or utc()):
        return dict(row)
    return None


def retry_success(db, video_id, operation):
    with db:
        db.execute("DELETE FROM retry_state WHERE video_id=? AND operation=?", (video_id, operation))


def forward_allowed(item, start):
    # Manual historical approval remains intact but never bypasses this runner policy.
    return item["eligibility"] == "eligible" and bool(item["publication_date"] and item["publication_date"] >= start)


def cleanup_staging(db, state, *, execute=False, retention_hours=DEFAULT_STALE_HOURS,
                    max_bytes=5 * 1024**3, max_files=100, clock=None):
    """Caller must hold the shared operation lock. No recursive deletion."""
    if retention_hours < 24 or max_bytes < 1 or not 1 <= max_files <= 1000:
        raise ValueError("Cleanup requires retention >=24h, positive byte limit and 1..1000 files")
    at = clock or utc()
    root = state / "acquisitions"
    protected = set()
    inodes = set()
    for row in db.execute("SELECT * FROM acquisitions"):
        for field in ("media_relative", "manifest_relative"):
            path = PROJECT / row[field]
            protected.add(path.resolve())
            if path.exists():
                info = path.stat(); inodes.add((info.st_dev, info.st_ino))
        receipt = PROJECT / row["manifest_relative"]
        manifest = json.loads(receipt.read_text())  # Unreadable receipt fails closed.
        for field in ("metadata_relative", "merger_log_relative"):
            if manifest.get(field):
                protected.add((PROJECT / manifest[field]).resolve())
    candidates, skipped = [], []
    total = 0
    media_pattern = r"media(?:\.f(?:398|251))?(?:\.temp)?\.(?:mkv|mp4|webm)(?:\.part(?:-Frag\d+)?|\.ytdl)?"
    evidence_names = {"metadata.json", "preflight.stderr.log", "download.stderr.log", "ffprobe.stderr.log"}
    for row in db.execute("SELECT * FROM acquisition_attempts WHERE status!='complete' ORDER BY started_at"):
        directory = PROJECT / row["work_relative"]
        expected = root / ".work" / row["video_id"] / row["attempt_id"]
        if (not re.fullmatch(r"[0-9a-f]{32}", row["attempt_id"]) or directory != expected
                or directory.resolve() != directory.absolute() or not directory.is_dir()):
            continue
        files = list(directory.iterdir())
        if any(p.is_symlink() or not p.is_file() for p in files):
            skipped.append({"attempt_id": row["attempt_id"], "reason": "unexpected or symlinked contents"}); continue
        if any(p.resolve() in protected for p in files):
            skipped.append({"attempt_id": row["attempt_id"], "reason": "manifest-referenced evidence"}); continue
        times = [datetime.fromisoformat(row['started_at']).timestamp()]
        if row['finished_at']:
            times.append(datetime.fromisoformat(row['finished_at']).timestamp())
        times.extend(max(p.stat().st_mtime, p.stat().st_ctime) for p in files)
        observed = max(times)
        if at.timestamp() - observed < retention_hours * 3600:
            continue
        for path in files:
            is_media = bool(re.fullmatch(media_pattern, path.name))
            if not is_media and not (path.name in evidence_names and at.timestamp() - observed >= 30 * 86400):
                continue
            info = path.stat()
            if info.st_nlink != 1 or (info.st_dev, info.st_ino) in inodes:
                skipped.append({"path": str(path), "reason": "linked/protected file"}); continue
            if len(candidates) >= max_files or total + info.st_size > max_bytes:
                continue
            total += info.st_size
            candidate = {"path": str(path), "attempt_id": row["attempt_id"], "size": info.st_size}
            candidates.append(candidate)
            if execute:
                current = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_nlink) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink):
                    raise ValueError("Staging file changed during cleanup")
                path.unlink()
                with db:
                    db.execute("INSERT INTO cleanup_events VALUES(?,?,?,?)", (at.isoformat(), row["attempt_id"], str(path.relative_to(PROJECT)), info.st_size))
                    db.execute("DELETE FROM cleanup_events WHERE rowid IN (SELECT rowid FROM cleanup_events ORDER BY rowid DESC LIMIT -1 OFFSET 5000)")
        if execute and row["status"] == "running":
            with db:
                db.execute("UPDATE acquisition_attempts SET status='interrupted',finished_at=?,detail=? WHERE attempt_id=?", (at.isoformat(), "Stale abandoned attempt recovered under operation lock", row["attempt_id"]))
            retry_failure(db, row["video_id"], "acquisition", KeyboardInterrupt("Abandoned acquisition"), clock=at)
        if execute and not any(directory.iterdir()):
            directory.rmdir()
    return {"execute": execute, "retention_hours": retention_hours, "files": candidates, "bytes": total, "skipped": skipped}


def retain_run_logs(db, state, *, current_run=None, clock=None):
    """Only completed registered run directories; never acquisition evidence."""
    at = clock or utc()
    root = state / "runs"
    rows = db.execute("SELECT * FROM runner_runs WHERE status!='running' ORDER BY started_at DESC").fetchall()
    used = count = 0
    for row in rows:
        path = root / row["run_id"]
        if row["run_id"] == current_run or path.resolve() != path.absolute() or not path.is_dir():
            continue
        files = list(path.iterdir())
        if any(p.is_symlink() or not p.is_file() or p.name not in ("events.jsonl", "summary.json") and not p.name.endswith('.stderr.log') for p in files):
            continue
        size = sum(p.stat().st_size for p in files)
        count += 1; used += size
        if count > RUN_LIMIT or used > RUN_BYTES or (at - datetime.fromisoformat(row["started_at"])).total_seconds() > 30 * 86400:
            for p in files:
                p.unlink()
            path.rmdir()
    with db:
        db.execute("DELETE FROM runner_runs WHERE run_id IN (SELECT run_id FROM runner_runs WHERE status!='running' ORDER BY started_at DESC LIMIT -1 OFFSET 200)")


@contextmanager
def run_record(db, state, result):
    """Runs only while the shared lock is held; old running rows are abandoned."""
    started = utc().isoformat()
    with db:
        db.execute("UPDATE runner_runs SET status='interrupted',finished_at=? WHERE status='running'", (started,))
        db.execute("INSERT INTO runner_runs VALUES(?,?,NULL,'running','{}')", (result['run_id'], started))
    result['started_at'] = started
    try:
        yield
        result['status'] = 'partial_failure' if result['failures'] else 'success'
    except KeyboardInterrupt:
        result['status'] = 'interrupted'
        raise
    except Exception as exc:
        result['status'] = 'fatal_failure'
        result['failures'].append({'operation': 'runner', 'error': fatal_diagnostic(exc)[:2000]})
        try:
            write_log(state / 'runs' / result['run_id'] / 'fatal.stderr.log', fatal_diagnostic(exc))
        except OSError:
            pass  # Preserve the original failure; CLI stderr is still available.
        raise
    finally:
        result['finished_at'] = utc().isoformat()
        directory = state / 'runs' / result['run_id']
        (directory / 'summary.json').write_text(encoded(result))
        # Compact database history; detailed per-item evidence stays in bounded logs.
        summary = {k: result.get(k) for k in ['encountered', 'new_ids', 'acquired', 'failures', 'download_attempts', 'warnings']}
        with db:
            db.execute("UPDATE runner_runs SET finished_at=?,status=?,summary=? WHERE run_id=?",
                       (result['finished_at'], result['status'], encoded(summary), result['run_id']))
        retain_run_logs(db, state, current_run=result['run_id'])


def runtime_report(db, items, start):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    verified = [dict(r) for r in db.execute("SELECT * FROM acquisitions")] if 'acquisitions' in names else []
    ids = {r['video_id'] for r in verified}
    runs = [dict(r) for r in db.execute("SELECT run_id,started_at,finished_at,status,summary FROM runner_runs ORDER BY started_at DESC LIMIT 5")] if 'runner_runs' in names else []
    for row in runs:
        row['summary'] = json.loads(row['summary'])
    return {"verified_acquisitions": len(verified),
            "pending_forward": [i['id'] for i in items if forward_allowed(i, start) and i['id'] not in ids],
            "retry_delays": [dict(r) for r in db.execute("SELECT * FROM retry_state ORDER BY next_retry")] if 'retry_state' in names else [],
            "recent_runs": runs}


def record_overlap(db, run_id):
    """A tiny SQLite receipt; no network, filesystem cleanup or acquisition."""
    at = utc().isoformat()
    try:
        with db:
            db.execute("INSERT INTO runner_runs VALUES(?,?,?,?,?)", (run_id, at, at, 'overlap', '{}'))
            # Do not orphan registered run directories during a burst of overlaps.
            db.execute("DELETE FROM runner_runs WHERE run_id IN (SELECT run_id FROM runner_runs WHERE status='overlap' ORDER BY started_at DESC LIMIT -1 OFFSET 50)")
    except sqlite3.Error:
        # The safe-overlap exit still takes precedence if SQLite is busy/unavailable.
        pass
