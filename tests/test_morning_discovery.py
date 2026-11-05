"""Morning metadata discovery: synthetic extraction and disposable databases only."""
from contextlib import redirect_stdout, redirect_stderr
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

from dls_video_downloader import acquisition as media
from dls_video_downloader import morning_discovery as morning
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.cli import main
from dls_video_downloader.comment_planning import plan
from dls_video_downloader.inventory import connect, import_document, decide
from dls_video_downloader.operations import OperationBusy

VIDEO='abcdefghijk'
CLUB='bbbbbbbbbbb'
START=datetime(2026,11,5,14,tzinfo=timezone.utc)


class MorningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name);self.path=self.state/'inventory.sqlite3'
        self.db=connect(self.path);self.addCleanup(self.db.close)
        self.now=START+timedelta(hours=3,minutes=30)
        self.elapsed=0;self.calls=[]
        self.inventory_clock=patch('dls_video_downloader.inventory.now',side_effect=lambda:self.now.isoformat())
        self.inventory_clock.start();self.addCleanup(self.inventory_clock.stop)
        self.live=[dict(id=VIDEO,title='DLS FULL SHOW',live_status='was_live')]
        self.videos=[dict(id=CLUB,title='In The Club')]
        self.full={VIDEO:dict(id=VIDEO,title='DLS FULL SHOW',channel_id=CHANNEL,
                   live_status='was_live',is_live=False,was_live=True,duration=8300,
                   release_timestamp=START.timestamp(),timestamp=(START+timedelta(hours=3)).timestamp(),
                   release_date='20261105',upload_date='20261105'),
                   CLUB:dict(id=CLUB,title='In The Club',channel_id=CHANNEL,live_status='not_live',
                             upload_date='20261105',timestamp=START.timestamp(),duration=450)}

    def fake(self,args,log,timeout):
        self.calls.append((args,log,timeout))
        if '--flat-playlist' in args:
            entries=self.live if args[-1].endswith('/streams') else self.videos
            return json.dumps(dict(channel_id=CHANNEL,epoch=self.now.timestamp(),entries=entries))
        return json.dumps(dict(epoch=self.now.timestamp(),**self.full[args[-1].split('=')[-1]]))

    def invoke(self,**kwargs):
        with patch.object(media,'run',side_effect=self.fake), \
             patch.object(socket,'socket',side_effect=AssertionError('network')), \
             patch('dls_video_downloader.operations.cleanup_staging',side_effect=AssertionError('cleanup')), \
             patch.object(media,'acquire',side_effect=AssertionError('acquire')), \
             patch('dls_video_downloader.comment_collection.collect',side_effect=AssertionError('comments')):
            return morning.discover(self.path,clock=lambda:self.now,monotonic=lambda:self.elapsed,**kwargs)

    def planned(self,video=VIDEO):
        return next(i for i in plan(self.path,self.state/'comments',now=self.now)['items'] if i['id']==video)

    def seed(self,**metadata):
        with self.db:
            import_document(self.db,dict(self.full[VIDEO],**metadata),'live','prior-full')

    def test_new_completed_show_visible_to_comment_planner_without_media(self):
        out=self.invoke()
        self.assertEqual(out['enriched'],[VIDEO]);self.assertTrue(self.planned()['due'])
        self.assertEqual(self.planned()['age_anchor_field'],'release_timestamp')
        self.assertEqual(self.db.execute('select count(*) from acquisitions').fetchone()[0],0)
        self.assertFalse((self.state/'comments').exists())

    def test_flat_completed_alone_cannot_establish_completion(self):
        original=self.fake
        def fail_full(args,*rest):
            if '--flat-playlist' not in args:raise ValueError('fixture full metadata unavailable')
            return original(args,*rest)
        self.fake=fail_full
        out=self.invoke()
        self.assertEqual(out['status'][VIDEO]['category'],'review')
        self.assertIsNone(out['status'][VIDEO]['live_status'])
        self.assertFalse(self.planned()['due'])

    def test_upcoming_and_live_remain_ineligible(self):
        for status in ('is_upcoming','is_live','post_live'):
            self.full[VIDEO]['live_status']=status
            self.full[VIDEO]['is_live']=status=='is_live'
            self.now+=timedelta(seconds=1)
            out=self.invoke()
            self.assertEqual(out['status'][VIDEO]['live_status'],status)
            self.assertFalse(self.planned()['due'])

    def test_previously_known_live_transitions_with_full_evidence(self):
        self.seed(live_status='is_live',is_live=True)
        before=self.db.execute('select first_seen from videos').fetchone()[0]
        self.invoke()
        self.assertEqual(self.planned()['category'],'daily_show')
        self.assertTrue(self.planned()['due'])
        self.assertEqual(self.db.execute('select first_seen from videos').fetchone()[0],before)
        statuses=[json.loads(r[0]).get('live_status') for r in self.db.execute('select metadata from observations')]
        self.assertIn('is_live',statuses);self.assertIn('was_live',statuses)

    def test_previously_known_upcoming_transitions_despite_video_retry(self):
        self.seed(live_status='is_upcoming')
        with self.db:
            self.db.execute('insert into retry_state values(?,?,?,?,?,?,?)',
                (VIDEO,'enrichment',3,'transient','upcoming',START.isoformat(),(self.now+timedelta(days=1)).isoformat()))
        before=self.db.execute('select * from retry_state').fetchall()
        self.invoke()
        self.assertTrue(self.planned()['due'])
        self.assertEqual(self.db.execute('select * from retry_state').fetchall(),before)

    def test_short_friday_no_duration_threshold(self):
        self.full[VIDEO]['duration']=3000
        self.invoke();self.assertTrue(self.planned()['due'])

    def test_club_normal_upload_selected_with_optional_videos_scan(self):
        out=self.invoke(videos_limit=5)
        self.assertEqual(set(out['enriched']),{VIDEO,CLUB})
        self.assertEqual(self.planned(CLUB)['category'],'the_club')
        self.assertTrue(self.planned(CLUB)['due'])
        self.assertEqual(self.planned(CLUB)['age_anchor_field'],'timestamp')

    def test_manual_decisions_never_changed(self):
        for choice in ('approve','skip','reject','clear'):
            with self.db:decide(self.db,VIDEO,choice,'persistent fixture')
            before=[tuple(r) for r in self.db.execute('select * from decisions')]
            self.invoke()
            self.assertEqual([tuple(r) for r in self.db.execute('select * from decisions')],before)
        with self.db:decide(self.db,VIDEO,'skip','stop')
        self.calls=[];self.invoke();self.assertEqual(len(self.calls),1)
        self.assertFalse(self.planned()['due'])

    def test_duplicate_discovery_preserves_identity_observations_and_provenance(self):
        self.invoke()
        counts=[self.db.execute('select count(*) from '+table).fetchone()[0] for table in ('videos','observations','classifications')]
        row=dict(self.db.execute('select * from videos').fetchone())
        self.calls=[];self.invoke()
        self.assertEqual(counts,[self.db.execute('select count(*) from '+t).fetchone()[0] for t in ('videos','observations','classifications')])
        self.assertEqual(row,dict(self.db.execute('select * from videos').fetchone()))
        self.assertEqual(len(self.calls),1) # Already full/completed needs no enrichment.
        provenance=json.loads(row['field_provenance'])
        self.assertEqual(provenance['release_timestamp']['rank'],2)
        self.assertEqual(json.loads(row['metadata'])['release_timestamp'],START.timestamp())

    def test_missing_full_status_preserves_other_source_evidence_and_uncertainty(self):
        self.full[VIDEO].pop('live_status')
        out=self.invoke()
        self.assertEqual(out['exit_code'],2)
        self.assertEqual(out['status_checks'][VIDEO],'unconfirmed')
        self.assertFalse(self.planned()['due'])
        self.assertEqual(self.planned()['source_timing']['release_timestamp']['value'],START.timestamp())

    def test_malformed_status_object_is_unconfirmed_not_completion(self):
        self.full[VIDEO]['live_status']={'unexpected':'status'}
        out=self.invoke()
        self.assertEqual(out['exit_code'],2)
        self.assertEqual(out['status_checks'][VIDEO],'unconfirmed')
        self.assertFalse(self.planned()['due'])

    def test_contradictory_status_does_not_force_completion(self):
        self.full[VIDEO]['is_live']=True
        self.invoke();self.assertFalse(self.planned()['due'])

    def test_failed_enrichment_preserves_prior_live_observation(self):
        self.seed(live_status='is_live',is_live=True)
        original=self.fake
        def failed(args,*rest):
            if '--flat-playlist' not in args:raise ValueError('ERROR: fixture extraction failed')
            return original(args,*rest)
        self.fake=failed
        out=self.invoke()
        self.assertEqual(out['exit_code'],2)
        self.assertEqual(self.planned()['category'],'review');self.assertFalse(self.planned()['due'])

    def test_full_identity_mismatch_not_imported(self):
        self.full[VIDEO]['id']=CLUB
        out=self.invoke();self.assertEqual(out['exit_code'],2)
        self.assertEqual(self.db.execute("select count(*) from observations where detail='full'").fetchone()[0],0)

    def test_emergency_bound_reports_incomplete_forward_scope(self):
        self.live=[dict(id=str(n)*11,title='DLS FULL SHOW') for n in range(5)]
        for e in self.live:self.full[e['id']]=dict(self.full[VIDEO],id=e['id'])
        out=self.invoke(max_metadata_requests=3)
        self.assertEqual(len(self.calls),4);self.assertEqual(len(out['deferred']),2)
        self.assertFalse(out['forward_coverage']['complete']);self.assertEqual(out['exit_code'],2)
        self.assertTrue(all(c[2]<=60 for c in self.calls))
        self.assertEqual(self.calls[0][0][self.calls[0][0].index('--playlist-end')+1],'10')

    def test_oversized_or_foreign_tab_fails_without_import(self):
        for document in ({'channel_id':CHANNEL,'entries':self.live*11},
                         {'channel_id':'foreign','entries':self.live}):
            self.fake=lambda *args:json.dumps(document)
            out=self.invoke();self.assertEqual(out['exit_code'],2)
            self.assertEqual(self.db.execute('select count(*) from videos').fetchone()[0],0)

    def test_timeout_budget_not_reset(self):
        original=self.fake
        def slow(*args):
            value=original(*args);self.elapsed+=100;return value
        self.fake=slow;out=self.invoke(runtime_seconds=100)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(out['stop_reason'],'runtime_or_video_cron_budget')
        self.assertFalse(self.planned()['due'])

    def test_metadata_flags_disable_all_asset_downloads(self):
        self.invoke()
        for args,_,_ in self.calls:
            for flag in ('--skip-download','--no-write-comments','--no-write-subs',
                         '--no-write-auto-subs','--no-write-thumbnail','--no-write-info-json',
                         '--no-write-description','--ignore-config','--no-plugin-dirs'):
                self.assertIn(flag,args)
            self.assertIn('node:'+media.NODE,args)
            self.assertEqual(args[args.index('--retries')+1],'0')

    def test_active_acquisition_defers_before_database_or_logs(self):
        before=self.path.read_bytes()
        with media.operation_lock(self.state/'acquisitions'):
            with self.assertRaises(OperationBusy):self.invoke()
        self.assertEqual(self.calls,[]);self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.state/'morning').exists())

    def test_existing_shared_lock_held_through_extraction(self):
        original=self.fake
        def guarded(*args):
            with self.assertRaises(OperationBusy):
                with media.operation_lock(self.state/'acquisitions'):pass
            return original(*args)
        self.fake=guarded;self.invoke()

    def test_near_video_cron_defers_without_writes(self):
        for minute in (-3,0,0.5):
            self.now=datetime(2026,11,5,19,tzinfo=timezone.utc)+timedelta(minutes=minute)
            before=self.path.read_bytes();out=self.invoke()
            self.assertEqual(out['stop_reason'],'video_cron_imminent')
            self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.state/'morning').exists());self.assertEqual(self.calls,[])

    def test_schedule_dst_uses_pacific_wall_clock(self):
        before=datetime(2026,10,30,16,tzinfo=timezone.utc)
        after=datetime(2026,11,2,17,tzinfo=timezone.utc)
        self.assertEqual(morning.next_video_run(before).hour,18)
        self.assertEqual(morning.next_video_run(after).hour,19)

    def test_no_cleanup_retry_frontier_receipt_or_run_history_writes(self):
        tables=('retry_state','scan_frontiers','runner_runs','cleanup_events','acquisitions','acquisition_attempts','decisions')
        before={t:[tuple(r) for r in self.db.execute('select * from '+t)] for t in tables}
        marker=self.state/'acquisitions/preserved.json';marker.parent.mkdir();marker.write_text('unchanged')
        self.invoke()
        self.assertEqual(before,{t:[tuple(r) for r in self.db.execute('select * from '+t)] for t in tables})
        self.assertEqual(marker.read_text(),'unchanged')
        self.assertFalse((self.state/'runs').exists())

    def test_blocking_stops_following_network_requests(self):
        self.fake=lambda *args:(_ for _ in ()).throw(ValueError('HTTP Error 429: Too Many Requests'))
        out=self.invoke(videos_limit=5)
        self.assertEqual(out['stop_reason'],'youtube_blocking')
        self.assertEqual(out['exit_code'],2)
        self.assertEqual(out['enriched'],[])

    def test_cli_uses_only_disposable_inventory_and_reports(self):
        for extra in (['--json'],[]):
            with patch.object(media,'run',side_effect=self.fake), \
                 patch.object(morning,'next_video_run',return_value=datetime.now(timezone.utc)+timedelta(hours=2)), \
                 redirect_stdout(io.StringIO()) as output:
                code=main(['--db',str(self.path),'discover-morning']+extra)
            self.assertEqual(code,0)
            self.assertIn('morning-metadata' if extra else 'Morning metadata',output.getvalue())

    def test_missing_publication_evidence_stays_explicit(self):
        for field in ('timestamp','release_timestamp','release_date','upload_date'):
            self.full[VIDEO].pop(field)
        out=self.invoke()
        self.assertEqual(out['status'][VIDEO]['category'],'daily_show')
        self.assertEqual(out['status'][VIDEO]['eligibility'],'date_unknown')
        self.assertFalse(self.planned()['due'])
        self.assertEqual(self.planned()['age_anchor_field'],'first_seen')

    def test_flat_negative_status_refreshes_previously_completed_full_status(self):
        self.seed()
        self.live[0]['live_status']='is_live'
        self.full[VIDEO].update(live_status='is_live',is_live=True)
        out=self.invoke()
        self.assertEqual(out['enriched'],[VIDEO])
        self.assertEqual(out['status'][VIDEO]['live_status'],'is_live')
        self.assertFalse(self.planned()['due'])

    def test_malformed_id_rejects_entire_tab_without_partial_import(self):
        self.live.append(dict(id='invalid',title='bad'))
        out=self.invoke()
        self.assertEqual(out['exit_code'],2)
        self.assertEqual(self.db.execute('select count(*) from videos').fetchone()[0],0)

    def test_discard_unrelated_extraction_payloads(self):
        self.full[VIDEO].update(formats=[{'url':'https://playback.example/secret'}],
                                comments=[{'text':'not requested'}],thumbnails=[{'url':'https://thumb.example'}])
        self.invoke()
        for row in self.db.execute('select metadata from observations'):
            metadata=json.loads(row[0])
            self.assertFalse(set(metadata)&{'formats','comments','thumbnails'})
        self.assertNotIn('playback.example',(self.state/'morning/last-run.json').read_text())

    def test_interruption_reports_and_releases_shared_lock(self):
        self.fake=lambda *args:(_ for _ in ()).throw(KeyboardInterrupt())
        out=self.invoke()
        self.assertEqual(out['exit_code'],130)
        self.assertEqual(out['stop_reason'],'interrupted')
        with media.operation_lock(self.state/'acquisitions'):pass

    def test_morning_does_not_update_regular_scan_frontier(self):
        with self.db:
            self.db.execute('insert into scan_frontiers values(?,?,?)',('live','old-head',START.isoformat()))
        before=[tuple(r) for r in self.db.execute('select * from scan_frontiers')]
        self.invoke()
        self.assertEqual(before,[tuple(r) for r in self.db.execute('select * from scan_frontiers')])

    def test_invalid_options_do_not_write(self):
        before=self.path.read_bytes()
        for kwargs in ({'streams_limit':26},{'videos_limit':-1},{'max_metadata_requests':101},{'runtime_seconds':181}):
            with self.assertRaises(ValueError):self.invoke(**kwargs)
        self.assertEqual(before,self.path.read_bytes());self.assertFalse((self.state/'morning').exists())


if __name__=='__main__':unittest.main()
