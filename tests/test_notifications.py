"""Offline DLS Pushover acceptance tests; synthetic credentials, no delivery."""
from contextlib import redirect_stdout,redirect_stderr
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from urllib import error,parse

from dls_video_downloader import notifications as notify,pushover
from dls_video_downloader.youtube_safety import Safety
from dls_video_downloader.cli import main

AT=datetime(2026,11,5,20,tzinfo=timezone.utc)
BOT="LOGIN_REQUIRED: Sign in to confirm you're not a bot"
ACCEPTED=dict(outcome='accepted',http_status=200,provider_request_id='12345678-1234-1234-1234-123456789abc')


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.state=self.base/'state';self.gate=Safety(self.state);self.now=AT;self.elapsed=0
        directory=self.base/'private';directory.mkdir(mode=0o700);self.config=directory/'pushover.json'
        self.config.write_text(json.dumps(dict(token='T'*30,user='U'*30)));self.config.chmod(0o600)
        self.credentials=pushover.Credentials('T'*30,'U'*30)
        self.delivery=patch.object(pushover,'deliver',return_value=dict(ACCEPTED));self.mock_delivery=self.delivery.start();self.addCleanup(self.delivery.stop)

    def block(self):return self.gate.observe(BOT,component='captions',operation='listing',now=self.now)
    def run_sender(self,**kwargs):return notify.run(self.state,config=self.config,clock=lambda:self.now,monotonic=lambda:self.elapsed,**kwargs)
    def rows(self):return notify.read_rows(notify.root_for(self.state))
    def db(self):return sqlite3.connect(notify.root_for(self.state)/'deliveries.sqlite3')

    def test_initial_bot_alert_critical_plain_safe_template(self):
        self.block();plan=notify.plan(self.state,now=AT);self.assertEqual(len(plan['due_ids']),1)
        row=plan['items'][0];self.assertEqual(row['level'],'critical');self.assertNotIn('https://',row['message']);self.assertNotIn(str(self.state),row['message']);self.assertNotIn(BOT,row['message'])

    def test_rate_limit_initial_alert_is_warning(self):
        self.gate.observe('HTTP Error 429',now=AT);self.assertEqual(notify.plan(self.state,now=AT)['items'][0]['level'],'warning')

    def test_deferred_jobs_do_not_duplicate_alerts(self):
        self.block()
        for _ in range(5):self.gate.status(AT)
        self.assertEqual(len(notify.plan(self.state,now=AT)['items']),1)

    def test_related_blocking_observations_not_repeated_alerts(self):
        self.block();self.gate.observe(BOT,now=AT+timedelta(minutes=1))
        self.assertEqual(len(notify.plan(self.state,now=AT+timedelta(minutes=1))['items']),1)

    def test_manual_review_escalation_distinct_from_initial(self):
        self.block();self.now+=timedelta(days=1)
        with self.gate.recovery(authorized=True,now=self.now):self.gate.observe(BOT,now=self.now)
        rows=notify.plan(self.state,now=self.now)['items'];self.assertEqual(len(rows),2);self.assertEqual({r['level'] for r in rows},{'critical'})

    def test_verified_recovery_is_distinct_informational_alert(self):
        self.block();self.now+=timedelta(days=1)
        with self.gate.recovery(authorized=True,now=self.now) as token:self.gate.recovered(token,now=self.now)
        rows=notify.plan(self.state,now=self.now)['items'];self.assertEqual(len(rows),2)
        recovered=next(r for r in rows if r['kind']=='controlled_recovery_succeeded');self.assertEqual(recovered['level'],'informational');self.assertIn('does not',recovered['message'])

    def test_expiry_alone_does_not_generate_recovery_alert(self):
        self.block();self.now+=timedelta(days=1);self.gate.status(self.now)
        self.assertEqual(len(notify.plan(self.state,now=self.now)['items']),1)

    def test_unresolved_event_is_warning_once(self):
        self.block();self.now+=timedelta(days=1);self.gate.review_event(self.now);self.gate.review_event(self.now)
        rows=notify.plan(self.state,now=self.now)['items'];self.assertEqual(len(rows),2);self.assertEqual(next(r for r in rows if r['kind']=='unresolved_review_due')['level'],'warning')

    def test_unknown_event_not_invented(self):
        self.assertIsNone(notify.alert(dict(version=1,alertable=True,event_id='x',event='integrity_failure',at=AT.isoformat(),state_after=dict(incident_id='i'))))
        self.assertEqual(notify.LEVELS,('critical','warning','informational'))

    def test_plan_and_default_run_no_config_read_or_runtime_write(self):
        self.block()
        with patch.object(pushover,'load_credentials',side_effect=AssertionError('Credential access forbidden')):
            self.run_sender();notify.plan(self.state,now=AT)
        self.assertFalse(notify.root_for(self.state).exists());self.mock_delivery.assert_not_called()

    def test_empty_execution_no_credentials_or_state_initialization(self):
        with patch.object(pushover,'load_credentials',side_effect=AssertionError('No credentials needed')):self.run_sender(execute=True)
        self.assertFalse(notify.root_for(self.state).exists())

    def test_valid_private_configuration(self):
        config=pushover.load_credentials(self.config);self.assertEqual(config.token,'T'*30);self.assertNotIn('T'*30,repr(config))

    def test_missing_credentials_safe_error_no_delivery(self):
        self.block();r=notify.run(self.state,execute=True,config=self.base/'absent/config.json',clock=lambda:AT)
        self.assertEqual(r['error'],'credentials_missing');self.mock_delivery.assert_not_called();self.assertFalse(notify.root_for(self.state).exists())

    def test_group_readable_file_rejected(self):
        self.config.chmod(0o640)
        with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(self.config)

    def test_nonprivate_directory_rejected(self):
        self.config.parent.chmod(0o755)
        with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(self.config)

    def test_symlink_file_rejected_without_reading_target(self):
        alias=self.config.parent/'alias.json';alias.symlink_to(self.config)
        with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(alias)

    def test_symlink_directory_rejected(self):
        alias=self.base/'alias';alias.symlink_to(self.config.parent)
        with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(alias/'pushover.json')

    def test_hardlinked_config_rejected(self):
        os.link(self.config,self.config.parent/'copy.json')
        with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(self.config)

    def test_malformed_config_error_does_not_echo_contents(self):
        self.config.write_text('SECRET_TOKEN_MUST_NOT_APPEAR')
        with self.assertRaises(pushover.ConfigurationError) as error:pushover.load_credentials(self.config)
        self.assertNotIn('SECRET',str(error.exception))

    def test_repository_and_reserved_source_rejected_before_open(self):
        with patch.object(pushover.os,'open',side_effect=AssertionError('Do not access reserved credentials')):
            for path in [Path.home()/'pushover.txt',pushover.PROJECT/'credentials.json']:
                with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(path)

    def test_success_is_durably_accepted_not_device_confirmed(self):
        self.block();r=self.run_sender(execute=True);self.assertEqual(r['work'][0]['outcome'],'accepted');self.assertEqual(self.rows()[0]['status'],'accepted')
        with self.db() as db:
            self.assertEqual(db.execute('SELECT outcome FROM attempts').fetchone()[0],'accepted');self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(self.mock_delivery.call_count,1)

    def test_repeated_execution_not_resent(self):
        self.block();self.run_sender(execute=True);self.run_sender(execute=True);self.assertEqual(self.mock_delivery.call_count,1)

    def test_semantic_and_event_id_deduplication(self):
        self.block();event=notify.events(self.state)[0];other=dict(event,source_event_id='another-event')
        root=notify.root_for(self.state)
        with notify.worker_lock(root),notify.Catalog(root) as store:store.ingest([event,event,other],AT)
        with self.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notifications').fetchone()[0],1);self.assertEqual(db.execute('SELECT count(*) FROM event_links').fetchone()[0],2)

    def test_provider_rejection_stops_batch_and_needs_operator(self):
        self.block();self.mock_delivery.return_value=dict(outcome='rejected',error='provider_rejected',http_status=400)
        r=self.run_sender(execute=True);self.assertEqual(r['exit_code'],2);self.assertEqual(self.rows()[0]['status'],'rejected');self.assertEqual(self.run_sender(execute=True)['selected_ids'],[])

    def test_temporary_failure_has_bounded_retry(self):
        self.block();self.mock_delivery.return_value=dict(outcome='retryable',error='provider_temporary',http_status=503)
        self.run_sender(execute=True);row=self.rows()[0];self.assertEqual(row['next_due_at'],(AT+timedelta(minutes=1)).isoformat())
        self.assertFalse(self.run_sender(execute=True)['selected_ids'])
        self.now+=timedelta(minutes=1);self.mock_delivery.return_value=dict(ACCEPTED);self.run_sender(execute=True);self.assertEqual(self.rows()[0]['status'],'accepted')

    def test_five_temporary_failures_suspend(self):
        self.block();self.mock_delivery.return_value=dict(outcome='retryable',error='provider_temporary')
        for _ in range(5):
            self.run_sender(execute=True);row=self.rows()[0]
            if row['next_due_at']:self.now=notify.instant(row['next_due_at'])
        self.assertEqual(row['status'],'review');self.assertIsNone(row['next_due_at'])

    def test_timeout_uncertainty_never_auto_resends(self):
        self.block();self.mock_delivery.return_value=dict(outcome='uncertain',error='worker_timeout');self.run_sender(execute=True)
        self.now+=timedelta(days=1);self.run_sender(execute=True);self.assertEqual(self.mock_delivery.call_count,1);self.assertEqual(self.rows()[0]['status'],'uncertain')

    def test_uncertain_retry_requires_explicit_duplicate_risk_ack(self):
        self.block();self.mock_delivery.return_value=dict(outcome='uncertain',error='network_uncertain');self.run_sender(execute=True);key=self.rows()[0]['notification_id']
        with self.assertRaises(ValueError):notify.retry(self.state,key,execute=True,now=AT)
        notify.retry(self.state,key,execute=True,acknowledge_duplicate_risk=True,now=AT);self.assertEqual(self.rows()[0]['status'],'pending')
        self.assertEqual(self.mock_delivery.call_count,1)

    def test_accepted_notification_cannot_be_retried(self):
        self.block();self.run_sender(execute=True)
        with self.assertRaises(ValueError):notify.retry(self.state,self.rows()[0]['notification_id'],execute=True)

    def test_known_rejected_retry_is_separate_network_free_action(self):
        self.block();self.mock_delivery.return_value=dict(outcome='rejected',error='http_rejected');self.run_sender(execute=True);key=self.rows()[0]['notification_id']
        notify.retry(self.state,key,execute=True,now=AT);self.assertEqual(self.rows()[0]['status'],'pending');self.assertEqual(self.mock_delivery.call_count,1)

    def test_crash_restart_marks_inflight_uncertain_without_resending(self):
        self.block();root=notify.root_for(self.state)
        with notify.worker_lock(root),notify.Catalog(root) as store:
            store.ingest(notify.events(self.state),AT);store.begin(self.rows()[0]['notification_id'],AT)
        with patch.object(pushover,'load_credentials',side_effect=AssertionError('No network work pending')):self.run_sender(execute=True)
        self.assertEqual(self.rows()[0]['status'],'uncertain');self.mock_delivery.assert_not_called()

    def test_attempt_committed_before_network_call(self):
        self.block()
        def delivery(*args):
            with self.db() as db:self.assertEqual(db.execute('SELECT outcome FROM attempts').fetchone()[0],'inflight')
            return dict(ACCEPTED)
        self.mock_delivery.side_effect=delivery;self.run_sender(execute=True)

    def test_crash_after_provider_acceptance_never_claims_uncommitted_success(self):
        self.block()
        with patch.object(notify.Catalog,'finish',side_effect=sqlite3.OperationalError('disk unavailable')):r=self.run_sender(execute=True)
        self.assertEqual(r['exit_code'],1);self.assertEqual(self.rows()[0]['status'],'inflight')
        self.run_sender(execute=True);self.assertEqual(self.rows()[0]['status'],'uncertain');self.assertEqual(self.mock_delivery.call_count,1)

    def test_concurrent_workers_cannot_send_same_event(self):
        self.block();started=threading.Event();finish=threading.Event()
        def delivery(*args):started.set();finish.wait(3);return dict(ACCEPTED)
        self.mock_delivery.side_effect=delivery
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.run_sender,execute=True);self.assertTrue(started.wait(2))
            second=self.run_sender(execute=True);self.assertEqual(second['exit_code'],75);finish.set();first.result(timeout=3)
        self.assertEqual(self.mock_delivery.call_count,1)

    def test_notification_attempts_never_modify_safety_events(self):
        self.block();paths=list((self.gate.root/'events').glob('*.json'));before={p:p.read_bytes() for p in paths}
        self.mock_delivery.return_value=dict(outcome='uncertain',error='network_uncertain');self.run_sender(execute=True)
        self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_test_message_is_separate_and_idempotent(self):
        self.run_sender(execute=True,operator_test='validation-1');self.run_sender(execute=True,operator_test='validation-1')
        self.assertEqual(self.mock_delivery.call_count,1);self.assertEqual(self.rows()[0]['kind'],'operator_test');self.assertIsNone(self.rows()[0]['incident_id']);self.assertFalse(self.gate.root.exists())

    def test_test_command_does_not_send_pending_incident_alerts(self):
        self.block();r=self.run_sender(execute=True,operator_test='validation-1');self.assertEqual(len(r['work']),1);self.assertEqual(self.rows()[0]['kind'],'operator_test')

    def test_max_message_and_runtime_budget(self):
        self.block();self.now+=timedelta(days=1);self.gate.review_event(self.now)
        def delivery(*args):self.elapsed=60;return dict(ACCEPTED)
        self.mock_delivery.side_effect=delivery;r=self.run_sender(execute=True,max_messages=2);self.assertEqual(self.mock_delivery.call_count,1);self.assertEqual(r['work'][-1]['outcome'],'deferred')

    def test_invalid_limits_no_runtime_writes(self):
        for limit in [0,6]:
            with self.assertRaises(ValueError):self.run_sender(execute=True,max_messages=limit)
        self.assertFalse(notify.root_for(self.state).exists())

    def test_cli_planning_never_reads_production_credentials(self):
        self.block()
        with patch.object(pushover,'load_credentials',side_effect=AssertionError('No credential access')):
            for command in ['plan','status','run']:
                with redirect_stdout(io.StringIO()):self.assertEqual(main(['--db',str(self.state/'inventory.sqlite3'),'notifications',command,'--json']),0)
        self.assertFalse(notify.root_for(self.state).exists())

    def test_cli_test_requires_execute_and_has_one_message_budget(self):
        with redirect_stdout(io.StringIO()):self.assertEqual(main(['--db',str(self.state/'inventory.sqlite3'),'notifications','test','--test-id','one','--json']),0)
        self.mock_delivery.assert_not_called()
        with redirect_stdout(io.StringIO()):code=main(['--db',str(self.state/'inventory.sqlite3'),'notifications','test','--test-id','one','--config',str(self.config),'--execute','--json'])
        self.assertEqual(code,0);self.assertEqual(self.mock_delivery.call_count,1)

    def test_no_notification_called_by_archive_safety_detection(self):
        self.block();self.mock_delivery.assert_not_called();self.assertFalse(notify.root_for(self.state).exists())

    def test_rejection_pauses_other_pending_alerts_until_explicit_requeue(self):
        self.block();self.now+=timedelta(days=1);self.gate.review_event(self.now)
        self.mock_delivery.return_value=dict(outcome='rejected',error='http_rejected',http_status=400)
        self.run_sender(execute=True,max_messages=2);self.assertEqual(self.mock_delivery.call_count,1)
        again=self.run_sender(execute=True);self.assertEqual(again['exit_code'],75);self.assertTrue(again['provider_hold']);self.assertEqual(self.mock_delivery.call_count,1)
        rejected=next(r for r in self.rows() if r['status']=='rejected')
        notify.retry(self.state,rejected['notification_id'],execute=True,now=self.now)
        self.assertIsNone(notify.plan(self.state,now=self.now)['provider_hold'])

    def test_status_includes_operator_tests_and_attempt_history(self):
        self.run_sender(execute=True,operator_test='validation')
        status=notify.plan(self.state,now=AT,include_tests=True)
        self.assertEqual(status['items'][0]['status'],'accepted');self.assertEqual(status['attempts'][0]['outcome'],'accepted')
        self.assertEqual(notify.plan(self.state,now=AT)['items'],[])

    def test_result_items_reflect_post_delivery_state(self):
        self.block();result=self.run_sender(execute=True)
        self.assertEqual(result['items'][0]['status'],'accepted');self.assertEqual(result['due_ids'],[])

    def test_pushover_quota_does_not_create_youtube_block(self):
        self.mock_delivery.return_value=dict(outcome='rejected',error='http_rejected',http_status=429)
        self.run_sender(execute=True,operator_test='quota')
        self.assertFalse(self.gate.root.exists());self.assertTrue(notify.plan(self.state,now=AT)['provider_hold'])

    def test_private_config_rejects_wrong_ownership_or_key_shapes(self):
        with patch.object(pushover.os,'getuid',return_value=os.getuid()+1):
            with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(self.config)
        for data in [dict(token='short',user='U'*30),dict(token='T'*30,user='U'*30,device='invalid device'),dict(token='T'*30,user='U'*30,extra='not supported')]:
            self.config.write_text(json.dumps(data))
            with self.assertRaises(pushover.ConfigurationError):pushover.load_credentials(self.config)

    def test_alert_contains_observation_time_not_a_current_status_claim(self):
        self.block();row=notify.plan(self.state,now=AT)['items'][0]
        self.assertIn('Observed UTC '+AT.isoformat(),row['message']);self.assertIn('was restricted',row['message'])

    def test_critical_alerts_precede_older_warnings(self):
        self.gate.observe('HTTP Error 429',now=AT);self.now+=timedelta(hours=1)
        with self.gate.recovery(authorized=True,now=self.now) as token:self.gate.recovered(token,now=self.now)
        self.now+=timedelta(minutes=1);self.block()
        result=notify.plan(self.state,now=self.now);first=next(r for r in result['items'] if r['notification_id']==result['due_ids'][0])
        self.assertEqual(first['level'],'critical')

    def test_missing_journal_does_not_claim_all_clear(self):
        result=notify.plan(self.state,now=AT);self.assertFalse(result['source_journal_present']);self.assertFalse(result['catalog_present'])
        self.assertIn('does not establish healthy',notify.render(result))

    def test_retry_cannot_overlap_an_active_sender(self):
        self.block();started=threading.Event();finish=threading.Event()
        def delivery(*args):started.set();finish.wait(3);return dict(ACCEPTED)
        self.mock_delivery.side_effect=delivery
        with ThreadPoolExecutor(max_workers=1) as pool:
            job=pool.submit(self.run_sender,execute=True);self.assertTrue(started.wait(2));key=self.rows()[0]['notification_id']
            with self.assertRaises(notify.SenderBusy):notify.retry(self.state,key,execute=True,acknowledge_duplicate_risk=True,now=AT)
            finish.set();job.result(timeout=3)
        self.assertEqual(self.mock_delivery.call_count,1)

    def test_invalid_catalog_fails_closed_without_delivery(self):
        root=notify.root_for(self.state);root.mkdir(parents=True);(root/'deliveries.sqlite3').write_bytes(b'invalid')
        with self.assertRaises(sqlite3.DatabaseError):notify.plan(self.state,now=AT)
        self.mock_delivery.assert_not_called()

    def test_cli_uncertain_retry_explains_required_acknowledgment(self):
        self.block();self.mock_delivery.return_value=dict(outcome='uncertain',error='worker_timeout');self.run_sender(execute=True)
        key=self.rows()[0]['notification_id'];error=io.StringIO()
        with redirect_stderr(error):code=main(['--db',str(self.state/'inventory.sqlite3'),'notifications','retry',key,'--execute'])
        self.assertEqual(code,1);self.assertIn('--acknowledge-duplicate-risk',error.getvalue());self.assertNotIn('T'*30,error.getvalue())


