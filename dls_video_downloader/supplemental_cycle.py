"""Thin supplemental orchestration; media acquisition is never a phase."""
from contextlib import contextmanager
from datetime import datetime, timezone, date
import fcntl
import math
from pathlib import Path
import sqlite3
import time

from . import comment_runner, morning_discovery, forward_metadata
from .classification import DEFAULT_START
from .comment_collection import DEFAULT_ROOT, CommentBusy, safe_diagnostic
from .comment_planning import plan, instant
from .operations import OperationBusy


@contextmanager
def cycle_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.cycle.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommentBusy('Another supplemental cycle is active') from exc
        yield


def _phase(outcome, reason, **fields):
    return dict(outcome=outcome, reason=reason, **fields)


def _busy(exc):
    return isinstance(exc, sqlite3.OperationalError) and any(s in str(exc).lower() for s in ('locked', 'busy'))


def run(inventory_path, root=DEFAULT_ROOT, *, execute=False, discover=False,
        runtime_seconds=600, discovery_seconds=180, comment_seconds=600,
        streams_limit=10, videos_limit=0, max_metadata_requests=20, max_videos=2,
        max_comments=1000, start=DEFAULT_START, clock=None, monotonic=None):
    """No execution without explicit intent. All external work is delegated.

    Phase mapping is intentionally extensible by another named phase, not a
    caption collector/schema, preservation manifest or persistent queue.
    """
    if discover and not execute:
        raise ValueError('--discover requires --execute; default planning never contacts YouTube')
    bounds = ((runtime_seconds,30,600),(discovery_seconds,30,180),(comment_seconds,30,600),
              (streams_limit,1,25),(videos_limit,0,25),(max_metadata_requests,1,100),
              (max_videos,1,10),(max_comments,1,1000))
    if any(type(v) is not int or not lo <= v <= hi for v,lo,hi in bounds):
        raise ValueError('Invalid cycle limits; see cycle --help')
    start = date.fromisoformat(start).isoformat()
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or time.monotonic
    started = monotonic()
    deadline = started + runtime_seconds
    root = Path(root).resolve()
    inventory_path = Path(inventory_path).resolve()
    if root == inventory_path.parent or root.is_relative_to(inventory_path.parent / 'acquisitions'):
        raise ValueError('Comment root must be independent from inventory/acquisitions')
    directory = inventory_path.parent / 'supplemental'
    result = dict(mode='discovery-execute' if discover else 'inventory-execute' if execute else 'plan',
                  started_at=instant(clock()).isoformat(), fresh_youtube_status=False,
                  limits=dict(runtime_seconds=runtime_seconds, discovery_seconds=discovery_seconds,
                              comment_seconds=comment_seconds, max_videos=max_videos,
                              max_comments=max_comments, streams=streams_limit, videos=videos_limit,
                              max_metadata_requests=max_metadata_requests), phases={}, exit_code=0, outcome='planned')
    def shared_cooldown():
        comment_until = comment_runner.read_cooldown(root/'runner')
        metadata_until = forward_metadata.read_state(inventory_path.parent/'morning').get('cooldown_until')
        values = [v for v in (comment_until, instant(metadata_until) if metadata_until else None) if v is not None]
        return max(values,default=None)

    if not execute:
        cooldown = shared_cooldown()
        snapshot = plan(inventory_path, root, now=instant(clock()), start=start, cooldown_until=cooldown)
        result['phases']['discovery'] = _phase('not_requested', 'fresh status not checked; requires --execute --discover',
                                               budget_seconds=0,metadata_requests=0,duration_seconds=0)
        result['phases']['comments'] = _phase('planned', 'existing inventory/history only',
                                              selected_ids=snapshot['due_ids'][:max_videos],
                                              due_count=len(snapshot['due_ids']), plan=snapshot,
                                              budget_seconds=min(comment_seconds,runtime_seconds),
                                              collection_attempts=0,duration_seconds=max(0,monotonic()-started))
        result['finished_at'] = instant(clock()).isoformat()
        return result

    def finish():
        result['finished_at'] = instant(clock()).isoformat()
        result['duration_seconds'] = max(0, monotonic()-started)
        outcomes = [p['outcome'] for p in result['phases'].values()]
        if result['exit_code'] == 130:
            result['outcome'] = 'interrupted'
        elif any(o in ('failed','partially_successful') for o in outcomes):
            result['outcome'] = 'partially_successful' if any(o in ('successful','partially_successful') for o in outcomes) else 'failed'
            result['exit_code'] = 2
        elif 'successful' in outcomes and 'deferred' in outcomes:
            result['outcome'] = 'partially_successful'
        else:
            result['outcome'] = 'successful' if 'successful' in outcomes else 'deferred' if 'deferred' in outcomes else 'skipped'
        result['report_path'] = str(directory/'last-cycle.json')
        comment_runner.atomic_json(directory/'last-cycle.json', result)
        return result

    try:
        with cycle_lock(directory):
            stop = None
            phase_start = monotonic()
            try:
                cooldown = shared_cooldown()
                if cooldown and cooldown > instant(clock()):
                    stop = 'collector_cooldown'
                discovery_started = monotonic()
                discovery_budget = 0
                if not discover:
                    result['phases']['discovery'] = _phase('not_requested','inventory-only execution')
                elif stop:
                    result['phases']['discovery'] = _phase('deferred',stop)
                else:
                    phase_start = monotonic()
                    budget = min(discovery_seconds, math.floor(deadline-phase_start))
                    discovery_budget = max(0,budget)
                    if budget < 30:
                        result['phases']['discovery'] = _phase('deferred','insufficient_remaining_runtime', budget_seconds=max(0,budget))
                    else:
                        try:
                            metadata = morning_discovery.discover(inventory_path, streams_limit=streams_limit,
                                videos_limit=videos_limit, max_metadata_requests=max_metadata_requests, runtime_seconds=budget,
                                start=start, clock=clock, monotonic=monotonic,
                                deadline=min(deadline,phase_start+budget))
                            requests = metadata.get('metadata_requests',0)
                            if metadata['exit_code'] == 130:
                                state = 'failed'; stop = 'interrupted'; result['exit_code']=130
                            elif metadata['exit_code']:
                                state = 'partially_successful' if metadata.get('encountered') else 'failed'
                            elif metadata.get('stop_reason'):
                                state = 'deferred'
                            else:
                                state = 'successful'
                            result['fresh_youtube_status'] = any(v in morning_discovery.STATUSES for v in metadata.get('status_checks',{}).values())
                            result['phases']['discovery'] = _phase(state,metadata.get('stop_reason'),
                                metadata_requests=requests,budget_seconds=budget,
                                duration_seconds=monotonic()-phase_start, result=metadata)
                            # Blocking is a shared safety signal, unlike a transient
                            # discovery failure. Use the existing cooldown format.
                            if metadata.get('stop_reason') == 'metadata_cooldown':
                                stop = 'metadata_cooldown'
                            if metadata.get('stop_reason') == 'youtube_blocking':
                                stop = 'youtube_blocking'
                                evidence = comment_runner.blocking_evidence(
                                    {'error':'\n'.join(f['error'] for f in metadata.get('failures',[]))}, instant(clock()))
                                if evidence:
                                    with comment_runner.runner_lock(root/'runner'):
                                        comment_runner.atomic_json(root/'runner/cooldown.json',evidence)
                                stop = 'youtube_blocking'
                        except forward_metadata.MetadataStateError as exc:
                            stop = 'metadata_safety_state_error'
                            result['phases']['discovery'] = _phase('failed',stop,error=safe_diagnostic(exc))
                        except OperationBusy as exc:
                            result['phases']['discovery'] = _phase('deferred','video_operation_active',error=safe_diagnostic(exc))
                        except sqlite3.OperationalError as exc:
                            result['phases']['discovery'] = _phase('deferred' if _busy(exc) else 'failed',
                                                                  'sqlite_busy' if _busy(exc) else 'discovery_error',error=safe_diagnostic(exc))
                        except KeyboardInterrupt:
                            result['phases']['discovery'] = _phase('failed','interrupted')
                            stop='interrupted';result['exit_code']=130
                        except Exception as exc:
                            result['phases']['discovery'] = _phase('failed','discovery_error',error=safe_diagnostic(exc))
                            # stop is set before cooldown persistence; an error
                            # writing it cannot enable later network work.
                discovery_phase = result['phases']['discovery']
                discovery_phase.setdefault('duration_seconds',max(0,monotonic()-discovery_started))
                discovery_phase.setdefault('budget_seconds',discovery_budget)
                discovery_phase.setdefault('metadata_requests',None if discovery_phase['outcome']=='failed' else 0)
                phase_start = monotonic()
                budget = min(comment_seconds, math.floor(deadline-phase_start))
                if stop or budget < 30:
                    result['phases']['comments'] = _phase('deferred',stop or 'insufficient_remaining_runtime',budget_seconds=max(0,budget))
                else:
                    try:
                        comments = comment_runner.run(inventory_path,root,execute=True,
                            max_videos=max_videos,max_comments=max_comments,runtime_seconds=budget,
                            start=start,clock=clock,monotonic=monotonic,
                            acquisition_lock=inventory_path.parent/'acquisitions/.acquisition.lock',
                            deadline=min(deadline,phase_start+budget))
                        work=comments['work']
                        completed=sum(w['status']=='completed' for w in work)
                        failed=sum(w['status']=='failed' for w in work)
                        state=('partially_successful' if completed else 'failed') if failed else (
                              'successful' if completed else 'deferred' if comments['stop_reason'] else 'skipped')
                        if comments['exit_code'] and comments['exit_code'] not in (75,130):
                            state='partially_successful' if completed else 'failed'
                        if comments['exit_code']==130:result['exit_code']=130
                        reason = comments['stop_reason']
                        if comments['exit_code']==1 and any('database is locked' in str(w.get('reason','')).lower() or 'database is busy' in str(w.get('reason','')).lower() for w in work):
                            state,reason='deferred','sqlite_busy'
                        result['phases']['comments']=_phase(state,reason,
                            collection_attempts=sum(bool(w.get('attempt_id')) for w in work),
                            budget_seconds=budget,duration_seconds=monotonic()-phase_start,result=comments)
                    except sqlite3.OperationalError as exc:
                        result['phases']['comments']=_phase('deferred' if _busy(exc) else 'failed',
                            'sqlite_busy' if _busy(exc) else 'comment_error',error=safe_diagnostic(exc))
                    except KeyboardInterrupt:
                        result['phases']['comments']=_phase('failed','interrupted');result['exit_code']=130
                    except Exception as exc:
                        result['phases']['comments']=_phase('failed','comment_error',error=safe_diagnostic(exc))
            except KeyboardInterrupt:
                result['exit_code']=130
                result['phases'].setdefault('discovery',_phase('deferred' if discover else 'not_requested','interrupted'))
                result['phases'].setdefault('comments',_phase('deferred','interrupted'))
            except Exception as exc:
                result['phases'].setdefault('discovery',_phase('deferred' if discover else 'not_requested','safety_state_error'))
                result['phases']['comments']=_phase('failed','safety_state_error',error=safe_diagnostic(exc))
            for name,phase in result['phases'].items():
                phase.setdefault('budget_seconds',0)
                phase.setdefault('duration_seconds',max(0,monotonic()-phase_start) if name=='comments' else 0)
                if name=='comments':phase.setdefault('collection_attempts',0)
            return finish()
    except CommentBusy:
        result['phases']={'discovery':_phase('deferred' if discover else 'not_requested','supplemental_cycle_active'),
                          'comments':_phase('deferred','supplemental_cycle_active')}
        result.update(outcome='deferred',exit_code=75,reason='supplemental_cycle_active')
        return result


def render(result):
    lines=[f"Supplemental cycle: {result['mode']} | {result['outcome']} (UTC)",
           'Media acquisition is independent; source coverage is not certified by operational success.']
    for name,phase in result['phases'].items():
        lines.append(f"{name}: {phase['outcome']} | {phase['reason'] or 'finished'}")
        if 'plan' in phase:
            from .comment_planning import render as render_plan
            lines.append(render_plan(phase['plan']))
        if 'result' in phase:
            renderer=morning_discovery.render if name=='discovery' else comment_runner.render
            lines.append(renderer(phase['result']))
    return '\n'.join(lines)
