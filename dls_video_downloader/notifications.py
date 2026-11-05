"""DLS safety-event alerts and independent durable Pushover delivery history.

Explicit sender only: archival code never invokes this adapter. Provider
acceptance is not device delivery. Uncertain outcomes require manual review.
"""
from contextlib import closing,contextmanager
from datetime import datetime,timedelta,timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from . import pushover

APPLICATION_ID=0x444C534E
PROJECT=Path(__file__).resolve().parent.parent
LEVELS=('critical','warning','informational')
STATES=('pending','inflight','accepted','retryable','rejected','uncertain','review')


def instant(value=None):
    value=datetime.now(timezone.utc) if value is None else value
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:raise ValueError('Timezone-aware notification time required')
    return value.astimezone(timezone.utc)


def identifier(value):
    return isinstance(value,str) and bool(re.fullmatch('[A-Za-z0-9_-]{1,80}',value))


def alert(event):
    """Fixed templates only: no raw diagnostics, local paths or provider secrets."""
    if event.get('version')!=1 or event.get('alertable') is not True:return None
    state=event.get('state_after',{});incident=state.get('incident_id');event_id=event.get('event_id')
    if not identifier(event_id) or not identifier(incident):raise ValueError('Invalid safety event identifiers')
    kind=event.get('event');reason=state.get('reason')
    if kind in ('incident_opened','legacy_restriction_imported'):
        stage='initial'
        auth=reason in ('authentication_challenge','access_restriction') or any(
            s.get('category') in ('authentication_challenge','access_restriction') for s in state.get('legacy_sources',[]))
        level='critical' if auth else 'warning'
        title='DLS Archive: YouTube verification required' if auth else 'DLS Archive: YouTube access restricted'
        message='YouTube access was restricted and routine work was deferred. Cooldown expiry alone does not establish recovery; consult current DLS safety status.'
    elif kind=='repeated_block_manual_review':
        stage='manual_review';level='critical';title='DLS Archive: manual review required'
        message='A controlled YouTube recovery was blocked again. Review is required before further recovery attempts.'
    elif kind=='unresolved_review_due':
        stage='unresolved';level='warning';title='DLS Archive: unresolved YouTube incident'
        message='At this review point the incident remained unresolved. Consult current DLS safety status before further operations.'
    elif kind=='controlled_recovery_succeeded':
        stage='recovery';level='informational';title='DLS Archive: controlled recovery observed'
        message='An authorized metadata probe succeeded. This does not verify every endpoint or restore scheduled jobs.'
    elif kind in ('recovery_rate_limited','recovery_incomplete'):
        stage=kind+'-'+event_id;level='warning';title='DLS Archive: recovery remains unresolved'
        message='A controlled recovery was incomplete or rate limited. No successful recovery is established by this event.'
    else:return None
    key=hashlib.sha256(('pushover:'+incident+':'+stage).encode()).hexdigest()
    return dict(notification_id=key,source_event_id=event_id,incident_id=incident,kind=kind,level=level,
                title=title,message=message+' Incident '+incident[:12]+'. Observed UTC '+instant(event['at']).isoformat()+'.',created_at=instant(event['at']).isoformat())


def test_alert(test_id,now):
    if not identifier(test_id):raise ValueError('Test ID must be 1..80 safe characters')
    # A connectivity test is explicit operator action, not a fabricated incident.
    return dict(notification_id=hashlib.sha256(('pushover-test:'+test_id).encode()).hexdigest(),
        source_event_id='test-'+test_id,incident_id=None,kind='operator_test',level='informational',
        title='DLS Archive: notification test',message='Pushover connectivity test. No YouTube request or recovery operation was performed.',
        created_at=instant(now).isoformat())


def events(state):
    result=[]
    for path in sorted((Path(state)/'youtube-safety/events').glob('[0-9]'*12+'-*.json')):
        if path.stat().st_size>256*1024:raise ValueError('Safety event exceeds bound')
        data=json.loads(path.read_bytes())
        if data.get('event_id')!=path.stem[13:]:raise ValueError('Safety event identity mismatch')
        value=alert(data)
        if value:result.append(value)
    return result


def root_for(state):return Path(state).resolve()/'notifications'


class ActionError(ValueError):
    """Fixed operator-facing messages only; never provider exception text."""


