"""Independent bounded comment orchestration; dry-run unless explicitly enabled."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from . import youtube_safety as safety
from .comment_collection import collect, CommentBusy, DEFAULT_ROOT, PROJECT, safe_diagnostic
from .comment_planning import plan, instant
from .classification import DEFAULT_START

MIN_SECONDS = 30
REPORT_LIMIT = 256 * 1024


class Deferred(Exception):
    pass


@contextmanager
def runner_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommentBusy('Another comment runner is active') from exc
        yield


def video_busy(path):
    """Probe existing media lock without creating it or holding it during IO."""
    try:
        stream = Path(path).open('rb')
    except FileNotFoundError:
        return False
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def read_cooldown(directory):
    path = directory / 'cooldown.json'
    try:
        with path.open('rb') as stream:
            raw = stream.read(16385)
    except FileNotFoundError:
        return None
    if len(raw) > 16384:
        raise ValueError('Oversized comment cooldown state; manual review required')
    data = json.loads(raw)
    if data.get('version') != 1:
        raise ValueError('Unsupported comment cooldown state; manual review required')
    return instant(data['until'])


def atomic_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > REPORT_LIMIT:
        raise ValueError('Comment runner report exceeds bounded size')
    fd, temporary = tempfile.mkstemp(prefix='.runner-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def blocking_evidence(result, now):
    """Inspect extractor diagnostics only, never source comment text."""
    coverage = result.get('coverage', {})
    text = '\n'.join([str(coverage.get('diagnostics', '')), str(result.get('error') or ''),
                      *map(str, result.get('warnings', []))])
    category=safety.classify(text)
    if not category:return None
    until=safety.retry_time(text,now,category)
    return {'version': 1, 'observed_at': now.isoformat(), 'until': until.isoformat(),
            'reason': 'extractor_rate_limit_or_block', 'attempt_id': result.get('attempt_id'),
            'diagnostic': safe_diagnostic(text)[:4000]}


def _summary(item):
    # Bounded strings; source payloads and comment text never enter runner logs.
    return {k: item[k] for k in ('id', 'category', 'collection_kind', 'age_anchor',
                                'age_anchor_field', 'first_observed_at', 'last_collection_at')} | {
        'title': (item.get('title') or '')[:300], 'phase': item['refresh']['phase'],
        'source_timing': item['source_timing'],
        'first_completed_observation': item['first_completed_observation'],
        'target_observations': [dict(author_id=t['author_id'], first_seen_at=t['first_seen_at'],
                                     comment_ids=t['observed_comment_ids'][:20],
                                     comment_id_count=len(t['observed_comment_ids']))
                                for t in item['refresh']['targets']],
        'confirmation_collection_at': item['confirmation_collection_at'],
        'hard_cutoff': item['refresh']['deadline_at'], 'next_due_at': item['refresh']['next_due_at']}


@safety.scoped(lambda a:Path(a['inventory_path']).resolve().parent,'comment-runner')
def run(inventory_path, root=DEFAULT_ROOT, *, execute=False, max_videos=2,
        max_comments=1000, runtime_seconds=600, start=DEFAULT_START,
        clock=None, monotonic=None, acquisition_lock=None, deadline=None):
    """Run sequentially under one deadline. Clock seams are for offline tests only."""
    if type(max_videos) is not int or not 1 <= max_videos <= 10:
        raise ValueError('max videos must be 1..10')
    if type(max_comments) is not int or not 1 <= max_comments <= 1000:
        raise ValueError('max comments must be 1..1000')
    if type(runtime_seconds) is not int or not MIN_SECONDS <= runtime_seconds <= 600:
        raise ValueError('runtime seconds must be 30..600')
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    end = monotonic() + runtime_seconds
    if deadline is not None:
        end = min(end, deadline)
    root = Path(root).resolve()
    if root == PROJECT / 'state' or root.is_relative_to(PROJECT / 'state/acquisitions'):
        raise ValueError('Comment runner state must be separate from video state')
    directory = root / 'runner'
    media_lock = acquisition_lock or PROJECT / 'state/acquisitions/.acquisition.lock'
    def current_plan():
        value=plan(inventory_path, root, now=instant(clock()), start=start,
                    cooldown_until=read_cooldown(directory))
        value['youtube_safety']=safety.current().status(clock())
        return value
    initial = current_plan()  # Fail before any writes on invalid input.
    initial_cooldown = read_cooldown(directory)
    by_id = {i['id']: i for i in initial['items']}
    selected = initial['due_ids'][:max_videos]
    result = {'run_id': uuid.uuid4().hex if execute else None,
              'mode': 'execute' if execute else 'dry-run', 'started_at': initial['evaluated_at'],
              'limits': {'max_videos': max_videos, 'max_comments': max_comments, 'runtime_seconds': runtime_seconds},
              'selected': [_summary(by_id[v]) for v in selected], 'work': [],
              'due_count': len(initial['due_ids']), 'budget_deferred': len(initial['due_ids']) - len(selected),
              'excluded_count': sum(i['status'] == 'excluded' for i in initial['items']),
              'not_due_count': sum(i['eligible'] and not i['due'] for i in initial['items']),
              'cooldown_until': initial_cooldown.isoformat() if initial_cooldown else None,
              'exit_code': 0, 'stop_reason': 'collector_cooldown' if initial_cooldown and initial_cooldown > instant(initial['evaluated_at']) else None}
    shared=safety.current().status(clock());result['youtube_safety']=shared
    result['blocked_due_count']=len(initial.get('deferred_due_ids',[]))
    if not shared['network_allowed']:
        result.update(stop_reason='shared_youtube_'+shared['status'],exit_code=75 if execute else 0)
        return result
    if not execute:
        return result
    try:
        with runner_lock(directory):
            try:
                for video_id in selected:
                    work = {'id': video_id, 'status': 'deferred', 'reason': None}
                    result['work'].append(work)
                    def preflight():
                        try:safety.current().ensure('comment_collection')
                        except safety.Blocked as exc:raise Deferred(str(exc)) from exc
                        fresh = current_plan()
                        item = next(i for i in fresh['items'] if i['id'] == video_id)
                        if not item['due']:
                            raise Deferred(item['reason'])
                        if video_busy(media_lock):
                            raise Deferred('video_acquisition_active')
                        remaining = min(end - monotonic(),
                                        (instant(item['refresh']['deadline_at']) - instant(clock())).total_seconds())
                        timeout = min(600, math.floor(remaining) - 1)
                        if timeout < MIN_SECONDS:
                            raise Deferred('insufficient_remaining_runtime')
                        work['candidate'] = _summary(item)
                        return timeout
                    try:
                        preflight()
                        # Collector repeats preflight under its own lock, after
                        # opening storage and immediately before starting extraction.
                        collected = collect(video_id, root=root, max_comments=max_comments,
                                            timeout=600, before_collect=preflight)
                        work.update(status='failed' if collected['exit_code'] else 'completed',
                                    reason=collected['outcome'], attempt_id=collected.get('attempt_id'),
                                    outcome=collected['outcome'], retrieved_count=collected.get('retrieved_count'),
                                    started_at=collected.get('collected_at'),
                                    finished_at=collected.get('coverage', {}).get('finished_at'),
                                    collection_record=collected.get('collection_record'),
                                    snapshot_path=collected.get('snapshot_path'))
                        cooldown = blocking_evidence(collected, instant(clock()))
                        if cooldown:
                            atomic_json(directory / 'cooldown.json', cooldown)
                            result.update(cooldown_until=cooldown['until'],
                                          stop_reason='interrupted' if collected['exit_code'] == 130 else cooldown['reason'],
                                          exit_code=130 if collected['exit_code'] == 130 else 2)
                            break
                        if collected['exit_code'] == 130:
                            result.update(stop_reason='interrupted', exit_code=130)
                            break
                        if collected['exit_code']:
                            result['exit_code'] = 2
                    except Deferred as exc:
                        work['reason'] = str(exc)
                        if str(exc) not in ('video_acquisition_active', 'insufficient_remaining_runtime', 'collector_cooldown', 'cooldown_exceeds_window'):
                            work['status'] = 'skipped'
                        if str(exc) in ('video_acquisition_active', 'insufficient_remaining_runtime',
                                       'collector_cooldown', 'cooldown_exceeds_window'):
                            result['stop_reason'] = str(exc)
                            break
                    except safety.Blocked as exc:
                        work['reason']=str(exc);result.update(stop_reason=str(exc),exit_code=75);break
                    except CommentBusy as exc:
                        work.update(reason=safe_diagnostic(exc))
                        result.update(stop_reason='comment_collector_busy', exit_code=75)
                        break
                    except KeyboardInterrupt:
                        work.update(status='failed', reason='interrupted')
                        result.update(stop_reason='interrupted', exit_code=130)
                        break
                    except Exception as exc:
                        # Unexpected persistence/programming errors require review;
                        # do not launch more network work after an unknown failure.
                        work.update(status='failed', reason=safe_diagnostic(exc))
                        result.update(stop_reason='runner_error', exit_code=1)
                        break
            finally:
                result['finished_at'] = instant(clock()).isoformat()
                attempted = {w['id'] for w in result['work']}
                for video_id in selected:
                    if video_id not in attempted:
                        result['work'].append({'id': video_id, 'status': 'deferred',
                                               'reason': result['stop_reason'] or 'not_started'})
                atomic_json(directory / 'last-run.json', result)
    except CommentBusy:
        result.update(stop_reason='comment_runner_busy', exit_code=75)
    return result


def render(result):
    lines = [f"Comment runner {result['mode']} (UTC): selected={len(result['selected'])}, due={result['due_count']}, budget-deferred={result['budget_deferred']}",
             f"Excluded={result['excluded_count']}; not due={result['not_due_count']}; cooldown={result['cooldown_until']}"]
    for item in result['selected']:
        lines.append(f"{item['id']} | {item['collection_kind']} | {item['title']}\n  anchor={item['age_anchor']} ({item['age_anchor_field']}); next={item['next_due_at']}; cutoff={item['hard_cutoff']}")
    for work in result['work']:
        lines.append(f"{work['id']} | {work['status']} | {work['reason']}")
    lines.append(f"Stop: {result['stop_reason'] or 'budgeted work finished'}; exit={result['exit_code']}")
    return '\n'.join(lines)
