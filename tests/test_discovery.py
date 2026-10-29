import json
from pathlib import Path
import tempfile
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dls_video_downloader import acquisition as media
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.discovery import workflow
from dls_video_downloader.inventory import connect, decide, import_document, report
from dls_video_downloader import operations
from dls_video_downloader.cli import main

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

    def test_discovery_executable_paths_do_not_depend_on_shell_path(self):
        self.run_workflow()
        for command in self.calls:
            self.assertEqual(command[0], media.YT_DLP)
            self.assertEqual(command[command.index('--ffmpeg-location')+1], media.FFMPEG)
            self.assertEqual(command[command.index('--js-runtimes')+1], 'node:'+media.NODE)

    def test_limits_supervised_filter_and_existing_receipt(self):
        self.run_workflow()
        receipt = self.state / 'fixture-receipt.json'
        receipt.write_text('{}')
        with self.db:
            self.db.execute('INSERT INTO acquisitions VALUES(?,?,?,?,?,?)', (CLUB, 'time', str((self.state/'media').relative_to(media.PROJECT)), str(receipt.relative_to(media.PROJECT)), 10, 'hash'))
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
            self.run_workflow(per_tab=201)
        result = self.run_workflow(per_tab=1, max_enrich=0, acquire_media=True)
        self.assertEqual(result['encountered'], {'live': 1, 'videos': 1})
        self.assertTrue(result['warnings'])
        self.assertEqual(result['download_attempts'], 0)
        self.assertEqual(len(self.calls), 2)

    def test_persistent_delay_then_successful_retry(self):
        at = datetime.now(timezone.utc)
        with patch.object(operations, 'utc', return_value=at), patch.object(media, 'acquire', side_effect=ValueError('network timeout')):
            first = self.run_workflow(acquire_media=True, only_id=DAILY)
        self.assertEqual(first['status'], 'partial_failure')
        self.db.close()
        self.db = connect(self.state / 'db.sqlite3')
        with patch.object(operations, 'utc', return_value=at + timedelta(minutes=30)), patch.object(media, 'acquire') as acquire:
            second = self.run_workflow(acquire_media=True, only_id=DAILY)
            acquire.assert_not_called()
        self.assertEqual(second['download_attempts'], 0)
        self.assertTrue(second['retry_deferred'])
        with patch.object(operations, 'utc', return_value=at + timedelta(minutes=61)), patch.object(media, 'acquire', return_value={'status': 'complete', 'sha256': 'hash'}) as acquire:
            third = self.run_workflow(acquire_media=True, only_id=DAILY)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(third['status'], 'success')
        self.assertEqual(self.db.execute("SELECT count(*) FROM retry_state WHERE operation='acquisition'").fetchone()[0], 0)

    def test_repeated_failures_back_off_without_double_counting(self):
        at = datetime.now(timezone.utc)
        for delay in (0, 61):
            with patch.object(operations, 'utc', return_value=at + timedelta(minutes=delay)), patch.object(media, 'acquire', side_effect=ValueError('network timeout')):
                self.run_workflow(acquire_media=True, only_id=DAILY)
        row = self.db.execute("SELECT * FROM retry_state WHERE video_id=? AND operation='acquisition'", (DAILY,)).fetchone()
        self.assertEqual(row['failures'], 2)
        self.assertEqual(datetime.fromisoformat(row['next_retry']), at + timedelta(minutes=181))

    def test_pre_window_and_unknown_approvals_require_manual_command(self):
        self.full[DAILY]['release_date'] = '20261025'
        del self.full[CLUB]['upload_date']
        with self.db:
            decide(self.db, DAILY, 'approve', 'Historic exception')
            decide(self.db, CLUB, 'approve', 'Undated exception')
        with patch.object(media, 'acquire') as acquire:
            result = self.run_workflow(acquire_media=True)
            acquire.assert_not_called()
        self.assertEqual(result['eligible'], [])
        self.assertEqual(len(result['deferred']), 2)
        self.assertTrue(all(i['eligibility'] == 'eligible' for i in report(self.db, '2026-10-26')['items'] if i['decision']))
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0], 2)

    def test_default_videos_coverage_finds_club_after_many_clips(self):
        clips = [{'id': f'clip{i:07d}', '_type': 'url', 'title': 'A routine excerpt', 'duration': 500} for i in range(70)]
        self.videos = clips + [self.videos[0]]
        with patch.object(media, 'acquire', return_value={'status': 'complete', 'sha256': 'hash'}) as acquire:
            result = self.run_workflow(acquire_media=True, only_id=CLUB)
        self.assertEqual(result['limits']['videos'], 75)
        self.assertEqual(result['limits']['streams'], 25)
        self.assertIn(CLUB, result['enriched'])
        self.assertEqual(acquire.call_args.args[1], CLUB)
        full_calls = [c for c in self.calls if '--flat-playlist' not in c]
        self.assertEqual(len(full_calls), 2)  # Daily + Club, not 70 excerpts.

    def test_daily_backlog_cannot_starve_new_club_enrichment(self):
        base = dict(self.live[0])
        for i in range(20):
            video_id = f'live{i:07d}'
            self.live.append({**base, 'id': video_id})
            self.full[video_id] = {**self.full[DAILY], 'id': video_id}
        self.videos = [{'id': f'clip{i:07d}', '_type': 'url', 'title': 'An ordinary excerpt', 'duration': 500} for i in range(70)] + [self.videos[0]]
        result = self.run_workflow(max_enrich=2)
        self.assertEqual(set(result['enriched']), {DAILY, CLUB})

    def test_cli_exit_codes_and_quiet_failure_visibility(self):
        arguments = ['--db', str(self.state/'db.sqlite3'), 'run', '--quiet']
        with patch('dls_video_downloader.discovery.workflow', side_effect=operations.OperationBusy('busy')), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 75)
        self.assertIn('overlap', error.getvalue())
        partial = {'failures': [{'error': 'network timeout'}], 'finished_at': 'timestamp', 'log': 'events.jsonl'}
        with patch('dls_video_downloader.discovery.workflow', return_value=partial), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 2)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('partial_failure', error.getvalue())
        with patch('dls_video_downloader.discovery.workflow', side_effect=ValueError('fatal fixture')), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 1)
        self.assertIn('fatal_failure', error.getvalue())

    def test_unexpected_item_error_is_fatal_not_retryable(self):
        def bug(*args, **kwargs):
            raise KeyError('unexpected fixture bug')
        with patch.object(media, 'acquire', side_effect=bug):
            with self.assertRaises(KeyError):
                self.run_workflow(acquire_media=True, only_id=DAILY)
        self.assertEqual(self.db.execute('SELECT count(*) FROM retry_state').fetchone()[0], 0)
        row = self.db.execute('SELECT * FROM runner_runs').fetchone()
        self.assertEqual(row['status'], 'fatal_failure')
        diagnostic = (self.state/'runs'/row['run_id']/'fatal.stderr.log').read_text()
        self.assertIn('KeyError', diagnostic)
        self.assertIn('test_discovery.py:', diagnostic)

    def test_quiet_error_before_run_initialization_has_safe_diagnostics(self):
        arguments = ['--db',str(self.state/'db.sqlite3'),'run','--quiet']
        def bug(*args, **kwargs):
            raise RuntimeError('Cookie: sensitive-fixture\nhttps://example.com/play?token=sensitive-fixture')
        with patch('dls_video_downloader.discovery.workflow',side_effect=bug), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments),1)
        self.assertEqual(output.getvalue(),'')
        self.assertIn('RuntimeError',error.getvalue())
        self.assertIn('test_discovery.py:',error.getvalue())
        self.assertNotIn('sensitive-fixture',error.getvalue())
        fallback = self.state/'runner-fatal.log'
        self.assertTrue(fallback.is_file())
        self.assertNotIn('sensitive-fixture',fallback.read_text())

    def test_per_run_download_budget_reports_remaining_candidates(self):
        with patch.object(media, 'acquire', return_value={'status': 'complete', 'sha256': 'hash'}) as acquire:
            result = self.run_workflow(acquire_media=True, max_downloads=1)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(result['download_attempts'], 1)
        self.assertEqual(len(result['deferred']), 1)

    def test_coverage_frontier_gap_is_visible_and_does_not_advance(self):
        self.run_workflow()
        self.live = []
        result = self.run_workflow()
        self.assertEqual(result['status'], 'partial_failure')
        self.assertTrue(any(f['operation'] == 'coverage' for f in result['failures']))
        self.assertEqual(self.db.execute("SELECT head_id FROM scan_frontiers WHERE source='live'").fetchone()[0], DAILY)

    def test_enrichment_failure_delay_persists(self):
        self.full[DAILY] = {'id': DAILY}
        self.run_workflow()
        self.calls = []
        result = self.run_workflow()
        self.assertTrue(result['retry_deferred'])
        self.assertFalse(any(c[-1].endswith('='+DAILY) for c in self.calls))

    def test_run_history_and_terminal_report(self):
        result = self.run_workflow()
        row = self.db.execute('SELECT * FROM runner_runs WHERE run_id=?', (result['run_id'],)).fetchone()
        self.assertEqual(row['status'], 'success')
        self.assertIsNotNone(row['finished_at'])
        summary = json.loads((Path(result['log']).parent / 'summary.json').read_text())
        self.assertEqual(summary['status'], 'success')
        status = operations.runtime_report(self.db, report(self.db, '2026-10-26')['items'], '2026-10-26')
        self.assertEqual(len(status['recent_runs']), 1)
        self.assertEqual(set(status['pending_forward']), {DAILY, CLUB})