class SenderBusy(ValueError):pass


@contextmanager
def worker_lock(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.sender.lock').open('a') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SenderBusy('notification_sender_busy') from None
        yield stream.fileno()


class Catalog:
    def __init__(self,root):
        root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(root/'deliveries.sqlite3',timeout=1);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        identity=(self.db.execute('PRAGMA application_id').fetchone()[0],self.db.execute('PRAGMA user_version').fetchone()[0])
        if identity==(0,0) and not self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            self.db.executescript(f'''BEGIN;
                PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;
                CREATE TABLE notifications(notification_id TEXT PRIMARY KEY,source_event_id TEXT NOT NULL,
                  incident_id TEXT,kind TEXT NOT NULL,level TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,
                  created_at TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL,next_due_at TEXT,
                  attempt_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,provider_request_id TEXT);
                CREATE TABLE event_links(event_id TEXT PRIMARY KEY,notification_id TEXT NOT NULL REFERENCES notifications);
                CREATE TABLE provider_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),hold_reason TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY,notification_id TEXT NOT NULL REFERENCES notifications,
                  started_at TEXT NOT NULL,finished_at TEXT,outcome TEXT NOT NULL,error TEXT,http_status INTEGER,provider_request_id TEXT);
                COMMIT;''')
        elif identity!=(APPLICATION_ID,1):self.db.close();raise ValueError('unsupported_notification_catalog')

    def __enter__(self):return self
    def __exit__(self,*args):self.db.close()

    def ingest(self,values,now):
        with self.db:
            for value in values:
                self.db.execute('''INSERT OR IGNORE INTO notifications(notification_id,source_event_id,incident_id,kind,level,title,message,created_at,status,updated_at,next_due_at)
                    VALUES(:notification_id,:source_event_id,:incident_id,:kind,:level,:title,:message,:created_at,'pending',:now,:now)''',dict(value,now=instant(now).isoformat()))
                self.db.execute('INSERT OR IGNORE INTO event_links VALUES(?,?)',(value['source_event_id'],value['notification_id']))

    def interrupted(self,now):
        # Caller owns the OS lock. Any previous network child has also released
        # it: remaining in-flight records are uncertain, never auto-resendable.
        stamp=instant(now).isoformat()
        with self.db:
            self.db.execute("UPDATE attempts SET outcome='uncertain',finished_at=?,error='abandoned_inflight' WHERE outcome='inflight'",(stamp,))
            self.db.execute("UPDATE notifications SET status='uncertain',updated_at=?,next_due_at=NULL,last_error='abandoned_inflight' WHERE status='inflight'",(stamp,))

    def begin(self,notification_id,now):
        attempt=uuid.uuid4().hex;stamp=instant(now).isoformat()
        with self.db:
            row=self.db.execute('SELECT * FROM notifications WHERE notification_id=?',(notification_id,)).fetchone()
            if row is None or row['status'] not in ('pending','retryable') or instant(row['next_due_at'])>instant(now):
                raise ValueError('notification_no_longer_due')
            self.db.execute("UPDATE notifications SET status='inflight',updated_at=?,attempt_count=attempt_count+1,next_due_at=NULL WHERE notification_id=?",(stamp,notification_id))
            self.db.execute("INSERT INTO attempts(attempt_id,notification_id,started_at,outcome) VALUES(?,?,?,'inflight')",(attempt,notification_id,stamp))
        return attempt

    def finish(self,attempt,result,now):
        row=self.db.execute('SELECT a.notification_id,n.attempt_count FROM attempts a JOIN notifications n USING(notification_id) WHERE a.attempt_id=?',(attempt,)).fetchone()
        outcome=result['outcome'];status=outcome;due=None;code=result.get('error')
        if outcome=='retryable':
            if row['attempt_count']>=5:status='review';code='retry_exhausted'
            else:due=(instant(now)+timedelta(seconds=min(3600,60*5**(row['attempt_count']-1)))).isoformat()
        with self.db:
            self.db.execute('UPDATE attempts SET finished_at=?,outcome=?,error=?,http_status=?,provider_request_id=? WHERE attempt_id=?',
                (instant(now).isoformat(),outcome,result.get('error'),result.get('http_status'),result.get('provider_request_id'),attempt))
            if outcome=='rejected':self.db.execute('INSERT OR REPLACE INTO provider_state VALUES(1,?,?)',(result.get('error') or 'provider_rejected',instant(now).isoformat()))
            self.db.execute('UPDATE notifications SET status=?,updated_at=?,next_due_at=?,last_error=?,provider_request_id=? WHERE notification_id=?',
                (status,instant(now).isoformat(),due,code,result.get('provider_request_id'),row['notification_id']))
        return status


def read_rows(root):
    path=root/'deliveries.sqlite3'
    if not path.exists():return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
        if (db.execute('PRAGMA application_id').fetchone()[0],db.execute('PRAGMA user_version').fetchone()[0])!=(APPLICATION_ID,1):raise ValueError('unsupported_notification_catalog')
        return [dict(r) for r in db.execute('SELECT * FROM notifications ORDER BY created_at,notification_id')]


def delivery_history(root):
    path=root/'deliveries.sqlite3'
    if not path.exists():return [],None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
        attempts=[dict(r) for r in db.execute('SELECT * FROM attempts ORDER BY rowid DESC LIMIT 50')]
        hold=db.execute('SELECT hold_reason FROM provider_state WHERE singleton=1').fetchone()
        return attempts,hold[0] if hold else None


def plan(state,*,now=None,operator_test=None,include_tests=False):
    now=instant(now);root=root_for(state);catalog={r['notification_id']:r for r in read_rows(root)}
    candidates=[test_alert(operator_test,now)] if operator_test else events(state)
    for value in candidates:catalog.setdefault(value['notification_id'],dict(value,status='pending',next_due_at=value['created_at'],attempt_count=0))
    rows=list(catalog.values())
    if operator_test:rows=[r for r in rows if r['notification_id']==candidates[0]['notification_id']]
    elif not include_tests:rows=[r for r in rows if r['kind']!='operator_test']
    due=sorted((r for r in rows if r['status'] in ('pending','retryable') and instant(r['next_due_at'])<=now),
               key=lambda r:(LEVELS.index(r['level']),instant(r['next_due_at']),r['notification_id']))
    attempts,hold=delivery_history(root)
    return dict(mode='plan',evaluated_at=now.isoformat(),credentials_checked=False,items=rows,provider_hold=hold,
                source_journal_present=(Path(state)/'youtube-safety/events').exists(),catalog_present=(root/'deliveries.sqlite3').exists(),
                attempts=attempts if include_tests else [],deferred_due_ids=[r['notification_id'] for r in due] if hold else [],
                due_ids=[] if hold else [r['notification_id'] for r in due],review_ids=[r['notification_id'] for r in rows if r['status'] in ('inflight','uncertain','rejected','review')])


def run(state,*,execute=False,config=pushover.DEFAULT_CONFIG,max_messages=1,runtime_seconds=60,
        operator_test=None,clock=None,monotonic=None):
    if type(max_messages) is not int or not 1<=max_messages<=5:raise ValueError('max_messages must be 1..5')
    if type(runtime_seconds) is not int or not 5<=runtime_seconds<=120:raise ValueError('runtime_seconds must be 5..120')
    clock=clock or (lambda:datetime.now(timezone.utc));monotonic=monotonic or time.monotonic
    deadline=monotonic()+runtime_seconds
    result=plan(state,now=clock(),operator_test=operator_test)
    result.update(selected_ids=result['due_ids'][:max_messages],work=[],exit_code=0)
    if not execute:return result
    result['mode']='execute'
    if not result['selected_ids']:
        if result.get('provider_hold'):result.update(exit_code=75,error='provider_delivery_paused')
        if any(r['status']=='inflight' for r in result['items']):
            try:
                with worker_lock(root_for(state)),Catalog(root_for(state)) as store:store.interrupted(clock())
                result.update(plan(state,now=clock(),operator_test=operator_test));result['mode']='execute'
            except SenderBusy:result.update(exit_code=75,error='notification_sender_busy')
        return result
    try:credentials=pushover.load_credentials(config)
    except pushover.ConfigurationError as exc:
        result.update(exit_code=1,error=str(exc) if str(exc) in {'reserved_credential_source','credentials_must_be_outside_repository','credentials_permissions','credentials_invalid','credentials_missing'} else 'credentials_invalid',credentials_checked=True);return result
    result.update(mode='execute',credentials_checked=True)
    root=root_for(state)
    try:
        with worker_lock(root) as fd, Catalog(root) as store:
            store.interrupted(clock())
            store.ingest([test_alert(operator_test,clock())] if operator_test else events(state),clock())
            fresh=plan(state,now=clock(),operator_test=operator_test)
            selected=fresh['due_ids'][:max_messages]
            result['selected_ids']=selected
            for notification_id in selected:
                remaining=math.floor(deadline-monotonic())
                if remaining<5:
                    result['work'].append(dict(notification_id=notification_id,outcome='deferred',reason='runtime_budget'));break
                value=next(r for r in fresh['items'] if r['notification_id']==notification_id)
                attempt=store.begin(notification_id,clock())
                try:delivery=pushover.deliver(credentials,value,min(20,remaining),fd)
                except BaseException:delivery=dict(outcome='uncertain',error='interrupted')
                status=store.finish(attempt,delivery,clock())
                result['work'].append(dict(notification_id=notification_id,attempt_id=attempt,status=status,**delivery))
                if delivery.get('error')=='interrupted':result['exit_code']=130;break
                if status!='accepted':result['exit_code']=2;break
    except SenderBusy:result.update(exit_code=75,error='notification_sender_busy')
    except Exception:
        result.update(exit_code=1,error='notification_sender_unavailable')
    snapshot=plan(state,now=clock(),operator_test=operator_test)
    for field in ('items','due_ids','review_ids','provider_hold','deferred_due_ids'):result[field]=snapshot[field]
    done={r['notification_id'] for r in result['work']}
    result['work'].extend(dict(notification_id=n,outcome='deferred',reason='sender_stopped') for n in result['selected_ids'] if n not in done)
    result['finished_at']=instant(clock()).isoformat()
    return result


def retry(state,notification_id,*,execute=False,acknowledge_duplicate_risk=False,now=None):
    if not re.fullmatch('[a-f0-9]{64}',notification_id):raise ActionError('Invalid notification ID')
    rows=read_rows(root_for(state));row=next((r for r in rows if r['notification_id']==notification_id),None)
    if row is None:raise ActionError('Unknown notification')
    if row['status']=='accepted':raise ActionError('Accepted notifications cannot be retried')
    if row['status'] in ('uncertain','inflight') and not acknowledge_duplicate_risk:raise ActionError('Uncertain delivery requires --acknowledge-duplicate-risk before retry')
    if not execute:return dict(mode='plan',notification_id=notification_id,status=row['status'],action='would_allow_retry',network=False)
    root=root_for(state)
    with worker_lock(root),Catalog(root) as store:
        store.interrupted(now)
        current=store.db.execute('SELECT status FROM notifications WHERE notification_id=?',(notification_id,)).fetchone()[0]
        if current=='accepted':raise ActionError('Accepted notifications cannot be retried')
        if current=='uncertain' and not acknowledge_duplicate_risk:raise ActionError('Uncertain delivery requires --acknowledge-duplicate-risk before retry')
        with store.db:
            store.db.execute("UPDATE notifications SET status='pending',updated_at=?,next_due_at=?,last_error=NULL WHERE notification_id=?",(instant(now).isoformat(),instant(now).isoformat(),notification_id))
            if current=='rejected':store.db.execute('DELETE FROM provider_state WHERE singleton=1')
    return dict(mode='execute',notification_id=notification_id,status='pending',network=False)


def render(result):
    lines=[f"DLS notifications: {result['mode']} | due={len(result.get('due_ids',[]))}; provider acceptance is not device confirmation."]
    if result.get('source_journal_present') is False:lines.append('No safety event journal present; this does not establish healthy YouTube access.')
    for row in result.get('items',[]):lines.append(f"{row['notification_id']} | {row['level']} | {row['status']} | {row['title']}")
    for row in result.get('work',[]):lines.append(f"{row['notification_id']} | {row.get('status',row.get('outcome'))}")
    if result.get('provider_hold'):lines.append('Pushover delivery paused: '+result['provider_hold']+'; review and explicitly requeue the rejected notification after correction.')
    if result.get('notification_id'):lines.append(f"{result['notification_id']} | {result.get('status',result.get('action'))}")
    if result.get('error'):lines.append(result['error'])
    return '\n'.join(lines)
