"""Repository-local YouTube admission, immutable incidents, explicit recovery.

The atomic event journal is authoritative: each committed event contains the
complete current state. No independently mutable snapshot can disagree with it.
Reads never create files or migrate legacy cooldown evidence.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
import fcntl
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

PROJECT = Path(__file__).resolve().parent.parent
_scope = ContextVar('youtube_safety_scope', default=None)
_recovery = ContextVar('youtube_safety_recovery', default=None)
LIMIT = 256 * 1024
BOT_HOURS = 24
_latches = {}


class Blocked(ValueError):
    """Global admission deferral, not an asset failure or local retry."""


def utc(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if isinstance(value,str): value=datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None: raise ValueError('UTC-aware time required')
    return value.astimezone(timezone.utc)


def classify(text):
    """Diagnostics/status only; never pass source comments or caption text here."""
    text=str(text)
    if re.search(r'(?i)LOGIN_REQUIRED|confirm.{0,80}(?:bot|human)|bot.verification|captcha|authentication required|HTTP (?:Error )?401\b',text):
        return 'authentication_challenge'
    if re.search(r'(?i)\b(?:HTTP(?: Error|/\S+)?\s*[:=]?\s*429|429 Too Many Requests)\b|too many requests|\brate[ -]limit(?:ed|ing| exceeded)?\b',text):
        return 'rate_limit'
    # A bare 403 can mean an expired signature, unavailable format or resource.
    if re.search(r'(?i)(?:IP|requests?|access).{0,30}(?:blocked|banned)|access denied.*(?:youtube|verification)',text):
        return 'access_restriction'
    return None


def retry_time(text, now, category, count=1, bot_hours=BOT_HOURS):
    now=utc(now)
    if category in ('authentication_challenge','access_restriction'):
        return now+timedelta(hours=bot_hours)
    match=re.search(r'(?im)^\s*Retry-After:\s*([^\r\n]+)',str(text))
    if match:
        try:
            raw=match[1].strip()
            seconds=int(raw) if raw.isdigit() else (utc(parsedate_to_datetime(raw))-now).total_seconds()
            if math.isfinite(seconds) and 0<seconds<=30*86400:
                return now+timedelta(seconds=max(60,seconds))
        except (ValueError,TypeError,OverflowError):pass
    return now+timedelta(hours=1 if count==1 else 6 if count==2 else 24)


def evidence(text):
    # Shared state contains diagnostic categories, not full raw stderr/URLs.
    text=re.sub(r'https?://\S+','[URL omitted]',str(text))
    labels=[]
    for pattern,label in [(r'LOGIN_REQUIRED','LOGIN_REQUIRED'),(r'(?i)confirm.{0,80}(?:bot|human)','verification requested'),
                          (r'\b429\b','HTTP 429'),(r'\b401\b','HTTP 401'),(r'\b403\b','HTTP 403')]:
        if re.search(pattern,text):labels.append(label)
    return labels or ['confirmed '+str(classify(text))]


def is_network(argv):
    if not argv:return False
    return Path(str(argv[0])).name in ('yt-dlp','yt-dlp-new') or any(
        str(a).startswith(('https://www.youtube.com/','https://youtube.com/','https://youtu.be/')) for a in argv)


def state_from_db(db):
    filename=db.execute('PRAGMA database_list').fetchone()[2]
    if not filename:raise ValueError('File-backed inventory required for network operations')
    return Path(filename).resolve().parent


@contextmanager
def scope(state, component='youtube'):
    token=_scope.set((Path(state).resolve(),component))
    try:yield
    finally:_scope.reset(token)


def scoped(resolver, component):
    def decorate(function):
        signature=inspect.signature(function)
        @wraps(function)
        def wrapped(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            parent=_scope.get()
            state=parent[0] if parent else resolver(bound.arguments)
            with scope(state,component):return function(*args,**kwargs)
        return wrapped
    return decorate


def current(fallback=None):
    value=_scope.get()
    return Safety(value[0] if value else fallback or PROJECT/'state')


def component(default='youtube'):
    return _scope.get()[1] if _scope.get() else default


class Safety:
    def __init__(self,state,*,bot_hours=None):
        if bot_hours is None:
            try:bot_hours=int(os.environ.get("DLS_YOUTUBE_BOT_COOLDOWN_HOURS",str(BOT_HOURS)))
            except ValueError as exc:raise Blocked("Invalid shared bot cooldown configuration") from exc
        if type(bot_hours) is not int or not 1<=bot_hours<=168:raise ValueError('Bot cooldown must be 1..168 hours')
        self.state=Path(state).resolve();self.root=self.state/'youtube-safety';self.bot_hours=bot_hours

    def _latest(self):
        paths=sorted((self.root/'events').glob('[0-9]'*12+'-*.json'))
        if not paths:return dict(version=1,sequence=0,status='normal',incident_id=None,incident_count=0,
                                 blocking_observations=0,manual_review_required=False,last_recovery_success=None,legacy_ack=[])
        path=paths[-1]
        if path.stat().st_size>LIMIT:raise Blocked('shared_safety_invalid: oversized event')
        try:
            event=json.loads(path.read_bytes());state=event['state_after']
            if event['version']!=1 or state['version']!=1 or state['sequence']!=int(path.name[:12]):raise ValueError()
            if state['status'] not in ('normal','cooldown','recovery_in_progress','manual_review'):raise ValueError()
            if state['status']!='normal' and (not state.get('incident_id') or not state.get('retry_at')):raise ValueError()
            for field in ('first_observed_at','last_observed_at','retry_at'):
                if state.get(field):utc(state[field])
            return state
        except (ValueError,KeyError,TypeError) as exc:raise Blocked('shared_safety_invalid: manual review required') from exc

    def legacy(self):
        result=[]
        for relative,field in [('comments/runner/cooldown.json','until'),('captions/runner/cooldown.json','until'),('morning/retry.json','cooldown_until')]:
            path=self.state/relative
            try:
                with path.open('rb') as stream:raw=stream.read(LIMIT+1)
            except FileNotFoundError:continue
            try:
                if len(raw)>LIMIT:raise ValueError()
                data=json.loads(raw)
                if data.get('version')!=1:raise ValueError()
                if not data.get(field):continue
                until=utc(data[field]).isoformat()
            except (ValueError,TypeError,KeyError) as exc:raise Blocked('shared_safety_invalid: legacy restriction needs review') from exc
            observed=data.get('observed_at')
            if observed:
                try:observed=utc(observed).isoformat()
                except (ValueError,TypeError):observed=None
            result.append(dict(observed_at=observed,path=relative,sha256=hashlib.sha256(raw).hexdigest(),until=until,
                category=classify(data.get('diagnostic','')) or 'legacy_restriction'))
        return result

    def status(self,now=None):
        now=utc(now);state=dict(self._latest())
        legacy=[l for l in self.legacy() if l['sha256'] not in state.get('legacy_ack',[])]
        # Project legacy evidence without rewriting it or extending its deadline.
        if legacy:
            until=max(utc(l['until']) for l in legacy)
            if state['status']=='normal':
                state.update(status='cooldown',incident_id='legacy-'+legacy[0]['sha256'][:20],reason='legacy_restriction',retry_at=until.isoformat(),legacy_pending=True,incident_count=state.get('incident_count',0)+1,blocking_observations=1,related_blocking_observations=1,first_observed_at=min((l['observed_at'] for l in legacy if l['observed_at']),default=None),last_observed_at=max((l['observed_at'] for l in legacy if l['observed_at']),default=None))
            elif state.get('retry_at') and until>utc(state['retry_at']):state['retry_at']=until.isoformat()
        if self.root in _latches:state.update(status='manual_review',manual_review_required=True,reason=_latches[self.root])
        state['legacy_evidence']=legacy
        state['stored_status']=state['status']
        if state['status']=='cooldown' and now>=utc(state['retry_at']):state['status']='recovery_eligible'
        state['recovery_requires_authorization']=state['status']!='normal'
        state['network_allowed']=state['status']=='normal'
        state['review_overdue']=bool(state.get('retry_at') and now>=utc(state['retry_at']) and state['status']!='normal')
        return state

    @contextmanager
    def _lock(self,name='.state.lock'):
        self.root.mkdir(parents=True,exist_ok=True)
        with (self.root/name).open('a') as stream:
            until=time.monotonic()+1
            while True:
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=until:raise Blocked('shared_safety_busy: defer')
                    time.sleep(.01)
            yield stream.fileno()

    def _commit(self,state,kind,at,*,alertable=True):
        state=dict(state)
        if state.get('legacy_evidence'):state['legacy_sources']=state['legacy_evidence']
        state.pop('legacy_evidence',None)
        if state['status']=='recovery_eligible':state['status']='cooldown'
        for k in ('network_allowed','stored_status','recovery_requires_authorization','review_overdue','legacy_pending'):state.pop(k,None)
        state['sequence']=self._latest()['sequence']+1
        event=dict(version=1,event_id=uuid.uuid4().hex,event=kind,at=utc(at).isoformat(),alertable=alertable,state_after=state)
        raw=json.dumps(event,sort_keys=True,allow_nan=False).encode()
        if len(raw)>LIMIT:raise Blocked('shared_safety_invalid: event too large')
        directory=self.root/'events';directory.mkdir(exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.pending-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            target=directory/f'{state["sequence"]:012d}-{event["event_id"]}.json'
            os.link(name,target)
            dirfd=os.open(directory,os.O_RDONLY)
            try:os.fsync(dirfd)
            finally:os.close(dirfd)
        finally:
            if os.path.exists(name):os.unlink(name)
        return state

    def ensure(self,operation='request',now=None):
        state=self.status(now)
        token=_recovery.get()
        if token and token['root']==self.root and state['stored_status']=='recovery_in_progress' and state.get('recovery_token')==token['id']:
            return state
        if not state['network_allowed']:raise Blocked('shared_youtube_'+state['status']+': '+str(state.get('reason','review required')))
        return state

    def begin(self,operation='request',now=None):
        self.ensure(operation,now)
        token=_recovery.get()
        if token and token['root']==self.root:
            if token['remaining']<=0:raise Blocked('recovery_operation_budget_exhausted')
            token['remaining']-=1
        return uuid.uuid4().hex

    def observe(self,text,**fields):
        if classify(text) is None:return None
        try:return self._observe(text,**fields)
        except BaseException:
            _latches[self.root]='shared_safety_persistence_failure'
            raise

    def _observe(self,text,*,component='youtube',operation='request',now=None,observation_id=None):
        category=classify(text)
        if category is None:return None
        for label in (component,operation):
            if not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',label):raise ValueError('Safety labels must be non-sensitive identifiers')
        now=utc(now)
        with self._lock():
            state=self.status(now)
            if observation_id and (not isinstance(observation_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',observation_id)):raise ValueError('Invalid safety observation identifier')
            if observation_id and state['stored_status']!='normal' and observation_id in state.get('recent_observation_ids',[]):return state
            recovering=state['stored_status']=='recovery_in_progress'
            repeated=recovering and category in ('authentication_challenge','access_restriction')
            fresh=bool(state['stored_status']=='normal' or state.get('legacy_pending'))
            state.update(incident_id=uuid.uuid4().hex if fresh else state['incident_id'],
                incident_count=state.get('incident_count',0)+int(fresh),
                blocking_observations=state.get('blocking_observations',0)+1,
                related_blocking_observations=1 if fresh else state.get('related_blocking_observations',state.get('blocking_observations',0))+1,
                first_observed_at=now.isoformat() if fresh else state.get('first_observed_at',now.isoformat()),
                last_observed_at=now.isoformat(),component=component,operation=operation,reason=category,bot_cooldown_hours=self.bot_hours,
                evidence=evidence(text),last_observation_id=observation_id,
                manual_review_required=bool(repeated or state.get('manual_review_required')))
            if observation_id:state['recent_observation_ids']=(([observation_id]) if fresh else (state.get('recent_observation_ids',[])+[observation_id])[-128:])
            retry=retry_time(text,now,category,state['related_blocking_observations'],self.bot_hours)
            if state.get('retry_at'):retry=max(retry,utc(state['retry_at']))
            state.update(retry_at=retry.isoformat(),status='manual_review' if state['manual_review_required'] else 'cooldown')
            return self._commit(state,'repeated_block_manual_review' if repeated else 'recovery_rate_limited' if recovering else 'incident_opened' if fresh else 'blocking_observed',now,alertable=fresh or recovering)

    def import_legacy(self,now=None):
        with self._lock():
            state=self.status(now)
            if not state.get('legacy_pending'):return state
            state.update(component='legacy',operation='import',blocking_observations=1)
            return self._commit(state,'legacy_restriction_imported',now)

    def review_event(self,now=None):
        """Explicit operator/event-adapter tick; planning never writes transitions."""
        with self._lock():
            state=self.status(now)
            if not state['review_overdue'] or state.get('review_event_incident')==state['incident_id']:return state
            state['review_event_incident']=state['incident_id']
            return self._commit(state,'unresolved_review_due',now)

    @contextmanager
    def recovery(self,*,authorized=False,reviewed=False,now=None):
        if not authorized:raise Blocked('recovery_requires_explicit_authorization')
        at=utc(now)
        with self._lock('.recovery.lock') as fd:
            with self._lock():
                state=self.status(at)
                if state['status']=='normal':raise Blocked('no_recovery_needed')
                if state.get('retry_at') and at<utc(state['retry_at']):raise Blocked('cooldown_still_active')
                if state['manual_review_required'] and not reviewed:raise Blocked('recovery_requires_manual_review_acknowledgment')
                token=dict(id=uuid.uuid4().hex,root=self.root,remaining=1,fd=fd,legacy_hashes={l['sha256'] for l in self.legacy()})
                state.update(status='recovery_in_progress',recovery_token=token['id'],component='recovery',operation='metadata_probe')
                self._commit(state,'recovery_authorized',at)
            context=_recovery.set(token)
            try:yield token
            finally:
                _recovery.reset(context)
                with self._lock():
                    state=self.status(now)
                    if state['stored_status']=='recovery_in_progress' and state.get('recovery_token')==token['id']:
                        state.update(status='cooldown',retry_at=max(utc(now)+timedelta(hours=1),utc(state['retry_at'])).isoformat())
                        self._commit(state,'recovery_incomplete',now)

    def recovered(self,token,now=None):
        with self._lock():
            state=self.status(now)
            if state['stored_status']!='recovery_in_progress' or state.get('recovery_token')!=token['id']:raise Blocked('recovery_not_admitted')
            legacy=self.legacy()
            if {l['sha256'] for l in legacy}-token['legacy_hashes'] or (state.get('retry_at') and utc(now)<utc(state['retry_at'])):
                raise Blocked('new_restriction_during_recovery; further authorization required')
            state.update(status='normal',manual_review_required=False,last_recovery_success=utc(now).isoformat(),
                         legacy_ack=[l['sha256'] for l in self.legacy()],retry_at=None,reason=None,recovery_token=None)
            return self._commit(state,'controlled_recovery_succeeded',now)


def apply_plan(result,state,now):
    """Expose policy-due work separately from network admission; no writes."""
    shared=Safety(state).status(now);result['youtube_safety']=shared
    if not shared['network_allowed']:
        result['deferred_due_ids']=result['due_ids'][:]
        for item in result.get('items',[]):
            if item.get('due'):
                item.update(policy_due=True,due=False,reason='shared_youtube_'+shared['status'])
                if 'status' in item:item['status']='deferred'
        result['due_ids']=[]
    return result
