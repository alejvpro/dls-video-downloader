"""Bounded runner acceptance tests: disposable data and synthetic extraction only."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import comment_runner as runner
from dls_video_downloader import comment_collection as collector
from dls_video_downloader.cli import main
from dls_video_downloader.comments import CommentStore, IRISHWEEGEE
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.inventory import connect, import_document, decide

DAY = timedelta(days=1)
START = datetime(2026, 11, 1, 14, tzinfo=timezone.utc)
IDS = ('aaaaaaaaaaa','bbbbbbbbbbb','ccccccccccc')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name);self.root = self.base/'comments'
        self.path = self.base/'inventory.sqlite3';self.media_lock = self.base/'media.lock'
        self.db = connect(self.path);self.addCleanup(self.db.close)
        self.now = START + 3*DAY;self.elapsed = 0
        self.network = []

    def ingest(self, video=IDS[0], release=START):
        with self.db:
            import_document(self.db, dict(id=video, channel_id=CHANNEL, title='DLS FULL SHOW',
                live_status='was_live', release_timestamp=release.timestamp(), release_date=release.strftime('%Y%m%d')),
                'live','fixture')
            self.db.execute('UPDATE videos SET first_seen=? WHERE id=?',(START.isoformat(),video))

    def record(self, video=IDS[0], when=None, target=False, outcome='uncertain'):
        with CommentStore(self.root) as store:
            return store.record(video,[{'id':'one','text':'original\n source  ', 'author_id':IRISHWEEGEE,
                                       'timestamp':123456,'_time_text':'2 days ago (edited)'}] if target else [],
                                outcome=outcome,collected_at=(when or self.now).isoformat())

    def extraction(self, argv, timeout, fd):
        self.network.append((argv[-1].split('=')[-1],timeout))
        return collector.Execution(stdout=json.dumps({'id':self.network[-1][0], 'comments':[]}).encode(),returncode=0)

    def invoke(self, execute=True, **kwargs):
        with patch.object(collector,'timestamp',side_effect=lambda:self.now.isoformat()), \
             patch.object(collector,'run_extractor',side_effect=self.extraction), \
             patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            return runner.run(self.path,self.root,execute=execute,clock=lambda:self.now,
                              monotonic=lambda:self.elapsed,acquisition_lock=self.media_lock,**kwargs)

    def files(self):
        return {str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_dry_run_no_writes_or_network_and_budget(self):
        for video in IDS:self.ingest(video)
        before=self.files();out=self.invoke(execute=False)
        self.assertEqual(len(out['selected']),2);self.assertEqual(out['budget_deferred'],1)
        self.assertEqual(out['mode'],'dry-run');self.assertEqual(self.files(),before)
        self.assertEqual(self.network,[]);self.assertFalse(self.root.exists())

    def test_empty_queue(self):
        out=self.invoke()
        self.assertEqual(out['work'],[]);self.assertEqual(out['due_count'],0)
        self.assertFalse((self.root/'comments.sqlite3').exists())

    def test_success_multiple_sequential_with_portable_evidence(self):
        for video in IDS:self.ingest(video)
        before=self.path.read_bytes();out=self.invoke()
        self.assertEqual([x[0] for x in self.network],list(IDS[:2]))
        self.assertTrue(all(w['status']=='completed' for w in out['work']))
        self.assertEqual(out['exit_code'],0);self.assertEqual(self.path.read_bytes(),before)
        for work in out['work']:
            self.assertEqual(work['started_at'],self.now.isoformat())
            self.assertEqual(work['finished_at'],self.now.isoformat())
            self.assertTrue(Path(work['collection_record']).exists())
        log=json.loads((self.root/'runner/last-run.json').read_bytes())
        self.assertEqual(log,out)
        self.assertLess((self.root/'runner/last-run.json').stat().st_size,runner.REPORT_LIMIT)

    def test_one_video_and_comment_budget(self):
        self.ingest();self.ingest(IDS[1])
        out=self.invoke(max_videos=1,max_comments=7)
        self.assertEqual(len(self.network),1)
        self.assertEqual(out['limits']['max_comments'],7)
        record=json.loads(Path(out['work'][0]['collection_record']).read_text())
        self.assertEqual(record['collection']['cap'],7)

    def test_ordinary_failure_does_not_starve_next_video(self):
        for video in IDS[:2]:self.ingest(video)
        original=self.extraction
        def fail(argv,timeout,fd):
            if not self.network:
                self.network.append((IDS[0],timeout))
                return collector.Execution(stderr=b'ERROR: temporary extraction failure',returncode=1)
            return original(argv,timeout,fd)
        self.extraction=fail
        out=self.invoke()
        self.assertEqual([w['status'] for w in out['work']],['failed','completed'])
        self.assertEqual(out['exit_code'],2)

    def test_rate_limit_stops_batch_persists_cooldown_and_preserves_history(self):
        self.ingest();self.ingest(IDS[1]);previous=self.record(when=self.now-2*DAY,target=True)
        snapshot=Path(self.root/IDS[0]/'snapshots'/f"{previous['snapshot_hash']}.json").read_bytes()
        def limited(argv,timeout,fd):
            self.network.append((argv[-1],timeout))
            return collector.Execution(stderr=b'ERROR: HTTP Error 429: Too Many Requests\nRetry-After: 28800',returncode=1)
        self.extraction=limited;out=self.invoke()
        self.assertEqual(len(self.network),1);self.assertEqual(out['exit_code'],2)
        self.assertEqual(out['stop_reason'],'extractor_rate_limit_or_block')
        self.assertEqual(runner.read_cooldown(self.root/'runner'),self.now+timedelta(hours=8))
        self.assertEqual(out['work'][1]['status'],'deferred')
        before=self.files();dry=self.invoke(execute=False)
        self.assertEqual(dry['selected'],[]);self.assertEqual(dry['stop_reason'],'collector_cooldown')
        self.assertEqual(self.files(),before)
        self.assertEqual(Path(self.root/IDS[0]/'snapshots'/f"{previous['snapshot_hash']}.json").read_bytes(),snapshot)

    def test_active_video_lock_defers_without_holding_or_modifying_it(self):
        self.ingest();self.media_lock.write_bytes(b'preserved')
        with self.media_lock.open('rb') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            out=self.invoke()
        self.assertEqual(out['stop_reason'],'video_acquisition_active')
        self.assertEqual(self.network,[]);self.assertEqual(self.media_lock.read_bytes(),b'preserved')
        self.assertFalse((self.root/'comments.sqlite3').exists())

    def test_probe_releases_video_lock_before_network(self):
        self.ingest();self.media_lock.touch()
        original=self.extraction
        def checked(*args):
            with self.media_lock.open('rb') as stream:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return original(*args)
        self.extraction=checked
        self.assertEqual(self.invoke()['exit_code'],0)

    def test_runner_overlap_no_collection_or_log_overwrite(self):
        self.ingest();directory=self.root/'runner'
        with runner.runner_lock(directory):
            out=self.invoke()
        self.assertEqual(out['exit_code'],75);self.assertEqual(out['stop_reason'],'comment_runner_busy')
        self.assertEqual(self.network,[]);self.assertFalse((directory/'last-run.json').exists())

    def test_manual_collector_overlap(self):
        self.ingest()
        with collector.collection_lock(self.root):out=self.invoke()
        self.assertEqual(out['exit_code'],75);self.assertEqual(out['stop_reason'],'comment_collector_busy')
        self.assertEqual(self.network,[])

    def test_stale_history_rechecked_inside_collector_lock(self):
        self.ingest();original=runner.collect
        def stale(*args,**kwargs):
            self.record()
            return original(*args,**kwargs)
        with patch.object(runner,'collect',side_effect=stale):out=self.invoke()
        self.assertEqual(out['work'][0]['status'],'skipped')
        self.assertEqual(out['work'][0]['reason'],'daily_collection');self.assertEqual(self.network,[])

    def test_changed_manual_decision_rechecked(self):
        self.ingest();original=runner.collect
        def stale(*args,**kwargs):
            with self.db:decide(self.db,IDS[0],'skip','concurrent manual fixture')
            return original(*args,**kwargs)
        with patch.object(runner,'collect',side_effect=stale):out=self.invoke()
        self.assertEqual(out['work'][0]['reason'],'blocked');self.assertEqual(self.network,[])

    def test_cutoff_rechecked_after_initial_plan(self):
        self.ingest();self.now=START+15*DAY-timedelta(minutes=2)
        original=runner.collect
        def late(*args,**kwargs):
            self.now=START+15*DAY
            return original(*args,**kwargs)
        with patch.object(runner,'collect',side_effect=late):out=self.invoke()
        self.assertEqual(out['work'][0]['reason'],'collection_window_expired')
        self.assertEqual(self.network,[])

    def test_final_day_attempt_timeout_clipped_to_hard_cutoff(self):
        self.ingest();self.record(when=START+DAY)
        self.now=START+15*DAY-timedelta(seconds=90)
        out=self.invoke()
        self.assertEqual(len(self.network),1);self.assertEqual(self.network[0][1],89)
        self.assertEqual(out['exit_code'],0)

    def test_no_automatic_collection_at_cutoff(self):
        self.ingest();self.now=START+15*DAY
        self.assertEqual(self.invoke()['selected'],[]);self.assertEqual(self.network,[])

    def test_confirmation_already_complete_not_selected(self):
        self.ingest();self.record(when=START,target=True);self.record(when=START+DAY)
        out=self.invoke();self.assertEqual(out['selected'],[]);self.assertEqual(self.network,[])

    def test_overall_deadline_is_not_reset_for_second_video(self):
        self.ingest();self.ingest(IDS[1]);original=self.extraction
        def slow(*args):
            value=original(*args);self.elapsed+=100;self.now+=timedelta(seconds=100)
            return value
        self.extraction=slow;out=self.invoke(runtime_seconds=180)
        self.assertEqual([n[1] for n in self.network],[179,79]);self.assertEqual(out['exit_code'],0)

    def test_insufficient_time_does_not_start_next_collection(self):
        self.ingest();self.ingest(IDS[1]);original=self.extraction
        def slow(*args):
            value=original(*args);self.elapsed+=575;return value
        self.extraction=slow;out=self.invoke()
        self.assertEqual(len(self.network),1)
        self.assertEqual(out['stop_reason'],'insufficient_remaining_runtime')
        self.assertEqual(out['work'][1]['status'],'deferred')

    def test_timeout_preserves_failed_attempt_and_stops_at_budget(self):
        self.ingest();self.ingest(IDS[1])
        def timeout(*args):
            self.elapsed+=600;self.network.append(args)
            return collector.Execution(returncode=-9,termination='timeout')
        self.extraction=timeout;out=self.invoke()
        self.assertEqual(len(self.network),1);self.assertEqual(out['exit_code'],2)
        self.assertEqual(out['work'][0]['status'],'failed')
        record=json.loads(Path(out['work'][0]['collection_record']).read_text())
        self.assertEqual(record['collection']['coverage']['retrieval_state'],'timeout')

    def test_interruption_records_evidence_and_stops(self):
        self.ingest();self.ingest(IDS[1])
        self.extraction=lambda *args:collector.Execution(returncode=-9,termination='interrupted')
        out=self.invoke();self.assertEqual(out['exit_code'],130)
        self.assertEqual(out['stop_reason'],'interrupted');self.assertEqual(out['work'][1]['status'],'deferred')
        self.assertTrue(Path(out['work'][0]['collection_record']).exists())

    def test_keyboard_interrupt_outside_collector_is_reported(self):
        self.ingest()
        with patch.object(runner,'collect',side_effect=KeyboardInterrupt):out=self.invoke()
        self.assertEqual(out['exit_code'],130);self.assertTrue((self.root/'runner/last-run.json').exists())

    def test_unknown_persistence_failure_stops_batch(self):
        self.ingest();self.ingest(IDS[1])
        with patch.object(runner,'collect',side_effect=ValueError('export failed')):out=self.invoke()
        self.assertEqual(out['exit_code'],1);self.assertEqual(out['stop_reason'],'runner_error')
        self.assertEqual(out['work'][1]['status'],'deferred')

    def test_cooldown_expiration_and_http_date(self):
        value=runner.blocking_evidence({'coverage':{'diagnostics':'ERROR: HTTP Error 429\nRetry-After: Thu, 05 Nov 2026 14:00:00 GMT'}},self.now)
        self.assertEqual(value['until'],'2026-11-05T14:00:00+00:00')
        self.root.mkdir();directory=self.root/'runner';directory.mkdir()
        runner.atomic_json(directory/'cooldown.json',dict(version=1,until=(self.now-timedelta(seconds=1)).isoformat()))
        self.ingest();self.assertEqual(self.invoke()['exit_code'],0);self.assertEqual(len(self.network),1)

    def test_blocking_diagnostics_conservative_and_comment_text_not_inspected(self):
        for text in ['HTTP Error 403: Forbidden', "Sign in to confirm you’re not a bot",'HTTP Error 402']:
            self.assertIsNotNone(runner.blocking_evidence({'error':text},self.now))
        self.assertIsNone(runner.blocking_evidence({'comments':[{'text':'HTTP Error 429'}]},self.now))
        self.assertIsNone(runner.blocking_evidence({'warnings':['unrelated warning']},self.now))
        until=runner.blocking_evidence({'error':'HTTP Error 429','coverage':{'diagnostics':'Retry-After: 999999'}},self.now)['until']
        self.assertGreaterEqual(datetime.fromisoformat(until),self.now+timedelta(seconds=999999))

    def test_invalid_cooldown_fails_closed_without_network(self):
        self.ingest();directory=self.root/'runner';directory.mkdir(parents=True)
        (directory/'cooldown.json').write_text('{broken')
        before=self.files()
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.network,[]);self.assertEqual(self.files(),before)

    def test_due_time_order_does_not_starve_older_work(self):
        self.ingest(IDS[1],release=START-timedelta(days=1));self.ingest(IDS[0])
        out=self.invoke(execute=False,max_videos=1)
        self.assertEqual(out['selected'][0]['id'],IDS[1])

    def test_no_video_lock_file_created(self):
        self.ingest();self.invoke();self.assertFalse(self.media_lock.exists())

    def test_cli_defaults_dry_run_json_and_human(self):
        self.ingest();before=self.files()
        args=['--db',str(self.path),'comments','run','--store-root',str(self.root)]
        for extra in (['--json'],[]):
            output=io.StringIO()
            with redirect_stdout(output):self.assertEqual(main(args+extra),0)
            if extra:self.assertEqual(json.loads(output.getvalue())['mode'],'dry-run')
            else:self.assertIn('dry-run',output.getvalue())
        self.assertEqual(before,self.files());self.assertEqual(self.network,[])

    def test_repeated_successful_invocation_does_not_duplicate_collection(self):
        self.ingest();self.invoke();before=len(self.network)
        out=self.invoke()
        self.assertEqual(len(self.network),before)
        self.assertEqual(out['selected'],[])
        with sqlite3.connect(self.root/'comments.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM collection_attempts').fetchone()[0],1)

    def test_initial_collection_wins_due_time_tie(self):
        self.ingest(IDS[0]);self.record(when=START)
        self.ingest(IDS[1],release=START+DAY)
        out=self.invoke(execute=False,max_videos=1)
        self.assertEqual(out['selected'][0]['id'],IDS[1])

    def test_video_acquisition_starting_between_candidates_defers_second(self):
        self.ingest();self.ingest(IDS[1])
        with patch.object(runner,'video_busy',side_effect=[False,False,True]):out=self.invoke()
        self.assertEqual(len(self.network),1)
        self.assertEqual(out['work'][1]['reason'],'video_acquisition_active')

    def test_no_inventory_classification_discovery_or_media_calls(self):
        self.ingest();before=self.path.read_bytes()
        with patch('dls_video_downloader.inventory.import_document',side_effect=AssertionError('import')), \
             patch('dls_video_downloader.inventory.reclassify',side_effect=AssertionError('classify')), \
             patch('dls_video_downloader.acquisition.acquire',side_effect=AssertionError('media')), \
             patch('dls_video_downloader.discovery.workflow',side_effect=AssertionError('discovery')):
            out=self.invoke()
        self.assertEqual(out['exit_code'],0);self.assertEqual(before,self.path.read_bytes())

    def test_cli_explicit_execute_uses_disposable_store(self):
        self.ingest()
        with patch.object(collector,'run_extractor',side_effect=self.extraction), \
             patch.object(runner,'video_busy',return_value=False),redirect_stdout(io.StringIO()) as output:
            code=main(['--db',str(self.path),'comments','run','--execute','--json',
                       '--store-root',str(self.root),'--max-videos','1'])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['mode'],'execute')
        self.assertEqual(len(self.network),1)

    def test_target_source_timestamp_and_uncertainty_preserved_by_runner(self):
        self.ingest()
        def positive(argv,timeout,fd):
            return collector.Execution(stdout=json.dumps({'id':IDS[0],'comments':[{
                'id':'target','author_id':IRISHWEEGEE,'text':'  untouched\ntext ',
                'timestamp':123456,'_time_text':'2 days ago (edited)'}]}).encode(),returncode=0)
        self.extraction=positive;out=self.invoke()
        snapshot=json.loads(Path(out['work'][0]['snapshot_path']).read_text())
        comment=snapshot['comments'][0]
        self.assertEqual(comment['timestamp'],123456)
        self.assertEqual(comment['_time_text'],'2 days ago (edited)')
        self.assertEqual(comment['text'],'  untouched\ntext ')
        next_plan=runner.plan(self.path,self.root,now=self.now)
        self.assertEqual(next_plan['items'][0]['refresh']['phase'],'confirmation')

    def test_collector_preflight_cannot_expand_caller_timeout(self):
        self.ingest()
        with patch.object(collector,'run_extractor',side_effect=self.extraction):
            collector.collect(IDS[0],root=self.root,timeout=40,before_collect=lambda:300)
        self.assertEqual(self.network[0][1],40)

    def test_failed_cooldown_persistence_stops_further_network(self):
        self.ingest();self.ingest(IDS[1])
        def limited(*args):
            self.network.append(args)
            return collector.Execution(stderr=b'ERROR: HTTP Error 429',returncode=1)
        self.extraction=limited
        original=runner.atomic_json
        def unavailable(path,value):
            if path.name=='cooldown.json':raise OSError('fixture storage failure')
            return original(path,value)
        with patch.object(runner,'atomic_json',side_effect=unavailable):out=self.invoke()
        self.assertEqual(len(self.network),1);self.assertEqual(out['exit_code'],1)
        self.assertEqual(out['work'][1]['status'],'deferred')

    def test_interrupted_blocked_extraction_retains_cooldown_and_interrupt_exit(self):
        self.ingest()
        self.extraction=lambda *args:collector.Execution(stderr=b'ERROR: HTTP Error 429',
                                                         returncode=-9,termination='interrupted')
        out=self.invoke()
        self.assertEqual(out['exit_code'],130)
        self.assertEqual(out['stop_reason'],'interrupted')
        self.assertIsNotNone(runner.read_cooldown(self.root/'runner'))

    def test_invalid_limits_do_not_write(self):
        for kwargs in ({'max_videos':0},{'max_videos':11},{'max_comments':1001},
                       {'runtime_seconds':29},{'runtime_seconds':601}):
            with self.assertRaises(ValueError):self.invoke(**kwargs)
        self.assertFalse(self.root.exists())


if __name__=='__main__':unittest.main()
