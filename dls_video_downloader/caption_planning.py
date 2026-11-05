"""Read-only caption selection and deterministic, independently anchored policy."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from . import youtube_safety as safety
from . import comment_planning
from .captions import DEFAULT_ROOT, read_history
from .classification import DEFAULT_START
from .comment_planning import instant
from .inventory import connect

MISSING = tuple(timedelta(hours=h) for h in (1,6,24,72,168,336,720))


def evaluate(history, *, now, completed_at, cooldown_until=None, mode='forward'):
    """Explicit UTC inputs, no IO. Historical orchestration is deliberately absent.

    One attempt consumes elapsed checkpoints. Technical failures have independent
    1h/6h/24h backoff and suspend after five consecutive failures. Successful empty
    listings are observations, not failures. Missing formats share the same finite
    search checkpoints; confirmations never restart after revisions.
    """
    now = instant(now)
    decision = dict(due=False,next_due_at=None,reason=None,phase='waiting',purpose=None,
                    monitoring_complete=False,suspended=False,manual_allowed=True,
                    anchor=instant(completed_at).isoformat() if completed_at else None)
    def finish(reason, due_at=None, phase='missing', purpose='refresh', suspended=False, complete=False):
        decision.update(reason=reason, phase=phase,purpose=purpose,suspended=suspended,monitoring_complete=complete)
        if due_at is not None:
            if cooldown_until and instant(cooldown_until)>due_at:
                due_at=instant(cooldown_until)
                if due_at>now:decision['reason']='collector_cooldown'
            decision.update(due=due_at<=now,next_due_at=due_at.isoformat())
        return decision
    if mode != 'forward': return finish('historical_collection_requires_explicit_separate_workflow',phase='historical',suspended=True)
    if completed_at is None: return finish('reliable_completion_observation_unknown',suspended=True)
    anchor=instant(completed_at)
    if anchor>now: return finish('completion_observation_in_future',suspended=True)
    rows=sorted(history,key=lambda h:instant(h['finished_at']))
    failures=0
    for row in reversed(rows):
        if not row.get('technical_failure'):break
        failures+=1
    if rows and rows[-1].get('ambiguities'):return finish('ambiguous_track_identity_requires_review',phase='review',suspended=True)
    if failures>=5:return finish('five_consecutive_technical_failures',phase='review',suspended=True)
    if any(not r.get('technical_failure') and not r.get('complete') and r.get('purpose') in ('initial','refresh') and instant(r['started_at'])>=anchor+MISSING[-1] for r in rows):
        return finish('missing_caption_monitoring_ended_manual_refresh_available',phase='complete',complete=True)
    first=next((instant(r['finished_at']) for r in rows if r.get('complete')),None)
    if first is not None:
        checks=[r for r in rows if r.get('purpose','').startswith('confirmation') and not r.get('technical_failure')]
        if any(instant(r['started_at'])>=first+timedelta(days=7) for r in checks):
            return finish('confirmation_monitoring_complete',phase='complete',complete=True)
        checked_day1=any(instant(r['started_at'])>=first+timedelta(days=1) for r in checks)
        # On a late invocation, do only the latest elapsed confirmation.
        day=7 if checked_day1 or now>=first+timedelta(days=7) else 1
        due_at=first+timedelta(days=day)
        purpose='confirmation_7d' if day==7 else 'confirmation_24h'
        phase='confirmation'
    elif not rows:
        due_at=anchor; purpose='initial';phase='missing'
    else:
        successful=[r for r in rows if not r.get('technical_failure')]
        last=instant(successful[-1]['started_at']) if successful else None
        future=[anchor+offset for offset in MISSING if last is None or anchor+offset>last]
        if last is not None and not future:
            return finish('missing_caption_monitoring_ended_manual_refresh_available',phase='complete',complete=True)
        due_at=future[0] if last else anchor
        purpose='refresh';phase='missing'
    if failures:
        retry=instant(rows[-1]['finished_at'])+timedelta(hours=(1 if failures==1 else 6 if failures==2 else 24))
        # Retry the unsuccessful operation, rather than consume a new checkpoint.
        due_at=retry;purpose=rows[-1]['purpose'];phase='retry'
    return finish('due' if due_at<=now else 'scheduled',due_at,phase,purpose)


def plan(inventory_path, root=DEFAULT_ROOT, *, now, start=DEFAULT_START, cooldown_until=None):
    """Reuse established selection, without inheriting the comment-age deadline."""
    now=instant(now)
    # This existing planner supplies selection and provenance only. Its independent
    # comment refresh due/suspension decisions never control caption eligibility.
    history=read_history(root);items=[]
    with closing(connect(inventory_path,readonly=True,timeout=1)) as db:
        db.execute('BEGIN')
        base=comment_planning.selection_view(db,now=now,start=start)
        for selected in base['items']:
            metadata=json.loads(db.execute('SELECT metadata FROM videos WHERE id=?',(selected['id'],)).fetchone()[0])
            daily=selected['category']=='daily_show' or metadata.get('was_live') is True or metadata.get('live_status')=='was_live'
            exclusion=selected['reason'] if selected['status']=='excluded' else None
            # A completion observation must be full extraction, not a flat listing
            # or the video's self-reported broadcast/publication timestamp.
            observations=db.execute("SELECT metadata,imported_at,observed_at FROM observations WHERE video_id=? AND detail='full' ORDER BY imported_at,observation_id",(selected['id'],)).fetchall()
            observed=next((o for o in observations if json.loads(o['metadata']).get('live_status') == ('was_live' if daily else 'not_live')),None)
            if observed is None:exclusion=exclusion or 'reliable_completed_status_required'
            # imported_at is our actual observation time; observed_at may be an
            # externally supplied extraction epoch, retained separately as evidence.
            anchor=observed['imported_at'] if observed else None
            anchor_field='first full completed observation imported_at'
            if not daily and observed:
                evidence=json.loads(observed['metadata'])
                source=evidence.get('timestamp')
                if type(source) in (int,float):
                    try:anchor=datetime.fromtimestamp(source,timezone.utc).isoformat();anchor_field='YouTube timestamp (upload availability)'
                    except (ValueError,OverflowError,OSError):pass
            rows=[h for h in history if h['video_id']==selected['id']]
            decision=evaluate(rows,now=now,completed_at=anchor,cooldown_until=cooldown_until)
            items.append(dict(id=selected['id'],title=selected['title'],category=selected['category'],
                eligible=exclusion is None,due=exclusion is None and decision['due'],
                reason=exclusion or decision['reason'],refresh=decision,
                completion_anchor=anchor,completion_anchor_field=anchor_field,
                completion_evidence=dict(observed_at=observed['observed_at'],imported_at=observed['imported_at']) if observed else None,
                source_timing=selected['source_timing'],last_collection_at=rows[-1]['finished_at'] if rows else None))
    due=sorted((i for i in items if i['due']),key=lambda i:(i['refresh']['next_due_at'],i['id']))
    return safety.apply_plan(dict(evaluated_at=now.isoformat(),due_ids=[i['id'] for i in due],items=items),Path(inventory_path).resolve().parent,now)


def render(result):
    lines=[f"Caption plan (UTC): due={len(result['due_ids'])}; monitoring does not certify transcript accuracy."]
    for item in result['items']:
        if item['eligible']:
            lines.append(f"{item['id']} | {item['title']} | {item['refresh']['phase']} | {item['reason']} | next={item['refresh']['next_due_at']}")
    return '\n'.join(lines)
