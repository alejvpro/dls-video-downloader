"""Realistic forward-scope regression fixtures; no live requests or production IO."""
from datetime import timedelta
import json
from unittest.mock import patch
import unittest

import test_morning_discovery as fixtures
from dls_video_downloader import forward_metadata as forward
from dls_video_downloader import acquisition as media
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.inventory import import_document

VIDEO=fixtures.VIDEO
START=fixtures.START


class ForwardTests(unittest.TestCase):
    setUp=fixtures.MorningTests.setUp
    fake=fixtures.MorningTests.fake
    invoke=fixtures.MorningTests.invoke
    seed=fixtures.MorningTests.seed
    planned=fixtures.MorningTests.planned

    def legacy(self,video='-KjkIeimaDA'):
        document={'channel_id':CHANNEL,'epoch':(START-timedelta(days=7)).timestamp(),
                  'entries':[{'id':video,'title':'LIVE at 9am ET | FULL SHOW 8/7/26 | The Dan Le Batard Show',
                              'live_status':'was_live','duration':11900}]}
        with self.db:import_document(self.db,document,'live','legacy-flat')
        return dict(self.db.execute('select * from videos where id=?',(video,)).fetchone())

    def four_current(self):
        self.seed(live_status='is_live',is_live=True)
        self.live=[dict(id=f'mini{i:07d}',title='Extra current broadcast',live_status='was_live') for i in range(3)]
        for item in self.live:
            self.full[item['id']]=dict(self.full[VIDEO],id=item['id'],title=item['title'])
        return {VIDEO,*[e['id'] for e in self.live]}

    def test_november5_live_show_and_three_minis_all_enriched(self):
        expected=self.four_current()
        old={v:self.legacy(v) for v in ('-KjkIeimaDA','0TFneZdBl3c')}
        original=self.fake
        def timed(*args):
            value=original(*args);self.elapsed+=15;self.now+=timedelta(seconds=15);return value
        self.fake=timed
        out=self.invoke()
        self.assertEqual(set(out['enriched']),expected)
        self.assertEqual(set(out['attempted']),expected)
        self.assertEqual(out['attempted'][0],VIDEO)
        self.assertEqual(len(self.calls),5);self.assertEqual(self.elapsed,75)
        self.assertTrue(out['forward_coverage']['complete']);self.assertEqual(out['exit_code'],0)
        self.assertEqual(out['forward_scope']['historical_count'],2)
        self.assertTrue(self.planned()['due'])
        for video,row in old.items():
            self.assertEqual(row,dict(self.db.execute('select * from videos where id=?',(video,)).fetchone()))
        self.assertFalse(set(old)&set(out['attempted']))

    def test_recent_first_seen_does_not_admit_old_undated_legacy_rows(self):
        old=self.legacy()
        self.assertEqual(old['first_seen'],self.now.isoformat())
        out=self.invoke()
        self.assertEqual(out['attempted'],[VIDEO])
        self.assertIn('-KjkIeimaDA',out['forward_scope']['historical_sample'])

    def test_upcoming_live_completed_then_no_redundant_full_fetch(self):
        for status in ('is_upcoming','is_live','was_live'):
            self.full[VIDEO].update(live_status=status,is_live=status=='is_live',was_live=status=='was_live')
            self.now+=timedelta(minutes=15);self.calls=[]
            out=self.invoke();self.assertEqual(out['attempted'],[VIDEO])
            self.assertEqual(out['status'][VIDEO]['live_status'],status)
        self.calls=[];out=self.invoke()
        self.assertEqual(out['attempted'],[]);self.assertEqual(len(self.calls),1)
        self.assertIn(VIDEO,out['forward_scope']['established'])

    def test_unattempted_unknown_date_head_survives_leaving_listing(self):
        self.live=[dict(id=f'mini{i:07d}',title='Extra current broadcast') for i in range(4)]
        for item in self.live:self.full[item['id']]=dict(self.full[VIDEO],id=item['id'],title=item['title'])
        first=self.invoke(max_metadata_requests=2)
        pending=set(first['deferred']);self.assertEqual(len(pending),2)
        self.assertFalse(first['forward_coverage']['complete'])
        self.live=[];self.now+=timedelta(minutes=30);self.calls=[]
        second=self.invoke()
        self.assertEqual(set(second['attempted']),pending)
        self.assertTrue(second['forward_coverage']['complete'])
        for video in pending:
            source=self.db.execute("select * from sources where video_id=? and source='morning:live'",(video,)).fetchone()
            self.assertIsNotNone(source)

    def test_partial_failure_backoff_then_only_failed_video_recovers(self):
        expected=self.four_current();bad=next(e['id'] for e in self.live)
        original=self.fake
        def partly(args,*rest):
            if args[-1].endswith(bad):raise ValueError('ERROR: transient fixture')
            return original(args,*rest)
        self.fake=partly;out=self.invoke()
        self.assertEqual(set(out['attempted']),expected)
        self.assertEqual(set(out['enriched']),expected-{bad})
        self.assertFalse(out['forward_coverage']['complete'])
        state=forward.read_state(self.state/'morning')
        self.assertEqual(state['failures'][bad]['count'],1)
        self.fake=original;self.calls=[];immediate=self.invoke()
        self.assertEqual(immediate['attempted'],[])
        self.assertEqual(immediate['retry_deferred'][0]['id'],bad)
        self.assertFalse(immediate['forward_coverage']['complete'])
        self.now+=timedelta(hours=1,seconds=1);self.calls=[]
        recovered=self.invoke()
        self.assertEqual(recovered['attempted'],[bad]);self.assertTrue(recovered['forward_coverage']['complete'])
        self.assertNotIn(bad,forward.read_state(self.state/'morning')['failures'])

    def test_progressive_failure_backoff(self):
        original=self.fake
        def failing(args,*rest):
            if '--flat-playlist' not in args:raise ValueError('ERROR: transient fixture')
            return original(args,*rest)
        self.fake=failing
        for expected in (1,6,24,24):
            out=self.invoke();self.assertEqual(out['attempted'],[VIDEO])
            record=forward.read_state(self.state/'morning')['failures'][VIDEO]
            due=forward.instant(record['next_retry'])
            self.assertEqual(due-self.now,timedelta(hours=expected))
            self.now=due+timedelta(seconds=1)

    def test_rate_limit_stops_all_requests_and_next_cycle_honors_cooldown(self):
        original=self.fake
        def blocked(args,*rest):
            if '--flat-playlist' not in args:raise ValueError('ERROR: HTTP Error 429')
            return original(args,*rest)
        self.fake=blocked;first=self.invoke()
        self.assertEqual(first['stop_reason'],'youtube_blocking')
        self.assertFalse(first['forward_coverage']['complete'])
        self.calls=[];self.now+=timedelta(hours=1)
        next_cycle=self.invoke();self.assertEqual(self.calls,[])
        self.assertEqual(next_cycle['stop_reason'],'metadata_cooldown')
        self.assertFalse(next_cycle['forward_coverage']['complete'])

    def test_default_emergency_bound_handles_abnormally_large_scope(self):
        self.live=[]
        entries=[]
        for i in range(25):
            video=f'fwd{i:08d}'
            entries.append(dict(id=video,title='Current extra broadcast',release_date='20261105'))
            self.full[video]=dict(self.full[VIDEO],id=video,title='Current extra broadcast')
        with self.db:
            import_document(self.db,{'channel_id':CHANNEL,'entries':entries},'live','recent-known')
        first=self.invoke()
        self.assertEqual(len(first['attempted']),20)
        self.assertEqual(len(first['deferred']),5)
        self.assertEqual(first['stop_reason'],'emergency_metadata_request_bound')
        self.assertFalse(first['forward_coverage']['complete']);self.assertEqual(first['exit_code'],2)
        second=self.invoke()
        self.assertEqual(set(second['attempted']),set(first['deferred']))
        self.assertTrue(second['forward_coverage']['complete'])

    def test_deadline_not_small_quota_is_reported_as_incomplete(self):
        self.four_current();original=self.fake
        def slow(*args):
            value=original(*args);self.elapsed+=60;self.now+=timedelta(seconds=60);return value
        self.fake=slow;out=self.invoke()
        self.assertFalse(out['forward_coverage']['complete'])
        self.assertEqual(out['stop_reason'],'runtime_or_video_cron_budget')
        self.assertGreater(len(out['forward_coverage']['unattempted_ids']),0)
        self.assertEqual(out['exit_code'],2)

    def test_new_provisional_head_can_reveal_old_date_without_continuing_backfill(self):
        self.full[VIDEO].update(release_date='20260807',upload_date='20260807',
                                release_timestamp=(START-timedelta(days=90)).timestamp())
        first=self.invoke();self.assertEqual(first['attempted'],[VIDEO])
        self.assertFalse(self.planned()['due'])
        self.live=[];self.calls=[];second=self.invoke()
        self.assertEqual(second['attempted'],[]);self.assertEqual(len(self.calls),1)

    def test_failed_retry_state_persistence_is_a_safety_error(self):
        original=self.fake
        def failing(args,*rest):
            if '--flat-playlist' not in args:raise ValueError('ERROR: HTTP Error 429')
            return original(args,*rest)
        self.fake=failing
        with patch.object(forward,'_save_state',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(forward.MetadataStateError):self.invoke()

    def test_same_video_on_both_tabs_is_enriched_once(self):
        self.videos=list(self.live)
        out=self.invoke(videos_limit=5)
        self.assertEqual(out['attempted'],[VIDEO])
        self.assertEqual(len(self.calls),3)
        self.assertTrue(out['forward_coverage']['complete'])
        self.assertEqual(self.db.execute('select count(*) from videos').fetchone()[0],1)

    def test_source_aliases_preserve_observations_and_manual_state(self):
        self.invoke()
        before=[tuple(r) for r in self.db.execute('select * from observations')]
        first_seen=self.db.execute('select first_seen from videos where id=?',(VIDEO,)).fetchone()[0]
        self.now+=timedelta(minutes=30);self.invoke()
        self.assertEqual(before,[tuple(r) for r in self.db.execute('select * from observations')])
        self.assertEqual(self.db.execute('select first_seen from videos where id=?',(VIDEO,)).fetchone()[0],first_seen)
        self.assertEqual(self.db.execute('pragma user_version').fetchone()[0],3)
        self.assertEqual(self.db.execute('select count(*) from decisions').fetchone()[0],0)


if __name__=='__main__':unittest.main()
