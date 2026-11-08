"""Forward metadata scope and retry evidence, not acquisition selection or a queue."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from .classification import CHANNEL, publication
from .comment_planning import age_anchor, instant
from .inventory import report

RECENT = timedelta(days=15)
RETRY_DELAYS = (timedelta(hours=1), timedelta(hours=6), timedelta(days=1))
BLOCK_DELAY = timedelta(hours=6)
STATE_LIMIT = 256 * 1024


def read_state(directory):
    try:
        with (Path(directory)/'retry.json').open('rb') as stream:
            raw=stream.read(STATE_LIMIT+1)
    except FileNotFoundError:
        return {'version':1,'failures':{},'cooldown_until':None,'deferred_initial':{}}
    if len(raw)>STATE_LIMIT:
        raise ValueError('Oversized metadata retry state; manual review required')
    state=json.loads(raw)
    if state.get('version')!=1 or not isinstance(state.get('failures'),dict):
        raise ValueError('Unsupported metadata retry state')
    state.setdefault('deferred_initial',{})
    if not isinstance(state['deferred_initial'],dict):
        raise ValueError('Invalid deferred initial-enrichment state')
    for video_id, value in state['deferred_initial'].items():
        if not isinstance(video_id,str) or not isinstance(value,str):
            raise ValueError('Invalid deferred initial-enrichment entry')
        instant(value)
    if state.get('cooldown_until'):instant(state['cooldown_until'])
    for value in state['failures'].values():
        if type(value['count']) is not int or value['count']<1:raise ValueError('Invalid metadata failure count')
        instant(value['next_retry'])
    return state


class MetadataStateError(ValueError):
    pass


def save_state(directory, state):
    try:
        _save_state(directory,state)
    except (OSError,ValueError) as exc:
        raise MetadataStateError(f"Cannot persist metadata retry/cooldown safety state: {exc}") from exc


def _save_state(directory, state):
    raw=json.dumps(state,ensure_ascii=False,indent=2).encode()
    if len(raw)>STATE_LIMIT:raise ValueError('Metadata retry state exceeds bound; manual review required')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.retry-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,directory/'retry.json')
    finally:
        if os.path.exists(name):os.unlink(name)


def failed(state, video, now, error):
    prior=state['failures'].get(video,{})
    count=prior.get('count',0)+1
    state['failures'][video]={'count':count,'last_failure':now.isoformat(),
        'next_retry':(now+RETRY_DELAYS[min(count-1,len(RETRY_DELAYS)-1)]).isoformat(),
        'error':str(error)[:1000]}



def enrichment_work(db, listed, *, now, start, retained_pending=()):
    """Return durable forward enrichment work derived from retained observations.

    A recent flat observation with no full observation remains pending even when
    absent from this run's bounded listing. Full, resolved Skip/Review records
    are complete; active/incomplete full records are follow-ups.
    """
    from datetime import date
    recent_floor=now-RECENT
    start_date=date.fromisoformat(start)
    rows=db.execute("SELECT id,metadata,field_provenance FROM videos ORDER BY id").fetchall()
    items={i['id']:i for i in report(db,start)['items']}
    initial=[];follow=[];resolved=[];completed=[];unknown=[]
    for row in rows:
        vid=row['id']; meta=json.loads(row['metadata']); prov=json.loads(row['field_provenance'])
        if meta.get('channel_id')!=CHANNEL: continue
        obs=db.execute("SELECT detail,observed_at FROM observations WHERE video_id=? ORDER BY observation_id",(vid,)).fetchall()
        fulls=[x for x in obs if x['detail']=='full']
        sources={x['source']:x['last_seen'] for x in db.execute('SELECT source,last_seen FROM sources WHERE video_id=?',(vid,))}
        sightings=[x for k,x in sources.items() if k in ('live','videos','morning:live','morning:videos')]
        recent_presentation=any(_recent(sources[k],recent_floor,now)
                                for k in ('morning:live','morning:videos') if k in sources)
        active_status=meta.get('live_status') in ('is_upcoming','is_live','post_live')
        status_at=prov.get('live_status',{}).get('at')
        recent_active=active_status and status_at and _recent(status_at,recent_floor,now)
        recent=vid in listed or vid in retained_pending or recent_presentation or recent_active
        published,_=publication(meta)
        in_date=published is None or date.fromisoformat(published)>=start_date
        if not recent or not in_date: continue
        item=items.get(vid,{})
        if item.get('decision') in ('skip','reject'):
            continue
        if not fulls:
            initial.append((vid, 'streams' if any(k in sources for k in ('live','morning:live')) else 'videos', min(sightings) if sightings else row['id']))
            if published is None:unknown.append(vid)
            continue
        completed.append(vid)
        status=meta.get('live_status');full_status=prov.get('live_status',{}).get('rank')==2
        complete=full_status and status in ('was_live','not_live') and not meta.get('is_live')
        if complete and item.get('classification') in ('skip','review','not_selected'):
            resolved.append(vid)
        elif not complete:
            follow.append((vid,'streams' if any(k in sources for k in ('live','morning:live')) else 'videos',min(sightings) if sightings else vid))
        else:resolved.append(vid)
    # Round robin by originating tab, oldest retained sightings first within tab.
    initial=_fair(initial);follow=_fair(follow)
    return {'initial_pending':[x[0] for x in initial], 'followup_required':[x[0] for x in follow],
            'initial_completed':completed,
            'resolved':resolved,'unknown_date':[x for x in unknown if x in {a[0] for a in initial}],
            'listed_initial_pending':[x for x in listed if any(x==a[0] for a in initial)],
            'initial_queue':[list(x) for x in initial],'followup_queue':[list(x) for x in follow],'pending_count':len(initial),'resolved_count':len(resolved)}


def _recent(value,floor,now):
    try:
        at=instant(value);return floor<=at<=now
    except (ValueError,TypeError):return False


def _fair(entries):
    groups={'streams':[],'videos':[]}
    for row in sorted(entries,key=lambda x:(x[2],x[0])):groups[row[1]].append(row)
    out=[]
    while groups['streams'] or groups['videos']:
        for tab in ('streams','videos'):
            if groups[tab]:out.append(groups[tab].pop(0))
    return out

def candidates(db, head, listed_status, *, now, start):
    """Every relevant forward ID needing metadata; no content-priority slots.

    Verified old publication wins over a recent listing. Missing publication is
    not age proof: only current/recent bounded-list evidence or fresh *full*
    active-status evidence can admit it. Legacy first_seen is never substituted.
    """
    items={i['id']:i for i in report(db,start)['items']}
    lower=max(now-RECENT,datetime.fromisoformat(start).replace(tzinfo=timezone.utc))
    pending=[];historical=[];excluded={};established=[];scope=[];ordering={}
    for row in db.execute('SELECT * FROM videos ORDER BY id'):
        video=row['id'];metadata=json.loads(row['metadata']);provenance=json.loads(row['field_provenance'])
        sources={r['source']:r for r in db.execute('SELECT * FROM sources WHERE video_id=?',(video,))}
        anchor,_,_=age_anchor(metadata,'live' in sources)
        if anchor is not None and not isinstance(anchor,datetime):
            anchor=datetime(anchor.year,anchor.month,anchor.day,tzinfo=timezone.utc)
        status=metadata.get('live_status');status_proof=provenance.get('live_status',{})
        full=status_proof.get('rank')==2 and metadata.get('channel_id')==CHANNEL
        complete=(full and status in ('was_live','not_live')
                  and not (metadata.get('is_live') is True and
                           provenance.get('is_live',{}).get('at')==status_proof.get('at')))
        presentation=[instant(sources[k]['last_seen']) for k in ('morning:live','morning:videos') if k in sources]
        recent_presentation=video in head or any(now-RECENT<=at<=now for at in presentation)
        recent_active=(full and status in ('is_upcoming','is_live','post_live') and
                       now-RECENT<=instant(status_proof['at'])<=now)
        in_scope=(lower<=anchor<=now+RECENT if anchor is not None else recent_presentation or recent_active)
        needs=not complete or listed_status.get(video) not in (None,status)
        if not in_scope:
            if anchor is not None and anchor>now+RECENT:
                excluded[video]='publication_beyond_forward_horizon'
            elif needs:
                historical.append(video)
            continue
        if items[video]['decision'] in ('skip','reject'):
            excluded[video]='manual_'+items[video]['decision'];continue
        scope.append(video)
        if needs:
            pending.append(video)
            # Oldest forward evidence first, without content-based slots. A known
            # current broadcast precedes newer, newly presented mini-broadcasts.
            ordering[video]=anchor or min(presentation,default=instant(status_proof.get('at',now.isoformat())))
        else:established.append(video)
    pending.sort(key=lambda video:(ordering[video],video))
    return {'scope':scope,'pending':pending,'established':established,'excluded':excluded,
            'historical_count':len(historical),'historical_sample':historical[:20],
            'recent_seconds':int(RECENT.total_seconds())}
