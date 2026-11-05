"""Supplemental cycle integration: synthetic YouTube, disposable SQLite only."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from dls_video_downloader import supplemental_cycle as cycle
from dls_video_downloader import morning_discovery as morning
from dls_video_downloader import comment_collection as collector
from dls_video_downloader import comment_runner
from dls_video_downloader import acquisition as media
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.cli import main
from dls_video_downloader.comments import CommentStore, IRISHWEEGEE
from dls_video_downloader.inventory import connect, import_document, decide
from dls_video_downloader.comment_planning import plan

START=datetime(2026,11,5,14,tzinfo=timezone.utc)
VIDEO='aaaaaaaaaaa';TODAY='bbbbbbbbbbb';CLUB='ccccccccccc'


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.path=self.base/'inventory.sqlite3';self.root=self.base/'comments'
        self.db=connect(self.path);self.addCleanup(self.db.close)
        self.now=START+timedelta(hours=3,minutes=30);self.elapsed=0;self.metadata=[];self.collections=[]
        self.inventory_clock=patch('dls_video_downloader.inventory.now',side_effect=lambda:self.now.isoformat())
        self.inventory_clock.start();self.addCleanup(self.inventory_clock.stop)
        self.head=[dict(id=TODAY,title='DLS FULL SHOW',live_status='is_live')]
        self.full={TODAY:self.document(TODAY,'was_live')}

    def document(self,video,status='was_live',title='DLS FULL SHOW',release=START):
        return dict(id=video,title=title,channel_id=CHANNEL,live_status=status,
                    was_live=status=='was_live',is_live=status=='is_live',
                    release_timestamp=release.timestamp(),release_date=release.strftime('%Y%m%d'),
                    upload_date=release.strftime('%Y%m%d'),timestamp=release.timestamp())

    def seed(self,video=VIDEO,status='was_live',**kwargs):
        with self.db:
            import_document(self.db,self.document(video,status,**kwargs),'live' if video!=CLUB else 'videos','fixture')
            self.db.execute('UPDATE videos SET first_seen=? WHERE id=?',(START.isoformat(),video))

    def record(self,when,video=VIDEO,target=False):
        with CommentStore(self.root) as store:
            return store.record(video,[dict(id='target',author_id=IRISHWEEGEE,text='untouched\n  source')]
                                if target else [],outcome='uncertain',collected_at=when.isoformat())

    def metadata_request(self,args,log,timeout):
        self.metadata.append((args,timeout))
        if '--flat-playlist' in args:
            return json.dumps(dict(channel_id=CHANNEL,epoch=self.now.timestamp(),entries=self.head))
        return json.dumps(dict(epoch=self.now.timestamp(),**self.full[args[-1].split('=')[-1]]))

    def comment_request(self,args,timeout,fd):
        video=args[-1].split('=')[-1];self.collections.append((video,timeout))
        return collector.Execution(stdout=json.dumps(dict(id=video,comments=[])).encode(),returncode=0)

    def invoke(self,**kwargs):
        with patch.object(media,'run',side_effect=self.metadata_request), \
             patch.object(collector,'run_extractor',side_effect=self.comment_request), \
             patch.object(collector,'timestamp',side_effect=lambda:self.now.isoformat()), \
             patch.object(socket,'socket',side_effect=AssertionError('live network forbidden')), \
             patch.object(media,'acquire',side_effect=AssertionError('media forbidden')):
            return cycle.run(self.path,self.root,clock=lambda:self.now,monotonic=lambda:self.elapsed,**kwargs)

    def files(self):
        return {str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_default_readonly_no_network_no_stores_no_logs(self):
        self.seed();before=self.files();out=self.invoke()
        self.assertEqual(out['mode'],'plan');self.assertFalse(out['fresh_youtube_status'])
        self.assertEqual(out['phases']['comments']['selected_ids'],[VIDEO])
        self.assertEqual(self.metadata,[]);self.assertEqual(self.collections,[])
        self.assertEqual(before,self.files());self.assertFalse((self.base/'supplemental').exists())
        self.assertFalse(self.root.exists())

    def test_discovery_flag_without_execute_rejected(self):
        with self.assertRaises(ValueError):self.invoke(discover=True)
        self.assertFalse((self.base/'supplemental').exists())
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as exc:
            main(['--db',str(self.path),'cycle','--discover'])
        self.assertEqual(exc.exception.code,2)

    def test_inventory_execution_never_discovers_or_changes_inventory(self):
        self.seed();before=self.path.read_bytes()
        with patch.object(morning,'discover',side_effect=AssertionError('discovery')):
            out=self.invoke(execute=True)
        self.assertEqual(out['outcome'],'successful');self.assertEqual(self.metadata,[])
        self.assertEqual(self.collections[0][0],VIDEO);self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(out['phases']['discovery']['outcome'],'not_requested')

    def test_discovery_then_comment_eligibility_without_mkv(self):
        self.seed(TODAY,'is_live')
        out=self.invoke(execute=True,discover=True)
        self.assertEqual([x[0] for x in self.collections],[TODAY])
        self.assertEqual(out['phases']['discovery']['outcome'],'successful')
        self.assertEqual(out['phases']['comments']['outcome'],'successful')
        self.assertTrue(out['fresh_youtube_status'])
        self.assertEqual(self.db.execute('select count(*) from acquisitions').fetchone()[0],0)
        self.assertFalse(list(self.base.rglob('*.mkv')))

    def test_live_and_uncertain_never_manufacture_completion(self):
        for status in ('is_live','is_upcoming',None):
            self.full[TODAY]['live_status']=status
            self.full[TODAY]['is_live']=status=='is_live'
            out=self.invoke(execute=True,discover=True)
            self.assertEqual(self.collections,[])
            item=next(i for i in plan(self.path,self.root,now=self.now)['items'] if i['id']==TODAY)
            self.assertFalse(item['due'])
            self.now+=timedelta(seconds=1)

    def test_previously_enriched_live_status_is_refreshed(self):
        self.seed(TODAY,'is_live')
        out=self.invoke(execute=True,discover=True)
        self.assertIn(TODAY,out['phases']['discovery']['result']['enriched'])
        self.assertEqual(self.collections[0][0],TODAY)

    def test_known_daily_and_all_current_minis_receive_metadata(self):
        self.seed(TODAY,'is_live')
        self.head=[dict(id=str(i)*11,title='Special mini-broadcast') for i in range(3)]
        for entry in self.head:self.full[entry['id']]=self.document(entry['id'],title=entry['title'])
        out=self.invoke(execute=True,discover=True,max_videos=1)
        data=out['phases']['discovery']['result']
        self.assertEqual(set(data['enriched']),{TODAY,*[e['id'] for e in self.head]})
        self.assertTrue(data['forward_coverage']['complete'])
        self.assertEqual(self.collections[0][0],TODAY)

    def test_every_recent_live_video_refreshes_next_cycle(self):
        self.seed(VIDEO,'is_live');self.seed(TODAY,'is_live')
        self.full[VIDEO]=self.document(VIDEO,'is_live');self.full[TODAY]=self.document(TODAY,'is_live')
        self.head=[]
        first=self.invoke(execute=True,discover=True)
        self.now+=timedelta(minutes=30)
        second=self.invoke(execute=True,discover=True)
        self.assertEqual(set(first['phases']['discovery']['result']['enriched']),{VIDEO,TODAY})
        self.assertEqual(set(second['phases']['discovery']['result']['enriched']),{VIDEO,TODAY})
        self.assertEqual(self.collections,[])

    def test_club_upload_treatment_unchanged(self):
        self.seed(CLUB,'not_live',title='In The Club')
        out=self.invoke(execute=True)
        self.assertEqual(out['outcome'],'successful')
        self.assertEqual(self.collections[0][0],CLUB)

    def test_transient_discovery_failure_allows_known_confirmation(self):
        self.now+=timedelta(days=3)
        self.seed();self.record(self.now-timedelta(days=2),target=True)
        self.metadata_request=lambda *args:(_ for _ in ()).throw(ValueError('ERROR: transient fixture'))
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['phases']['discovery']['outcome'],'failed')
        self.assertEqual(out['phases']['comments']['outcome'],'successful')
        self.assertEqual(out['outcome'],'partially_successful');self.assertEqual(out['exit_code'],2)
        self.assertEqual(self.collections[0][0],VIDEO)
        item=plan(self.path,self.root,now=self.now)['items'][0]
        self.assertTrue(item['refresh']['monitoring_complete'])

    def test_failed_discovery_does_not_promote_live_video(self):
        self.seed(TODAY,'is_live')
        self.metadata_request=lambda *args:(_ for _ in ()).throw(ValueError('ERROR: transient fixture'))
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.collections,[]);self.assertFalse(out['fresh_youtube_status'])
        self.assertEqual(plan(self.path,self.root,now=self.now)['items'][0]['category'],'review')

    def test_failed_comments_does_not_invalidate_metadata(self):
        self.comment_request=lambda *args:collector.Execution(stderr=b'ERROR: fixture extraction failed',returncode=1)
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['phases']['discovery']['outcome'],'successful')
        self.assertEqual(out['phases']['comments']['outcome'],'failed')
        self.assertEqual(plan(self.path,self.root,now=self.now)['items'][0]['category'],'daily_show')
        self.assertEqual(out['outcome'],'partially_successful')

    def test_active_shared_cooldown_blocks_both_network_phases(self):
        self.seed();directory=self.root/'runner';directory.mkdir(parents=True)
        comment_runner.atomic_json(directory/'cooldown.json',dict(version=1,until=(self.now+timedelta(hours=6)).isoformat()))
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['phases']['discovery']['reason'],'shared_youtube_cooldown')
        self.assertEqual(out['phases']['comments']['reason'],'shared_youtube_cooldown')
        self.assertEqual(self.metadata,[]);self.assertEqual(self.collections,[])

    def test_discovery_blocking_persists_cooldown_stops_comments(self):
        self.seed()
        self.metadata_request=lambda *args:(_ for _ in ()).throw(ValueError('ERROR: HTTP Error 429'))
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['comments']['reason'],'youtube_blocking')
        self.assertGreater(comment_runner.read_cooldown(self.root/'runner'),self.now)

    def test_comment_rate_limit_stops_remaining_work(self):
        self.seed();self.seed(CLUB,'not_live',title='The Club')
        def blocked(*args):
            self.collections.append(args)
            return collector.Execution(stderr=b'ERROR: HTTP Error 429',returncode=1)
        self.comment_request=blocked
        out=self.invoke(execute=True)
        self.assertEqual(len(self.collections),1)
        self.assertEqual(out['phases']['comments']['result']['stop_reason'],'extractor_rate_limit_or_block')

    def test_overall_budget_spans_discovery_and_comments(self):
        original=self.metadata_request
        def slow(*args):
            value=original(*args);self.elapsed+=50;self.now+=timedelta(seconds=50);return value
        self.metadata_request=slow
        out=self.invoke(execute=True,discover=True,runtime_seconds=180)
        self.assertEqual(out['phases']['comments']['budget_seconds'],80)
        self.assertLessEqual(self.collections[0][1],79)

    def test_insufficient_budget_never_starts_comment_phase(self):
        original=self.metadata_request
        def slow(*args):
            value=original(*args);self.elapsed+=80;return value
        self.metadata_request=slow
        out=self.invoke(execute=True,discover=True,runtime_seconds=180)
        self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['comments']['reason'],'insufficient_remaining_runtime')

    def test_absolute_phase_deadline_cannot_reset_discovery_budget(self):
        self.seed()
        out=self.invoke(execute=True,discover=True,discovery_seconds=30,runtime_seconds=60)
        self.assertTrue(all(t<=30 for _,t in self.metadata))
        self.assertEqual(out['limits']['runtime_seconds'],60)

    def test_cycle_overlap_defers_without_network_or_report_overwrite(self):
        self.seed()
        with cycle.cycle_lock(self.base/'supplemental'):
            out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['exit_code'],75);self.assertEqual(out['outcome'],'deferred')
        self.assertEqual(self.metadata,[]);self.assertEqual(self.collections,[])
        self.assertFalse((self.base/'supplemental/last-cycle.json').exists())

    def test_acquisition_lock_defers_both_without_waiting(self):
        self.seed()
        with media.operation_lock(self.base/'acquisitions'):
            out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.metadata,[]);self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['discovery']['outcome'],'deferred')
        self.assertEqual(out['phases']['comments']['outcome'],'deferred')

    def test_media_lock_not_held_during_comment_phase(self):
        original=self.comment_request
        def unlocked(*args):
            with media.operation_lock(self.base/'acquisitions'):pass
            return original(*args)
        self.comment_request=unlocked
        self.assertEqual(self.invoke(execute=True,discover=True)['exit_code'],0)

    def test_inventory_reader_sees_committed_snapshot_during_writer(self):
        self.seed()
        self.db.execute('BEGIN IMMEDIATE');self.db.execute("UPDATE videos SET latest_title='uncommitted'")
        try:
            out=self.invoke()
            title=out['phases']['comments']['plan']['items'][0]['title']
            self.assertEqual(title,'DLS FULL SHOW')
        finally:self.db.rollback()
        self.assertEqual(self.db.execute('pragma integrity_check').fetchone()[0],'ok')

    def test_comment_execution_independent_of_reserved_inventory_writer(self):
        self.seed();self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("UPDATE videos SET latest_title='uncommitted'")
        try:out=self.invoke(execute=True)
        finally:self.db.rollback()
        self.assertEqual(out['phases']['comments']['outcome'],'successful')
        self.assertEqual(self.db.execute('select latest_title from videos').fetchone()[0],'DLS FULL SHOW')

    def test_discovery_sqlite_contention_defers_then_comments_use_committed_data(self):
        self.seed();self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("UPDATE videos SET latest_title='uncommitted'")
        try:out=self.invoke(execute=True,discover=True)
        finally:self.db.rollback()
        self.assertEqual(out['phases']['discovery']['reason'],'sqlite_busy')
        self.assertEqual(out['phases']['comments']['outcome'],'successful')
        self.assertEqual(self.db.execute('pragma integrity_check').fetchone()[0],'ok')
        summary=json.loads((self.base/'morning/last-run.json').read_text())
        self.assertEqual(summary['stop_reason'],'sqlite_busy')

    def test_exclusive_inventory_lock_wait_is_bounded(self):
        self.seed();self.db.execute('BEGIN EXCLUSIVE');started=time.monotonic()
        try:out=self.invoke(execute=True)
        finally:self.db.rollback()
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(out['phases']['comments']['reason'],'sqlite_busy')
        self.assertEqual(self.collections,[])
        self.assertEqual(self.db.execute('pragma integrity_check').fetchone()[0],'ok')

    def test_comment_catalog_contention_is_bounded_and_preserves_history(self):
        self.seed();self.record(self.now-timedelta(days=2))
        with sqlite3.connect(self.root/'comments.sqlite3') as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:out=self.invoke(execute=True)
            finally:blocker.rollback()
        self.assertEqual(out['phases']['comments']['reason'],'sqlite_busy')
        self.assertEqual(self.collections,[])
        with sqlite3.connect(self.root/'comments.sqlite3') as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('select count(*) from collection_attempts').fetchone()[0],1)

    def test_reserved_comment_writer_defers_store_write_and_safe_retry(self):
        self.seed();self.record(self.now-timedelta(days=2))
        saved={p:p.read_bytes() for p in self.root.rglob('*.json')}
        with sqlite3.connect(self.root/'comments.sqlite3') as blocker:
            blocker.execute('BEGIN IMMEDIATE');started=time.monotonic()
            try:out=self.invoke(execute=True)
            finally:blocker.rollback()
        self.assertLess(time.monotonic()-started,7)
        self.assertEqual(out['phases']['comments']['reason'],'sqlite_busy')
        self.assertEqual(self.collections,[])
        for p,raw in saved.items():self.assertEqual(raw,p.read_bytes())
        retry=self.invoke(execute=True)
        self.assertEqual(retry['phases']['comments']['outcome'],'successful')
        with sqlite3.connect(self.root/'comments.sqlite3') as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('select count(*) from collection_attempts').fetchone()[0],2)

    def test_no_migrations_of_old_inventory(self):
        self.db.execute('PRAGMA user_version=2');self.db.commit();before=self.path.read_bytes()
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['phases']['discovery']['outcome'],'failed')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.db.execute('pragma user_version').fetchone()[0],2)

    def test_interruption_stops_later_phases_and_preserves_report(self):
        self.metadata_request=lambda *args:(_ for _ in ()).throw(KeyboardInterrupt())
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['exit_code'],130);self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['comments']['reason'],'interrupted')
        self.assertTrue(Path(out['report_path']).exists())

    def test_comment_timeout_is_failed_phase_not_failed_metadata(self):
        self.comment_request=lambda *args:collector.Execution(returncode=-9,termination='timeout')
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(out['phases']['discovery']['outcome'],'successful')
        self.assertEqual(out['phases']['comments']['outcome'],'failed')

    def test_repeated_cycles_do_not_duplicate_collection(self):
        self.seed();self.invoke(execute=True);self.invoke(execute=True)
        self.assertEqual(len(self.collections),1)
        with sqlite3.connect(self.root/'comments.sqlite3') as db:
            self.assertEqual(db.execute('select count(*) from collection_attempts').fetchone()[0],1)

    def test_manual_decisions_media_and_old_snapshots_preserved(self):
        self.now+=timedelta(days=3)
        self.seed();old=self.record(self.now-timedelta(days=2),target=True)
        media_path=self.base/'acquisitions/preserved.mkv';media_path.parent.mkdir();media_path.write_bytes(b'fixture media')
        receipt=self.base/'acquisitions/acquisition.json';receipt.write_text('fixture receipt')
        with self.db:decide(self.db,VIDEO,'approve','fixture')
        decisions=[tuple(r) for r in self.db.execute('select * from decisions')]
        saved={p:p.read_bytes() for p in self.root.rglob('*.json')}
        out=self.invoke(execute=True)
        self.assertEqual(out['phases']['comments']['outcome'],'successful')
        for p,raw in saved.items():self.assertEqual(raw,p.read_bytes())
        self.assertEqual(media_path.read_bytes(),b'fixture media');self.assertEqual(receipt.read_text(),'fixture receipt')
        self.assertEqual(decisions,[tuple(r) for r in self.db.execute('select * from decisions')])

    def test_day14_and_day15_policy_unchanged(self):
        self.seed();self.now=START+timedelta(days=14)
        out=self.invoke(execute=True);self.assertEqual(len(self.collections),1)
        self.now=START+timedelta(days=15)
        out=self.invoke(execute=True);self.assertEqual(len(self.collections),1)
        self.assertEqual(out['phases']['comments']['outcome'],'skipped')

    def test_completed_confirmation_stays_completed(self):
        self.now+=timedelta(days=3)
        self.seed();self.record(self.now-timedelta(days=2),target=True);self.record(self.now-timedelta(days=1))
        self.invoke(execute=True);self.assertEqual(self.collections,[])

    def test_phase_reports_json_and_human(self):
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(json.loads(Path(out['report_path']).read_text()),out)
        self.assertEqual(out['phases']['discovery']['metadata_requests'],2)
        self.assertEqual(out['phases']['comments']['collection_attempts'],1)
        rendered=cycle.render(out)
        self.assertIn('discovery: successful',rendered);self.assertIn('comments: successful',rendered)
        self.assertEqual(out['phases']['captions']['outcome'],'not_requested')

    def test_max_video_budget_and_manual_skip_are_respected(self):
        self.seed();self.seed(CLUB,'not_live',title='The Club');self.seed(TODAY)
        with self.db:decide(self.db,TODAY,'skip','persistent fixture')
        decisions=[tuple(r) for r in self.db.execute('select * from decisions')]
        out=self.invoke(execute=True,max_videos=1)
        self.assertEqual(len(self.collections),1)
        self.assertEqual(out['phases']['comments']['result']['budget_deferred'],1)
        self.assertNotIn(TODAY,[v for v,_ in self.collections])
        self.assertEqual(decisions,[tuple(r) for r in self.db.execute('select * from decisions')])

    def test_mixed_comment_results_remain_partial(self):
        self.seed();self.seed(CLUB,'not_live',title='The Club')
        original=self.comment_request
        def mixed(args,*rest):
            if args[-1].endswith(VIDEO):
                self.collections.append((VIDEO,rest[0]))
                return collector.Execution(stderr=b'ERROR: transient fixture',returncode=1)
            return original(args,*rest)
        self.comment_request=mixed
        out=self.invoke(execute=True)
        self.assertEqual(out['phases']['comments']['outcome'],'partially_successful')
        self.assertEqual(out['exit_code'],2);self.assertEqual(len(self.collections),2)

    def test_comment_interruption_prevents_second_video(self):
        self.seed();self.seed(CLUB,'not_live',title='The Club')
        self.comment_request=lambda *args:collector.Execution(returncode=-9,termination='interrupted')
        out=self.invoke(execute=True)
        self.assertEqual(out['exit_code'],130);self.assertEqual(out['outcome'],'interrupted')
        self.assertEqual(out['phases']['comments']['collection_attempts'],1)
        self.assertEqual(out['phases']['comments']['result']['work'][1]['status'],'deferred')

    def test_failed_blocking_state_write_still_prevents_comments(self):
        self.seed()
        self.metadata_request=lambda *args:(_ for _ in ()).throw(ValueError('HTTP Error 429'))
        original=comment_runner.atomic_json
        def broken(path,value):
            if path.name=='cooldown.json':raise OSError('fixture state storage error')
            return original(path,value)
        with patch.object(comment_runner,'atomic_json',side_effect=broken):out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['discovery']['outcome'],'failed')
        self.assertEqual(out['phases']['comments']['reason'],'youtube_blocking')

    def test_unknown_status_does_not_claim_fresh_verified_status(self):
        self.full[TODAY].pop('live_status')
        out=self.invoke(execute=True,discover=True)
        self.assertFalse(out['fresh_youtube_status']);self.assertEqual(self.collections,[])

    def test_invalid_cooldown_blocks_discovery_without_initializing_catalog(self):
        self.seed();directory=self.root/'runner';directory.mkdir(parents=True)
        (directory/'cooldown.json').write_text('invalid')
        out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.metadata,[]);self.assertEqual(self.collections,[])
        self.assertEqual(out['outcome'],'failed')
        self.assertFalse((self.root/'comments.sqlite3').exists())

    def test_metadata_retry_cooldown_blocks_inventory_only_comments(self):
        from dls_video_downloader import forward_metadata
        self.seed()
        forward_metadata.save_state(self.base/'morning',dict(version=1,failures={},
            cooldown_until=(self.now+timedelta(hours=6)).isoformat()))
        out=self.invoke(execute=True)
        self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['comments']['reason'],'shared_youtube_cooldown')
        self.assertFalse((self.root/'comments.sqlite3').exists())

    def test_metadata_state_write_failure_stops_comment_phase(self):
        from dls_video_downloader import forward_metadata
        self.seed()
        self.metadata_request=lambda *args:(_ for _ in ()).throw(ValueError('HTTP Error 429'))
        with patch.object(forward_metadata,'_save_state',side_effect=OSError('fixture disk error')):
            out=self.invoke(execute=True,discover=True)
        self.assertEqual(self.collections,[])
        self.assertEqual(out['phases']['comments']['reason'],'metadata_safety_state_error')

    def test_incomplete_forward_enrichment_is_not_reported_successful(self):
        self.seed(TODAY,'is_live')
        self.head=[dict(id=f'mini{i:07d}',title='Mini broadcast') for i in range(3)]
        for item in self.head:self.full[item['id']]=self.document(item['id'],title=item['title'])
        out=self.invoke(execute=True,discover=True,max_metadata_requests=2)
        self.assertEqual(out['phases']['discovery']['outcome'],'partially_successful')
        self.assertFalse(out['phases']['discovery']['result']['forward_coverage']['complete'])
        self.assertEqual(out['exit_code'],2)

    def test_cli_default_json_and_human_are_readonly(self):
        self.seed();before=self.files()
        for extra in (['--json'],[]):
            with redirect_stdout(io.StringIO()) as output:
                code=main(['--db',str(self.path),'cycle','--store-root',str(self.root)]+extra)
            self.assertEqual(code,0)
            if extra:self.assertEqual(json.loads(output.getvalue())['mode'],'plan')
            else:self.assertIn('existing inventory/history only',output.getvalue())
        self.assertEqual(self.files(),before)


if __name__=='__main__':unittest.main()
