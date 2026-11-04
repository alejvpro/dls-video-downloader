"""Offline preservation of yt-dlp comment objects; no retrieval or video inventory access."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid


IRISHWEEGEE = "UCbzm6UmzlMA-Yq6v3hvEipA"
APPLICATION_ID = 0x444C5343
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
COMMENT_ID = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
OUTCOMES = {"success", "empty", "disabled", "partial", "uncertain", "failed", "invalid"}


def encoded(value):
    """Canonical UTF-8 JSON, without changing strings or accepting lossy JSON types."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("Expected finite JSON values with string object keys")
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def timestamp(value=None):
    if value is not None and not isinstance(value, str):
        raise ValueError("Collection time must be an ISO timestamp string")
    instant = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Collection time must include a timezone")
    return instant.astimezone(timezone.utc).isoformat()


SCHEMA = (
    """CREATE TABLE snapshots (
        video_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL, comment_count INTEGER NOT NULL,
        PRIMARY KEY(video_id, snapshot_hash))""",
    """CREATE TABLE collection_attempts (
        attempt_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, collected_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL, tool_json TEXT NOT NULL, configuration_json TEXT NOT NULL,
        cap INTEGER, requested_outcome TEXT NOT NULL, outcome TEXT NOT NULL,
        warnings_json TEXT NOT NULL, error TEXT, coverage_json TEXT NOT NULL,
        retrieved_count INTEGER, top_level_count INTEGER, reply_count INTEGER,
        unknown_parent_count INTEGER, snapshot_hash TEXT, target_matches_json TEXT NOT NULL,
        UNIQUE(attempt_id, video_id),
        FOREIGN KEY(video_id, snapshot_hash) REFERENCES snapshots)""",
    """CREATE TABLE comment_versions (
        video_id TEXT NOT NULL, comment_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        text_hash TEXT NOT NULL, author_id TEXT, author TEXT, parent_id TEXT,
        first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
        PRIMARY KEY(video_id, comment_id, payload_hash))""",
    """CREATE TABLE attempt_comments (
        attempt_id TEXT NOT NULL, video_id TEXT NOT NULL, comment_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, position INTEGER NOT NULL,
        PRIMARY KEY(attempt_id, comment_id), UNIQUE(attempt_id, position),
        FOREIGN KEY(attempt_id, video_id) REFERENCES collection_attempts(attempt_id, video_id),
        FOREIGN KEY(video_id, comment_id, payload_hash) REFERENCES comment_versions)""",
    """CREATE TABLE refresh_state (
        video_id TEXT PRIMARY KEY, last_attempt_id TEXT NOT NULL,
        last_successful_attempt_id TEXT, last_nonempty_snapshot_hash TEXT,
        next_due_at TEXT, failure_count INTEGER NOT NULL DEFAULT 0,
        suspended INTEGER NOT NULL DEFAULT 0, needs_review INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(last_attempt_id) REFERENCES collection_attempts,
        FOREIGN KEY(last_successful_attempt_id) REFERENCES collection_attempts,
        FOREIGN KEY(video_id, last_nonempty_snapshot_hash) REFERENCES snapshots)""",
    """CREATE TABLE target_presence (
        video_id TEXT NOT NULL, author_id TEXT NOT NULL,
        first_observed_at TEXT, last_observed_at TEXT,
        PRIMARY KEY(video_id, author_id))""",
    "CREATE INDEX attempts_video_time ON collection_attempts(video_id, collected_at)",
)


