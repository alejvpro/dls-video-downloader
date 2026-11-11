"""Offline effectiveness reporting over disposable evidence only."""
from contextlib import redirect_stdout
from datetime import date,datetime,timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import captions
from dls_video_downloader.cli import main
from dls_video_downloader.comments import CommentStore,IRISHWEEGEE
from dls_video_downloader.inventory import connect,import_document
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.supplemental_report import build, _discovery_receipts
from dls_video_downloader.morning_discovery import _save_receipt


class SupplementalReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dbpath=self.root/'inventory.sqlite3'
        self.comments=self.root/'comments';self.caption_root=self.root/'captions'
        self.db=connect(self.dbpath);self.addCleanup(self.db.close)
        self.daily='abcdefghijk';self.club='bbbbbbbbbbb';self.unknown='ccccccccccc';self.uncertain='ddddddddddd'
        with self.db:
            import_document(self.db,dict(id=self.daily,title='DLS FULL SHOW',channel_id=CHANNEL,live_status='was_live',was_live=True,
                release_date='20261105',upload_date='20261105',release_timestamp=1793894400),'live','synthetic')
            import_document(self.db,dict(id=self.club,title='In The Club',channel_id=CHANNEL,live_status='not_live',
                upload_date='20261106',timestamp=1793980800),'videos','synthetic')
            import_document(self.db,dict(id=self.unknown,title='DLS FULL SHOW',channel_id=CHANNEL,live_status='was_live',was_live=True),'live','synthetic')
            import_document(self.db,dict(id=self.uncertain,title='DLS FULL SHOW',channel_id=CHANNEL,live_status='was_live',was_live=True,
                release_date='20261107',upload_date='20261107'),'live','synthetic')
            self.db.execute("INSERT INTO observations(video_id,detail,digest,metadata,observed_at,imported_at) VALUES(?,?,?,?,?,?)",
                (self.daily,'full','a'*64,json.dumps(dict(id=self.daily,live_status='was_live')),
                 '2026-11-05T18:00:00+00:00','2026-11-05T18:00:00+00:00'))
        with CommentStore(self.comments) as store:
            for at,text,source,purpose in [
                ('2026-11-05T19:00:00+00:00','Shadow Show starts 3:03','scheduled','initial'),
                ('2026-11-05T22:00:00+00:00','[0:00 - 3:02] Standby\n[3:03 - 7:32] Shadow Show\nPersonnel','scheduled','confirmation')]:
                store.record(self.daily,[dict(id='Ugcomment',author_id=IRISHWEEGEE,author='IrishWeegee',text=text)],
                    outcome='uncertain',coverage={'assessment':'unknown','retrieval_state':'returned_nonempty'},
                    collected_at=at,configuration={'invocation':{'source':source,'purpose':purpose}})
            # A schema-1 legacy attempt without provenance remains readable as unknown.
            store.record(self.club,[],outcome='empty',coverage={'assessment':'complete'},collected_at='2026-11-06T20:00:00+00:00',
                configuration={'invocation':{'source':'manual','purpose':'initial'}})
            store.record(self.club,[dict(id='Ugclubcomment',author_id=IRISHWEEGEE,author='IrishWeegee',text='[0:00] Complete guide')],
                outcome='success',coverage={'assessment':'complete'},collected_at='2026-11-06T22:00:00+00:00',
                configuration={'invocation':{'source':'scheduled','purpose':'confirmation'}})
            store.record(self.uncertain,[],outcome='uncertain',coverage={'assessment':'unknown'},
                collected_at='2026-11-07T20:00:00+00:00',configuration={'invocation':{'source':'scheduled','purpose':'initial'}})
        raw_json=b'{"wireMagic":"pb3","events":[{"tStartMs":1,"dDurationMs":2,"segs":[{"utf8":"x"}]}]}'
        raw_vtt=b'WEBVTT\n\n00:00:00.001 --> 00:00:00.003\nx\n'
        with captions.CaptionStore(self.caption_root) as store:
            store.record(dict(video_id=self.daily,started_at='2026-11-05T19:10:00+00:00',finished_at='2026-11-05T19:11:00+00:00',
                purpose='initial',invocation_source='scheduled',listing_success=True,tracks=[dict(track_id='d'*64,identity={'language':'en','kind':'automatic'})],
                formats=[dict(track_id='d'*64,format='json3',status='preserved'),dict(track_id='d'*64,format='vtt',status='preserved')],
                complete=True,technical_failure=False,outcome='complete'),{('d'*64,'json3'):raw_json,('d'*64,'vtt'):raw_vtt})

    def test_report_groups_date_scope_and_distinguishes_observed_uncertainty(self):
        out=build(self.dbpath,self.comments,self.caption_root,start='2026-11-05',end='2026-11-06')
        self.assertEqual(out['selected_video_count'],2)
        self.assertEqual(out['unknown_date_selected_ids'],[self.unknown])
        daily=next(v for v in out['videos'] if v['id']==self.daily)
        club=next(v for v in out['videos'] if v['id']==self.club)
        self.assertEqual(daily['group'],'daily_show');self.assertEqual(club['group'],'the_club')
        self.assertEqual(daily['comments']['attempt_count'],2)
        self.assertEqual(daily['comments']['invocation_source'] if 'invocation_source' in daily['comments'] else daily['comments']['first_attempt']['invocation_source'],'scheduled')
        self.assertTrue(daily['comments']['coverage_uncertain'])
        self.assertEqual(daily['comments']['irishweegee_first_observation_attempt'],1)
        self.assertEqual(len(daily['comments']['revisions']),1)
        self.assertGreater(daily['comments']['revisions'][0]['latest_text_length'],daily['comments']['revisions'][0]['first_text_length'])
        self.assertTrue(daily['captions']['first_attempt_complete'])
        self.assertTrue(daily['captions']['first_attempt_json3_preserved'] and daily['captions']['first_attempt_vtt_preserved'])
        self.assertEqual(club['comments']['first_attempt']['invocation_source'],'manual')
        self.assertEqual(out['date_scope']['timezone'],'America/Los_Angeles')

    def test_irishweegee_denominators_expose_negative_unknown_and_scheduled_cohorts(self):
        out=build(self.dbpath,self.comments,self.caption_root,start='2026-11-05',end='2026-11-07')
        daily=out['cohort']['daily_show']
        # Daily: one positive with uncertain coverage, one attempted but unknown.
        self.assertEqual(daily['first_attempt_irishweegee'],1)
        self.assertEqual(daily['first_attempt_irishweegee_attempted_denominator'],2)
        self.assertEqual(daily['first_attempt_irishweegee_unknown'],1)
        self.assertEqual(daily['first_attempt_irishweegee_definitive_negatives'],0)
        # Club's earlier manual complete negative is not confused with its later scheduled positive.
        club=next(v for v in out['videos'] if v['id']==self.club)
        self.assertFalse(club['comments']['first_attempt']['invocation_source']=='scheduled')
        self.assertEqual(club['comments']['irishweegee_first_attempt'],False)
        self.assertEqual(club['comments']['irishweegee_first_scheduled_attempt'],True)
        self.assertEqual(out['cohort']['the_club']['first_attempt_irishweegee_definitive_negatives'],1)
        self.assertEqual(out['cohort']['the_club']['first_scheduled_attempt_irishweegee_observed'],1)
        self.assertEqual(out['cohort']['the_club']['first_scheduled_attempt_irishweegee_attempted_denominator'],1)

    def test_cli_is_readonly_and_human_render_is_concise(self):
        before=self.dbpath.read_bytes();out=io.StringIO()
        with redirect_stdout(out),patch.object(socket,'socket',side_effect=AssertionError('network')):
            code=main(['--db',str(self.dbpath),'supplemental-report','--start-date','2026-11-05','--end-date','2026-11-06'])
        self.assertEqual(code,0);self.assertIn('daily_show:',out.getvalue());self.assertEqual(self.dbpath.read_bytes(),before)
        self.assertFalse((self.root/'youtube-safety').exists())

    def test_receipt_retention_is_bounded(self):
        for i in range(125):
            _save_receipt(self.root/'morning',dict(run_id=f'{i:032x}',started_at=f'2026-11-10T10:{i%60:02d}:00+00:00',trigger='scheduled',exit_code=0))
        receipts=list((self.root/'morning/history').glob('*.json'))
        self.assertEqual(len(receipts),120)
        self.assertTrue(all(json.loads(p.read_text())['trigger']=='scheduled' for p in receipts))
        self.assertTrue((self.root/'morning/last-run.json').exists())

    def test_legacy_streams_receipt_is_corrected_in_read_only_report(self):
        directory=self.root/'morning';(directory/'history').mkdir(parents=True)
        path=directory/'history'/'legacy.json'
        original={'run_id':'legacy','started_at':'2026-11-10T17:25:00+00:00','limits':{'streams':25,'videos':10},
            'encountered':{'live':25},'operation_attempts':[{'kind':'channel_listing','scope':'streams'}],
            'warnings':['live: bounded head reached 25; not exhaustive discovery'],
            'forward_coverage':{'complete':True}}
        path.write_text(json.dumps(original))
        row=_discovery_receipts(directory,'2026-11-10','2026-11-10')[0]
        self.assertEqual(row['tab_coverage']['streams']['entries_observed'],25)
        self.assertTrue(row['tab_coverage']['streams']['cap_hit'])
        self.assertFalse(row['coverage']['complete'])
        self.assertEqual(json.loads(path.read_text()),original)


if __name__=='__main__':unittest.main()
