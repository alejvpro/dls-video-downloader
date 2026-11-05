"""Independent exact-byte caption assets and portable observations (schema 1)."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from .comments import VIDEO_ID
from .comment_collection import CommentBusy, PROJECT
from .comment_planning import instant

DEFAULT_ROOT = PROJECT / 'state/captions'
APPLICATION_ID = 0x444C5343
FORMATS = ('json3', 'vtt')
MAX_BYTES = 16 * 1024 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate(raw, fmt):
    """Validate containers/timing syntax only; never rewrite or interpret speech."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Empty or oversized caption response')
    text = raw.decode('utf-8-sig')
    if fmt == 'json3':
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get('events'), list) or not data['events']:
            raise ValueError('JSON3 requires a nonempty events array')
        for event in data['events']:
            if not isinstance(event, dict) or type(event.get('tStartMs')) not in (int, float) or not math.isfinite(event['tStartMs']) or event['tStartMs'] < 0:
                raise ValueError('Invalid JSON3 event timing')
            if 'dDurationMs' in event and (type(event['dDurationMs']) not in (int,float) or not math.isfinite(event['dDurationMs']) or event['dDurationMs']<0):
                raise ValueError('Invalid JSON3 event duration')
            if 'segs' in event and (not isinstance(event['segs'], list) or any(not isinstance(s, dict) or not isinstance(s.get('utf8'), str) for s in event['segs'])):
                raise ValueError('Invalid JSON3 text segments')
    elif fmt == 'vtt':
        if not re.match(r'^WEBVTT(?:[^\S\n][^\n]*)?\r?\n', text):
            raise ValueError('Missing WEBVTT header')
        timing = re.compile(r'(?m)^((?:\d{2,}:)?\d{2}:\d{2}\.\d{3}) --> ((?:\d{2,}:)?\d{2}:\d{2}\.\d{3})(?:[^\S\n].*)?\r?$')
        cues = list(timing.finditer(text))
        def seconds(value):
            fields = value.split(':'); seconds = float(fields[-1]); minutes = int(fields[-2])
            if seconds >= 60 or minutes >= 60: raise ValueError('Invalid VTT timestamp')
            return (int(fields[0])*3600 if len(fields) == 3 else 0) + minutes*60 + seconds
        if not cues or len(cues) != text.count('-->') or any(seconds(m[2]) < seconds(m[1]) for m in cues):
            raise ValueError('Invalid or empty VTT cue structure')
    else:
        raise ValueError('Unsupported caption representation')


