"""Read-only cohort report over existing inventory, comment and caption evidence."""
from contextlib import closing
from datetime import date, datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import statistics

from . import captions
from .inventory import connect, report as inventory_report
from .comments import IRISHWEEGEE


def _read_db(path):
    path=Path(path)
    if not path.exists():return None
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
    return db


def _local_date(value):
    if not value:return None
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None: return stamp.date().isoformat()
        from zoneinfo import ZoneInfo
        return stamp.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
    except (ValueError,TypeError):return None


def _elapsed_hours(anchor,attempt):
    if not anchor or not attempt:return None
    try:
        a=datetime.fromisoformat(anchor);b=datetime.fromisoformat(attempt)
        if a.tzinfo is None or b.tzinfo is None:return None
        return round((b-a).total_seconds()/3600,2)
    except (ValueError,TypeError):return None


def _text_evidence(root,video_id,attempt):
    digest=attempt.get('snapshot_hash')
    if not digest:return []
    path=Path(root)/video_id/'snapshots'/f'{digest}.json'
    try:
        data=json.loads(path.read_bytes())
        return [dict(comment_id=c.get('id'),text_length=len(c.get('text','')),
                     line_count=len(c.get('text','').splitlines()),
                     text_hash=hashlib.sha256(c.get('text','').encode()).hexdigest())
                for c in data.get('comments',[]) if c.get('author_id')==IRISHWEEGEE]
    except (OSError,ValueError,TypeError):return []