class PushoverTransportTests(unittest.TestCase):
    def response(self,raw,status=200):
        response=MagicMock();response.status=status;response.read.return_value=raw;response.__enter__.return_value=response;return response

    def payload(self):return dict(token='T'*30,user='U'*30,title='DLS test',message='Fixed safe message')

    def test_https_post_ordinary_priority_no_credentials_in_url(self):
        with patch.object(pushover.request,'build_opener') as opener:
            opener.return_value.open.return_value=self.response(b'{"status":1,"request":"12345678-1234-1234-1234-123456789abc"}')
            result=pushover.send(self.payload())
        self.assertEqual(result['outcome'],'accepted');req=opener.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://api.pushover.net/1/messages.json');self.assertEqual(req.method,'POST')
        body=parse.parse_qs(req.data.decode());self.assertEqual(body['priority'],['0']);self.assertNotIn('retry',body);self.assertNotIn('expire',body);self.assertNotIn('T'*30,req.full_url)

    def test_http_400_rejection_discards_sensitive_body(self):
        with patch.object(pushover.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error.HTTPError('private',400,'T'*30,{},None);r=pushover.send(self.payload())
        self.assertEqual(r['outcome'],'rejected');self.assertNotIn('T'*30,json.dumps(r))

    def test_http_503_temporary_failure(self):
        with patch.object(pushover.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error.HTTPError('private',503,'failure',{},None);r=pushover.send(self.payload())
        self.assertEqual(r['outcome'],'retryable')

    def test_provider_rejection_not_success(self):
        with patch.object(pushover.request,'build_opener') as opener:
            opener.return_value.open.return_value=self.response(json.dumps(dict(status=0,errors=['T'*30,'U'*30])).encode());r=pushover.send(self.payload())
        self.assertEqual(r['outcome'],'rejected');self.assertNotIn('T'*30,json.dumps(r));self.assertNotIn('U'*30,json.dumps(r))

    def test_network_timeout_uncertain_sanitized(self):
        with patch.object(pushover.request,'build_opener') as opener:
            opener.return_value.open.side_effect=TimeoutError('T'*30);r=pushover.send(self.payload())
        self.assertEqual(r,dict(outcome='uncertain',error='network_uncertain'))

    def test_invalid_or_oversized_response_not_accepted(self):
        for body in [b'not json',b'{}',b'x'*16385]:
            with patch.object(pushover.request,'build_opener') as opener:
                opener.return_value.open.return_value=self.response(body);r=pushover.send(self.payload())
            self.assertEqual(r['outcome'],'uncertain')

    def test_redirects_are_not_followed(self):self.assertIsNone(pushover.NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_parent_passes_secrets_only_on_stdin(self):
        process=MagicMock();process.returncode=0;process.communicate.return_value=(json.dumps(ACCEPTED).encode(),b'')
        with patch.object(pushover.subprocess,'Popen',return_value=process) as launch,patch.object(pushover.os,'killpg'):
            r=pushover.deliver(pushover.Credentials('T'*30,'U'*30),dict(title='DLS',message='test'),10,9)
        self.assertEqual(r['outcome'],'accepted');self.assertNotIn('T'*30,str(launch.call_args));self.assertIn(b'T'*30,process.communicate.call_args.args[0]);self.assertEqual(launch.call_args.kwargs['pass_fds'],(9,))

    def test_parent_timeout_cleanup_returns_uncertain(self):
        process=MagicMock();process.communicate.side_effect=subprocess.TimeoutExpired('worker',10)
        with patch.object(pushover.subprocess,'Popen',return_value=process),patch.object(pushover.os,'killpg') as kill:
            r=pushover.deliver(pushover.Credentials('T'*30,'U'*30),dict(title='DLS',message='test'),10,9)
        self.assertEqual(r['error'],'worker_timeout');kill.assert_called_once();process.wait.assert_called_once()

    def test_parent_untrusted_worker_output_cannot_echo_secrets(self):
        process=MagicMock();process.returncode=0;process.communicate.return_value=(json.dumps(dict(outcome='uncertain',error='T'*30,provider_request_id='U'*30)).encode(),b'')
        with patch.object(pushover.subprocess,'Popen',return_value=process),patch.object(pushover.os,'killpg'):
            r=pushover.deliver(pushover.Credentials('T'*30,'U'*30),dict(title='DLS',message='test'),10,9)
        self.assertNotIn('T'*30,json.dumps(r));self.assertNotIn('U'*30,json.dumps(r))


if __name__=='__main__':unittest.main()
