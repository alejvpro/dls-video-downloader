import json
import fcntl
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from dls_video_downloader import acquisition as acq
from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.inventory import connect, import_file, import_document, decide

ID = "MbO7y_90zUI"


class AcquisitionTests(unittest.TestCase):
    def test_tool_timeout_is_bounded(self):
        log = self.root / 'timeout.log'
        with self.assertRaisesRegex(ValueError, 'timed out'):
            acq.run([sys.executable, '-B', '-c', 'import time; time.sleep(10)'], log, 0.05)
        self.assertTrue(log.is_file())

    def setUp(self):
        parent = acq.PROJECT / ".test-tmp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "inventory.sqlite3")
        self.metadata = {"id": ID, "channel_id": CHANNEL, "title": "LIVE at 9am ET | The Dan Le Batard Show",
                         "live_status": "was_live", "upload_date": "20261026", "duration": 11505,
                         "formats": [{"format_id": "398", "height": 720, "fps": 30, "vcodec": "av01.0.05M.08", "filesize": 100},
                                     {"format_id": "251", "acodec": "opus", "filesize": 20}]}
        self.source = self.root / "input.json"
        self.source.write_text(json.dumps({"channel_id": CHANNEL, "entries": [self.metadata]}))
        with self.db:
            import_file(self.db, self.source, "live")
        self.calls = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fake_run(self, args, log, timeout):
        self.calls.append(args)
        if "--dump-single-json" in args:
            log.write_text("Node runtime recognized")
            return json.dumps(self.metadata)
        if args[0] == acq.FFPROBE:
            log.write_text("")
            return json.dumps({"format": {"format_name": "matroska,webm", "duration": "11505.1"},
                               "streams": [{"codec_type": "video", "codec_name": "av1", "width": 1280, "height": 720, "avg_frame_rate": "30/1"},
                                           {"codec_type": "audio", "codec_name": "opus"}]})
        log.write_text("ffmpeg command line: -c copy")
        (log.parent / "media.mkv").write_bytes(b"tiny mock media")
        return ""

    def acquire(self):
        return acq.acquire(self.db, ID, root=self.root / "outputs")

    def test_success_and_repeat_without_network(self):
        with patch.object(acq, "run", side_effect=self.fake_run):
            result = self.acquire()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["video_format"], "398")
            self.assertEqual(result["audio_format"], "251")
            self.assertEqual(len(result["sha256"]), 64)
            before = len(self.calls)
            repeated = self.acquire()
        self.assertEqual(repeated["status"], "already_verified")
        self.assertTrue(all(args[0] == acq.FFPROBE for args in self.calls[before:]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM acquisitions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0], 1)
        self.assertEqual(list((self.root / 'outputs' / ID).glob('*.mkv')), [Path(result['path'])])
        self.assertEqual(list((self.root / 'outputs' / '.work').rglob('*.mkv')), [])
        self.assertFalse((self.root / 'outputs' / '.work' / ID).exists())
        self.assertEqual(Path(result['path']).stat().st_nlink, 1)
        self.assertTrue((acq.PROJECT / result['metadata_relative']).is_file())
        self.assertTrue((acq.PROJECT / result['merger_log_relative']).is_file())

    def test_legacy_cleanup_preserves_media_manifest_and_evidence(self):
        with patch.object(acq, 'run', side_effect=self.fake_run):
            result = self.acquire()
        media = Path(result['path'])
        manifest_path = media.parent / 'acquisition.json'
        manifest = json.loads(manifest_path.read_text())
        evidence = (acq.PROJECT / manifest['metadata_relative']).parent
        legacy = self.root / 'outputs' / '.work' / ID / manifest['attempt_id']
        legacy.parent.mkdir(parents=True)
        evidence.rename(legacy)
        for key in ('metadata_relative', 'merger_log_relative'):
            manifest[key] = str((legacy / Path(manifest[key]).name).relative_to(acq.PROJECT))
        manifest_path.write_text(json.dumps(manifest))
        acq.os.link(media, legacy / 'media.mkv')
        self.assertEqual(media.stat().st_nlink, 2)
        before = media.read_bytes(), manifest_path.read_bytes(), media.stat().st_ino
        with patch.object(acq, 'run', side_effect=self.fake_run):
            self.assertEqual(self.acquire()['status'], 'already_verified')
        self.assertEqual(before, (media.read_bytes(), manifest_path.read_bytes(), media.stat().st_ino))
        self.assertEqual(media.stat().st_nlink, 1)
        self.assertFalse((legacy / 'media.mkv').exists())
        self.assertTrue((legacy / 'metadata.json').is_file())

    def test_cleanup_failure_does_not_fail_verified_acquisition(self):
        with patch.object(acq, 'run', side_effect=self.fake_run), patch.object(Path, 'unlink', side_effect=PermissionError()):
            result = self.acquire()
        self.assertEqual(result['status'], 'complete')
        self.assertIn('deferred', result['cleanup'])
        self.assertEqual(self.db.execute('SELECT status FROM acquisition_attempts').fetchone()[0], 'complete')
        with patch.object(acq, 'run', side_effect=self.fake_run):
            self.assertEqual(self.acquire()['status'], 'already_verified')
        self.assertEqual(Path(result['path']).stat().st_nlink, 1)

    def test_lock_blocks_concurrent_operation_without_cleanup(self):
        root = self.root / 'outputs'
        root.mkdir()
        with (root / '.acquisition.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(acq, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'Another acquisition'):
                    self.acquire()
                run.assert_not_called()

    def test_cleanup_preserves_other_interrupted_attempts(self):
        old = self.root / 'outputs' / '.work' / ID / ('a' * 32)
        old.mkdir(parents=True)
        partial = old / 'media.mkv.part'
        partial.write_bytes(b'partial evidence')
        with patch.object(acq, 'run', side_effect=self.fake_run):
            result = self.acquire()
        self.assertEqual(partial.read_bytes(), b'partial evidence')
        self.assertFalse((old.parent / result['attempt_id']).exists())

    def test_cleanup_refuses_independent_staged_media(self):
        with patch.object(acq, 'run', side_effect=self.fake_run):
            result = self.acquire()
        evidence = (acq.PROJECT / result['metadata_relative']).parent
        staged = evidence / 'media.mkv'
        staged.write_bytes(b'not the verified inode')
        with patch.object(acq, 'run', side_effect=self.fake_run):
            repeated = self.acquire()
        self.assertEqual(repeated['status'], 'already_verified')
        self.assertIn('not the verified media hard link', repeated['cleanup'])
        self.assertEqual(staged.read_bytes(), b'not the verified inode')
        self.assertEqual(acq.sha256(Path(result['path'])), result['sha256'])

    def test_rejected_and_review_are_blocked_without_network(self):
        with self.db:
            decide(self.db, ID, "reject", "Do not acquire")
        with patch.object(acq, "run") as run:
            with self.assertRaises(ValueError):
                self.acquire()
            run.assert_not_called()
        with self.db:
            decide(self.db, ID, "clear", "Remove override")
            self.db.execute("UPDATE classifications SET category='review' WHERE video_id=?", (ID,))
        with patch.object(acq, "run") as run:
            with self.assertRaises(ValueError):
                self.acquire()
            run.assert_not_called()

    def test_approved_review_can_acquire(self):
        with self.db:
            self.db.execute("UPDATE classifications SET category='review' WHERE video_id=?", (ID,))
            decide(self.db, ID, "approve", "Keep")
        with patch.object(acq, "run", side_effect=self.fake_run):
            self.assertEqual(self.acquire()["status"], "complete")

    def test_explicit_historical_approval_remains_available_manually(self):
        self.metadata['upload_date'] = '20261025'
        with self.db:
            import_document(self.db, self.metadata, 'other', 'historical-fixture')
            decide(self.db, ID, 'approve', 'Explicit historical exception')
        with patch.object(acq, 'run', side_effect=self.fake_run):
            self.assertEqual(self.acquire()['status'], 'complete')

    def test_scheduled_acquisition_rechecks_source_date_before_media(self):
        self.metadata['upload_date'] = '20261025'
        with patch.object(acq, 'run', side_effect=self.fake_run):
            with self.assertRaisesRegex(ValueError, 'forward-window'):
                acq.acquire(self.db, ID, root=self.root/'outputs', _forward_only=True)
        self.assertEqual(len(self.calls), 1)  # Metadata only, no media command.

    def test_missing_formats(self):
        self.metadata['formats'] = []
        with patch.object(acq, "run", side_effect=self.fake_run):
            with self.assertRaisesRegex(ValueError, 'formats unavailable'):
                self.acquire()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.db.execute("SELECT status FROM acquisition_attempts").fetchone()[0], "failed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM acquisitions").fetchone()[0], 0)

    def test_acquisition_failure_delay_then_recovery(self):
        from datetime import timedelta
        from dls_video_downloader import operations
        available = self.metadata['formats']
        self.metadata['formats'] = []
        with patch.object(acq, 'run', side_effect=self.fake_run):
            with self.assertRaises(ValueError):
                self.acquire()
        pending = self.db.execute("SELECT * FROM retry_state WHERE video_id=?", (ID,)).fetchone()
        self.assertEqual(pending['failures'], 1)
        with patch.object(acq, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Retry deferred'):
                self.acquire()
            run.assert_not_called()
        self.metadata['formats'] = available
        with patch.object(operations, 'utc', return_value=operations.utc()+timedelta(days=1)), patch.object(acq, 'run', side_effect=self.fake_run):
            self.assertEqual(self.acquire()['status'], 'complete')
        self.assertEqual(self.db.execute('SELECT count(*) FROM retry_state').fetchone()[0], 0)

    def test_programming_error_does_not_create_transient_retry(self):
        with patch.object(acq, '_acquire_locked', side_effect=RuntimeError('unexpected fixture bug')):
            with self.assertRaises(RuntimeError):
                self.acquire()
        self.assertEqual(self.db.execute('SELECT count(*) FROM retry_state').fetchone()[0], 0)

    def test_insufficient_storage(self):
        with patch.object(acq, "run", side_effect=self.fake_run), patch.object(acq.shutil, "disk_usage", return_value=type('Usage', (), {'free': 1})()):
            with self.assertRaisesRegex(ValueError, "Insufficient"):
                self.acquire()
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_download(self):
        def interrupted(args, log, timeout):
            if '--dump-single-json' in args:
                return self.fake_run(args, log, timeout)
            (log.parent / 'media.mkv.part').write_bytes(b'partial')
            raise KeyboardInterrupt()
        with patch.object(acq, "run", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.acquire()
        self.assertEqual(self.db.execute("SELECT status FROM acquisition_attempts").fetchone()[0], "interrupted")
        self.assertFalse((self.root / 'outputs' / ID / 'acquisition.json').exists())

    def test_invalid_output(self):
        original = self.fake_run
        def invalid(args, log, timeout):
            result = original(args, log, timeout)
            if args[0] == acq.FFPROBE:
                return result.replace('"av1"', '"h264"')
            return result
        with patch.object(acq, "run", side_effect=invalid):
            with self.assertRaisesRegex(ValueError, '720p AV1'):
                self.acquire()
        self.assertEqual(self.db.execute("SELECT count(*) FROM acquisitions").fetchone()[0], 0)
        self.assertFalse((self.root / 'outputs' / ID / 'media.mkv').exists())

    def test_corruption_refuses_redownload_or_replacement(self):
        with patch.object(acq, "run", side_effect=self.fake_run):
            result = self.acquire()
        # Disposable fixture only; production outputs are never used by tests.
        Path(result['path']).write_bytes(b'corrupt')
        with patch.object(acq, "run") as run:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                self.acquire()
            run.assert_not_called()

    def test_incomplete_publication_blocks_redownload(self):
        final = self.root / 'outputs' / ID
        final.mkdir(parents=True)
        (final / 'media.mkv').write_bytes(b'uncertain')
        with patch.object(acq, "run") as run:
            with self.assertRaisesRegex(ValueError, 'manual recovery'):
                self.acquire()
            run.assert_not_called()