def build(inventory_path,comment_root,caption_root,*,start,end):
    start=date.fromisoformat(start).isoformat();end=date.fromisoformat(end).isoformat()
    if end<start:raise ValueError('end date precedes start date')
    comment_root=Path(comment_root);caption_root=Path(caption_root)
    with closing(connect(inventory_path,readonly=True,timeout=1)) as inv:
        inv.execute('BEGIN')
        base=inventory_report(inv,start,end)
        chosen=[];unknown=[]
        for item in base['items']:
            if item['decision'] in ('skip','reject'):continue
            if item['classification'] not in ('daily_show','the_club') and item['decision']!='approve':continue
            if item['publication_date'] is None:unknown.append(item['id']);continue
            if start<=item['publication_date']<=end:
                video=item.copy()
                row=inv.execute('SELECT first_seen,metadata FROM videos WHERE id=?',(item['id'],)).fetchone()
                metadata=json.loads(row['metadata'])
                daily=item['classification']=='daily_show' or metadata.get('was_live') is True or metadata.get('live_status')=='was_live'
                completed=None
                if daily:
                    for observation in inv.execute("SELECT metadata,imported_at FROM observations WHERE video_id=? AND detail='full' ORDER BY imported_at,observation_id",(item['id'],)):
                        if json.loads(observation['metadata']).get('live_status')=='was_live':completed=observation['imported_at'];break
                else:
                    completed=metadata.get('upload_date') or metadata.get('release_date')
                acquired=inv.execute('SELECT completed_at FROM acquisitions WHERE video_id=?',(item['id'],)).fetchone()
                video.update(group='daily_show' if daily and item['classification']!='the_club' else item['classification'],
                    completion_observed_at=completed,completion_evidence_kind='first_full_was_live_imported_at_proxy' if daily and completed else 'upload_or_release_date' if completed else 'unknown',
                    acquired=bool(acquired),acquired_at=acquired['completed_at'] if acquired else None)
                chosen.append(video)
    comments_db=_read_db(comment_root/'comments.sqlite3')
    try:
        caption_history=captions.read_history(caption_root)
        from .comment_planning import plan as comment_plan
        from .caption_planning import plan as caption_plan
        evaluated=datetime.now(timezone.utc)
        cp=comment_plan(inventory_path,comment_root,now=evaluated,start=start)
        kp=caption_plan(inventory_path,caption_root,now=evaluated,start=start)
        comment_policy={i['id']:i for i in cp['items']};caption_policy={i['id']:i for i in kp['items']}
        by_comment={}
        for item in chosen:
            video=item['id'];attempts=[]
            revisions=[]
            if comments_db:
                revisions=[dict(comment_id=r['comment_id'],author_id=r['author_id'],text_revisions=r['revision_count'],
                    first_observed_at=r['first_observed_at'],latest_observed_at=r['latest_observed_at'])
                    for r in comments_db.execute('''SELECT comment_id,author_id,count(DISTINCT text_hash) AS revision_count,
                        min(first_observed_at) AS first_observed_at,max(last_observed_at) AS latest_observed_at
                        FROM comment_versions WHERE video_id=? GROUP BY video_id,comment_id,author_id
                        HAVING count(DISTINCT text_hash)>1 ORDER BY first_observed_at''',(video,))]
                for row in comments_db.execute('SELECT * FROM collection_attempts WHERE video_id=? ORDER BY collected_at,attempt_id',(video,)):
                    a=dict(row);config=json.loads(a['configuration_json']);coverage=json.loads(a['coverage_json']);matches=json.loads(a['target_matches_json'])
                    evidence=_text_evidence(comment_root,video,a)
                    target=matches.get(IRISHWEEGEE,[])
                    attempts.append(dict(attempt_id=a['attempt_id'],attempted_at=a['collected_at'],outcome=a['outcome'],
                        invocation_source=config.get('invocation',{}).get('source','unknown'),
                        purpose=config.get('invocation',{}).get('purpose','unknown'),usable_comments=bool((a['retrieved_count'] or 0)>0),
                        retrieved_count=a['retrieved_count'],coverage_assessment=coverage.get('assessment','unknown'),
                        retrieval_state=coverage.get('retrieval_state','unknown'),irishweegee_observed=bool(target),
                        irishweegee_comment_count=len(target),irishweegee_text=evidence))
                    for e in evidence:by_comment.setdefault((video,e['comment_id']),[]).append((a['collected_at'],e))
            first=attempts[0] if attempts else None
            first_irish=next((a for a in attempts if a['irishweegee_observed']),None)
            first_scheduled=next((a for a in attempts if a['invocation_source']=='scheduled'),None)
            scheduled_presence=(True if first_scheduled and first_scheduled['irishweegee_observed'] else
                               False if first_scheduled and first_scheduled['coverage_assessment']=='complete' else None)
            presence=(True if first and first['irishweegee_observed'] else
                      False if first and first['coverage_assessment']=='complete' else None)
            cp_item=comment_policy.get(video);kp_item=caption_policy.get(video)
            caption_rows=[r for r in caption_history if r.get('video_id')==video]
            first_caption=caption_rows[0] if caption_rows else None
            first_complete=next((r for r in caption_rows if r.get('complete')),None)
            item['comments']=dict(attempt_count=len(attempts),first_attempt=first,
                first_scheduled_attempt=first_scheduled,first_attempt_usable=first['usable_comments'] if first else None,
                irishweegee_first_attempt=presence,
                irishweegee_first_attempt_evidence=('observed' if presence is True else 'not_observed_with_complete_coverage' if presence is False else 'unknown_or_uncertain_coverage' if first else 'no_attempt'),
                irishweegee_first_scheduled_attempt=scheduled_presence,
                irishweegee_first_scheduled_attempt_evidence=('observed' if scheduled_presence is True else 'not_observed_with_complete_coverage' if scheduled_presence is False else 'unknown_or_uncertain_coverage' if first_scheduled else 'no_scheduled_attempt'),
                first_scheduled_attempt_usable=first_scheduled['usable_comments'] if first_scheduled else None,
                irishweegee_first_observed_at=first_irish['attempted_at'] if first_irish else None,
                irishweegee_first_observation_attempt=(attempts.index(first_irish)+1 if first_irish else None),
                revisions=revisions,coverage_uncertain=any(a['coverage_assessment']!='complete' for a in attempts),
                policy_status=cp_item.get('status') if cp_item else 'unknown',policy_reason=cp_item.get('reason') if cp_item else None,
                next_due_at=cp_item.get('refresh',{}).get('next_due_at') if cp_item else None,
                outstanding=bool(cp_item and cp_item.get('eligible') and not cp_item.get('refresh',{}).get('monitoring_complete')))
            for (vid,cid),values in by_comment.items():
                if vid==video and len({v[1]['text_hash'] for v in values})>1:
                    ordered=sorted(values,key=lambda x:x[0]);detail=dict(comment_id=cid,
                        author_id=IRISHWEEGEE,observations=len(ordered),first_observed_at=ordered[0][0],latest_observed_at=ordered[-1][0],
                        first_text_length=ordered[0][1]['text_length'],latest_text_length=ordered[-1][1]['text_length'],
                        first_line_count=ordered[0][1]['line_count'],latest_line_count=ordered[-1][1]['line_count'])
                    existing=next((r for r in item['comments']['revisions'] if r['comment_id']==cid),None)
                    if existing:existing.update(detail)
                    else:item['comments']['revisions'].append(detail)
            item['captions']=dict(attempt_count=len(caption_rows),first_attempt_at=first_caption.get('started_at') if first_caption else None,
                first_scheduled_attempt_at=next((r['started_at'] for r in caption_rows if r.get('invocation_source')=='scheduled'),None),
                first_attempt_tracks_advertised=(bool(first_caption.get('tracks')) if first_caption and first_caption.get('listing_success') else None),
                first_attempt_complete=(bool(first_caption.get('complete')) if first_caption else None),
                first_complete_at=first_complete.get('finished_at') if first_complete else None,
                complete_attempt_number=(caption_rows.index(first_complete)+1 if first_complete else None),
                confirmation_count=sum(str(r.get('purpose','')).startswith('confirmation') for r in caption_rows),
                revisions=sum(any(f.get('status')=='revised' for f in r.get('formats',[])) for r in caption_rows),
                failed_attempts=sum(a['outcome'] in ('failed','partial','invalid') for a in caption_rows),
                outcomes=[dict(at=r.get('started_at'),purpose=r.get('purpose','unknown'),source=r.get('invocation_source','unknown'),
                               outcome=r.get('outcome'),complete=bool(r.get('complete')),technical_failure=bool(r.get('technical_failure')),
                               listing_success=bool(r.get('listing_success')),format_statuses=[f.get('status') for f in r.get('formats',[])]) for r in caption_rows],
                policy_status=kp_item.get('refresh',{}).get('phase') if kp_item else 'unknown',
                policy_reason=kp_item.get('reason') if kp_item else None,
                next_due_at=kp_item.get('refresh',{}).get('next_due_at') if kp_item else None,
                outstanding=bool(kp_item and kp_item.get('eligible') and not kp_item.get('refresh',{}).get('monitoring_complete')))
            item['comments']['failed_attempts']=sum(a['outcome'] in ('failed','partial','invalid') for a in attempts)
            item['comments']['first_attempt_elapsed_hours_from_completion_proxy']=_elapsed_hours(item['completion_observed_at'],first['attempted_at']) if first else None
            item['captions']['first_attempt_elapsed_hours_from_completion_proxy']=_elapsed_hours(item['completion_observed_at'],first_caption['started_at']) if first_caption else None
            item['captions']['first_complete_elapsed_hours_from_completion_proxy']=_elapsed_hours(item['completion_observed_at'],first_complete['finished_at']) if first_complete else None
            if first_caption:
                statuses={f.get('format'):f.get('status') for f in first_caption.get('formats',[])}
                item['captions']['first_attempt_formats']=statuses
                item['captions']['first_attempt_json3_preserved']=statuses.get('json3') in ('preserved','revised','unchanged','reused')
                item['captions']['first_attempt_vtt_preserved']=statuses.get('vtt') in ('preserved','revised','unchanged','reused')
        groups={}
        for group in ('daily_show','the_club','other_approved'):
            vids=[i for i in chosen if (i['group'] if i['group'] in ('daily_show','the_club') else 'other_approved')==group]
            attempted=[i for i in vids if i['comments']['attempt_count']]
            first_usable=sum(i['comments']['first_attempt_usable'] is True for i in attempted)
            first_irish_known=[i for i in attempted if i['comments']['irishweegee_first_attempt'] is not None]
            first_irish=sum(i['comments']['irishweegee_first_attempt'] is True for i in first_irish_known)
            first_irish_negative=sum(i['comments']['irishweegee_first_attempt'] is False for i in attempted)
            first_irish_unknown=sum(i['comments']['irishweegee_first_attempt'] is None for i in attempted)
            scheduled_attempted=[i for i in vids if i['comments']['first_scheduled_attempt']]
            scheduled_known=[i for i in scheduled_attempted if i['comments']['irishweegee_first_scheduled_attempt'] is not None]
            scheduled_hits=sum(i['comments']['irishweegee_first_scheduled_attempt'] is True for i in scheduled_known)
            scheduled_negative=sum(i['comments']['irishweegee_first_scheduled_attempt'] is False for i in scheduled_attempted)
            scheduled_unknown=sum(i['comments']['irishweegee_first_scheduled_attempt'] is None for i in scheduled_attempted)
            caption_attempted=[i for i in vids if i['captions']['attempt_count']]
            caption_advertised_known=[i for i in caption_attempted if i['captions']['first_attempt_tracks_advertised'] is not None]
            groups[group]=dict(known_videos=len(vids),comment_attempted=len(attempted),comment_never_attempted=len(vids)-len(attempted),
                comment_attempt_count_total=sum(i['comments']['attempt_count'] for i in vids),
                first_attempt_usable=first_usable,first_attempt_usable_rate=first_usable/len(attempted) if attempted else None,
                first_attempt_irishweegee=first_irish,first_attempt_irishweegee_rate=first_irish/len(first_irish_known) if first_irish_known else None,
                first_attempt_irishweegee_known_denominator=len(first_irish_known),
                first_attempt_irishweegee_attempted_denominator=len(attempted),
                first_attempt_irishweegee_definitive_negatives=first_irish_negative,
                first_attempt_irishweegee_unknown=first_irish_unknown,
                first_attempt_irishweegee_rate_all_attempted=first_irish/len(attempted) if attempted else None,
                first_scheduled_attempt_irishweegee_observed=scheduled_hits,
                first_scheduled_attempt_irishweegee_attempted_denominator=len(scheduled_attempted),
                first_scheduled_attempt_irishweegee_known_denominator=len(scheduled_known),
                first_scheduled_attempt_irishweegee_definitive_negatives=scheduled_negative,
                first_scheduled_attempt_irishweegee_unknown=scheduled_unknown,
                first_scheduled_attempt_irishweegee_rate=(scheduled_hits/len(scheduled_attempted) if scheduled_attempted else None),
                later_irishweegee_first_observation=sum(bool(i['comments']['irishweegee_first_observation_attempt'] and i['comments']['irishweegee_first_observation_attempt']>1) for i in attempted),
                comment_revised_ids=sum(bool(i['comments']['revisions']) for i in vids),caption_attempted=len(caption_attempted),
                caption_attempt_count_total=sum(i['captions']['attempt_count'] for i in vids),
                caption_never_attempted=len(vids)-len(caption_attempted),caption_first_attempt_advertised=sum(i['captions']['first_attempt_tracks_advertised'] is True for i in caption_advertised_known),
                caption_first_attempt_advertised_known_denominator=len(caption_advertised_known),
                caption_first_attempt_advertised_rate=(sum(i['captions']['first_attempt_tracks_advertised'] is True for i in caption_advertised_known)/len(caption_advertised_known) if caption_advertised_known else None),
                caption_first_attempt_complete=sum(i['captions']['first_attempt_complete'] is True for i in caption_attempted),
                caption_first_attempt_complete_rate=(sum(i['captions']['first_attempt_complete'] is True for i in caption_attempted)/len(caption_attempted) if caption_attempted else None),
                average_comment_attempts_per_known_video=round(sum(i['comments']['attempt_count'] for i in vids)/len(vids),2) if vids else 0,
                average_caption_attempts_per_known_video=round(sum(i['captions']['attempt_count'] for i in vids)/len(vids),2) if vids else 0,
                videos_with_scheduled_comment_attempt=sum(bool(i['comments']['first_scheduled_attempt']) for i in vids),
                videos_with_scheduled_caption_attempt=sum(bool(i['captions']['first_scheduled_attempt_at']) for i in vids),
                median_comment_first_attempt_hours_from_completion_proxy=statistics.median([i['comments']['first_attempt_elapsed_hours_from_completion_proxy'] for i in vids if i['comments']['first_attempt_elapsed_hours_from_completion_proxy'] is not None]) if any(i['comments']['first_attempt_elapsed_hours_from_completion_proxy'] is not None for i in vids) else None,
                median_caption_complete_hours_from_completion_proxy=statistics.median([i['captions']['first_complete_elapsed_hours_from_completion_proxy'] for i in vids if i['captions']['first_complete_elapsed_hours_from_completion_proxy'] is not None]) if any(i['captions']['first_complete_elapsed_hours_from_completion_proxy'] is not None for i in vids) else None,
                captions_complete=sum(bool(i['captions']['first_complete_at']) for i in vids),caption_outstanding=sum(i['captions']['outstanding'] for i in vids))
        scope_ids={i['id'] for i in chosen}
        return dict(schema_version=1,read_only=True,date_scope=dict(start=start,end=end,inclusive=True,timezone='America/Los_Angeles',
            meaning='selected inventory videos by authoritative publication/episode date; attempts include all retained history for those videos'),
            cohort=groups,selected_video_count=len(chosen),unknown_date_selected_ids=unknown,
            current_due_work=dict(evaluated_at=evaluated.isoformat(),comments_due=sum(v in scope_ids for v in cp['due_ids']),
                captions_due=sum(v in scope_ids for v in kp['due_ids']),comments_due_ids=[v for v in cp['due_ids'] if v in scope_ids],
                captions_due_ids=[v for v in kp['due_ids'] if v in scope_ids],
                all_inventory_comments_due=len(cp['due_ids']),all_inventory_captions_due=len(kp['due_ids'])),
            discovery_receipt_scope='receipt run start date in Pacific time, inclusive',
            inventory_limitations=['This report cannot identify videos absent from inventory.',
                'A first full was_live imported_at timestamp is only a completion-observed proxy, not broadcast end.',
                'Unknown/legacy provenance is not inferred from command names.'],videos=chosen,
            discovery_receipts=_discovery_receipts(Path(inventory_path).resolve().parent/'morning',start,end))
    finally:
        if comments_db:comments_db.close()


