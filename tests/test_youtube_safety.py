"""Shared YouTube admission acceptance: local fake children, no network."""
from contextlib import redirect_stdout,redirect_stderr
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import youtube_safety as safety
from dls_video_downloader import comment_collection as network, acquisition as media
from dls_video_downloader import supplemental_cycle as cycle, caption_collection
from dls_video_downloader import comment_runner, caption_runner, morning_discovery
from dls_video_downloader.youtube_recovery import recover
from dls_video_downloader.cli import main
from dls_video_downloader.inventory import connect, import_document
from dls_video_downloader.classification import CHANNEL

AT=datetime(2026,11,5,20,25,tzinfo=timezone.utc)
VIDEO='aaaaaaaaaaa'
BOT="ERROR: Sign in to confirm you’re not a bot. LOGIN_REQUIRED"


def process_observe(root,index):
    safety.Safety(root).observe('HTTP Error 429',component='worker',operation='metadata',now=AT,observation_id=str(index))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.gate=safety.Safety(self.root)
        self.path=self.root/'inventory.sqlite3';self.db=connect(self.path);self.addCleanup(self.db.close)
        with self.db,patch('dls_video_downloader.inventory.now',return_value=AT.isoformat()):
            import_document(self.db,dict(id=VIDEO,channel_id=CHANNEL,title='DLS FULL SHOW',release_date='20261105',upload_date='20261105',release_timestamp=AT.timestamp(),live_status='was_live',was_live=True),'live','fixture')

    def block(self,text=BOT,**fields):
        return self.gate.observe(text,component='captions',operation='listing',now=AT,**fields)

    def events(self):return [json.loads(p.read_bytes()) for p in sorted((self.gate.root/'events').glob('*.json'))]

    def test_normal_readonly_status_no_files(self):
        s=self.gate.status(AT);self.assertTrue(s['network_allowed']);self.assertFalse(self.gate.root.exists())

    def test_bot_default_24h_and_evidence(self):
        s=self.block();self.assertEqual(s['reason'],'authentication_challenge');self.assertEqual(s['retry_at'],(AT+timedelta(hours=24)).isoformat());self.assertEqual(s['incident_count'],1)
        self.assertFalse(self.gate.status(AT)['network_allowed']);self.assertEqual(self.events()[0]['event'],'incident_opened')

    def test_configurable_bot_default(self):
        gate=safety.Safety(self.root,bot_hours=36);gate.observe(BOT,now=AT)
        self.assertEqual(gate.status(AT)['retry_at'],(AT+timedelta(hours=36)).isoformat())

    def test_expiry_requires_authorization_not_automatic_success(self):
        self.block();later=AT+timedelta(days=1)
        self.assertEqual(self.gate.status(later)['status'],'recovery_eligible')
        with self.assertRaises(safety.Blocked):self.gate.ensure(now=later)
        self.assertIsNone(self.gate.status(later)['last_recovery_success'])
        with self.assertRaises(safety.Blocked):
            with self.gate.recovery(now=later):pass

    def test_ordinary_errors_and_bare_403_not_global(self):
        for text in ['connection reset','timeout','HTTP Error 503','HTTP Error 403: Forbidden','video unavailable','malformed caption payload','HTTP Error 402']:
            with self.subTest(text=text):self.assertIsNone(self.gate.observe(text,now=AT))
        self.assertFalse(self.gate.root.exists());self.assertTrue(self.gate.status(AT)['network_allowed'])

    def test_429_and_trustworthy_retry_after(self):
        s=self.block('HTTP Error 429\nRetry-After: 900');self.assertEqual(s['reason'],'rate_limit');self.assertEqual(s['retry_at'],(AT+timedelta(minutes=15)).isoformat())

    def test_retry_after_http_date_and_invalid_fallback(self):
        self.assertEqual(safety.retry_time('Retry-After: Thu, 05 Nov 2026 21:25:00 GMT',AT,'rate_limit'),AT+timedelta(hours=1))
        for value in ['garbage','-1','0']:
            self.assertEqual(safety.retry_time('Retry-After: '+value,AT,'rate_limit'),AT+timedelta(hours=1))

    def test_rate_backoff_bounded(self):
        self.assertEqual([(safety.retry_time('',AT,'rate_limit',n)-AT).total_seconds()/3600 for n in [1,2,3,40]],[1,6,24,24])

    def test_duplicate_observation_id_no_duplicate_event(self):
        self.block(observation_id='same');self.block(observation_id='same');self.assertEqual(len(self.events()),1)

    def test_deferred_jobs_do_not_emit_duplicate_incidents(self):
        self.block()
        for _ in range(10):
            with self.assertRaises(safety.Blocked):self.gate.ensure(now=AT)
        self.assertEqual(len(self.events()),1)

    def test_related_observations_keep_incident_and_first_time(self):
        first=self.block();second=self.gate.observe(BOT,component='video',operation='metadata',now=AT+timedelta(minutes=1))
        self.assertEqual(first['incident_id'],second['incident_id']);self.assertEqual(second['blocking_observations'],2);self.assertEqual(second['first_observed_at'],AT.isoformat());self.assertFalse(self.events()[-1]['alertable'])

    def test_shared_state_contains_no_sensitive_raw_diagnostics(self):
        self.block(BOT+' https://www.youtube.com/x?token=SECRET\nCookie: TOPSECRET\nAuthorization: Bearer PASSWORD')
        raw=json.dumps(self.events());self.assertNotIn('SECRET',raw);self.assertNotIn('PASSWORD',raw);self.assertNotIn('https://',raw)

    def test_successful_controlled_recovery_retains_incident_history(self):
        self.block();later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later) as token:
            self.gate.begin(now=later)
            result=self.gate.recovered(token,now=later)
        self.assertEqual(result['status'],'normal');self.assertTrue(self.gate.status(later)['network_allowed']);self.assertEqual(len(self.events()),3);self.assertEqual(result['last_recovery_success'],later.isoformat())

    def test_repeated_challenge_during_recovery_needs_review(self):
        self.block();later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later):
            self.gate.observe(BOT,component='recovery',operation='probe',now=later)
        s=self.gate.status(later);self.assertEqual(s['status'],'manual_review');self.assertTrue(s['manual_review_required'])
        with self.assertRaises(safety.Blocked):
            with self.gate.recovery(authorized=True,now=later+timedelta(days=1)):pass
        with self.gate.recovery(authorized=True,reviewed=True,now=later+timedelta(days=1)) as token:self.gate.recovered(token,now=later+timedelta(days=1))
        self.assertTrue(self.gate.status(later+timedelta(days=1))['network_allowed'])

    def test_recovery_before_expiry_rejected(self):
        self.block()
        with self.assertRaises(safety.Blocked):
            with self.gate.recovery(authorized=True,now=AT):pass

    def test_one_recovery_operation_budget(self):
        self.block();later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later):
            self.gate.begin(now=later)
            with self.assertRaises(safety.Blocked):self.gate.begin(now=later)

    def test_concurrent_recovery_admission(self):
        self.block();later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later):
            def other():
                with self.assertRaises(safety.Blocked):
                    with safety.Safety(self.root).recovery(authorized=True,now=later):pass
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(other).result(timeout=3)

    def test_process_interruption_recovery_remains_closed(self):
        self.block();later=AT+timedelta(days=1)
        with self.assertRaises(KeyboardInterrupt):
            with self.gate.recovery(authorized=True,now=later):raise KeyboardInterrupt()
        self.assertFalse(self.gate.status(later)['network_allowed']);self.assertEqual(self.events()[-1]['event'],'recovery_incomplete')

    def test_ordinary_failed_probe_never_marks_recovered(self):
        self.block();later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later):pass
        self.assertIsNone(self.gate.status(later)['last_recovery_success'])

    def test_recovery_hard_process_restart_can_be_reauthorized(self):
        self.block();later=AT+timedelta(days=1)
        # An abandoned in-progress event cannot authorize a normal job. A fresh
        # explicit recovery must acquire the OS lock before replacing its token.
        with self.gate._lock():
            state=self.gate.status(later);state.update(status='recovery_in_progress',recovery_token='dead')
            self.gate._commit(state,'recovery_authorized',later)
        with self.assertRaises(safety.Blocked):self.gate.begin(now=later)
        with self.gate.recovery(authorized=True,now=later) as token:self.gate.recovered(token,now=later)
        self.assertTrue(self.gate.status(later)['network_allowed'])

    def test_concurrent_process_incident_updates_no_loss(self):
        ctx=multiprocessing.get_context('fork');processes=[ctx.Process(target=process_observe,args=(self.root,i)) for i in range(4)]
        for p in processes:p.start()
        for p in processes:p.join(5)
        for p in processes:self.assertEqual(p.exitcode,0)
        self.assertEqual(self.gate.status(AT)['blocking_observations'],4);self.assertEqual(len(self.events()),4)

    def test_atomic_interruption_keeps_previous_committed_event(self):
        self.block();before=[p.read_bytes() for p in (self.gate.root/'events').glob('*.json')]
        with patch('dls_video_downloader.youtube_safety.os.link',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.gate.observe('HTTP Error 429',now=AT)
        self.assertEqual(before,[p.read_bytes() for p in (self.gate.root/'events').glob('*.json')]);self.assertFalse(self.gate.status(AT)['network_allowed'])

    def test_malformed_latest_state_fails_closed(self):
        directory=self.gate.root/'events';directory.mkdir(parents=True);(directory/('000000000001-'+'a'*32+'.json')).write_text('{broken')
        with self.assertRaises(safety.Blocked):self.gate.status(AT)

    def test_legacy_readonly_and_import_does_not_extend_or_rewrite(self):
        directory=self.root/'captions/runner';directory.mkdir(parents=True);path=directory/'cooldown.json'
        raw=json.dumps(dict(version=1,observed_at=AT.isoformat(),until=(AT+timedelta(hours=6)).isoformat(),diagnostic=BOT)).encode();path.write_bytes(raw)
        s=self.gate.status(AT);self.assertEqual(s['retry_at'],(AT+timedelta(hours=6)).isoformat());self.assertFalse(self.gate.root.exists())
        self.gate.import_legacy(AT);self.assertEqual(path.read_bytes(),raw);self.assertEqual(self.gate.status(AT)['retry_at'],s['retry_at'])
        self.gate.import_legacy(AT);self.assertEqual(len(self.events()),1)

    def test_expired_legacy_does_not_silently_clear(self):
        p=self.root/'comments/runner';p.mkdir(parents=True);(p/'cooldown.json').write_text(json.dumps(dict(version=1,until=(AT-timedelta(seconds=1)).isoformat())))
        self.assertEqual(self.gate.status(AT)['status'],'recovery_eligible')
        self.assertFalse(self.gate.root.exists())

    def test_recovery_acknowledges_legacy_without_changing_it(self):
        p=self.root/'comments/runner';p.mkdir(parents=True);file=p/'cooldown.json';file.write_text(json.dumps(dict(version=1,until=AT.isoformat())));raw=file.read_bytes()
        with self.gate.recovery(authorized=True,now=AT) as token:self.gate.recovered(token,now=AT)
        self.assertTrue(self.gate.status(AT)['network_allowed']);self.assertEqual(file.read_bytes(),raw)

    def test_corrupt_legacy_fails_closed(self):
        p=self.root/'comments/runner';p.mkdir(parents=True);(p/'cooldown.json').write_text('corrupt')
        with self.assertRaises(safety.Blocked):self.gate.status(AT)

    def test_review_due_event_deduplicated_and_status_readonly(self):
        self.block();later=AT+timedelta(days=1);self.assertTrue(self.gate.status(later)['review_overdue']);self.assertEqual(len(self.events()),1)
        self.gate.review_event(later);self.gate.review_event(later);self.assertEqual(len(self.events()),2);self.assertEqual(self.events()[-1]['event'],'unresolved_review_due')

    def test_caption_origin_block_defers_video_before_subprocess(self):
        self.block()
        with safety.scope(self.root,'video'),patch('subprocess.Popen') as process:
            with self.assertRaises(safety.Blocked):media.run([media.YT_DLP,'https://www.youtube.com/watch?v='+VIDEO],self.root/'log',30)
        process.assert_not_called()

    def test_actual_fake_child_detection_defers_other_components(self):
        lock=self.root/'child.lock'
        with lock.open('w') as stream,safety.scope(self.root,'captions'):
            command=[sys.executable,'-B','-c',"import sys;sys.stderr.write(\"LOGIN_REQUIRED: Sign in to confirm you're not a bot\");sys.exit(1)",'https://www.youtube.com/watch?v='+VIDEO]
            result=network.run_extractor(command,10,stream.fileno())
        self.assertEqual(result.returncode,1);self.assertFalse(self.gate.status()['network_allowed'])
        with patch.object(network,'run_extractor') as request:
            with self.assertRaises(safety.Blocked):network.collect(VIDEO,root=self.root/'comments')
        request.assert_not_called();self.assertFalse((self.root/'comments/comments.sqlite3').exists())

    def test_video_origin_block_defers_supplemental_all_phases(self):
        self.gate.observe(BOT,component='video',operation='metadata',now=AT)
        with patch.object(media,'run') as request,patch.object(comment_runner,'collect') as comments,patch.object(caption_collection,'collect') as captions:
            r=cycle.run(self.path,self.root/'comments',execute=True,discover=True,captions=True,clock=lambda:AT)
        self.assertEqual(r['outcome'],'deferred');self.assertEqual(r['exit_code'],75);request.assert_not_called();comments.assert_not_called();captions.assert_not_called()
        self.assertFalse((self.root/'supplemental').exists())

    def test_video_workflow_blocked_before_cleanup_or_retry_writes(self):
        self.block();before=self.path.read_bytes()
        from dls_video_downloader.discovery import workflow
        with patch.object(media,'run') as request,patch.object(media,'PROJECT',self.root.resolve()):
            with self.assertRaises(safety.Blocked):workflow(self.db,state=self.root)
        self.assertEqual(before,self.path.read_bytes());request.assert_not_called()

    def test_caption_payload_boundary_checks_new_global_block(self):
        self.block()
        with safety.scope(self.root,'captions'),patch('subprocess.Popen') as process:
            with self.assertRaises(safety.Blocked):caption_collection.fetch('https://www.youtube.com/api/timedtext',10,0)
        process.assert_not_called()

    def test_local_ffprobe_verification_not_blocked(self):
        self.block()
        with safety.scope(self.root,'video'):
            value=media.run([sys.executable,'-c','print("local verification")'],self.root/'probe.log',5)
        self.assertIn('local verification',value)

    def test_global_block_keeps_existing_asset_bytes(self):
        p=self.root/'acquisitions'/VIDEO/'media.mkv';p.parent.mkdir(parents=True);p.write_bytes(b'existing immutable media')
        self.block()
        with self.assertRaises(safety.Blocked):network.collect(VIDEO,root=self.root/'comments')
        self.assertEqual(p.read_bytes(),b'existing immutable media')

    def test_readonly_plan_distinguishes_due_but_blocked(self):
        self.block();before=len(self.events())
        r=cycle.run(self.path,self.root/'comments',clock=lambda:AT)
        self.assertEqual(r['youtube_safety']['status'],'cooldown');self.assertEqual(r['phases']['comments']['plan']['due_ids'],[])
        self.assertIn(VIDEO,r['phases']['comments']['plan']['deferred_due_ids']);self.assertEqual(len(self.events()),before)
        self.assertFalse((self.root/'captions').exists());self.assertFalse((self.root/'supplemental').exists())

    def test_controlled_recovery_cli_flags_do_not_launch_by_default(self):
        self.block()
        with patch.object(network,'run_extractor') as request,redirect_stdout(io.StringIO()):
            code=main(['--db',str(self.path),'youtube-safety','recover',VIDEO,'--json'])
        self.assertEqual(code,0);request.assert_not_called()

    def test_controlled_recovery_success_reuses_guard_and_preserves_no_assets(self):
        self.block();later=AT+timedelta(days=1)
        response=network.Execution(stdout=json.dumps(dict(id=VIDEO,live_status='was_live')).encode(),returncode=0)
        with patch.object(network,'run_extractor',return_value=response) as request:
            result=recover(self.gate,VIDEO,authorized=True,clock=lambda:later)
        self.assertEqual(result['status'],'normal');self.assertEqual(request.call_count,1);self.assertEqual(request.call_args.args[1],60)
        self.assertFalse((self.root/'captions').exists());self.assertFalse((self.root/'comments').exists())

    def test_controlled_recovery_repeated_bot_is_manual_review(self):
        self.block();later=AT+timedelta(days=1)
        with patch.object(network,'run_extractor',return_value=network.Execution(stderr=BOT.encode(),returncode=1)) as request:
            with self.assertRaises(safety.Blocked):recover(self.gate,VIDEO,authorized=True,clock=lambda:later)
        self.assertEqual(request.call_count,1);self.assertTrue(self.gate.status(later)['manual_review_required'])

    def test_controlled_recovery_success_requires_valid_identity(self):
        self.block();later=AT+timedelta(days=1)
        response=network.Execution(stdout=b'{"id":"wrong","live_status":"was_live"}',returncode=0)
        with patch.object(network,'run_extractor',return_value=response):
            with self.assertRaises(safety.Blocked):recover(self.gate,VIDEO,authorized=True,clock=lambda:later)
        self.assertIsNone(self.gate.status(later)['last_recovery_success']);self.assertFalse(self.gate.status(later)['network_allowed'])

    def test_recovery_rate_limit_backoff_without_bot_review_escalation(self):
        self.block('HTTP Error 429');later=AT+timedelta(hours=1)
        with self.gate.recovery(authorized=True,now=later):self.gate.observe('HTTP Error 429\nRetry-After: 7200',now=later)
        state=self.gate.status(later);self.assertFalse(state['manual_review_required']);self.assertEqual(state['retry_at'],(later+timedelta(hours=2)).isoformat())

    def test_nonadjacent_duplicate_observation_deduplicated(self):
        self.block(observation_id='one');self.block(observation_id='two');self.block(observation_id='one')
        self.assertEqual(len(self.events()),2)

    def test_atomic_keyboard_interrupt_never_publishes_partial_json(self):
        self.block();before=len(self.events())
        with patch('dls_video_downloader.youtube_safety.os.link',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.gate.observe(BOT,now=AT)
        self.assertEqual(len(self.events()),before);self.assertFalse(self.gate.status(AT)['network_allowed'])

    def test_environment_cooldown_configuration_is_bounded(self):
        with patch.dict('os.environ',{'DLS_YOUTUBE_BOT_COOLDOWN_HOURS':'30'}):
            gate=safety.Safety(self.root);state=gate.observe(BOT,now=AT)
        self.assertEqual(state['retry_at'],(AT+timedelta(hours=30)).isoformat())
        with patch.dict('os.environ',{'DLS_YOUTUBE_BOT_COOLDOWN_HOURS':'garbage'}):
            with self.assertRaises(safety.Blocked):safety.Safety(self.root)

    def test_custom_asset_root_cannot_bypass_inventory_safety_scope(self):
        self.block()
        with tempfile.TemporaryDirectory() as output,safety.scope(self.root,'manual-comments'),patch.object(network,'run_extractor') as request:
            with self.assertRaises(safety.Blocked):network.collect(VIDEO,root=Path(output)/'comments')
        request.assert_not_called()

    def test_first_discovery_block_stops_before_second_tab_or_acquisition(self):
        from dls_video_downloader.discovery import workflow
        calls=[]
        def blocked(args,log,timeout):
            calls.append(args);self.block();raise safety.Blocked('shared_youtube_cooldown: authentication_challenge')
        with patch.object(media,'PROJECT',self.root.resolve()),patch.object(media,'run',side_effect=blocked):
            result=workflow(self.db,state=self.root,acquire_media=True)
        self.assertEqual(len(calls),1);self.assertEqual(result['status'],'deferred');self.assertEqual(result['failures'],[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM retry_state').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM acquisitions').fetchone()[0],0)

    def test_stale_runner_plan_rechecks_shared_block_before_collection(self):
        from dls_video_downloader import comment_planning
        real=comment_planning.plan;count=0
        def changing(*a,**kw):
            nonlocal count
            value=real(*a,**kw);count+=1
            if count==1:self.block()
            return value
        with patch.object(comment_runner,'plan',side_effect=changing),patch.object(comment_runner,'collect') as request:
            result=comment_runner.run(self.path,self.root/'comments',execute=True,clock=lambda:AT)
        request.assert_not_called();self.assertEqual(result['exit_code'],75);self.assertFalse((self.root/'comments/comments.sqlite3').exists())

    def test_state_write_failure_latches_process_closed(self):
        with patch('dls_video_downloader.youtube_safety.os.link',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.block()
        self.assertFalse(self.gate.status(AT)['network_allowed'])
        with safety.scope(self.root),patch('subprocess.Popen') as process:
            with self.assertRaises(safety.Blocked):media.run([media.YT_DLP],self.root/'log',10)
        process.assert_not_called()

    def test_shared_state_update_does_not_hold_a_media_lock(self):
        lock=self.root/'acquisitions/.acquisition.lock';lock.parent.mkdir();lock.write_bytes(b'')
        import fcntl
        with lock.open('r') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.block()
        self.assertFalse(self.gate.status(AT)['network_allowed'])

    def test_no_schema_change_when_video_cli_defers(self):
        self.block();before=self.path.read_bytes()
        with redirect_stderr(io.StringIO()),patch.object(media,'run') as request:
            result=main(['--db',str(self.path),'run'])
        self.assertEqual(result,75);self.assertEqual(self.path.read_bytes(),before);request.assert_not_called()

    def test_new_incident_after_recovery_resets_related_backoff(self):
        self.block('HTTP Error 429');later=AT+timedelta(hours=1)
        with self.gate.recovery(authorized=True,now=later) as token:self.gate.recovered(token,now=later)
        state=self.gate.observe('HTTP Error 429',now=later)
        self.assertEqual(state['incident_count'],2);self.assertEqual(state['related_blocking_observations'],1)
        self.assertEqual(state['retry_at'],(later+timedelta(hours=1)).isoformat())

    def test_reused_request_id_after_recovery_cannot_hide_a_new_incident(self):
        self.block(observation_id='repeat');later=AT+timedelta(days=1)
        with self.gate.recovery(authorized=True,now=later) as token:self.gate.recovered(token,now=later)
        self.gate.observe(BOT,now=later,observation_id='repeat')
        self.assertFalse(self.gate.status(later)['network_allowed']);self.assertEqual(self.gate.status(later)['incident_count'],2)

    def test_controlled_recovery_yields_to_active_acquisition(self):
        self.block();later=AT+timedelta(days=1);path=self.root/'acquisitions/.acquisition.lock';path.parent.mkdir();path.write_bytes(b'')
        import fcntl
        with path.open('r') as lock,patch.object(network,'run_extractor') as request:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(safety.Blocked):recover(self.gate,VIDEO,authorized=True,clock=lambda:later)
        request.assert_not_called();self.assertEqual(len(self.events()),1)

    def test_new_legacy_restriction_during_probe_cannot_be_acknowledged_away(self):
        self.block();later=AT+timedelta(days=1)
        with self.assertRaises(safety.Blocked):
            with self.gate.recovery(authorized=True,now=later) as token:
                directory=self.root/'captions/runner';directory.mkdir(parents=True)
                (directory/'cooldown.json').write_text(json.dumps(dict(version=1,until=(later+timedelta(hours=6)).isoformat())))
                self.gate.recovered(token,now=later)
        self.assertFalse(self.gate.status(later)['network_allowed']);self.assertIsNone(self.gate.status(later)['last_recovery_success'])
        self.assertEqual(self.gate.status(later)['retry_at'],(later+timedelta(hours=6)).isoformat())

    def test_naive_times_rejected(self):
        with self.assertRaises(ValueError):self.gate.status(AT.replace(tzinfo=None))


if __name__=='__main__':unittest.main()
