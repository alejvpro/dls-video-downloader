"""Sequential caption work after comments; explicit opt-in, independent state."""
from datetime import datetime, timezone
import math
from pathlib import Path
import time

from . import youtube_safety as safety
from . import caption_collection, caption_planning, comment_runner, forward_metadata
from .captions import DEFAULT_ROOT
from .classification import DEFAULT_START
from .comment_collection import CommentBusy, safe_diagnostic
from .comment_planning import instant


def cooldown(inventory_path, root):
    values=[comment_runner.read_cooldown(Path(root)/'runner'),
            comment_runner.read_cooldown(Path(inventory_path).parent/'comments/runner')]
    metadata=forward_metadata.read_state(Path(inventory_path).parent/'morning').get('cooldown_until')
    if metadata:values.append(instant(metadata))
    return max((v for v in values if v),default=None)


@safety.scoped(lambda a:Path(a['inventory_path']).resolve().parent,'caption-runner')
def run(inventory_path, root=DEFAULT_ROOT, *, execute=False, max_videos=1, runtime_seconds=180,
        start=DEFAULT_START, clock=None, monotonic=None, deadline=None):
    if type(max_videos) is not int or not 1<=max_videos<=2:raise ValueError('Caption max videos must be 1..2')
    if type(runtime_seconds) is not int or not 30<=runtime_seconds<=300:raise ValueError('Caption runtime must be 30..300')
    clock=clock or (lambda:datetime.now(timezone.utc));monotonic=monotonic or time.monotonic
    end=min(monotonic()+runtime_seconds,deadline if deadline is not None else math.inf)
    root=Path(root).resolve();inventory_path=Path(inventory_path).resolve()
    if root==inventory_path.parent or root.is_relative_to(inventory_path.parent/'acquisitions') or root.is_relative_to(inventory_path.parent/'comments'):
        raise ValueError('Caption root must be independent of inventory/media/comments')
    directory=root/'runner'
    def current():return caption_planning.plan(inventory_path,root,now=clock(),start=start,cooldown_until=cooldown(inventory_path,root))
    initial=current();selected=initial['due_ids'][:max_videos]
    result=dict(mode='execute' if execute else 'dry-run',selected_ids=selected,due_count=len(initial['due_ids']),
        budget_deferred=len(initial['due_ids'])-len(selected),work=[],stop_reason=None,exit_code=0)
    shared=safety.current().status(clock());result['youtube_safety']=shared
    result['blocked_due_count']=len(initial.get('deferred_due_ids',[]))
    if not shared['network_allowed']:
        result.update(stop_reason='shared_youtube_'+shared['status'],exit_code=75 if execute else 0);return result
    if not execute:return result
    try:
        with comment_runner.runner_lock(directory):
            for video in selected:
                work=dict(id=video,status='deferred');result['work'].append(work)
                def preflight():
                    safety.current().ensure('caption_collection')
                    item=next((i for i in current()['items'] if i['id']==video),None)
                    if not item or not item['due']:raise comment_runner.Deferred(item['reason'] if item else 'no_longer_eligible')
                    if comment_runner.video_busy(Path(inventory_path).parent/'acquisitions/.acquisition.lock'):
                        raise comment_runner.Deferred('video_acquisition_active')
                    if end-monotonic()<5:raise comment_runner.Deferred('insufficient_remaining_runtime')
                    return item
                try:
                    item=preflight();budget=math.floor(end-monotonic())
                    if budget<30:raise comment_runner.Deferred('insufficient_remaining_runtime')
                    def remaining_preflight():
                        preflight()
                        return max(0,math.floor(end-monotonic()))
                    collected=caption_collection.collect(video,root=root,timeout=min(300,budget),purpose=item['refresh']['purpose'],
                        completion_evidence=item['completion_evidence'],before_collect=remaining_preflight,clock=clock,monotonic=monotonic)
                    work.update(status='failed' if collected['exit_code'] else 'completed',outcome=collected['outcome'],
                        attempt_id=collected['attempt_id'],collection_record=collected['collection_record'],
                        complete=collected['complete'],started_at=collected['started_at'],finished_at=collected['finished_at'])
                    if collected.get('blocking'):
                        comment_runner.atomic_json(directory/'cooldown.json',collected['blocking'])
                        result.update(stop_reason='collector_cooldown',exit_code=2);break
                    if collected['exit_code']:result['exit_code']=collected['exit_code']
                    if collected['exit_code']==130:result['stop_reason']='interrupted';break
                except safety.Blocked as exc:
                    work['reason']=str(exc);result['stop_reason']=str(exc);result['exit_code']=75;break
                except comment_runner.Deferred as exc:
                    work['reason']=str(exc);result['stop_reason']=str(exc);break
                except CommentBusy:
                    result.update(stop_reason='caption_collector_busy',exit_code=75);break
                except KeyboardInterrupt:
                    result.update(stop_reason='interrupted',exit_code=130);break
                except Exception as exc:
                    work.update(status='failed',reason=safe_diagnostic(exc));result.update(stop_reason='caption_runner_error',exit_code=1);break
            done={w['id'] for w in result['work']}
            result['work'].extend(dict(id=v,status='deferred',reason=result['stop_reason'] or 'not_started') for v in selected if v not in done)
            result['finished_at']=instant(clock()).isoformat()
            comment_runner.atomic_json(directory/'last-run.json',result)
    except CommentBusy:result.update(stop_reason='caption_runner_busy',exit_code=75)
    return result


def render(result):
    lines=[f"Caption runner {result['mode']}: due={result['due_count']}, selected={len(result['selected_ids'])}; UTC"]
    for work in result['work']:lines.append(f"{work['id']} | {work['status']} | {work.get('outcome',work.get('reason'))}")
    return '\n'.join(lines)