def immutable(path, raw):
    """Publish atomically without replacing an existing distinct artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.caption-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try: os.link(name, path)
        except FileExistsError:
            if path.read_bytes() != raw: raise ValueError('Immutable caption artifact differs; manual review required')
    finally:
        os.unlink(name)


@contextmanager
def caption_lock(root):
    import fcntl
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    with (root / '.collector.lock').open('a') as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise CommentBusy('Another caption collector is active') from exc
        yield stream.fileno()


class CaptionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if self.root == PROJECT/'state' or any(self.root.is_relative_to(PROJECT/'state'/p) for p in ('acquisitions','comments')):
            raise ValueError('Caption store must be independent of other assets')
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root/'captions.sqlite3', timeout=1)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        identity = (self.db.execute('PRAGMA application_id').fetchone()[0], self.db.execute('PRAGMA user_version').fetchone()[0])
        if identity == (0, 0) and not self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            self.db.executescript(f'''
                BEGIN;
                PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;
                CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, record TEXT NOT NULL);
                CREATE INDEX attempts_video ON attempts(video_id, finished_at);
                CREATE TABLE assets(video_id TEXT NOT NULL, track_id TEXT NOT NULL, format TEXT NOT NULL,
                  sha256 TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL,
                  PRIMARY KEY(video_id,track_id,format,sha256));
                CREATE TABLE observations(attempt_id TEXT NOT NULL REFERENCES attempts,
                  video_id TEXT NOT NULL, track_id TEXT NOT NULL, format TEXT NOT NULL,
                  sha256 TEXT NOT NULL, PRIMARY KEY(attempt_id,track_id,format),
                  FOREIGN KEY(video_id,track_id,format,sha256) REFERENCES assets);
                COMMIT;
            ''')
        elif identity != (APPLICATION_ID, 1):
            self.db.close(); raise ValueError('Unsupported caption catalog')

    def __enter__(self): return self
    def __exit__(self, *args): self.db.close()

    def latest(self, video_id):
        return latest_assets(read_history(self.root, video_id))

    def record(self, record, payloads):
        """Record one attempt; validated bytes are durable even if later work fails.

        Immutable portable records are published before commit. On a DB failure,
        an orphan record/blob may remain as genuine collection evidence; no prior
        catalog history is replaced. Never silently adopt or delete orphan data.
        """
        record = json.loads(encoded(record)); video = record['video_id']
        if not VIDEO_ID.fullmatch(video): raise ValueError('Invalid YouTube ID')
        record['version'] = 1; record['asset_type'] = 'captions'
        record['attempt_id'] = uuid.uuid4().hex
        if record.get('blocking'):
            # The identifier is allocated here, after blocking evidence was
            # detected. Link both portable evidence and cooldown prospectively.
            record['blocking']['attempt_id'] = record['attempt_id']
        for field in ('started_at','finished_at'): record[field] = instant(record[field]).isoformat()
        formats = record.setdefault('formats', [])
        for row in formats:
            key = (row['track_id'], row['format'])
            if not re.fullmatch('[0-9a-f]{64}', row['track_id']) or row['format'] not in FORMATS:
                raise ValueError('Invalid caption asset key')
            if key in payloads:
                raw = payloads[key]; validate(raw, row['format'])
                sha = digest(raw)
                row.update(sha256=sha, size=len(raw), path=f'{video}/tracks/{row["track_id"]}/{sha}.{row["format"]}')
                immutable(self.root/row['path'], raw)
            if row.get('sha256'):
                if not re.fullmatch('[0-9a-f]{64}',row['sha256']) or type(row.get('size')) is not int or row['size']<=0:
                    raise ValueError('Invalid caption checksum or size')
                expected = f'{video}/tracks/{row["track_id"]}/{row["sha256"]}.{row["format"]}'
                if row.get('path') != expected: raise ValueError('Unsafe caption asset path')
                raw = (self.root/expected).read_bytes()
                if len(raw) != row['size'] or digest(raw) != row['sha256']: raise ValueError('Caption checksum mismatch')
        relative = f'{video}/collections/{record["attempt_id"]}.json'
        record['collection_record'] = relative
        raw = encoded(record)
        with self.db:
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?)', (record['attempt_id'],video,record['started_at'],record['finished_at'],raw.decode()))
            for row in formats:
                if row.get('sha256'):
                    values = (video,row['track_id'],row['format'],row['sha256'],row['size'],row['path'])
                    self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?)', values)
                    self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?)', (record['attempt_id'],*values[:4]))
            immutable(self.root/relative, raw)
        return record


def read_history(root, video_id=None):
    path = Path(root)/'captions.sqlite3'
    if not path.exists(): return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
        db.execute('PRAGMA query_only=ON')
        if (db.execute('PRAGMA application_id').fetchone()[0],db.execute('PRAGMA user_version').fetchone()[0]) != (APPLICATION_ID,1):
            raise ValueError('Unsupported caption catalog')
        sql = 'SELECT record FROM attempts' + (' WHERE video_id=?' if video_id else '') + ' ORDER BY finished_at,rowid'
        return [json.loads(row[0]) for row in db.execute(sql, (video_id,) if video_id else ())]


def latest_assets(history):
    result = {}
    for attempt in history:
        for row in attempt.get('formats', []):
            if row.get('sha256'): result[(row['track_id'],row['format'])] = dict(row,source_attempt_id=attempt['attempt_id'])
    return result