class CommentStore:
    """Use an explicit dedicated root, normally state/comments; close after use.

    Only comment objects belong in ``comments``. Tool/configuration/coverage are
    caller-supplied JSON provenance, not full extraction documents or credentials.
    Invalid comment collections are recorded as invalid attempts, without importing
    a subset. Invalid API parameters raise ValueError before any attempt is written.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "comments.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            with self._transaction():
                app = self.db.execute("PRAGMA application_id").fetchone()[0]
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if app == 0 and version == 0 and not tables:
                    for statement in SCHEMA:
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute("PRAGMA user_version=1")
                elif (app, version) != (APPLICATION_ID, 1):
                    raise ValueError("Not a supported comment catalog; refusing migration")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _publish(self, video_id, snapshot_hash, raw):
        """Publish complete bytes without replacing evidence; verify existing bytes."""
        directory = self.root / video_id / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{snapshot_hash}.json"
        if final.exists():
            if final.read_bytes() != raw:
                raise ValueError("Existing comment snapshot is damaged; refusing replacement")
            return
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, final)
            except FileExistsError:
                if final.read_bytes() != raw:
                    raise ValueError("Conflicting comment snapshot; refusing replacement")
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)

    def record(self, video_id, comments=None, *, outcome, coverage=None, cap=None,
               tool=None, configuration=None, warnings=None, error=None,
               collected_at=None, target_author_ids=(IRISHWEEGEE,)):
        """Record one attempt and return its ID, effective outcome, hashes and matches.

        ``coverage={'assessment': 'complete', ...evidence...}`` is an explicit
        caller assertion. Without it, success/empty becomes uncertain. Reaching
        cap always becomes partial. A failed extraction with usable objects is
        preserved as partial, with requested_outcome='failed'. No retry is run.
        """
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid YouTube video ID")
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise ValueError("Unknown collection outcome")
        if cap is not None and (type(cap) is not int or cap < 1):
            raise ValueError("cap must be a positive integer or None")
        at = timestamp(collected_at)
        recorded_at = timestamp()
        tool = {} if tool is None else tool
        configuration = {} if configuration is None else configuration
        coverage = {} if coverage is None else coverage
        warnings = [] if warnings is None else warnings
        if not all(type(v) is dict for v in (tool, configuration, coverage)):
            raise ValueError("Tool, configuration and coverage must be objects")
        if type(warnings) is not list or any(type(w) is not str for w in warnings):
            raise ValueError("Warnings must be a list of strings")
        if error is not None and type(error) is not str:
            raise ValueError("Error must be a string or None")
        assessment = coverage.get("assessment", "unknown")
        if assessment not in ("complete", "partial", "unknown"):
            raise ValueError("Unknown coverage assessment")
        if not isinstance(target_author_ids, (list, tuple)) or any(
                not isinstance(a, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", a)
                for a in target_author_ids):
            raise ValueError("Targets must be a list/tuple of YouTube channel IDs")
        targets = sorted(set(target_author_ids))
        # Round-trip to detach caller-owned data, validate JSON and avoid mutation.
        provenance = json.loads(encoded([tool, configuration, coverage, warnings]))
        tool, configuration, evidence, warnings = provenance
        requested = outcome
        usable = None
        validation_error = None
        try:
            if comments is None:
                if outcome not in ("disabled", "failed", "invalid"):
                    raise ValueError("This outcome requires a comment list")
            else:
                if type(comments) is not list:
                    raise ValueError("Comments must be a list of objects")
                if outcome in ("disabled", "invalid") and comments:
                    raise ValueError("Disabled/invalid outcome cannot contain usable comments")
                seen = set()
                for c in comments:
                    if type(c) is not dict or not isinstance(c.get("id"), str) or not COMMENT_ID.fullmatch(c["id"]):
                        raise ValueError("Malformed comment ID or object")
                    if c["id"] in seen:
                        raise ValueError("Duplicate comment ID")
                    seen.add(c["id"])
                    if type(c.get("text")) is not str:
                        raise ValueError("Comment text must be a string")
                    parent = c.get("parent")
                    if parent is not None and (not isinstance(parent, str) or not COMMENT_ID.fullmatch(parent)):
                        raise ValueError("Malformed parent comment ID")
                    for key in ("author", "author_id"):
                        if c.get(key) is not None and type(c[key]) is not str:
                            raise ValueError(f"{key} must be a string or null")
                usable = json.loads(encoded(comments))
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            validation_error = str(exc)
            outcome = "invalid"
            usable = None
        count = len(comments) if type(comments) is list else None
        cap_reached = cap is not None and count is not None and count >= cap
        if validation_error:
            error = f"{error + '; ' if error else ''}Invalid input: {validation_error}"
        if outcome in ("success", "empty"):
            outcome = ("partial" if cap_reached or assessment == "partial" else
                       "uncertain" if assessment != "complete" else
                       "success" if usable else "empty")
        elif outcome == "uncertain" and (cap_reached or assessment == "partial"):
            outcome = "partial"
        elif outcome == "failed" and usable:
            outcome = "partial"
        evidence["effective_assessment"] = (
            "complete" if outcome in ("success", "empty") else
            "unknown" if outcome == "uncertain" else outcome)
        evidence["cap_reached"] = cap_reached
        # Disabled/failure without comments has no snapshot, rather than a false empty set.
        save = usable is not None and outcome not in ("disabled", "failed", "invalid")
        snapshot_hash = None
        raw = None
        if save:
            raw = encoded({"format_version": 1, "video_id": video_id,
                           "comments": sorted(usable, key=lambda c: c["id"])})
            snapshot_hash = digest(raw)
        matches = {author: [c["id"] for c in (usable or []) if c.get("author_id") == author]
                   for author in targets}
        parents = sum(c.get("parent") == "root" for c in usable) if usable is not None else None
        replies = sum(c.get("parent") not in (None, "root") for c in usable) if usable is not None else None
        unknown = len(usable) - parents - replies if usable is not None else None
        attempt_id = uuid.uuid4().hex
        with self._transaction():
            if save:
                # Filesystem publication precedes catalog commit. A rollback may leave
                # an unreferenced valid snapshot, which retry safely verifies/reuses.
                self._publish(video_id, snapshot_hash, raw)
                self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?)",
                                (video_id, snapshot_hash, len(usable)))
            self.db.execute("""INSERT INTO collection_attempts VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                attempt_id, video_id, at, recorded_at, encoded(tool).decode(),
                encoded(configuration).decode(), cap, requested, outcome,
                encoded(warnings).decode(), error, encoded(evidence).decode(), count,
                parents, replies, unknown, snapshot_hash, encoded(matches).decode()))
            for position, c in enumerate(usable if save else []):
                payload_hash = digest(encoded(c))
                self.db.execute("""INSERT INTO comment_versions VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(video_id, comment_id, payload_hash) DO UPDATE SET
                    first_observed_at=min(first_observed_at, excluded.first_observed_at),
                    last_observed_at=max(last_observed_at, excluded.last_observed_at)""", (
                    video_id, c["id"], payload_hash, digest(c["text"].encode("utf-8")),
                    c.get("author_id"), c.get("author"), c.get("parent"), at, at))
                self.db.execute("INSERT INTO attempt_comments VALUES(?,?,?,?,?)",
                                (attempt_id, video_id, c["id"], payload_hash, position))
            for author, comment_ids in matches.items():
                seen_at = at if comment_ids else None
                self.db.execute("""INSERT INTO target_presence VALUES(?,?,?,?)
                    ON CONFLICT(video_id, author_id) DO UPDATE SET
                    first_observed_at=CASE WHEN excluded.first_observed_at IS NULL THEN first_observed_at
                        WHEN first_observed_at IS NULL THEN excluded.first_observed_at
                        ELSE min(first_observed_at, excluded.first_observed_at) END,
                    last_observed_at=CASE WHEN excluded.last_observed_at IS NULL THEN last_observed_at
                        WHEN last_observed_at IS NULL THEN excluded.last_observed_at
                        ELSE max(last_observed_at, excluded.last_observed_at) END""",
                    (video_id, author, seen_at, seen_at))
            # Refresh fields are reserved state, not a scheduling implementation.
            self.db.execute("INSERT OR IGNORE INTO refresh_state(video_id,last_attempt_id) VALUES(?,?)",
                            (video_id, attempt_id))
            latest = self.db.execute("""SELECT collected_at FROM collection_attempts WHERE attempt_id=
                (SELECT last_attempt_id FROM refresh_state WHERE video_id=?)""", (video_id,)).fetchone()[0]
            if at >= latest:
                unsuccessful = requested in ("failed", "invalid") or outcome in ("partial", "invalid")
                self.db.execute("""UPDATE refresh_state SET last_attempt_id=?,
                    failure_count=CASE WHEN ? THEN failure_count+1 ELSE 0 END,
                    needs_review=CASE WHEN ? THEN 1 ELSE needs_review END WHERE video_id=?""",
                    (attempt_id, unsuccessful, outcome == "invalid", video_id))
            if outcome in ("success", "empty"):
                self.db.execute("""UPDATE refresh_state SET last_successful_attempt_id=? WHERE video_id=?
                    AND (last_successful_attempt_id IS NULL OR ? >=
                    (SELECT collected_at FROM collection_attempts WHERE attempt_id=last_successful_attempt_id))""",
                    (attempt_id, video_id, at))
            if save and usable:
                self.db.execute("""UPDATE refresh_state SET last_nonempty_snapshot_hash=(
                    SELECT snapshot_hash FROM collection_attempts WHERE video_id=?
                    AND snapshot_hash IS NOT NULL AND retrieved_count>0
                    ORDER BY collected_at DESC, recorded_at DESC, attempt_id DESC LIMIT 1)
                    WHERE video_id=?""", (video_id, video_id))
        return {"attempt_id": attempt_id, "outcome": outcome,
                "snapshot_hash": snapshot_hash, "target_matches": matches}

    def load_snapshot(self, video_id, snapshot_hash):
        """Read original objects, verifying content address and video identity."""
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid YouTube video ID")
        if not isinstance(snapshot_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_hash):
            raise ValueError("Invalid snapshot hash")
        raw = (self.root / video_id / "snapshots" / f"{snapshot_hash}.json").read_bytes()
        if digest(raw) != snapshot_hash:
            raise ValueError("Comment snapshot hash mismatch")
        result = json.loads(raw)
        if result.get("video_id") != video_id or result.get("format_version") != 1:
            raise ValueError("Unexpected comment snapshot identity or format")
        return result
