"""Bounded channel discovery and explicit bounded acquisition orchestration."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from . import youtube_safety as safety
from . import acquisition as media
from . import forward_metadata as forward
from .classification import CHANNEL, DEFAULT_START
from .inventory import FIELDS, encoded, import_document, now, report
from .operations import (LOG_LIMIT, cleanup_staging, forward_allowed, retry_pending,
                         retry_failure, retry_success, run_record, safe_text)
from .operations import OperationBusy, record_overlap

RECOVERY_PAGE_SIZE = 200
RECOVERY_TIMEOUT_SECONDS = 120


def _read_recovery(path):
    try:
        raw=Path(path).read_bytes()
    except FileNotFoundError:
        return {'version':1,'tabs':{}}
    if len(raw)>65536:
        raise ValueError('Oversized forward recovery state; manual review required')
    data=json.loads(raw)
    if data.get('version')!=1 or not isinstance(data.get('tabs'),dict):
        raise ValueError('Invalid forward recovery state; manual review required')
    for tab,item in data['tabs'].items():
        if tab not in ('live','videos') or not isinstance(item,dict) or type(item.get('next_start')) is not int or item['next_start']<1:
            raise ValueError('Invalid per-tab recovery cursor; manual review required')
    return data


def _write_recovery(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if not data.get('tabs'):
        path.unlink(missing_ok=True)
        return
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2).encode()
    if len(raw)>65536:raise ValueError('Forward recovery state exceeds bound')
    fd,name=tempfile.mkstemp(prefix='.discovery-recovery-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def compact(document, flat=False):
    def entry(value):
        result = {k: value[k] for k in (*FIELDS, "id", "_type") if value.get(k) is not None}
        return result
    result = entry(document)
    if document.get("epoch") is not None:
        result["epoch"] = document["epoch"]
    if flat:
        result["entries"] = [entry(e) for e in document["entries"]]
    return result


def metadata_command():
    """Shared metadata-only extraction flags; no configuration or media writes."""
    return [media.YT_DLP, "--ignore-config", "--no-cache-dir", "--skip-download",
              "--ffmpeg-location", media.FFMPEG,
              "--js-runtimes", "node:" + media.NODE, "--no-write-comments", "--no-write-subs",
              "--no-write-auto-subs", "--no-write-thumbnail", "--no-write-info-json",
              "--socket-timeout", "30", "--retries", "0", "--extractor-retries", "0", "--dump-single-json"]


def channel_entries(document, limit):
    """Validate the official channel and bounded listing before inventory import."""
    entries = document.get("entries")
    if document.get("channel_id") != CHANNEL or not isinstance(entries, list) or len(entries) > limit:
        raise ValueError("Unexpected channel identity or unbounded/missing playlist entries")
    return entries


def enrichment_candidates(db, positions, items, verified, start, *, honor_retries=True):
    """Existing bounded discovery priority, shared with morning metadata scans."""
    enrich, deferred = [], []
    for video_id, position in positions.items():
        item = items[video_id]
        live = db.execute("SELECT 1 FROM sources WHERE video_id=? AND source='live'", (video_id,)).fetchone()
        if item['publication_date'] and item['publication_date'] < start:
            continue
        pending = retry_pending(db, video_id, 'enrichment') if honor_retries else None
        if pending:
            deferred.append(pending)
            continue
        if video_id not in verified and item["decision"] not in ("reject", "skip") and (
                item["decision"] == "approve" or item["classification"] in ("daily_show", "the_club")
                or (live and item["classification"] == "review")):
            enrich.append((position, video_id))
    enrich.sort()
    first_daily = next((entry for entry in enrich if items[entry[1]]['classification'] == 'daily_show'), None)
    first_club = next((entry for entry in enrich if items[entry[1]]['classification'] == 'the_club'), None)
    preferred = [entry for entry in (first_daily, first_club) if entry]
    return preferred + [entry for entry in enrich if entry not in preferred], deferred


@safety.scoped(lambda a:a['state'] or safety.state_from_db(a['db']),'video-runner')
def workflow(db, *, acquire_media=False, per_tab=None, streams_limit=25, videos_limit=75,
             max_enrich=8, max_downloads=2, start=DEFAULT_START, only_id=None, state=None,
             stale_hours=72, retry_base_minutes=60, retry_max_hours=24, invocation_source='manual'):
    if per_tab is not None:
        streams_limit = videos_limit = per_tab
    if (not 1 <= streams_limit <= 200 or not 1 <= videos_limit <= 200
            or not 0 <= max_enrich <= 20 or not 1 <= max_downloads <= 3
            or stale_hours < 24 or not 1 <= retry_base_minutes <= 1440
            or not 1 <= retry_max_hours <= 168):
        raise ValueError("Invalid limits: tabs 1..200, enrichment 0..20, downloads 1..3, retention >=24h")
    if only_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", only_id):
        raise ValueError("Invalid supervised video ID")
    if invocation_source not in ('scheduled','manual','other','unknown'):
        raise ValueError('Invalid invocation source')
    state = (state or media.PROJECT / "state").resolve()
    if not state.is_relative_to(media.PROJECT):
        raise ValueError("Workflow state must remain inside the project")
    safety.current().ensure('video_run')
    try:
        with media.operation_lock(state / "acquisitions"):
            safety.current().ensure('video_run')
            return _workflow_locked(db, acquire_media, streams_limit, videos_limit, max_enrich,
                                    max_downloads, start, only_id, state, stale_hours,
                                    retry_base_minutes, retry_max_hours, invocation_source)
    except OperationBusy:
        record_overlap(db, 'overlap-' + uuid.uuid4().hex)
        raise


def _workflow_locked(db, acquire_media, streams_limit, videos_limit, max_enrich,
                     max_downloads, start, only_id, state, stale_hours,
                     retry_base_minutes, retry_max_hours, invocation_source):
    run_id = now().replace(":", "") + "-" + uuid.uuid4().hex[:8]
    directory = state / "runs" / run_id
    directory.mkdir(parents=True)
    events = directory / "events.jsonl"
    result = {"run_id": run_id, "mode": "discovery+acquisition" if acquire_media else "discovery-only",
              'trigger':invocation_source,
              "start_date": start, "limits": {"streams": streams_limit, "videos": videos_limit, "max_enrich": max_enrich, "max_downloads": max_downloads},
              "encountered": {}, "new_ids": [], "enriched": [], "failures": [], "acquired": [],
              "deferred": [], "already_verified": [], "warnings": [], "log": str(events), "retry_deferred": [], "coverage": {}}

    def event(kind, **values):
        with events.open("a") as stream:
            if stream.tell() < LOG_LIMIT:
                stream.write(encoded({"at": now(), "event": kind, **values}) + "\n")
            stream.flush()

    def failure(operation, exc, video_id=None):
        item = {"operation": operation, "id": video_id,
                "error": safe_text(exc)[:1000]}
        result["failures"].append(item)
        event("failure", **item)

    with run_record(db, state, result):
        common = metadata_command()
        result['cleanup'] = cleanup_staging(db, state, execute=True, retention_hours=stale_hours)
        event('cleanup', files=len(result['cleanup']['files']), bytes=result['cleanup']['bytes'])
        before = {r[0] for r in db.execute("SELECT id FROM videos")}
        positions = {}
        event("start", mode=result["mode"], limits=result["limits"], only_id=only_id)
        recovery_path=state/'discovery-recovery.json'
        recovery=_read_recovery(recovery_path)
        recovery_changed=False
        for source, tab, limit in (("live", "streams", streams_limit), ("videos", "videos", videos_limit)):
            result['coverage'][source]={'requested_limit':limit,'listing_attempted':True,
                'listing_status':'attempted','listing_complete':False,'forward_window_complete':False,
                'prior_head_seen':False,'recovery_pending':False,'complete':False}
            try:
                safety.current().ensure('metadata')
                document = json.loads(media.run(common + ["--flat-playlist", "--playlist-end", str(limit),
                    f"https://www.youtube.com/channel/{CHANNEL}/{tab}"], directory / f"{source}.stderr.log", 300))
                entries = channel_entries(document, limit)
                with db:
                    imported = import_document(db, compact(document, flat=True), source, f"channel:{CHANNEL}/{tab}")
                result["encountered"][source] = len(entries)
                frontier = db.execute("SELECT head_id FROM scan_frontiers WHERE source=?", (source,)).fetchone()
                ids = [e['id'] for e in entries]
                overlap = bool(frontier and frontier[0] in ids)
                tab_report={'prior_head_seen':overlap,'initial_scan':not bool(frontier),
                            'head_entries':len(entries),'requested_limit':limit,
                    'listing_attempted':True,'listing_status':'succeeded',
                    'listing_complete':len(entries)<limit,
                            'cap_reached':len(entries)==limit,'recovery_page_size':0,
                            'recovery_page_start':None,'frontier_reached':overlap if frontier else None,
                            'recovery_pending':False,'complete':False}
                if frontier and not overlap:
                    cursor=recovery['tabs'].get(source)
                    if cursor and cursor.get('terminal_missing'):
                        tab_report.update(recovery_pending=True,recovery_state='frontier_unavailable_manual_review')
                    else:
                        page_start=cursor['next_start'] if cursor else limit+1
                        page_end=page_start+RECOVERY_PAGE_SIZE-1
                        tab_report.update(recovery_page_start=page_start,recovery_page_size=RECOVERY_PAGE_SIZE)
                        tab_report['recovery_listing_attempted']=True
                        try:
                            safety.current().ensure('metadata')
                            page_doc=json.loads(media.run(common+["--flat-playlist","--playlist-start",str(page_start),
                                "--playlist-end",str(page_end),f"https://www.youtube.com/channel/{CHANNEL}/{tab}"],
                                directory/f"{source}.recovery-{page_start}.stderr.log",RECOVERY_TIMEOUT_SECONDS))
                            page=channel_entries(page_doc,RECOVERY_PAGE_SIZE)
                            with db:
                                page_import=import_document(db,compact(page_doc,flat=True),source,f"channel:{CHANNEL}/{tab}")
                            result['recovery_entries'] = result.get('recovery_entries',{})
                            result['recovery_entries'][source]=len(page)
                            page_ids=[e['id'] for e in page]
                            result['encountered'][source]+=len(page)
                            tab_report['recovery_page_cap_hit']=len(page)==RECOVERY_PAGE_SIZE
                            tab_report['recovery_page_entries']=len(page)
                            event('recovery_page',source=source,start=page_start,entries=len(page),
                                  cap_hit=len(page)==RECOVERY_PAGE_SIZE,frontier_reached=frontier[0] in page_ids)
                            tab_report['new_observations']=imported['new_observations']+page_import['new_observations']
                            if frontier[0] in page_ids:
                                boundary=page_ids.index(frontier[0])
                                for offset,entry in enumerate(page[:boundary],start=page_start-1):
                                    positions[entry['id']]=min(offset,positions.get(entry['id'],offset))
                                tab_report.update(frontier_reached=True,recovery_pending=False,recovery_state='reconciled')
                                recovery['tabs'].pop(source,None);recovery_changed=True
                                with db:
                                    db.execute("INSERT INTO scan_frontiers VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET head_id=excluded.head_id,scanned_at=excluded.scanned_at",(source,ids[0],now()))
                            elif len(page)<RECOVERY_PAGE_SIZE:
                                tab_report.update(frontier_reached=False,recovery_pending=True,recovery_state='frontier_unavailable_manual_review')
                                recovery['tabs'][source]={'frontier_id':frontier[0],'next_start':page_start,
                                    'started_at':(cursor or {}).get('started_at',now()),'terminal_missing':True}
                                recovery_changed=True
                                failure('coverage',ValueError(f'{source}: previous frontier was not present in the bounded recovery listing'))
                            else:
                                for offset,entry in enumerate(page,start=page_start-1):
                                    positions[entry['id']]=min(offset,positions.get(entry['id'],offset))
                                recovery['tabs'][source]={'frontier_id':frontier[0],'next_start':page_end+1,
                                    'started_at':(cursor or {}).get('started_at',now())}
                                tab_report.update(frontier_reached=False,recovery_pending=True,recovery_state='page_deferred')
                                recovery_changed=True
                                failure('coverage',ValueError(f'{source}: recovery page cap reached before previous frontier'))
                        except safety.Blocked:
                            raise
                        except (ValueError,OSError) as exc:
                            failure('coverage_recovery',exc)
                            tab_report.update(recovery_pending=True,recovery_state='page_failed')
                    result['warnings'].append(f'{source}: previous frontier not in shallow head; bounded recovery remains incomplete')
                elif not frontier and len(entries) == limit:
                    result['warnings'].append(f'{source}: initial bounded baseline reached {limit} entries')
                if ids and (not frontier or overlap or tab_report.get('frontier_reached')):
                    with db:
                        db.execute("INSERT INTO scan_frontiers VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET head_id=excluded.head_id,scanned_at=excluded.scanned_at", (source, ids[0], now()))
                for position, item in enumerate(entries):
                    positions[item["id"]] = min(position, positions.get(item["id"], limit))
                tab_report['complete']=(not tab_report['recovery_pending'] and
                    (not frontier or tab_report['frontier_reached']) and
                    (not tab_report['cap_reached'] or bool(frontier and tab_report['frontier_reached'])))
                tab_report['forward_window_complete']=tab_report['complete']
                result['coverage'][source]=tab_report
                event("tab_imported", source=source, entries=len(entries), observations=imported["new_observations"])
            except safety.Blocked as exc:
                result['coverage'][source]['listing_status']='blocked'
                result.update(status='deferred',shared_block=str(exc))
                result['deferred'].append({'operation':'youtube','reason':str(exc)})
                return result
            except (ValueError, OSError) as exc:
                result['coverage'][source].update(listing_status='failed',complete=False,forward_window_complete=False)
                failure(source, exc)
        requested_tabs=[v for v in result['coverage'].values() if v.get('requested_limit',0)]
        result['coverage_summary']={'complete':bool(requested_tabs) and all(v.get('complete') for v in requested_tabs),
            'streams':result['coverage'].get('live',{}),'videos':result['coverage'].get('videos',{}),
            'recovery_pending_tabs':[k for k,v in result['coverage'].items() if v.get('recovery_pending')],
            'fixed_forward_start':start,
            'basis':'requested forward heads reconciled to prior tab frontiers; not exhaustive channel inventory'}
        if not result['coverage_summary']['complete']:
            result['exit_code']=2
        items = {i["id"]: i for i in report(db, start)["items"]}
        result["new_ids"] = sorted(set(positions) - before)
        verified = {r[0] for r in db.execute("SELECT video_id FROM acquisitions")}
        enrich, deferred = enrichment_candidates(db, positions, items, verified, start)
        metadata_retry=forward.read_state(state)
        prior_metadata_retry=json.dumps(metadata_retry,sort_keys=True)
        work=forward.enrichment_work(db,positions,now=datetime.now(timezone.utc),start=start,
                                     retained_pending=metadata_retry['deferred_initial'])
        for video_id in work['initial_pending']:
            metadata_retry['deferred_initial'].setdefault(video_id,datetime.now(timezone.utc).isoformat())
        initial_queue=[];followup_queue=[]
        for video_id in work['initial_pending']:
            if video_id not in items or video_id in verified or items[video_id]['decision'] in ('reject','skip'):
                continue
            pending_retry=retry_pending(db,video_id,'enrichment')
            if pending_retry:
                result['retry_deferred'].append(pending_retry)
                continue
            initial_queue.append((positions.get(video_id, streams_limit+videos_limit),video_id))
        resolved_ids=set(work['resolved'])
        enrich=[entry for entry in enrich if entry[1] not in resolved_ids]
        for video_id in work['followup_required']:
            if video_id not in items or video_id in verified or items[video_id]['decision'] in ('reject','skip'):
                continue
            pending_retry=retry_pending(db,video_id,'enrichment')
            if pending_retry:
                result['retry_deferred'].append(pending_retry)
                continue
            followup_queue.append((positions.get(video_id, streams_limit+videos_limit),video_id))
        # A known unresolved daily show keeps its established early status-check
        # priority. Remaining new/deferred first enrichments are interleaved by
        # tab, followed by other specific follow-up checks.
        active_followups=set()
        for _,video_id in followup_queue:
            row=db.execute('SELECT metadata FROM videos WHERE id=?',(video_id,)).fetchone()
            if row and json.loads(row['metadata']).get('live_status') in ('is_upcoming','is_live','post_live'):
                active_followups.add(video_id)
        urgent_active=[entry for entry in followup_queue if entry[1] in active_followups]
        other_followups=[entry for entry in followup_queue if entry not in urgent_active]
        enrich=list(dict.fromkeys(urgent_active+enrich+initial_queue+other_followups)); result['enrichment_work']=work
        result['enrichment_status']={'initial_pending':len(work['initial_pending']),'followup_required':len(work['followup_required']),'resolved':len(work['resolved']),'budget':max_enrich}
        result['retry_deferred'].extend(deferred)
        if json.dumps(metadata_retry,sort_keys=True)!=prior_metadata_retry:
            forward.save_state(state,metadata_retry)
        # Pending initial-enrichment IDs are now durable before the catch-up
        # cursor advances. If interrupted before this point, the same page is
        # safely replayed; afterward deferred IDs survive head movement.
        if recovery_changed:
            _write_recovery(recovery_path,recovery)
        enriched_metadata = {}
        failed_enrichment = set()
        for _, video_id in enrich[:max_enrich]:
            try:
                safety.current().ensure('metadata')
                document = json.loads(media.run(common + ["--no-playlist", "https://www.youtube.com/watch?v=" + video_id],
                    directory / f"{video_id}.stderr.log", 180))
                if document.get("id") != video_id or document.get("channel_id") != CHANNEL or not document.get("live_status"):
                    raise ValueError("Missing full metadata or unexpected video/channel identity")
                with db:
                    imported = import_document(db, compact(document), "other", f"full:{video_id}")
                enriched_metadata[video_id] = document
                retry_success(db, video_id, 'enrichment')
                result["enriched"].append(video_id)
                event("enriched", id=video_id, observations=imported["new_observations"])
            except safety.Blocked as exc:
                result.update(status='deferred',shared_block=str(exc))
                result['deferred'].append({'operation':'youtube','reason':str(exc)})
                return result
            except (ValueError, OSError) as exc:
                failed_enrichment.add(video_id)
                retry_failure(db, video_id, 'enrichment', exc, base_minutes=retry_base_minutes, max_hours=retry_max_hours)
                failure("enrichment", exc, video_id)
        work=forward.enrichment_work(db,positions,now=datetime.now(timezone.utc),start=start,
                                     retained_pending=metadata_retry['deferred_initial'])
        for video_id in list(metadata_retry['deferred_initial']):
            if video_id not in work['initial_pending']:
                metadata_retry['deferred_initial'].pop(video_id,None)
        if json.dumps(metadata_retry,sort_keys=True)!=prior_metadata_retry:
            forward.save_state(state,metadata_retry)
        work['completed_this_run']=[v for v in result.get('enriched',[]) if v not in work['initial_pending']]
        attempted_ids=set(result.get('enriched',[]))|failed_enrichment
        work['deferred_this_run']=[v for v in work['initial_pending']
            if v not in attempted_ids and not retry_pending(db,v,'enrichment')]
        result['enrichment_work']=work
        attempted_enrichment=len(result.get('enriched',[]))+len(failed_enrichment)
        result['enrichment_status']={'initial_pending':len(work['initial_pending']),'initial_completed':len(work['initial_completed']),'followup_required':len(work['followup_required']),'resolved':len(work['resolved']),'budget_deferred':len(work['deferred_this_run']),'budget':max_enrich,'attempted':attempted_enrichment,'budget_exhausted':attempted_enrichment>=max_enrich and bool(work['initial_pending'] or work['followup_required'])}
        items = report(db, start)["items"]
        for item in items:
            if item["id"] in positions or item["eligibility"] == "eligible":
                event("classification", id=item["id"], classification=item["classification"],
                      reason=item["classification_reason"], decision=item["decision"], eligibility=item["eligibility"])
        result["classifications"] = dict(Counter(i["classification"] for i in items if i["id"] in positions))
        result["new_classifications"] = dict(Counter(i["classification"] for i in items if i["id"] in result["new_ids"]))
        result["pending_reviews"] = [i["id"] for i in items if i["id"] in positions and i["classification"] == "review" and not i["decision"]]
        result["active_reviews"] = [i["id"] for i in items if i["classification"] == "review" and not i["decision"] and i["publication_date"] and i["publication_date"] >= start]
        result["eligible"] = [i["id"] for i in items if forward_allowed(i, start)]
        attempts = 0
        for item in sorted(items, key=lambda i: (i["publication_date"] or "9999", i["id"])):
            if not forward_allowed(item, start):
                if item['eligibility'] == 'eligible':
                    result['deferred'].append({'id': item['id'], 'reason': 'scheduled policy requires verified forward-window date, even for approvals'})
                continue
            video_id = item["id"]
            if video_id in verified:
                result["already_verified"].append(video_id)
                continue
            pending = retry_pending(db, video_id, 'acquisition') or retry_pending(db, video_id, 'enrichment')
            if pending:
                result['retry_deferred'].append(pending)
                event('retry_deferred', id=video_id, next_retry=pending['next_retry'], category=pending['category'])
                continue
            fresh = enriched_metadata.get(video_id)
            if video_id in failed_enrichment or (fresh and (fresh.get("live_status") not in ("was_live", "not_live") or fresh.get("is_live"))):
                result["deferred"].append({"id": video_id, "reason": "metadata failed or broadcast not ready"})
                continue
            if not acquire_media or (only_id and video_id != only_id) or attempts >= max_downloads:
                result["deferred"].append({"id": video_id, "reason": "discovery-only, supervised restriction, or run limit"})
                continue
            attempts += 1
            try:
                outcome = media.acquire(db, video_id, start, root=state / "acquisitions", _lock_held=True,
                                        retry_base_minutes=retry_base_minutes, retry_max_hours=retry_max_hours, _forward_only=True)
                retry_success(db, video_id, 'acquisition')
                result["acquired"].append({"id": video_id, "status": outcome["status"], "sha256": outcome["sha256"]})
                event("acquired", **result["acquired"][-1])
            except safety.Blocked as exc:
                result.update(status='deferred',shared_block=str(exc))
                result['deferred'].append({'operation':'youtube','reason':str(exc)})
                return result
            except (ValueError, OSError) as exc:
                if not retry_pending(db, video_id, 'acquisition'):
                    retry_failure(db, video_id, 'acquisition', exc, base_minutes=retry_base_minutes, max_hours=retry_max_hours)
                failure("acquisition", exc, video_id)
        result["download_attempts"] = attempts
        event("finished", failures=len(result["failures"]), acquisitions=len(result["acquired"]))
        return result
