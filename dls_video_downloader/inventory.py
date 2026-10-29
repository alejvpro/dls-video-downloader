"""SQLite inventory and sanitized immutable observations."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .classification import classify, eligibility, publication, RULE_VERSION

FIELDS = ("title", "description", "channel_id", "channel", "uploader_id", "uploader",
          "upload_date", "release_date", "timestamp", "release_timestamp", "duration",
          "live_status", "was_live", "is_live", "availability", "view_count",
          "like_count", "comment_count", "tags", "categories", "language")
SEEDS = {
    "Zvvx0Lcavkk": ("approve", "User-approved LeBron James reaction livestream"),
    "hukn5XofWLg": ("reject", "User-rejected USA vs. Bosnia reaction livestream"),
    "oLwsHdl7L8o": ("skip", "User excludes Football America"),
    "ncdF1Yx9eEE": ("skip", "User excludes Canes Insight"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def connect(path, readonly=False):
    path = Path(path).resolve()
    if readonly:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, 3) or (readonly and version not in (1, 2, 3)):
        db.close()
        raise ValueError(f"Unsupported inventory schema: {version}")
    if not readonly and version == 0:
        db.executescript("""
        CREATE TABLE videos (
          id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          initial_title TEXT, latest_title TEXT, metadata TEXT NOT NULL,
          field_provenance TEXT NOT NULL);
        CREATE TABLE observations (
          observation_id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
          detail TEXT NOT NULL, digest TEXT NOT NULL, metadata TEXT NOT NULL,
          observed_at TEXT NOT NULL, imported_at TEXT NOT NULL,
          UNIQUE(video_id, detail, digest));
        CREATE TABLE sources (
          video_id TEXT NOT NULL REFERENCES videos(id), source TEXT NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          PRIMARY KEY(video_id, source));
        CREATE TABLE imports (
          digest TEXT NOT NULL, source TEXT NOT NULL, input_name TEXT NOT NULL,
          imported_at TEXT NOT NULL, entry_count INTEGER NOT NULL,
          PRIMARY KEY(digest, source));
        CREATE TABLE decisions (
          decision_id INTEGER PRIMARY KEY, video_id TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('approve','reject','skip','clear')),
          reason TEXT NOT NULL, decided_at TEXT NOT NULL);
        CREATE TABLE classifications (
          classification_id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
          category TEXT NOT NULL, reason TEXT NOT NULL, rule_version TEXT NOT NULL,
          evidence_digest TEXT NOT NULL, classified_at TEXT NOT NULL);
        PRAGMA user_version=1;
        """)
    if not readonly and db.execute("PRAGMA user_version").fetchone()[0] == 1:
        db.executescript("""
        CREATE TABLE acquisition_attempts (
          attempt_id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
          started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
          detail TEXT NOT NULL, work_relative TEXT NOT NULL);
        CREATE TABLE acquisitions (
          video_id TEXT PRIMARY KEY REFERENCES videos(id), completed_at TEXT NOT NULL,
          media_relative TEXT NOT NULL, manifest_relative TEXT NOT NULL,
          size INTEGER NOT NULL, sha256 TEXT NOT NULL);
        PRAGMA user_version=2;
        """)
    if not readonly and db.execute("PRAGMA user_version").fetchone()[0] == 2:
        migration = """
        CREATE TABLE retry_state (
          video_id TEXT NOT NULL REFERENCES videos(id), operation TEXT NOT NULL,
          failures INTEGER NOT NULL, category TEXT NOT NULL, reason TEXT NOT NULL,
          last_failure TEXT NOT NULL, next_retry TEXT NOT NULL,
          PRIMARY KEY(video_id, operation));
        CREATE TABLE runner_runs (
          run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, summary TEXT NOT NULL);
        CREATE TABLE scan_frontiers (
          source TEXT PRIMARY KEY, head_id TEXT NOT NULL, scanned_at TEXT NOT NULL);
        CREATE TABLE cleanup_events (
          cleaned_at TEXT NOT NULL, attempt_id TEXT NOT NULL,
          path TEXT NOT NULL, size INTEGER NOT NULL);
        PRAGMA user_version=3;
        """
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                # Recheck under the write lock if another process migrated first.
                if db.execute("PRAGMA user_version").fetchone()[0] == 2:
                    for statement in migration.split(';'):
                        if statement.strip():
                            db.execute(statement)
        except BaseException:
            db.close()
            raise
    return db


def decide(db, video_id, decision, reason):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError(f"Invalid YouTube ID: {video_id}")
    old = db.execute("SELECT decision,reason FROM decisions WHERE video_id=? ORDER BY decision_id DESC LIMIT 1", (video_id,)).fetchone()
    if not old or tuple(old) != (decision, reason):
        db.execute("INSERT INTO decisions(video_id,decision,reason,decided_at) VALUES(?,?,?,?)", (video_id, decision, reason, now()))


def seed(db):
    for video_id, (decision, reason) in SEEDS.items():
        if not db.execute("SELECT 1 FROM decisions WHERE video_id=?", (video_id,)).fetchone():
            decide(db, video_id, decision, reason)


def reclassify(db):
    for row in db.execute("SELECT * FROM videos").fetchall():
        metadata = json.loads(row["metadata"])
        sources = [r[0] for r in db.execute("SELECT source FROM sources WHERE video_id=? ORDER BY source", (row["id"],))]
        old = db.execute("SELECT * FROM classifications WHERE video_id=? ORDER BY classification_id DESC LIMIT 1", (row["id"],)).fetchone()
        previous = (old["category"], old["reason"]) if old else None
        category, reason = classify(metadata, sources, previous)
        # Keep the original reason when the same daily-show identity survives a retitle.
        if old and old["category"] == category == "daily_show":
            reason = old["reason"]
        evidence = hashlib.sha256(encoded([metadata, sources]).encode()).hexdigest()
        if not old or (old["category"], old["reason"], old["rule_version"], old["evidence_digest"]) != (category, reason, RULE_VERSION, evidence):
            db.execute("INSERT INTO classifications(video_id,category,reason,rule_version,evidence_digest,classified_at) VALUES(?,?,?,?,?,?)", (row["id"], category, reason, RULE_VERSION, evidence, now()))


def import_file(db, filename, source):
    raw = Path(filename).read_bytes()
    document = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return import_document(db, document, source, str(filename), digest)


def import_document(db, document, source, input_name, digest=None):
    """Import in-memory extraction results using the same observation rules."""
    filename = input_name
    digest = digest or hashlib.sha256(encoded(document).encode()).hexdigest()
    if db.execute("SELECT 1 FROM imports WHERE digest=? AND source=?", (digest, source)).fetchone():
        return {"file": str(filename), "unchanged": True, "new_observations": 0}
    entries = document.get("entries") if isinstance(document, dict) else None
    flat = entries is not None
    if not flat:
        entries = [document]
    imported_at = now()
    epoch = document.get("epoch") if isinstance(document, dict) else None
    observed_at = datetime.fromtimestamp(epoch, timezone.utc).isoformat() if isinstance(epoch, (int, float)) else imported_at
    count = 0
    for entry in entries:
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError(f"Invalid YouTube ID in {filename}: {video_id!r}")
        detail = "flat" if flat or entry.get("_type") == "url" else "full"
        metadata = {k: entry[k] for k in FIELDS if entry.get(k) is not None}
        if not metadata.get("channel_id") and document.get("channel_id"):
            metadata["channel_id"] = document["channel_id"]
        payload = encoded(metadata)
        observation_digest = hashlib.sha256(payload.encode()).hexdigest()
        old = db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        current = json.loads(old["metadata"]) if old else {}
        provenance = json.loads(old["field_provenance"]) if old else {}
        first = old["first_seen"] if old else imported_at
        last = imported_at
        # Titles and mutable counters may change in flat observations. Rich fields
        # from full extraction cannot be downgraded by a later flat extraction.
        for key, value in metadata.items():
            prior = provenance.get(key)
            rank = 2 if detail == "full" else 1
            if prior is None or (observed_at >= prior["at"] and (rank >= prior["rank"] or key in ("title", "view_count", "like_count", "comment_count"))):
                current[key] = value
                provenance[key] = {"at": observed_at, "rank": rank}
        if old:
            db.execute("UPDATE videos SET first_seen=?,last_seen=?,latest_title=?,metadata=?,field_provenance=? WHERE id=?", (first, last, current.get("title"), encoded(current), encoded(provenance), video_id))
        else:
            db.execute("INSERT INTO videos VALUES(?,?,?,?,?,?,?)", (video_id, first, last, metadata.get("title"), current.get("title"), encoded(current), encoded(provenance)))
        cursor = db.execute("INSERT OR IGNORE INTO observations(video_id,detail,digest,metadata,observed_at,imported_at) VALUES(?,?,?,?,?,?)", (video_id, detail, observation_digest, payload, observed_at, imported_at))
        count += cursor.rowcount
        db.execute("INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(video_id,source) DO UPDATE SET last_seen=excluded.last_seen", (video_id, source, imported_at, imported_at))
    db.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (digest, source, str(filename), imported_at, len(entries)))
    reclassify(db)
    return {"file": str(filename), "unchanged": False, "entries": len(entries), "new_observations": count}


def report(db, start, end=None):
    items = []
    for row in db.execute("SELECT * FROM videos ORDER BY id"):
        metadata = json.loads(row["metadata"])
        classification = db.execute("SELECT * FROM classifications WHERE video_id=? ORDER BY classification_id DESC LIMIT 1", (row["id"],)).fetchone()
        decision = db.execute("SELECT * FROM decisions WHERE video_id=? ORDER BY decision_id DESC LIMIT 1", (row["id"],)).fetchone()
        category = classification["category"] if classification else "review"
        manual = decision["decision"] if decision and decision["decision"] != "clear" else None
        state, reason = eligibility(metadata, category, manual, start, end)
        published, date_field = publication(metadata)
        warnings = []
        if not published:
            warnings.append("Publication date unknown; title dates are not authoritative")
        if metadata.get("release_date") and metadata.get("upload_date") and metadata["release_date"] != metadata["upload_date"]:
            warnings.append("Release and upload dates differ; release date takes precedence")
        items.append({"id": row["id"], "title": row["latest_title"], "initial_title": row["initial_title"],
                      "classification": category, "classification_reason": classification["reason"] if classification else "Unclassified",
                      "decision": manual, "decision_reason": decision["reason"] if decision else None,
                      "publication_date": published, "date_field": date_field,
                      "eligibility": state, "eligibility_reason": reason,
                      "outside_range": bool(published and (published < start or (end and published > end))),
                      "warnings": warnings})
    from collections import Counter
    return {"schema_version": 1, "start_date": start, "end_date": end,
            "totals": {"videos": len(items), "unique_ids": len(items),
                       "observations": db.execute("SELECT count(*) FROM observations").fetchone()[0],
                       "classification_records": db.execute("SELECT count(*) FROM classifications").fetchone()[0],
                       "decision_records": db.execute("SELECT count(*) FROM decisions").fetchone()[0],
                       "automatic_candidates": sum(i["classification"] in ("daily_show", "the_club") for i in items),
                       "missing_publication_date": sum(i["publication_date"] is None for i in items),
                       "outside_range": sum(i["outside_range"] for i in items)},
            "by_source": dict(db.execute("SELECT source,count(*) FROM sources GROUP BY source").fetchall()),
            "classifications": dict(Counter(i["classification"] for i in items)),
            "manual_decisions": dict(Counter(i["decision"] for i in items if i["decision"])),
            "eligibility": dict(Counter(i["eligibility"] for i in items)), "items": items}
