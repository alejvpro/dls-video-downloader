import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import acquisition as media
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.discovery import workflow
from dls_video_downloader.inventory import connect, decide, import_document, report

DAILY = "IX9Dv4cBUKg"
CLUB = "GbncAA6bkx4"
SKIP = "KKL1eQ2eJCw"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        parent = media.PROJECT / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.state = Path(self.tmp.name)
        self.db = connect(self.state / "db.sqlite3")
        self.live = [{"id": DAILY, "_type": "url", "title": "LIVE at 9am ET | The Dan Le Batard Show", "live_status": "was_live"}]
        self.videos = [{"id": CLUB, "_type": "url", "title": "In The Club", "duration": 450},
                       {"id": SKIP, "_type": "url", "title": "Mystery Crate", "duration": 3000}]
        self.full = {
            DAILY: {"id": DAILY, "title": self.live[0]["title"], "channel_id": CHANNEL, "live_status": "was_live", "release_date": "20261028", "duration": 11405},
            CLUB: {"id": CLUB, "title": "In The Club", "channel_id": CHANNEL, "live_status": "not_live", "upload_date": "20261028", "duration": 450},
        }
        self.calls = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fake(self, args, log, timeout):
        self.calls.append(args)
        if "--flat-playlist" in args:
            limit = int(args[args.index("--playlist-end") + 1])
            entries = self.live if args[-1].endswith("/streams") else self.videos
            return json.dumps({"channel_id": CHANNEL, "epoch": 100, "entries": entries[:limit]})
        return json.dumps(self.full[args[-1].split("=")[-1]])

    def run_workflow(self, **kwargs):
        with patch.object(media, "run", side_effect=self.fake):
            return workflow(self.db, state=self.state, **kwargs)

    def test_discovery_new_known_repeated_and_no_download(self):
        with self.db:
            import_document(self.db, {"channel_id": CHANNEL, "entries": self.live}, "live", "fixture")
        with patch.object(media, 'acquire') as acquire:
            first = self.run_workflow()
            before = report(self.db, "2026-10-26")
            second = self.run_workflow()
            acquire.assert_not_called()
        self.assertEqual(first['new_ids'], sorted([CLUB, SKIP]))
        self.assertEqual(second['new_ids'], [])
        self.assertEqual(before, report(self.db, "2026-10-26"))
        self.assertEqual(first['encountered'], {'live': 1, 'videos': 2})
        self.assertEqual(first['classifications'], {'daily_show': 1, 'the_club': 1, 'skip': 1})
        self.assertEqual(set(first['eligible']), {DAILY, CLUB})

    def test_limits_supervised_filter_and_existing_receipt(self):
        self.run_workflow()
        with self.db:
            self.db.execute('INSERT INTO acquisitions VALUES(?,?,?,?,?,?)', (CLUB, 'time', 'media', 'receipt', 10, 'hash'))
        with patch.object(media, 'acquire', return_value={'status': 'complete', 'sha256': 'hash'}) as acquire:
            result = self.run_workflow(acquire_media=True, only_id=DAILY, max_downloads=1)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(acquire.call_args.args[1], DAILY)
        self.assertIn(CLUB, result['already_verified'])
        self.assertTrue(all('--playlist-end' in c for c in self.calls if '--flat-playlist' in c))

    def test_overrides_preserved_and_review_never_downloaded(self):
        self.videos.append({'id': 'abcdefghijk', 'title': 'Original interview', '_type': 'url'})
        with self.db:
            decide(self.db, DAILY, 'reject', 'Persistent')
        with patch.object(media, 'acquire', return_value={'status': 'complete', 'sha256': 'hash'}) as acquire:
            result = self.run_workflow(acquire_media=True)
        self.assertNotIn(DAILY, result['eligible'])
        self.assertIn('abcdefghijk', result['pending_reviews'])
        self.assertEqual(acquire.call_args.args[1], CLUB)
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0], 1)

    def test_missing_date_and_outside_window(self):
        del self.full[DAILY]['release_date']
        self.full[CLUB]['upload_date'] = '20261023'
        with patch.object(media, 'acquire') as acquire:
            result = self.run_workflow(acquire_media=True)
            acquire.assert_not_called()
        self.assertEqual(result['eligible'], [])
        self.assertEqual(report(self.db, '2026-10-26')['eligibility']['date_unknown'], 1)

    def test_ongoing_livestream_enriched_but_not_acquired(self):
        self.live[0]['live_status'] = 'is_live'
        self.full[DAILY]['live_status'] = 'is_live'
        self.full[DAILY]['is_live'] = True
        result = self.run_workflow(acquire_media=True, only_id=DAILY)
        self.assertIn(DAILY, result['enriched'])
        self.assertNotIn(DAILY, result['eligible'])
        self.assertEqual(result['acquired'], [])

    def test_enrichment_failure_and_missing_metadata(self):
        self.full[DAILY] = {'id': DAILY}
        result = self.run_workflow(acquire_media=True, only_id=DAILY)
        self.assertTrue(result['failures'])
        self.assertEqual(result['acquired'], [])
        # Other candidate enrichment continues after the failure.
        self.assertIn(CLUB, result['enriched'])

    def test_item_failure_does_not_prevent_next_candidate(self):
        with patch.object(media, 'acquire', side_effect=[ValueError('network failed'), {'status': 'complete', 'sha256': 'hash'}]) as acquire:
            result = self.run_workflow(acquire_media=True, max_downloads=2)
        self.assertEqual(acquire.call_count, 2)
        self.assertEqual(len(result['failures']), 1)
        self.assertEqual(len(result['acquired']), 1)

    def test_failed_tab_still_reports_other_tab(self):
        original = self.fake
        def failing(args, log, timeout):
            if args[-1].endswith('/streams'):
                raise ValueError('tab unavailable')
            return original(args, log, timeout)
        with patch.object(media, 'run', side_effect=failing):
            result = workflow(self.db, state=self.state)
        self.assertEqual(result['encountered'], {'videos': 2})
        self.assertTrue(result['failures'])

    def test_operation_lock_rejects_discovery_and_direct_acquisition(self):
        self.run_workflow()
        with media.operation_lock(self.state / 'acquisitions'):
            with patch.object(media, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'Another acquisition'):
                    workflow(self.db, state=self.state)
                with self.assertRaisesRegex(ValueError, 'Another acquisition'):
                    media.acquire(self.db, DAILY, root=self.state / 'acquisitions')
                run.assert_not_called()

    def test_bounds_and_zero_enrichment(self):
        with self.assertRaises(ValueError):
            self.run_workflow(per_tab=51)
        result = self.run_workflow(per_tab=1, max_enrich=0, acquire_media=True)
        self.assertEqual(result['encountered'], {'live': 1, 'videos': 1})
        self.assertTrue(result['warnings'])
        self.assertEqual(result['download_attempts'], 0)
        self.assertEqual(len(self.calls), 2)
