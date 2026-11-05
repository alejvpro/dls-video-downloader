"""Narrow metadata discovery/status pass; no cleanup, acquisition or comments."""
from contextlib import closing
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from . import acquisition as media
from .classification import CHANNEL, DEFAULT_START
from .discovery import compact, metadata_command, channel_entries
from .inventory import connect, import_document, report
from .operations import safe_text, write_log
from . import forward_metadata as forward


STATUSES = {'was_live', 'not_live', 'is_live', 'is_upcoming', 'post_live'}


def next_video_run(now):
    """Known installed Pacific schedule; no cron reads or changes."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Timezone-aware current time required')
    local = now.astimezone(ZoneInfo('America/Los_Angeles'))
    for day in (0, 1):
        for hour in (11, 14, 18, 21):
            slot = (local + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot >= local:
                return slot.astimezone(timezone.utc)


def discover(inventory_path, *, streams_limit=10, videos_limit=0, max_metadata_requests=20,
             runtime_seconds=180, start=DEFAULT_START, clock=None, monotonic=None, deadline=None):
    """Explicit metadata-writing command; tests must supply disposable paths.

    Shared operation lock preserves existing inventory-writer serialization. Refuse
    the known cron's budget+60s guard window; this is not a scheduler override.
    """
    for value, lower, upper in ((streams_limit, 1, 25), (videos_limit, 0, 25),
                                 (max_metadata_requests, 1, 100), (runtime_seconds, 30, 180)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError('Forward limits: streams 1..25, videos 0..25, emergency metadata requests 1..100, runtime 30..180 seconds')
    start = date.fromisoformat(start).isoformat()
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    now = clock()
    slot = next_video_run(now)
    result = {'mode': 'morning-metadata', 'started_at': now.isoformat(),
              'limits': {'streams': streams_limit, 'videos': videos_limit, 'max_metadata_requests': max_metadata_requests,
                         'runtime_seconds': runtime_seconds},
              'next_video_run': slot.isoformat(), 'new_ids': [], 'encountered': {},
              'enriched': [], 'deferred': [], 'failures': [], 'warnings': [], 'status': {},
              'exit_code': 0, 'stop_reason': None, 'status_checks': {}}
    guard_slot = next_video_run(now - timedelta(seconds=60))
    if (guard_slot - now).total_seconds() <= runtime_seconds + 60:
        result['stop_reason'] = 'video_cron_imminent'
        return result
    state = Path(inventory_path).resolve().parent
    phase_deadline = monotonic() + runtime_seconds
    if deadline is not None:
        phase_deadline = min(phase_deadline, deadline)
    # Unlike the regular workflow, never call run_record, cleanup, seed, retry
    # updates, frontier updates, overlap receipts, or acquisition.
    with media.operation_lock(state / 'acquisitions'):
        with closing(connect(inventory_path, readonly=True, timeout=1)) as existing:
            if existing.execute('PRAGMA user_version').fetchone()[0] != 3:
                raise ValueError('Morning discovery requires existing inventory schema 3; no migrations')
        with closing(connect(inventory_path, timeout=1)) as db:
            return _discover(db, state, result, start, clock, monotonic,
                             streams_limit, videos_limit, max_metadata_requests, phase_deadline)


def _discover(db, state, result, start, clock, monotonic, streams, videos, maximum, deadline):
    directory = state / 'morning'
    directory.mkdir(parents=True, exist_ok=True)
    common = metadata_command()
    common[1:1] = ['--no-plugin-dirs', '--no-js-runtimes']
    common += ['--no-write-description', '--no-write-playlist-metafiles']
    positions, listed_status = {}, {}
    before = {r[0] for r in db.execute('SELECT id FROM videos')}
    retry = forward.read_state(directory)
    prior_retry = json.dumps(retry,sort_keys=True)
    if retry.get('cooldown_until') and forward.instant(retry['cooldown_until']) > clock():
        result['stop_reason'] = 'metadata_cooldown'
    result['retry_deferred'] = []
    result['attempted'] = []
    result['metadata_requests'] = 0

    def extract(args, log):
        remaining = min(deadline - monotonic(),
                        (next_video_run(clock() - timedelta(seconds=60)) - clock()).total_seconds() - 60)
        if remaining < 5:
            result['stop_reason'] = 'runtime_or_video_cron_budget'
            return None
        result['metadata_requests'] += 1
        if '--no-playlist' in args:
            result['attempted'].append(args[-1].split('=')[-1])
        return json.loads(media.run(common + args, directory / log, min(60, int(remaining))))

    def failure(operation, exc, video=None):
        message = safe_text(str(exc))[:1000]
        result['failures'].append({'operation': operation, 'id': video, 'error': message})
        result['exit_code'] = 2
        if video is not None:
            forward.failed(retry,video,clock(),message)
        if re.search(r'(?i)429|too many requests|rate.?limit|confirm.*bot|HTTP Error (?:402|403)', message):
            result['stop_reason'] = 'youtube_blocking'
            retry['cooldown_until'] = (clock()+forward.BLOCK_DELAY).isoformat()

    try:
        for source, tab, limit in (('live', 'streams', streams), ('videos', 'videos', videos)):
            if not limit or result['stop_reason']:
                continue
            try:
                document = extract(['--flat-playlist', '--playlist-end', str(limit),
                                    f'https://www.youtube.com/channel/{CHANNEL}/{tab}'], f'{source}.stderr.log')
                if document is None:
                    break
                if not isinstance(document, dict):
                    raise ValueError('Expected channel metadata object')
                entries = channel_entries(document, limit)
                for entry in entries:
                    if not isinstance(entry, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(entry.get('id', ''))):
                        raise ValueError('Malformed channel entry identity')
                # Flat live-status labels are hints for enrichment only. They must
                # not establish completion/classification for a newly seen daily.
                local = compact(document, flat=True)
                for entry in local['entries']:
                    listed_status[entry['id']] = entry.get('live_status')
                    for field in ('live_status', 'was_live', 'is_live'):
                        entry.pop(field, None)
                with db:
                    import_document(db, local, source, f'morning:channel:{CHANNEL}/{tab}', source_alias=f'morning:{source}')
                result['encountered'][source] = len(entries)
                if len(entries) == limit:
                    result['warnings'].append(f'{source}: bounded head reached {limit}; not exhaustive discovery')
                for position, entry in enumerate(entries):
                    positions[entry['id']] = min(position, positions.get(entry['id'], limit))
            except (ValueError, OSError) as exc:
                failure(source, exc)
        result['new_ids'] = sorted(set(positions) - before)
        scope = forward.candidates(db, positions, listed_status, now=clock(), start=start)
        result['forward_scope'] = scope
        for video in scope['pending']:
            backoff = retry['failures'].get(video)
            if backoff and forward.instant(backoff['next_retry']) > clock():
                result['retry_deferred'].append({'id':video,**backoff})
                result['deferred'].append(video)
                continue
            if len(result['attempted']) >= maximum or result['stop_reason']:
                result['deferred'].append(video)
                if not result['stop_reason']:
                    result['stop_reason'] = 'emergency_metadata_request_bound'
                continue
            index = len(result['attempted'])
            try:
                document = extract(['--no-playlist', f'https://www.youtube.com/watch?v={video}'],
                                   f'enrich-{index}.stderr.log')
                if document is None:
                    result['deferred'].append(video)
                    continue
                if not isinstance(document, dict) or document.get('id') != video or document.get('channel_id') != CHANNEL:
                    raise ValueError('Unexpected full-metadata video/channel identity')
                status = document.get('live_status')
                result['status_checks'][video] = 'unconfirmed'
                if not isinstance(status, str) or status not in STATUSES or (status in ('was_live', 'not_live') and document.get('is_live') is True):
                    evidence = compact(document)
                    for field in ('live_status', 'was_live', 'is_live'):
                        evidence.pop(field, None)
                    with db:
                        import_document(db, evidence, 'other', f'morning:full:{video}')
                    raise ValueError('Missing, unknown or contradictory full live status; completion unconfirmed')
                result['status_checks'][video] = status
                with db:
                    import_document(db, compact(document), 'other', f'morning:full:{video}')
                result['enriched'].append(video)
                row=db.execute('SELECT metadata FROM videos WHERE id=?',(video,)).fetchone()
                anchor,_,_=forward.age_anchor(json.loads(row[0]), 'live' in [r[0] for r in db.execute('SELECT source FROM sources WHERE video_id=?',(video,))])
                if anchor is None:
                    raise ValueError('Full metadata lacks publication evidence; remains unresolved')
                retry['failures'].pop(video,None)
            except (ValueError, OSError) as exc:
                failure('enrichment', exc, video)
        current = {i['id']: i for i in report(db, start)['items']}
        for video in sorted(set(positions)|set(scope['scope'])):
            metadata = json.loads(db.execute('SELECT metadata FROM videos WHERE id=?', (video,)).fetchone()[0])
            result['status'][video] = {'live_status': metadata.get('live_status'),
                                      'category': current[video]['classification'],
                                      'decision': current[video]['decision'],
                                      'eligibility': current[video]['eligibility'],
                                      'publication_date': current[video]['publication_date']}
        return result
    except sqlite3.OperationalError as exc:
        busy = any(s in str(exc).lower() for s in ('locked','busy'))
        result.update(stop_reason='sqlite_busy' if busy else 'inventory_error', exit_code=75 if busy else 1)
        raise
    except KeyboardInterrupt:
        result.update(stop_reason='interrupted', exit_code=130)
        return result
    finally:
        requested = {'live'} | ({'videos'} if videos else set())
        pending = result.get('forward_scope',{}).get('pending',[])
        unattempted = sorted(set(pending)-set(result['attempted']))
        result['forward_coverage'] = {
            'complete': requested <= set(result['encountered']) and not result['failures']
                        and not unattempted and not result['stop_reason'],
            'candidate_count': len(pending), 'attempted_count': len(result['attempted']),
            'unattempted_ids': unattempted,
            'failed_ids': [f['id'] for f in result['failures'] if f['id'] is not None],
            'scope': 'bounded_heads_and_recent_source_evidence'}
        if not result['forward_coverage']['complete'] and result['exit_code']==0:
            result['exit_code']=2
        result['finished_at'] = clock().isoformat()
        if json.dumps(retry,sort_keys=True) != prior_retry:
            forward.save_state(directory,retry)
        write_log(directory / 'last-run.json', json.dumps(result, ensure_ascii=False, indent=2))


def render(result):
    lines = [f"Morning metadata: encountered={result['encountered']}; enriched={len(result['enriched'])}; new={len(result['new_ids'])}",
             f"Stop={result['stop_reason']}; failures={len(result['failures'])}; exit={result['exit_code']}",
             f"Forward coverage complete={result.get('forward_coverage',{}).get('complete',False)}; historical backlog={result.get('forward_scope',{}).get('historical_count','unknown')}"]
    for video, item in result['status'].items():
        lines.append(f"{video} | {item['category']} | status={item['live_status'] or 'unknown'} | {item['eligibility']}")
    for item in result['failures']:
        lines.append(f"{item['operation']} {item['id'] or ''}: {item['error']}")
    return '\n'.join(lines)