def _discovery_receipts(directory,start,end):
    output=[]
    for path in sorted((directory/'history').glob('*.json')) if (directory/'history').exists() else []:
        try:r=json.loads(path.read_bytes())
        except (OSError,ValueError):continue
        local=_local_date(r.get('started_at'))
        if local and start<=local<=end:
            output.append(dict(run_id=r.get('run_id'),started_at=r.get('started_at'),trigger=r.get('trigger','unknown'),
                limits=r.get('limits'),encountered=r.get('encountered'),new_ids=r.get('new_ids'),
                tab_coverage=r.get('tab_coverage'),
                metadata_requests=r.get('metadata_requests'),enriched=len(r.get('enriched',[])),deferred=len(r.get('deferred',[])),
                failures=len(r.get('failures',[])),coverage=r.get('forward_coverage'),stop_reason=r.get('stop_reason')))
    return output


def render(result):
    lines=[f"Supplemental effectiveness, Pacific episode dates {result['date_scope']['start']} through {result['date_scope']['end']} (inclusive)"]
    for name,values in result['cohort'].items():
        lines.append(f"{name}: videos={values['known_videos']}, comment-attempted={values['comment_attempted']} ({values['comment_attempt_count_total']} attempts), never-attempted={values['comment_never_attempted']}, first usable={values['first_attempt_usable']}/{values['comment_attempted']}, IrishWeegee first observed={values['first_attempt_irishweegee']}/{values['first_attempt_irishweegee_attempted_denominator']} attempted (definitive negatives={values['first_attempt_irishweegee_definitive_negatives']}, unknown={values['first_attempt_irishweegee_unknown']}; known-result rate={values['first_attempt_irishweegee']}/{values['first_attempt_irishweegee_known_denominator']}), scheduled-first observed={values['first_scheduled_attempt_irishweegee_observed']}/{values['first_scheduled_attempt_irishweegee_attempted_denominator']} (definitive negatives={values['first_scheduled_attempt_irishweegee_definitive_negatives']}, unknown={values['first_scheduled_attempt_irishweegee_unknown']}), caption-attempted={values['caption_attempted']} ({values['caption_attempt_count_total']} attempts), captions complete={values['captions_complete']}/{values['known_videos']}")
    lines.append(f"Unknown-date selected inventory IDs: {len(result['unknown_date_selected_ids'])}; discovery receipts: {len(result['discovery_receipts'])}")
    lines.append('Absence under uncertain coverage is unknown; report does not imply exhaustive retrieval or identify undiscovered videos.')
    return '\n'.join(lines)
