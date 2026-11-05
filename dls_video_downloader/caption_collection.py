"""Bounded original-English caption retrieval; network only on explicit execution."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse, parse_qs

from . import youtube_safety as safety
from . import comment_collection as network
from . import comment_runner
from .captions import CaptionStore, DEFAULT_ROOT, FORMATS, MAX_BYTES, read_history, caption_lock, digest, encoded, validate
from .comments import VIDEO_ID
from .comment_planning import instant

MAX_TRACKS = 4


def identify(data):
    """Identify source tracks using URL language/kind/translation evidence.

    URLs are transient execution inputs only. Safe availability evidence includes
    non-English/translation listings without scheduling their payloads.
    """
    groups={};availability=[];ambiguities=[]
    for field,kind in (('subtitles','manual'),('automatic_captions','automatic')):
        tracks=data.get(field,{})
        if tracks is None:raise ValueError('Caption listing omitted track data')
        if not isinstance(tracks,dict):raise ValueError('Malformed caption listing')
        for alias,representations in tracks.items():
            if alias=='live_chat':continue
            if not isinstance(representations,list):raise ValueError('Malformed caption formats')
            for f in representations:
                if not isinstance(f,dict):raise ValueError('Malformed caption representation')
                url=f.get('url','')
                if not isinstance(url,str):raise ValueError('Malformed caption URL')
                parsed=urlparse(url);q=parse_qs(parsed.query)
                language=q.get('lang',[None])[-1]; translated=bool(q.get('tlang'))
                fmt=f.get('ext'); evidence=dict(alias=alias,kind=kind,language=language,translated=translated,format=fmt,
                                              source_kind=q.get('kind',[None])[-1],target_language=q.get('tlang',[None])[-1])
                availability.append(evidence)
                if translated or (language and not re.fullmatch(r'en(?:-[A-Za-z0-9]+)*',language)):continue
                if language is None:
                    if alias.startswith('en'):ambiguities.append('English-labelled track lacks source-language evidence')
                    continue
                valid_kind=(q.get('kind')==['asr']) if kind=='automatic' else not q.get('kind')
                if not valid_kind:
                    ambiguities.append('English track type conflicts with source kind');continue
                if parsed.scheme!='https' or parsed.hostname not in ('www.youtube.com','youtube.com') or parsed.path!='/api/timedtext':
                    ambiguities.append('Unexpected original caption endpoint');continue
                identity=dict(kind=kind,language=language,name=q.get('name',[''])[-1],source_id=q.get('vssId',[None])[-1])
                key=digest(encoded(identity))
                track=groups.setdefault(key,dict(track_id=key,identity=identity,aliases=[],formats={},evidence=dict(original=True,translation=False,source_language=language,source_kind=evidence['source_kind'])))
                if alias not in track['aliases']:track['aliases'].append(alias)
                if fmt not in FORMATS:continue
                if q.get('fmt')!=[fmt]:
                    ambiguities.append('Requested caption format conflicts with source format');continue
                if fmt in track['formats'] and track['formats'][fmt]!=url:
                    ambiguities.append('Conflicting original English URLs share a source identity')
                track['formats'].setdefault(fmt,url)
    # Bound pathological listings without falsely claiming full coverage.
    if len(groups)>MAX_TRACKS:ambiguities.append('Original English track count exceeds safety bound')
    return sorted(groups.values(),key=lambda t:t['track_id'])[:MAX_TRACKS],availability,sorted(set(ambiguities))


def listing_command(video):
    return [network.YT_DLP,'--ignore-config','--no-plugin-dirs','--no-cache-dir','--no-playlist',
        '--no-js-runtimes','--js-runtimes','node:'+network.NODE,'--ffmpeg-location',network.FFMPEG,
        '--skip-download','--list-subs','--dump-single-json','--no-write-subs','--no-write-auto-subs',
        '--no-write-comments','--no-write-thumbnail','--no-write-info-json','--no-write-description',
        '--no-write-playlist-metafiles','--socket-timeout','20','--retries','0','--extractor-retries','0',
        '--fragment-retries','0','--verbose',f'https://www.youtube.com/watch?v={video}']


# A killable child preserves HTTP body bytes (no conversion/decompression). No
# redirect/authentication/bypass or retries. Parent enforces an absolute deadline
# and pipe limits using the existing subprocess cleanup implementation.
HTTP_WORKER = '''import sys,urllib.request,urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
try:
 req=urllib.request.Request(sys.argv[1],headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'identity'})
 with urllib.request.build_opener(NoRedirect).open(req,timeout=20) as response:
  print('Content-Type: '+response.headers.get('Content-Type',''),file=sys.stderr)
  if response.headers.get('Content-Encoding','identity') not in ('identity',''):raise ValueError('Unexpected content encoding')
  total=0
  while True:
   data=response.read(65536)
   if not data:break
   total+=len(data)
   if total>16777216:raise ValueError('Caption payload exceeds size bound')
   sys.stdout.buffer.write(data)
except urllib.error.HTTPError as exc:
 print('HTTP Error '+str(exc.code),file=sys.stderr)
 if exc.headers.get('Retry-After'):print('Retry-After: '+exc.headers['Retry-After'],file=sys.stderr)
 sys.exit(1)
except Exception as exc:
 print(type(exc).__name__,file=sys.stderr);sys.exit(1)
'''


def fetch(url,timeout,lock_fd):
    return network.run_extractor([sys.executable,'-B','-c',HTTP_WORKER,url],timeout,lock_fd)


@safety.scoped(lambda a:Path(a['root']).resolve().parent,'captions')
def collect(video_id, *, root=DEFAULT_ROOT, timeout=180, purpose='manual', completion_evidence=None,
            before_collect=None, clock=None, monotonic=None):
    if not isinstance(video_id,str) or not VIDEO_ID.fullmatch(video_id):raise ValueError('Expected an 11-character YouTube ID')
    if type(timeout) is not int or not 30<=timeout<=300:raise ValueError('Caption timeout must be 30..300')
    if purpose not in ('manual','initial','refresh','confirmation_24h','confirmation_7d'):raise ValueError('Unsupported caption purpose')
    clock=clock or (lambda:datetime.now(timezone.utc));monotonic=monotonic or time.monotonic
    end=monotonic()+timeout
    root=Path(root).resolve()
    # Validate root before acquiring a lock/creating operational data.
    from .comment_collection import PROJECT
    if root==PROJECT/'state' or any(root.is_relative_to(PROJECT/'state'/p) for p in ('acquisitions','comments')):
        raise ValueError('Caption store must be independent')
    safety.current().ensure('caption_collection')
    with caption_lock(root) as fd:
        until=comment_runner.read_cooldown(root/'runner')
        if until and until>instant(clock()):raise comment_runner.Deferred('collector_cooldown')
        if before_collect:before_collect()
        safety.current().ensure('caption_collection')
        with CaptionStore(root) as store:
            prior=store.latest(video_id);payloads={}
            history=read_history(root,video_id)
            last=history[-1] if history else {}
            recovered={(r['track_id'],r['format']) for r in last.get('formats',[]) if r.get('sha256')} if last.get('technical_failure') and last.get('purpose')==purpose else set()
            record=dict(video_id=video_id,started_at=instant(clock()).isoformat(),purpose=purpose,
                completion_evidence=completion_evidence,formats=[],tracks=[],availability=[],ambiguities=[],warnings=[],
                complete=False,technical_failure=False,listing_success=False,outcome='failed',error=None,
                tool=dict(name='yt-dlp',executable=network.YT_DLP,node_executable=network.NODE,version=None),
                coverage=dict(scope='extractor-advertised original English tracks',finality='not_certified',accuracy='not_assessed'),
                configuration=dict(formats=list(FORMATS),max_tracks=MAX_TRACKS,max_bytes=MAX_BYTES,
                                   timeout_seconds=timeout,retries=0,socket_timeout=20))
            def budget():
                safety.current().ensure('caption_request')
                permitted=before_collect() if before_collect else None
                remaining=math.floor(end-monotonic())
                if type(permitted) is int:remaining=min(remaining,permitted)
                if remaining<5:raise TimeoutError('Insufficient remaining caption runtime')
                return min(60,remaining)
            def diagnostic(execution):
                message=network.safe_diagnostic(execution.stderr.decode('utf-8',errors='replace'))
                record['warnings'].extend(line for line in message.splitlines() if 'WARNING:' in line)
                if re.search(r'HTTP Error 401\b',message):message='Access denied: '+message
                if comment_runner.blocking_evidence(dict(error=message),instant(clock())):raise ValueError(message)
                if execution.termination:raise ValueError(execution.termination)
                if execution.returncode:raise ValueError(message or 'Caption subprocess failed')
                return message
            try:
                execution=network.run_extractor(listing_command(video_id),budget(),fd)
                # Retain tool identity even when diagnostic() rejects a blocked
                # or nonzero extraction before a metadata object is available.
                tool_diagnostic=network.safe_diagnostic(execution.stderr.decode('utf-8',errors='replace'))
                version=re.search(r'\[debug\] yt-dlp version ([^\n]+)',tool_diagnostic)
                record['tool']['version']=version.group(1) if version else None
                message=diagnostic(execution)
                # --list-subs can print tables before the single JSON line.
                lines=execution.stdout.decode('utf-8').splitlines()
                data=json.loads(next(line for line in reversed(lines) if line.startswith('{')))
                if not isinstance(data,dict):raise ValueError('Malformed video metadata object')
                if not any(field in data for field in ('subtitles','automatic_captions')):raise ValueError('Extraction omitted caption listing fields')
                if data.get('id')!=video_id:raise ValueError('Listing identifies a different video')
                record['video_status']=dict(live_status=data.get('live_status'),was_live=data.get('was_live'))
                if data.get('live_status') not in ('was_live','not_live'):raise ValueError('Fresh metadata does not establish completed video')
                tracks,availability,ambiguities=identify(data)
                record.update(listing_success=True,availability=availability,ambiguities=ambiguities,
                              tracks=[{k:v for k,v in t.items() if k!='formats'} for t in tracks])
                for track in tracks:
                    for fmt in FORMATS:
                        key=(track['track_id'],fmt);previous=prior.get(key)
                        row=dict(track_id=key[0],format=fmt,status='not_advertised',advertised=fmt in track['formats'])
                        record['formats'].append(row)
                        if previous and (key in recovered or purpose not in ('manual','confirmation_24h','confirmation_7d')):
                            row.update({k:previous[k] for k in ('sha256','size','path')});row.update(status='reused',retrieved_at=previous.get('retrieved_at'),reused_from_attempt=previous.get('source_attempt_id'))
                            continue
                        if fmt not in track['formats']:continue
                        response=None;stage='retrieval'
                        try:
                            response=fetch(track['formats'][fmt],budget(),fd)
                            info=diagnostic(response)
                            stage='validation'
                            validate(response.stdout,fmt)
                            raw=response.stdout;payloads[key]=raw
                            row['status']='unchanged' if previous and digest(raw)==previous['sha256'] else 'revised' if previous else 'preserved'
                            row['retrieved_at']=instant(clock()).isoformat()
                            content_type=re.search(r'(?im)^Content-Type:\s*([^\r\n]+)',info)
                            row['content_type']=content_type.group(1) if content_type else None
                        except (OSError,ValueError,TimeoutError,UnicodeError,comment_runner.Deferred) as exc:
                            row.update(status='deferred' if isinstance(exc,comment_runner.Deferred) else 'validation_failed' if stage=='validation' else 'retrieval_failed',error=network.safe_diagnostic(exc))
                            record['technical_failure']=True
                            blocked=comment_runner.blocking_evidence(dict(error=row['error']),instant(clock()))
                            if blocked:record['blocking']=blocked;raise ValueError(row['error'])
                            if 'interrupted' in row['error'] or 'timeout' in row['error'] or isinstance(exc,(TimeoutError,comment_runner.Deferred)):raise ValueError(row['error'])
                # Required formats may be recovered from verified earlier bytes.
                preserved=lambda r:r.get('sha256') is not None or (r['track_id'],r['format']) in payloads
                record['complete']=bool(tracks) and not ambiguities and all(preserved(r) for r in record['formats']) and len(record['formats'])==len(tracks)*len(FORMATS)
                record['outcome']=('revised' if any(r['status']=='revised' for r in record['formats']) else 'unchanged' if all(r['status'] in ('unchanged','reused') for r in record['formats']) else 'complete') if record['complete'] else 'partial' if any(preserved(r) for r in record['formats']) else 'ambiguous' if ambiguities else 'missing' if not tracks else 'failed' if record['technical_failure'] else 'missing_formats'
            except safety.Blocked:
                if not record['listing_success']:raise
                record.update(technical_failure=True,error='shared_youtube_block',outcome='partial' if payloads else 'failed')
            except (OSError,ValueError,TimeoutError,StopIteration,UnicodeError,comment_runner.Deferred) as exc:
                record.update(technical_failure=True,error=network.safe_diagnostic(exc),outcome='partial' if payloads else 'failed')
            except KeyboardInterrupt:
                record.update(technical_failure=True,error='interrupted',outcome='partial' if payloads else 'failed')
            if record['error'] or record['technical_failure']:
                error=record['error'] or '\n'.join(r.get('error','') for r in record['formats'])
                record['blocking']=comment_runner.blocking_evidence(dict(error=error),instant(clock()))
            if record.get('blocking') and safety.current().status(clock())['network_allowed']:
                safety.current().observe(record['error'] or '\n'.join(r.get('error','') for r in record['formats']),component='captions',operation='collection',now=clock())
            record['finished_at']=instant(clock()).isoformat()
            saved=store.record(record,payloads)
            if saved.get('blocking'):
                directory=root/'runner';directory.mkdir(parents=True,exist_ok=True)
                comment_runner.atomic_json(directory/'cooldown.json',saved['blocking'])
            saved['exit_code']=130 if record['error']=='interrupted' or any(r.get('error')=='interrupted' for r in record['formats']) else 2 if record['technical_failure'] or record['ambiguities'] else 0
            return saved
