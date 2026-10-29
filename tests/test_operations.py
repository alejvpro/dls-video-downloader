from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import acquisition as media
from dls_video_downloader import operations as ops
from dls_video_downloader.inventory import connect, import_document
from dls_video_downloader.classification import CHANNEL

ID = 'abcdefghijk'


class OperationTests(unittest.TestCase):
    def setUp(self):
        parent = media.PROJECT / '.test-tmp'
        parent.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.state = Path(self.tmp.name)
        self.db = connect(self.state/'db.sqlite3')
        with self.db:
            import_document(self.db, {'id': ID, 'channel_id': CHANNEL, 'title': 'Fixture'}, 'other', 'fixture')
        self.clock = ops.utc() + timedelta(days=4)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def attempt(self, status='failed', name='media.f398.mp4.part', token='a'*32):
        folder = self.state/'acquisitions'/'.work'/ID/token
        folder.mkdir(parents=True)
        path = folder/name
        path.write_bytes(b'temporary bytes')
        old = (ops.utc() - timedelta(days=4)).isoformat()
        with self.db:
            self.db.execute('INSERT INTO acquisition_attempts VALUES(?,?,?,?,?,?,?)',
                            (token, ID, old, old if status!='running' else None, status, 'failed network request', str(folder.relative_to(media.PROJECT))))
        return path

    def cleanup(self, **kwargs):
        with media.operation_lock(self.state/'acquisitions'):
            return ops.cleanup_staging(self.db, self.state, clock=self.clock, **kwargs)

    def test_dry_run_then_safe_execute_preserves_history(self):
        path = self.attempt()
        result = self.cleanup()
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(path.exists())
        result = self.cleanup(execute=True)
        self.assertFalse(path.exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM acquisition_attempts').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM cleanup_events').fetchone()[0], 1)

    def test_complete_attempt_never_cleaned(self):
        path = self.attempt(status='complete')
        self.assertEqual(self.cleanup(execute=True)['files'], [])
        self.assertTrue(path.exists())

    def test_recent_partial_not_removed(self):
        path = self.attempt()
        os.utime(path, (self.clock.timestamp(), self.clock.timestamp()))
        self.assertEqual(self.cleanup(execute=True)['files'], [])
        self.assertTrue(path.exists())

    def test_symlink_and_unknown_files_are_preserved(self):
        path = self.attempt(name='unknown.bin')
        self.assertEqual(self.cleanup(execute=True)['files'], [])
        link = path.parent/'media.mkv.part'
        link.symlink_to(path)
        self.assertTrue(self.cleanup(execute=True)['skipped'])
        self.assertTrue(path.exists())
        self.assertTrue(link.is_symlink())

    def test_protected_manifest_evidence_and_final_media(self):
        path = self.attempt()
        final = self.state/'acquisitions'/ID
        final.mkdir()
        media_path = final/'media.mkv'
        media_path.write_bytes(b'verified fixture')
        receipt = final/'acquisition.json'
        receipt.write_text(json.dumps({'metadata_relative': str(path.relative_to(media.PROJECT))}))
        with self.db:
            self.db.execute('INSERT INTO acquisitions VALUES(?,?,?,?,?,?)', (ID, ops.utc().isoformat(), str(media_path.relative_to(media.PROJECT)), str(receipt.relative_to(media.PROJECT)), 16, 'hash'))
        self.assertTrue(self.cleanup(execute=True)['skipped'])
        self.assertTrue(path.exists())
        self.assertEqual(media_path.read_bytes(), b'verified fixture')

    def test_hardlink_never_deleted_as_incomplete_media(self):
        path = self.attempt()
        other = self.state/'final.mkv'
        os.link(path, other)
        self.assertTrue(self.cleanup(execute=True)['skipped'])
        self.assertTrue(path.exists())
        self.assertEqual(other.read_bytes(), b'temporary bytes')

    def test_stale_abandoned_attempt_is_recorded(self):
        path = self.attempt(status='running')
        self.cleanup(execute=True)
        self.assertFalse(path.exists())
        self.assertEqual(self.db.execute('SELECT status FROM acquisition_attempts').fetchone()[0], 'interrupted')
        self.assertIsNotNone(ops.retry_pending(self.db, ID, 'acquisition', clock=self.clock))

    def test_real_concurrent_process_prevents_cleanup(self):
        path = self.attempt(status='running')
        lock_path = self.state/'acquisitions'/'.acquisition.lock'
        script = 'import fcntl,sys,time; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(30)'
        with subprocess.Popen([sys.executable, '-B', '-c', script, str(lock_path)], stdout=subprocess.PIPE, text=True) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaises(ops.OperationBusy):
                    self.cleanup(execute=True)
                from dls_video_downloader.discovery import workflow
                with self.assertRaises(ops.OperationBusy):
                    workflow(self.db, state=self.state)
                self.assertEqual(self.db.execute("SELECT status FROM runner_runs").fetchone()[0], 'overlap')
                self.assertTrue(path.exists())
            finally:
                child.terminate(); child.wait(timeout=5)

    def test_inherited_operation_lock_survives_parent_descriptor_close(self):
        root = self.state/'acquisitions'
        with media.operation_lock(root):
            script = 'import time; print("ready",flush=True); time.sleep(30)'
            child = subprocess.Popen([sys.executable,'-B','-c',script],stdout=subprocess.PIPE,text=True,pass_fds=(media._operation_fd,))
            self.assertEqual(child.stdout.readline().strip(),'ready')
        try:
            with self.assertRaises(ops.OperationBusy):
                with media.operation_lock(root):
                    pass
        finally:
            child.terminate(); child.wait(timeout=5); child.stdout.close()
        with media.operation_lock(root):
            pass

    def test_cleanup_has_byte_and_file_limits(self):
        first = self.attempt()
        second = self.attempt(token='b'*32)
        result = self.cleanup(execute=True, max_files=1)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(sum(p.exists() for p in (first, second)), 1)

    def test_failure_categories_and_delay_cap(self):
        for error, category in [('network timeout','transient'),('Required 398 formats unavailable','formats'),('private video','unavailable'),('checksum mismatch','verification'),('Insufficient storage','storage')]:
            with self.subTest(category=category):
                result = ops.retry_failure(self.db, ID, category, ValueError(error), clock=self.clock)
                self.assertEqual(result['category'], category)
        for _ in range(20):
            result = ops.retry_failure(self.db, ID, 'acquisition', ValueError('network timeout'), clock=self.clock)
        self.assertEqual(result['next_retry'], (self.clock+timedelta(hours=24)).isoformat())
        self.assertEqual(result['failures'], 20)

    def test_diagnostics_are_bounded_and_sanitized(self):
        path = self.state/'tool.log'
        ops.write_log(path, 'Cookie: secret\nhttps://example.com/play?token=secret\n'+'x'*ops.LOG_LIMIT*2)
        self.assertLessEqual(path.stat().st_size, ops.LOG_LIMIT)
        self.assertNotIn('secret', path.read_text())

    def test_truncated_unicode_diagnostic_remains_valid_utf8(self):
        path = self.state/'unicode.log'
        ops.write_log(path, '€' * ops.LOG_LIMIT)
        self.assertLessEqual(path.stat().st_size, ops.LOG_LIMIT)
        self.assertIn('[diagnostic truncated]', path.read_text())

    def test_fatal_diagnostic_keeps_original_error_if_reporting_also_fails(self):
        try:
            try:
                raise RuntimeError('original fixture bug')
            except RuntimeError:
                raise OSError('logging fixture failure')
        except OSError as error:
            diagnostic = ops.fatal_diagnostic(error)
        self.assertIn('OSError: logging fixture failure', diagnostic)
        self.assertIn('RuntimeError: original fixture bug', diagnostic)
        self.assertIn('test_operations.py:', diagnostic)

    def test_retry_state_is_visible_in_a_separate_process(self):
        pending = ops.retry_failure(self.db, ID, 'acquisition', ValueError('network timeout'), clock=self.clock)
        script = ('import json,sys; from dls_video_downloader.inventory import connect; '
                  'from dls_video_downloader.operations import retry_pending; '
                  'c=connect(sys.argv[1],readonly=True); print(json.dumps(retry_pending(c,sys.argv[2],"acquisition")))')
        result = subprocess.run([sys.executable,'-B','-c',script,str(self.state/'db.sqlite3'),ID],
                                capture_output=True,text=True,check=True,timeout=10,cwd=media.PROJECT)
        self.assertEqual(json.loads(result.stdout)['next_retry'], pending['next_retry'])

    def test_schema_three_migration_rolls_back_on_midway_failure(self):
        # A disposable v2 fixture containing original inventory data.
        with self.db:
            for name in ['retry_state','runner_runs','scan_frontiers','cleanup_events']:
                self.db.execute('DROP TABLE '+name)
            self.db.execute('PRAGMA user_version=2')
        before = [tuple(r) for r in self.db.execute('SELECT * FROM videos')]
        connection = sqlite3.connect(self.state/'db.sqlite3')
        def authorizer(action, name, *unused):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == 'scan_frontiers' else sqlite3.SQLITE_OK
        connection.set_authorizer(authorizer)
        with patch('dls_video_downloader.inventory.sqlite3.connect', return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError):
                connect(self.state/'db.sqlite3')
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertFalse(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='retry_state'").fetchone())
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM videos')], before)
        migrated = connect(self.state/'db.sqlite3')
        self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 3)
        migrated.close()

    def test_run_logs_have_count_retention(self):
        for i in range(102):
            run_id = f'run-{i:03}'
            directory = self.state/'runs'/run_id
            directory.mkdir(parents=True)
            (directory/'events.jsonl').write_text('{}\n')
            (directory/'summary.json').write_text('{}')
            at = (ops.utc()-timedelta(minutes=i)).isoformat()
            with self.db:
                self.db.execute('INSERT INTO runner_runs VALUES(?,?,?,?,?)', (run_id,at,at,'success','{}'))
        ops.retain_run_logs(self.db,self.state)
        self.assertEqual(len(list((self.state/'runs').iterdir())), ops.RUN_LIMIT)

    def test_failed_evidence_retained_for_thirty_days(self):
        path = self.attempt(name='download.stderr.log')
        self.assertEqual(self.cleanup(execute=True)['files'], [])
        self.assertTrue(path.exists())
        with media.operation_lock(self.state/'acquisitions'):
            result = ops.cleanup_staging(self.db,self.state,execute=True,clock=self.clock+timedelta(days=31))
        self.assertFalse(path.exists())
        self.assertEqual(len(result['files']),1)
