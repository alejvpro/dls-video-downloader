"""Offline caption lifecycle acceptance tests; synthetic bytes/disposable stores."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import captions as assets, caption_collection as collect
from dls_video_downloader import caption_planning as planning, caption_runner as runner
from dls_video_downloader import supplemental_cycle as cycle, comment_collection as network
from dls_video_downloader.youtube_safety import Blocked
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.cli import main
from dls_video_downloader.inventory import connect, import_document, decide
from dls_video_downloader.comment_runner import atomic_json, runner_lock, Deferred

VIDEO='aaaaaaaaaaa';OTHER='bbbbbbbbbbb'
AT=datetime(2026,11,5,17,tzinfo=timezone.utc)
JSON3=b'{ "wireMagic":"pb3", "events":[{"tStartMs":1000,"dDurationMs":3000,"segs":[{"utf8":"  original\\ntext  ","isSpeakerChange":1},{"utf8":" second","tOffsetMs":250}]}]}\n'
VTT=b'WEBVTT\nKind: captions\nLanguage: en\n\n00:00:01.000 --> 00:00:04.000\n  original\ntext  \n\n'


def listing(kind='automatic',name='',lang='en',alias=None,translated=False):
    key=alias or ('en-orig' if kind=='automatic' else 'en')
    suffix=('&kind=asr' if kind=='automatic' else '')+('&name='+name if name else '')+('&tlang=en' if translated else '')
    reps=[dict(ext=f,name='English',url=f'https://www.youtube.com/api/timedtext?v={VIDEO}&lang={lang}{suffix}&fmt={f}') for f in assets.FORMATS]
    return {'subtitles':{key:reps} if kind=='manual' else {},'automatic_captions':{key:reps} if kind=='automatic' else {},'id':VIDEO,'live_status':'was_live','was_live':True}


def execution(data=None,error=None,termination=None):
    return network.Execution(stdout=(json.dumps(data).encode() if isinstance(data,dict) else data or b''),
        stderr=(error or '[debug] yt-dlp version stable@2026.10.26').encode(),returncode=1 if error else 0,termination=termination)


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'captions';self.path=self.base/'inventory.sqlite3'
        self.now=AT;self.elapsed=0;self.db=connect(self.path);self.addCleanup(self.db.close)
        self.inventory_clock=patch('dls_video_downloader.inventory.now',side_effect=lambda:self.now.isoformat());self.inventory_clock.start();self.addCleanup(self.inventory_clock.stop)
        self.net=patch.object(network,'run_extractor',return_value=execution(listing()));self.mock_listing=self.net.start();self.addCleanup(self.net.stop)
        self.http=patch.object(collect,'fetch',side_effect=lambda url,*args:execution(JSON3 if 'fmt=json3' in url else VTT));self.mock_fetch=self.http.start();self.addCleanup(self.http.stop)

    def seed(self,video=VIDEO,status='was_live',club=False):
        d=dict(id=video,title='In The Club' if club else 'DLS FULL SHOW',channel_id=CHANNEL,
            live_status=status,was_live=status=='was_live',release_date='20261105',upload_date='20261105',
            release_timestamp=(AT-timedelta(hours=4)).timestamp(),timestamp=(AT-timedelta(hours=1)).timestamp())
        with self.db:import_document(self.db,d,'videos' if club else 'live','synthetic')

    def collect(self,**kwargs):
        return collect.collect(VIDEO,root=self.root,clock=lambda:self.now,monotonic=lambda:self.elapsed,**kwargs)

    def history(self):return assets.read_history(self.root,VIDEO)

    def decision(self,when,history=None,**kwargs):
        return planning.evaluate(self.history() if history is None else history,now=when,completed_at=AT,**kwargs)

    def test_initial_both_formats_exact_and_portable(self):
        r=self.collect();self.assertTrue(r['complete']);self.assertEqual(r['tool']['version'],'stable@2026.10.26')
        for row,raw in zip(r['formats'],(JSON3,VTT)):
            p=self.root/row['path'];self.assertEqual(p.read_bytes(),raw);self.assertEqual(row['sha256'],hashlib.sha256(raw).hexdigest())
        portable=json.loads((self.root/r['collection_record']).read_bytes());self.assertEqual(portable['attempt_id'],r['attempt_id'])
        self.assertNotIn('https://',json.dumps(portable));self.assertNotIn('url',json.dumps(portable))
        with sqlite3.connect(self.root/'captions.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_aliases_are_one_track(self):
        data=listing();data['automatic_captions']['en']=data['automatic_captions']['en-orig']
        self.mock_listing.return_value=execution(data);r=self.collect()
        self.assertEqual(len(r['tracks']),1);self.assertEqual(set(r['tracks'][0]['aliases']),{'en','en-orig'});self.assertEqual(self.mock_fetch.call_count,2)

    def test_manual_and_asr_preserved_independently(self):
        data=listing();data['subtitles']=listing('manual')['subtitles'];self.mock_listing.return_value=execution(data)
        r=self.collect();self.assertEqual(len(r['tracks']),2);self.assertEqual(self.mock_fetch.call_count,4);self.assertTrue(r['complete'])

    def test_manual_two_named_tracks_are_distinct(self):
        data=listing('manual',name='first');data['subtitles']['en-second']=listing('manual',name='second')['subtitles']['en']
        tracks,_,_=collect.identify(data);self.assertEqual(len(tracks),2)

    def test_translation_nonenglish_chat_excluded_but_evidence_retained(self):
        data=listing(translated=True);data['subtitles']=listing('manual',lang='fr',alias='fr')['subtitles'];data['subtitles']['live_chat']=[{'ext':'json','url':'chat'}]
        self.mock_listing.return_value=execution(data);r=self.collect()
        self.assertEqual(r['outcome'],'missing');self.mock_fetch.assert_not_called();self.assertTrue(r['availability']);self.assertNotIn('live_chat',json.dumps(r))

    def test_ambiguous_english_requires_review(self):
        data=listing();data['automatic_captions']['en-orig'][0]['url']='https://www.youtube.com/api/timedtext?fmt=json3'
        self.mock_listing.return_value=execution(data);r=self.collect();self.assertFalse(r['complete']);self.assertTrue(r['ambiguities'])
        self.assertTrue(self.decision(AT)['suspended'])

    def test_english_key_alone_not_identity(self):
        tracks,_,_=collect.identify(listing(lang='fr',alias='en-orig'));self.assertFalse(tracks)

    def test_unexpected_endpoint_rejected(self):
        d=listing();d['automatic_captions']['en-orig'][0]['url']=d['automatic_captions']['en-orig'][0]['url'].replace('www.youtube.com','example.com')
        self.assertTrue(collect.identify(d)[2])

    def test_missing_then_later_appears(self):
        self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}))
        r=self.collect(purpose='initial');self.assertEqual(r['outcome'],'missing');self.assertFalse(r['technical_failure'])
        self.now+=timedelta(hours=1);self.mock_listing.return_value=execution(listing());self.assertTrue(self.collect(purpose='refresh')['complete'])

    def test_partial_success_recovery_only_missing_format(self):
        self.mock_fetch.side_effect=[execution(JSON3),execution(error='connection failed')]
        r=self.collect(purpose='initial');self.assertFalse(r['complete']);self.assertEqual(r['outcome'],'partial');self.assertEqual(len(list(self.root.rglob('*.json3'))),1)
        self.mock_fetch.reset_mock();self.mock_fetch.side_effect=lambda *a:execution(VTT)
        self.now+=timedelta(hours=1);r2=self.collect(purpose='initial')
        self.assertTrue(r2['complete']);self.assertEqual(self.mock_fetch.call_count,1);self.assertEqual(r2['formats'][0]['status'],'reused')

    def test_symmetric_partial_recovery(self):
        self.mock_fetch.side_effect=[execution(error='connection failed'),execution(VTT)]
        self.collect(purpose='initial');self.mock_fetch.reset_mock();self.mock_fetch.side_effect=lambda *a:execution(JSON3)
        self.assertTrue(self.collect(purpose='initial')['complete']);self.assertEqual(self.mock_fetch.call_count,1)

    def test_confirmation_partial_recovery_does_not_redownload_success(self):
        self.collect(purpose='initial');self.now+=timedelta(days=1)
        self.mock_fetch.side_effect=[execution(JSON3),execution(error='connection failed')]
        self.collect(purpose='confirmation_24h');self.mock_fetch.reset_mock();self.mock_fetch.side_effect=lambda *a:execution(VTT)
        self.assertTrue(self.collect(purpose='confirmation_24h')['complete']);self.assertEqual(self.mock_fetch.call_count,1)

    def test_duplicate_manual_attempts_deduplicate_assets(self):
        one=self.collect();two=self.collect();self.assertNotEqual(one['attempt_id'],two['attempt_id'])
        self.assertEqual(len(list(self.root.rglob('*.json3'))),1);self.assertEqual(len(self.history()),2);self.assertEqual(two['outcome'],'unchanged')

    def test_revisions_preserve_old_bytes(self):
        first=self.collect();old=(self.root/first['formats'][0]['path']).read_bytes()
        self.mock_fetch.side_effect=lambda url,*a:execution(JSON3.replace(b'original',b'revision') if 'json3' in url else VTT)
        self.now+=timedelta(days=1);r=self.collect(purpose='confirmation_24h')
        self.assertEqual(r['outcome'],'revised');self.assertEqual(len(list(self.root.rglob('*.json3'))),2);self.assertEqual((self.root/first['formats'][0]['path']).read_bytes(),old)

    def test_corrupt_response_rejected_without_artifact(self):
        self.mock_fetch.side_effect=lambda *a:execution(b'<html>error</html>')
        r=self.collect();self.assertTrue(r['technical_failure']);self.assertFalse(r['complete']);self.assertEqual(len(list(self.root.rglob('*.vtt'))),0)

    def test_validator_rejects_empty_broken_and_negative_timing(self):
        for raw,fmt in [(b'', 'json3'),(b'{}','json3'),(b'{"events":[{"tStartMs":-1}]}','json3'),(b'WEBVTT\n\n','vtt'),(b'WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nx\n','vtt')]:
            with self.subTest(fmt=fmt,raw=raw):
                with self.assertRaises(ValueError):assets.validate(raw,fmt)

    def test_immutable_collision_detected(self):
        p=self.base/'immutable';assets.immutable(p,b'first')
        with self.assertRaises(ValueError):assets.immutable(p,b'other')
        self.assertEqual(p.read_bytes(),b'first')

    def test_transaction_failure_preserves_history_and_safe_retry(self):
        self.collect()
        with assets.CaptionStore(self.root) as store:
            store.db.execute("CREATE TRIGGER refuse BEFORE INSERT ON attempts BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.collect()
        self.assertEqual(len(self.history()),1)
        with assets.CaptionStore(self.root) as store:store.db.execute('DROP TRIGGER refuse')
        self.collect();self.assertEqual(len(self.history()),2)

    def test_tampered_preexisting_bytes_fail_closed(self):
        r=self.collect(purpose='initial');(self.root/r['formats'][0]['path']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.collect(purpose='refresh')
        self.assertEqual(len(self.history()),1)

    def test_catalog_reopen_and_history(self):
        self.collect();self.assertEqual(len(assets.read_history(self.root)),1)
        with assets.CaptionStore(self.root) as store:self.assertEqual(len(store.latest(VIDEO)),2)

    def test_invalid_video_no_process_or_store(self):
        with self.assertRaises(ValueError):collect.collect('../bad',root=self.root)
        self.assertFalse(self.root.exists());self.mock_listing.assert_not_called()

    def test_fresh_live_status_stops_payloads(self):
        d=listing();d['live_status']='is_live';self.mock_listing.return_value=execution(d)
        self.assertTrue(self.collect()['technical_failure']);self.mock_fetch.assert_not_called()

    def test_wrong_video_metadata_rejected(self):
        d=listing();d['id']=OTHER;self.mock_listing.return_value=execution(d)
        self.assertFalse(self.collect()['listing_success']);self.mock_fetch.assert_not_called()

    def test_rate_limit_stops_remaining_formats(self):
        self.mock_fetch.side_effect=lambda *a:execution(error='HTTP Error 429\nRetry-After: 30000')
        r=self.collect();self.assertTrue(r['blocking']);self.assertEqual(self.mock_fetch.call_count,1)
        self.assertGreater(datetime.fromisoformat(r['blocking']['until']),AT+timedelta(hours=6))

    def test_listing_blocking_warning_stops_payload(self):
        self.mock_listing.return_value=network.Execution(stdout=json.dumps(listing()).encode(),stderr=b'WARNING: HTTP Error 403: Sign in to confirm you are not a bot',returncode=0)
        self.assertTrue(self.collect()['blocking']);self.mock_fetch.assert_not_called()

    def test_interruption_preserves_first_payload(self):
        self.mock_fetch.side_effect=[execution(JSON3),execution(termination='interrupted')]
        r=self.collect();self.assertEqual(r['exit_code'],130);self.assertEqual(len(list(self.root.rglob('*.json3'))),1)

    def test_timeout_preserves_prior_success(self):
        one=self.collect();self.mock_listing.return_value=execution(termination='timeout');r=self.collect()
        self.assertFalse(r['complete']);self.assertTrue((self.root/one['formats'][0]['path']).exists())

    def test_budget_exhaustion_preserves_partial(self):
        def http(*args):self.elapsed=180;return execution(JSON3)
        self.mock_fetch.side_effect=http;r=self.collect();self.assertFalse(r['complete']);self.assertEqual(len(list(self.root.rglob('*.json3'))),1);self.assertEqual(self.mock_fetch.call_count,1)

    def test_acquisition_priority_checked_between_requests(self):
        def preflight():
            if self.mock_fetch.call_count:raise Deferred('video_acquisition_active')
        r=self.collect(before_collect=preflight);self.assertEqual(len(list(self.root.rglob('*.json3'))),1);self.assertFalse(r['complete']);self.assertEqual(r['formats'][1]['status'],'deferred')

    def test_source_command_is_metadata_only_no_writes_or_retries(self):
        cmd=collect.listing_command(VIDEO)
        for flag in ['--skip-download','--no-write-comments','--no-write-subs','--no-write-auto-subs','--no-write-thumbnail','--ignore-config','--list-subs']:self.assertIn(flag,cmd)
        self.assertEqual(cmd[cmd.index('--retries')+1],'0');self.assertNotIn('--write-auto-subs',cmd)

    def test_http_worker_bounded_no_conversion(self):
        self.http.stop()
        with patch.object(network,'run_extractor') as run:
            collect.fetch('https://www.youtube.com/api/timedtext',12,5)
            args=run.call_args.args;self.assertEqual(args[1:],(12,5));self.assertIn('Accept-Encoding',args[0][3]);self.assertIn('16777216',args[0][3])

    def test_missing_schedule_exact_thresholds(self):
        self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}))
        for hours in [0,1,6,24,72,168,336,720]:
            self.now=AT+timedelta(hours=hours)
            self.assertTrue(self.decision(self.now)['due'])
            self.collect(purpose='initial' if hours==0 else 'refresh')
            self.assertFalse(self.decision(self.now)['due'])
        self.assertTrue(self.decision(self.now)['monitoring_complete']);self.assertTrue(self.decision(self.now)['manual_allowed'])

    def test_missed_checkpoints_not_replayed(self):
        self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}));self.collect(purpose='initial')
        self.now=AT+timedelta(days=8);self.assertTrue(self.decision(self.now)['due']);self.collect(purpose='refresh')
        d=self.decision(self.now);self.assertFalse(d['due']);self.assertEqual(d['next_due_at'],(AT+timedelta(days=14)).isoformat())

    def test_success_confirmations_do_not_restart_after_revision(self):
        self.collect(purpose='initial');self.assertEqual(self.decision(AT)['next_due_at'],(AT+timedelta(days=1)).isoformat())
        self.now=AT+timedelta(days=1);self.collect(purpose='confirmation_24h')
        self.assertEqual(self.decision(self.now)['next_due_at'],(AT+timedelta(days=7)).isoformat())
        self.now=AT+timedelta(days=7);self.collect(purpose='confirmation_7d');self.assertTrue(self.decision(self.now)['monitoring_complete'])
        self.now+=timedelta(days=1);self.collect(purpose='manual');self.assertFalse(self.decision(self.now)['due'])

    def test_late_confirmation_skips_elapsed_day1(self):
        self.collect(purpose='initial');self.now+=timedelta(days=8)
        self.assertEqual(self.decision(self.now)['purpose'],'confirmation_7d');self.collect(purpose='confirmation_7d');self.assertTrue(self.decision(self.now)['monitoring_complete'])

    def test_confirmation_track_absence_does_not_delete_or_restart(self):
        first=self.collect(purpose='initial');self.now+=timedelta(days=7);self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}))
        self.collect(purpose='confirmation_7d');self.assertTrue(self.decision(self.now)['monitoring_complete']);self.assertTrue((self.root/first['formats'][0]['path']).exists())

    def test_five_failure_backoff_and_review(self):
        self.mock_listing.return_value=execution(error='connection failed')
        for index,hours in enumerate([1,6,24,24,24]):
            self.collect(purpose='initial');d=self.decision(self.now)
            if index<4:self.assertEqual(d['next_due_at'],(self.now+timedelta(hours=hours)).isoformat())
            else:self.assertTrue(d['suspended']);self.assertTrue(d['manual_allowed'])
            self.now+=timedelta(hours=hours)

    def test_manual_refresh_after_terminal_missing_does_not_restart_monitoring(self):
        self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}))
        self.now=AT+timedelta(days=30);self.collect(purpose='refresh')
        self.now+=timedelta(days=3);self.mock_listing.return_value=execution(listing());self.collect(purpose='manual')
        self.assertTrue(self.decision(self.now)['monitoring_complete']);self.assertFalse(self.decision(self.now)['due'])

    def test_unknown_completion_and_historical_are_explicit(self):
        self.assertTrue(planning.evaluate([],now=AT,completed_at=None)['suspended'])
        d=planning.evaluate([],now=AT,completed_at=AT-timedelta(days=1000),mode='historical');self.assertFalse(d['due']);self.assertTrue(d['manual_allowed'])

    def test_timezone_aware_and_repeatable(self):
        d=self.decision(AT,history=[]);self.assertEqual(d,self.decision(AT,history=[]))
        with self.assertRaises(ValueError):self.decision(AT.replace(tzinfo=None),history=[])
        from zoneinfo import ZoneInfo
        self.assertEqual(d,self.decision(AT.astimezone(ZoneInfo('America/Los_Angeles')),history=[]))

    def test_cooldown_postpones_due(self):
        d=self.decision(AT,history=[],cooldown_until=AT+timedelta(hours=6));self.assertFalse(d['due']);self.assertEqual(d['reason'],'collector_cooldown')

    def test_plan_no_store_initialization_or_inventory_changes(self):
        self.seed();before=self.path.read_bytes();r=planning.plan(self.path,self.root,now=AT)
        self.assertEqual(r['due_ids'],[VIDEO]);self.assertEqual(r['items'][0]['completion_anchor'],AT.isoformat());self.assertFalse(self.root.exists());self.assertEqual(before,self.path.read_bytes())

    def test_plan_uses_completion_observation_not_livestream_start(self):
        self.seed();item=planning.plan(self.path,self.root,now=AT)['items'][0]
        self.assertEqual(item['completion_anchor'],AT.isoformat());self.assertNotEqual(item['completion_anchor'],datetime.fromtimestamp(item['source_timing']['release_timestamp']['value'],timezone.utc).isoformat())

    def test_plan_no_mkv_and_no_comment_cutoff_inheritance(self):
        self.seed();self.now=AT+timedelta(days=16)
        self.assertEqual(planning.plan(self.path,self.root,now=self.now)['due_ids'],[VIDEO]);self.assertFalse((self.base/'acquisitions').exists())

    def test_live_upcoming_postlive_remain_excluded(self):
        for status in ['is_live','is_upcoming','post_live']:
            self.seed(status=status);self.assertFalse(planning.plan(self.path,self.root,now=AT)['due_ids'])

    def test_selected_club_upload_eligible(self):
        self.seed(status='not_live',club=True);self.assertEqual(planning.plan(self.path,self.root,now=AT)['due_ids'],[VIDEO])

    def test_manual_skip_and_review_excluded(self):
        self.seed()
        with self.db:decide(self.db,VIDEO,'skip','synthetic')
        self.assertFalse(planning.plan(self.path,self.root,now=AT)['due_ids'])

    def test_flat_completed_listing_not_reliable_completion(self):
        d=dict(id=VIDEO,title='DLS FULL SHOW',channel_id=CHANNEL,live_status='was_live',release_date='20261105')
        with self.db:import_document(self.db,dict(entries=[d]),'live','flat')
        self.assertFalse(planning.plan(self.path,self.root,now=AT)['due_ids'])

    def test_caption_selection_independent_of_corrupt_comment_catalog(self):
        self.seed();p=self.base/'comments';p.mkdir();(p/'comments.sqlite3').write_bytes(b'invalid')
        self.assertTrue(planning.plan(self.path,self.root,now=AT)['due_ids'])

    def test_runner_execute_and_repeated_run_no_duplicate_collection(self):
        self.seed();first=runner.run(self.path,self.root,execute=True,clock=lambda:AT);self.assertEqual(first['work'][0]['status'],'completed')
        count=self.mock_fetch.call_count;second=runner.run(self.path,self.root,execute=True,clock=lambda:AT)
        self.assertFalse(second['work']);self.assertEqual(self.mock_fetch.call_count,count)

    def test_runner_dry_run_no_artifacts(self):
        self.seed();r=runner.run(self.path,self.root,clock=lambda:AT);self.assertEqual(r['selected_ids'],[VIDEO]);self.assertFalse(self.root.exists());self.mock_listing.assert_not_called()

    def test_runner_busy_acquisition_defers(self):
        self.seed()
        with patch.object(runner.comment_runner,'video_busy',return_value=True):r=runner.run(self.path,self.root,execute=True,clock=lambda:AT)
        self.assertEqual(r['stop_reason'],'video_acquisition_active');self.mock_fetch.assert_not_called()

    def test_runner_overlap_defers(self):
        self.seed()
        with runner_lock(self.root/'runner'):r=runner.run(self.path,self.root,execute=True,clock=lambda:AT)
        self.assertEqual(r['exit_code'],75);self.mock_fetch.assert_not_called()

    def test_collector_overlap_defers(self):
        with assets.caption_lock(self.root):
            with self.assertRaises(network.CommentBusy):self.collect()
        self.mock_listing.assert_not_called()

    def test_runner_rate_limit_persists_cooldown(self):
        self.seed();self.mock_fetch.side_effect=lambda *a:execution(error='HTTP Error 429')
        r=runner.run(self.path,self.root,execute=True,clock=lambda:AT);self.assertEqual(r['stop_reason'],'collector_cooldown')
        self.assertTrue((self.root/'runner/cooldown.json').exists());self.assertFalse(runner.run(self.path,self.root,clock=lambda:AT)['selected_ids'])

    def test_runner_insufficient_budget_no_network(self):
        self.seed();values=iter([0,0,179,179])
        r=runner.run(self.path,self.root,execute=True,clock=lambda:AT,monotonic=lambda:next(values),runtime_seconds=180)
        self.assertEqual(r['stop_reason'],'insufficient_remaining_runtime');self.mock_fetch.assert_not_called()

    def test_cycle_default_plan_no_write(self):
        self.seed();before=self.path.read_bytes();r=cycle.run(self.path,self.base/'comments',clock=lambda:AT)
        self.assertIn('captions',r['phases']);self.assertEqual(r['phases']['captions']['plan']['due_ids'],[VIDEO]);self.assertFalse(self.root.exists());self.assertFalse((self.base/'supplemental').exists());self.assertEqual(self.path.read_bytes(),before)

    def test_existing_execute_flags_never_enable_captions(self):
        self.seed()
        with patch.object(cycle.comment_runner,'run',return_value=dict(work=[],stop_reason=None,exit_code=0)):
            r=cycle.run(self.path,self.base/'comments',execute=True,clock=lambda:AT)
        self.assertEqual(r['phases']['captions']['outcome'],'not_requested');self.assertFalse(self.root.exists());self.mock_fetch.assert_not_called()

    def test_caption_gate_requires_execute(self):
        with self.assertRaises(ValueError):cycle.run(self.path,captions=True)
        self.assertFalse(self.root.exists())

    def test_cycle_order_and_independent_failure(self):
        self.seed();order=[]
        def comments(*a,**kw):order.append('comments');return dict(work=[dict(status='completed',attempt_id='comment')],stop_reason=None,exit_code=0)
        def captions(*a,**kw):order.append('captions');return dict(work=[dict(status='failed',attempt_id='caption',complete=False)],stop_reason=None,exit_code=2)
        with patch.object(cycle.comment_runner,'run',side_effect=comments),patch.object(cycle.caption_runner,'run',side_effect=captions):
            r=cycle.run(self.path,self.base/'comments',execute=True,captions=True,clock=lambda:AT)
        self.assertEqual(order,['comments','captions']);self.assertEqual(r['phases']['comments']['outcome'],'successful');self.assertEqual(r['phases']['captions']['outcome'],'failed')

    def test_cycle_overall_budget_deferred(self):
        self.seed()
        def comments(*a,**kw):self.elapsed=590;return dict(work=[],stop_reason=None,exit_code=0)
        with patch.object(cycle.comment_runner,'run',side_effect=comments):
            r=cycle.run(self.path,self.base/'comments',execute=True,captions=True,clock=lambda:AT,monotonic=lambda:self.elapsed)
        self.assertEqual(r['phases']['captions']['reason'],'insufficient_remaining_runtime');self.mock_fetch.assert_not_called()

    def test_cycle_caption_success_does_not_hide_comment_failure(self):
        self.seed()
        with patch.object(cycle.comment_runner,'run',return_value=dict(work=[dict(status='failed')],stop_reason=None,exit_code=2)):
            r=cycle.run(self.path,self.base/'comments',execute=True,captions=True,clock=lambda:AT)
        self.assertEqual(r['phases']['captions']['outcome'],'successful');self.assertEqual(r['phases']['comments']['outcome'],'failed');self.assertEqual(r['outcome'],'partially_successful')

    def test_cycle_blocking_comments_prevent_captions(self):
        self.seed()
        with patch.object(cycle.comment_runner,'run',return_value=dict(work=[],stop_reason='extractor_rate_limit_or_block',exit_code=2)):
            r=cycle.run(self.path,self.base/'comments',execute=True,captions=True,clock=lambda:AT)
        self.assertEqual(r['phases']['captions']['outcome'],'deferred');self.mock_fetch.assert_not_called()

    def test_cli_plan_and_dry_run_are_readonly(self):
        self.seed()
        for args in [['captions','plan'],['captions','run']]:
            out=io.StringIO()
            with redirect_stdout(out):code=main(['--db',str(self.path),*args,'--store-root',str(self.root),'--json'])
            self.assertEqual(code,0);json.loads(out.getvalue())
        self.assertFalse(self.root.exists());self.mock_fetch.assert_not_called()

    def test_worker_preserves_http_bytes_without_network(self):
        from types import SimpleNamespace
        response=io.BytesIO(JSON3);response.headers={'Content-Type':'application/json'}
        output=io.BytesIO();error=io.StringIO()
        with patch('urllib.request.build_opener') as opener,patch('sys.argv',['worker','https://www.youtube.com/api/timedtext']),patch('sys.stdout',SimpleNamespace(buffer=output)),patch('sys.stderr',error):
            opener.return_value.open.return_value=response
            exec(collect.HTTP_WORKER,{})
        self.assertEqual(output.getvalue(),JSON3)
        request=opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header('Accept-encoding'),'identity')
        self.assertEqual(opener.return_value.open.call_count,1)

    def test_worker_http_blocking_diagnostic_no_retry(self):
        import urllib.error
        from types import SimpleNamespace
        error=io.StringIO()
        with patch('urllib.request.build_opener') as opener,patch('sys.argv',['worker','https://www.youtube.com/api/timedtext?secret=value']),patch('sys.stdout',SimpleNamespace(buffer=io.BytesIO())),patch('sys.stderr',error):
            opener.return_value.open.side_effect=urllib.error.HTTPError('private',429,'blocked',{'Retry-After':'600'},None)
            with self.assertRaises(SystemExit):exec(collect.HTTP_WORKER,{})
        self.assertIn('HTTP Error 429',error.getvalue());self.assertIn('Retry-After: 600',error.getvalue());self.assertNotIn('secret',error.getvalue());self.assertEqual(opener.return_value.open.call_count,1)

    def test_cycle_discovery_failure_does_not_block_known_caption(self):
        self.seed()
        with patch.object(cycle.morning_discovery,'discover',side_effect=OSError('transient discovery failure')),patch.object(cycle.comment_runner,'run',return_value=dict(work=[],stop_reason=None,exit_code=0)):
            r=cycle.run(self.path,self.base/'comments',execute=True,discover=True,captions=True,clock=lambda:AT)
        self.assertEqual(r['phases']['discovery']['outcome'],'failed');self.assertEqual(r['phases']['captions']['outcome'],'successful')

    def test_cycle_discovery_comments_captions_order(self):
        self.seed();order=[]
        def discovery(*a,**kw):order.append('discovery');return dict(exit_code=0,metadata_requests=1,stop_reason=None,status_checks={})
        def comments(*a,**kw):order.append('comments');return dict(work=[],stop_reason=None,exit_code=0)
        def captions(*a,**kw):order.append('captions');return dict(work=[],stop_reason=None,exit_code=0)
        with patch.object(cycle.morning_discovery,'discover',side_effect=discovery),patch.object(cycle.comment_runner,'run',side_effect=comments),patch.object(cycle.caption_runner,'run',side_effect=captions):
            cycle.run(self.path,self.base/'comments',execute=True,discover=True,captions=True,clock=lambda:AT)
        self.assertEqual(order,['discovery','comments','captions'])

    def test_caption_cooldown_also_blocks_existing_cycle_network_phases(self):
        self.seed();directory=self.root/'runner';directory.mkdir(parents=True)
        atomic_json(directory/'cooldown.json',dict(version=1,until=(AT+timedelta(hours=6)).isoformat()))
        with patch.object(cycle.morning_discovery,'discover') as discovery,patch.object(cycle.comment_runner,'run') as comments:
            r=cycle.run(self.path,self.base/'comments',execute=True,discover=True,clock=lambda:AT)
        discovery.assert_not_called();comments.assert_not_called();self.mock_fetch.assert_not_called()
        self.assertEqual(r['phases']['comments']['reason'],'shared_youtube_cooldown')

    def test_corrupt_caption_catalog_isolated_in_readonly_plan(self):
        self.seed();self.root.mkdir();(self.root/'captions.sqlite3').write_bytes(b'bad catalog')
        r=cycle.run(self.path,self.base/'comments',clock=lambda:AT)
        self.assertEqual(r['phases']['captions']['outcome'],'failed');self.assertEqual(r['phases']['comments']['outcome'],'planned')

    def test_inventory_contention_bounded_and_no_caption_initialization(self):
        self.seed();self.db.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaises(sqlite3.OperationalError):planning.plan(self.path,self.root,now=AT)
        finally:self.db.rollback()
        self.assertFalse(self.root.exists());self.mock_fetch.assert_not_called()

    def test_first_completion_anchor_survives_later_metadata_observations(self):
        self.seed();self.now+=timedelta(days=2);self.seed()
        self.assertEqual(planning.plan(self.path,self.root,now=self.now)['items'][0]['completion_anchor'],AT.isoformat())

    def test_missing_format_without_technical_error_remains_recoverable(self):
        data=listing();data['automatic_captions']['en-orig']=data['automatic_captions']['en-orig'][:1]
        self.mock_listing.return_value=execution(data);r=self.collect(purpose='initial')
        self.assertFalse(r['complete']);self.assertFalse(r['technical_failure']);self.assertEqual(r['outcome'],'partial')
        self.mock_fetch.reset_mock();self.mock_listing.return_value=execution(listing());self.now+=timedelta(hours=1)
        self.assertTrue(self.collect(purpose='refresh')['complete']);self.assertEqual(self.mock_fetch.call_count,1)

    def test_two_payload_formats_share_track_and_generation_observation(self):
        r=self.collect();self.assertEqual(r['formats'][0]['track_id'],r['formats'][1]['track_id']);self.assertEqual(len(r['tracks']),1)

    def test_catalog_lock_failure_preserves_prior_files(self):
        prior=self.collect()
        with sqlite3.connect(self.root/'captions.sqlite3',timeout=1) as db:
            db.execute('BEGIN EXCLUSIVE')
            try:
                with self.assertRaises(sqlite3.OperationalError):self.collect()
            finally:db.rollback()
        self.assertTrue((self.root/prior['formats'][0]['path']).exists());self.assertEqual(len(self.history()),1)

    def test_stale_plan_rechecked_before_collect(self):
        self.seed();real=runner.caption_planning.plan;calls=0
        def changing(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                with self.db:decide(self.db,VIDEO,'skip','changed by operator')
            return real(*args,**kwargs)
        with patch.object(runner.caption_planning,'plan',side_effect=changing):r=runner.run(self.path,self.root,execute=True,clock=lambda:AT)
        self.assertFalse(self.root.joinpath(VIDEO).exists());self.mock_fetch.assert_not_called();self.assertEqual(r['work'][0]['status'],'deferred')

    def test_missing_listing_fields_are_extraction_failure_not_empty(self):
        self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live'))
        r=self.collect();self.assertTrue(r['technical_failure']);self.assertFalse(r['listing_success'])

    def test_direct_collector_honors_its_blocking_cooldown(self):
        self.mock_listing.return_value=execution(error='HTTP Error 429');self.collect()
        self.mock_listing.reset_mock()
        with self.assertRaises(Blocked):self.collect()
        self.mock_listing.assert_not_called()

    def test_cycle_partial_payloads_have_partial_phase_outcome(self):
        self.seed();self.mock_fetch.side_effect=[execution(JSON3),execution(error='connection failed')]
        with patch.object(cycle.comment_runner,'run',return_value=dict(work=[],stop_reason=None,exit_code=0)):
            r=cycle.run(self.path,self.base/'comments',execute=True,captions=True,clock=lambda:AT)
        phase=r['phases']['captions'];self.assertEqual(phase['outcome'],'partially_successful');self.assertEqual(phase['incomplete'],1)

    def test_late_first_success_keeps_both_confirmation_opportunities(self):
        self.now=AT+timedelta(days=29);self.collect(purpose='refresh')
        self.now+=timedelta(days=1);self.mock_listing.return_value=execution(dict(id=VIDEO,live_status='was_live',subtitles={},automatic_captions={}))
        self.collect(purpose='confirmation_24h')
        self.assertEqual(self.decision(self.now)['next_due_at'],(AT+timedelta(days=36)).isoformat())

    def test_malformed_metadata_retains_failure_observation(self):
        self.mock_listing.return_value=execution(b'{"id":"aaaaaaaaaaa","live_status":"was_live","subtitles":[]}')
        # Empty malformed lists must not be mistaken for an empty dictionary.
        result=self.collect();self.assertTrue(result['technical_failure'])

    def test_conflicting_alias_urls_require_review(self):
        data=listing();data['automatic_captions']['en']=[dict(f,url=f['url']+'&audio_track=other') for f in data['automatic_captions']['en-orig']]
        tracks,_,ambiguities=collect.identify(data);self.assertTrue(ambiguities);self.assertEqual(len(tracks),1)

    def test_advertised_track_without_required_formats_is_not_missing_track(self):
        data=listing();source=data['automatic_captions']['en-orig'][0]
        data['automatic_captions']['en-orig']=[dict(source,ext='srt',url=source['url'].replace('fmt=json3','fmt=srt'))]
        self.mock_listing.return_value=execution(data);r=self.collect()
        self.assertEqual(r['outcome'],'missing_formats');self.assertEqual(len(r['tracks']),1);self.mock_fetch.assert_not_called()

    def test_login_required_replay_retains_version_and_cooldown_attempt_link(self):
        diagnostic=("[debug] yt-dlp version stable@2026.10.26 from yt-dlp/yt-dlp [c91f8de0b] (darwin_exe)\n"
                    "[debug] JS runtimes: node-22.23.4\n"
                    "[debug] [youtube] tv_samsung player response playability status: LOGIN_REQUIRED\n"
                    "[debug] [youtube] visionos player response playability status: LOGIN_REQUIRED\n"
                    "[debug] [youtube] web player response playability status: LOGIN_REQUIRED\n"
                    "ERROR: Sign in to confirm you’re not a bot. Use --cookies for authentication.")
        self.mock_listing.return_value=execution(error=diagnostic)
        record=self.collect();self.assertEqual(record['outcome'],'failed');self.assertEqual(record['exit_code'],2)
        self.assertFalse(record['listing_success']);self.assertFalse(record['complete']);self.assertEqual(record['formats'],[])
        self.assertIn('stable@2026.10.26',record['tool']['version']);self.mock_fetch.assert_not_called()
        portable=json.loads((self.root/record['collection_record']).read_bytes())
        cooldown=json.loads((self.root/'runner/cooldown.json').read_bytes())
        self.assertEqual(portable['blocking']['attempt_id'],record['attempt_id'])
        self.assertEqual(cooldown['attempt_id'],record['attempt_id'])
        self.assertEqual(portable,self.history()[0])
        self.assertEqual(datetime.fromisoformat(cooldown['until']),AT+timedelta(hours=24))
        self.mock_listing.reset_mock()
        with self.assertRaises(Blocked):self.collect()
        self.mock_listing.assert_not_called()
        decision=self.decision(AT,cooldown_until=cooldown['until'])
        self.assertFalse(decision['due']);self.assertFalse(decision['monitoring_complete']);self.assertFalse(decision['suspended'])
        later=self.decision(AT+timedelta(hours=24,seconds=1),cooldown_until=cooldown['until'])
        self.assertTrue(later['due']);self.assertTrue(later['manual_allowed'])

    def test_nonblocking_metadata_failure_also_retains_version(self):
        self.mock_listing.return_value=execution(error='[debug] yt-dlp version stable@2026.10.26\nERROR: connection failed')
        record=self.collect();self.assertEqual(record['tool']['version'],'stable@2026.10.26')
        self.assertFalse(record.get('blocking'));self.mock_fetch.assert_not_called()

    def test_readonly_real_payload_fixtures_if_available(self):
        root=Path('/tmp/dls-caption-payload-20261105')
        if not root.exists():self.skipTest('Optional supervised fixtures not retained')
        expected={'json3':'b49b8a4427ccbe8c70bc76dafb44f0832fdf19a49ffa8d0fbe759132babce18e','vtt':'46eb621ead413f3b6480d980e37f1c2d8e3595cfd93b53dea0c78d3b14d8312a'}
        for fmt,sha in expected.items():
            raw=(root/f'MbO7y_90zUI.en-orig.{fmt}').read_bytes();assets.validate(raw,fmt);self.assertEqual(assets.digest(raw),sha)


if __name__=='__main__':unittest.main()
