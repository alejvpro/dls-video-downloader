"""Unattended dispatch acceptance tests: disposable state and mocked provider."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from dls_video_downloader import notifications as n,pushover
from dls_video_downloader.youtube_safety import Safety
from dls_video_downloader.cli import main

AT=datetime(2026,11,5,20,tzinfo=timezone.utc)
BOT="LOGIN_REQUIRED: Sign in to confirm you're not a bot"

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)/'state';self.now=AT
        self.loader=patch.object(pushover,'load_credentials',return_value=pushover.Credentials('T'*30,'U'*30)).start()
        self.sender=patch.object(pushover,'deliver',return_value={'outcome':'accepted','http_status':200}).start()
        self.addCleanup(patch.stopall)
    def poll(self,**kw):return n.dispatch(self.state,clock=lambda:self.now,**kw)
    def block(self):Safety(self.state).observe(BOT,now=self.now)
    def test_missing_journal_empty_execution_no_writes_or_credentials(self):
        r=self.poll(execute=True);self.assertFalse(self.state.exists());self.loader.assert_not_called();self.sender.assert_not_called()
        self.assertFalse(r['source_journal_present']);self.assertEqual(r['summary']['attempted'],0)
    def test_default_read_only(self):
        self.block();before={p:p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        self.poll();self.assertFalse((self.state/'notifications').exists());self.loader.assert_not_called()
        self.assertEqual(before,{p:p.read_bytes() for p in self.state.rglob('*') if p.is_file()})
    def test_block_does_not_block_alert_and_repeated_polls_do_not_spam(self):
        self.block();self.assertEqual(self.poll(execute=True)['summary']['accepted'],1)
        for _ in range(10):self.poll(execute=True)
        self.assertEqual(self.sender.call_count,1)
    def test_legacy_evidence_not_imported(self):
        self.state.mkdir();(self.state/'caption-cooldown.json').write_text('{"reason":"LOGIN_REQUIRED"}')
        self.poll(execute=True);self.assertFalse((self.state/'youtube-safety').exists());self.sender.assert_not_called()
    def test_failure_backoff_and_safe_retry(self):
        self.block();self.sender.return_value={'outcome':'retryable','error':'worker_launch_failed'}
        self.poll(execute=True);self.poll(execute=True);self.assertEqual(self.sender.call_count,1)
        self.now+=timedelta(minutes=5);self.sender.return_value={'outcome':'accepted','http_status':200}
        self.assertEqual(self.poll(execute=True)['summary']['accepted'],1)
    def test_uncertainty_never_automatically_retries(self):
        self.block();self.sender.return_value={'outcome':'uncertain','error':'timeout'}
        self.poll(execute=True);self.now+=timedelta(days=10)
        r=self.poll(execute=True);self.assertEqual(self.sender.call_count,1);self.assertEqual(r['summary']['review_required'],1)
    def test_rejection_holds_provider(self):
        self.block();self.sender.return_value={'outcome':'rejected','error':'provider_rejected'}
        self.poll(execute=True);self.poll(execute=True);self.assertEqual(self.sender.call_count,1)
    def test_batch_and_escalation(self):
        self.block();self.now+=timedelta(days=1)
        gate=Safety(self.state)
        with gate.recovery(authorized=True,now=self.now):gate.observe(BOT,now=self.now)
        r=self.poll(execute=True,max_messages=1);self.assertEqual(r['summary']['attempted'],1);self.assertEqual(r['summary']['pending_due'],1)
        self.poll(execute=True);self.assertEqual(self.sender.call_count,2)
    def test_verified_recovery_informational(self):
        self.block();self.now+=timedelta(days=1);gate=Safety(self.state)
        with gate.recovery(authorized=True,now=self.now) as token:gate.recovered(token,now=self.now)
        r=self.poll(execute=True,max_messages=5);self.assertEqual(r['summary']['accepted'],2)
        self.assertIn('informational',[x['level'] for x in r['items']])
    def test_rate_limit_warning(self):
        Safety(self.state).observe('HTTP Error 429',now=self.now)
        self.assertEqual(self.poll()['items'][0]['level'],'warning')
    def test_expiry_not_recovery(self):
        self.block();self.poll(execute=True);self.now+=timedelta(days=2)
        self.poll(execute=True);self.assertEqual(self.sender.call_count,1)
    def test_sender_contention(self):
        self.block()
        with n.worker_lock(n.root_for(self.state)):
            r=self.poll(execute=True)
        self.assertEqual(r['error'],'notification_sender_busy');self.sender.assert_not_called()
    def test_restart_inflight_is_uncertain(self):
        self.block()
        with n.worker_lock(n.root_for(self.state)),n.Catalog(n.root_for(self.state)) as store:
            store.ingest(n.events(self.state),AT);store.begin(n.events(self.state)[0]['notification_id'],AT)
        self.assertEqual(self.poll(execute=True)['summary']['review_required'],1);self.sender.assert_not_called()
    def test_deadline_no_send(self):
        self.block();ticks=iter([0,59])
        r=self.poll(execute=True,runtime_seconds=60,monotonic=lambda:next(ticks))
        self.sender.assert_not_called();self.assertEqual(r['work'][0]['reason'],'runtime_budget')
    def test_dispatch_cli_default_no_state(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--db',str(self.state/'inventory.sqlite3'),'notifications','dispatch','--json']),0)
        self.assertEqual(json.loads(output.getvalue())['operation'],'incident_dispatch');self.assertFalse(self.state.exists())
    def test_operator_tests_not_allowed(self):
        with self.assertRaises(ValueError):self.poll(operator_test='not-allowed')

    def test_concurrent_workers_one_attempt(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.block();entered=threading.Event();release=threading.Event()
        def delivery(*args):
            entered.set();release.wait(3);return {'outcome':'accepted','http_status':200}
        self.sender.side_effect=delivery
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.poll,execute=True);self.assertTrue(entered.wait(2))
            second=pool.submit(self.poll,execute=True).result()
            release.set();self.assertEqual(first.result()['summary']['accepted'],1)
        self.assertEqual(second['error'],'notification_sender_busy');self.assertEqual(self.sender.call_count,1)
    def test_journal_unchanged_by_provider_failure(self):
        self.block();root=self.state/'youtube-safety'
        before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.sender.return_value={'outcome':'retryable','error':'worker_launch_failed'}
        self.poll(execute=True)
        self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})
    def test_retry_budget_exhausted(self):
        self.block();self.sender.return_value={'outcome':'retryable','error':'worker_launch_failed'}
        for _ in range(8):
            self.poll(execute=True);self.now+=timedelta(hours=2)
        self.assertEqual(self.sender.call_count,5)
        self.assertEqual(self.poll()['items'][0]['status'],'review')

    def test_report_bound_keeps_full_counts(self):
        values=[dict(n.test_alert('synthetic-'+str(i),AT),kind='incident_opened') for i in range(60)]
        with patch.object(n,'events',return_value=values):r=self.poll()
        self.assertEqual(len(r['items']),50);self.assertEqual(r['summary']['pending_due'],60)
        self.assertEqual(r['report_omitted']['items'],10)
