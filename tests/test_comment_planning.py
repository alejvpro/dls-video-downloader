"""Disposable inventory/catalog integration tests; all planning is offline."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader.classification import CHANNEL
from dls_video_downloader.cli import main
from dls_video_downloader.comment_planning import plan, render
from dls_video_downloader.comments import CommentStore, IRISHWEEGEE
from dls_video_downloader.inventory import connect, import_document, decide

VIDEO = 'abcdefghijk'
START = datetime(2026, 11, 1, 14, tzinfo=timezone.utc)
DAY = timedelta(days=1)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'inventory.sqlite3'
        self.store = self.root / 'comments'
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def ingest(self, *, category='daily', **overrides):
        metadata = dict(id=VIDEO, channel_id=CHANNEL, title='DLS FULL SHOW',
                        live_status='was_live', release_date='20261101',
                        upload_date='20261101', release_timestamp=START.timestamp(),
                        timestamp=(START + timedelta(hours=3)).timestamp())
        if category == 'club':
            metadata.update(title='The Club', live_status='not_live')
        elif category == 'review':
            metadata.update(title='Unclassified production', live_status='not_live')
        metadata.update(overrides)
        metadata = {k: v for k,v in metadata.items() if v is not None}
        with self.db:
            import_document(self.db, metadata, 'videos' if category != 'daily' else 'live', 'synthetic')
            self.db.execute('UPDATE videos SET first_seen=?', (START.isoformat(),))

    def result(self, days=2, **kwargs):
        return plan(self.path, self.store, now=START + days * DAY, **kwargs)

    def item(self, **kwargs):
        return self.result(**kwargs)['items'][0]

    def record(self, day, *, target=False, outcome='uncertain'):
        with CommentStore(self.store) as store:
            return store.record(VIDEO, [{'id':'comment-one', 'author_id':IRISHWEEGEE,
                                         'text':'Original\n  text'}] if target else [],
                                outcome=outcome, collected_at=(START + day * DAY).isoformat())

    def test_completed_daily_without_media_is_due(self):
        self.ingest()
        item = self.item()
        self.assertTrue(item['due'])
        self.assertEqual(item['category'], 'daily_show')
        self.assertEqual(item['age_anchor_field'], 'release_timestamp')
        self.assertEqual(item['age_anchor'], START.isoformat())
        self.assertIsNotNone(item['age_anchor_provenance'])
        self.assertIsNotNone(item['first_completed_observation'])
        self.assertEqual(item['collection_kind'], 'initial')
        self.assertFalse((self.root / 'acquisitions').exists())

    def test_club_uses_availability_not_livestream_start(self):
        self.ingest(category='club')
        item = self.item()
        self.assertTrue(item['due'])
        self.assertEqual(item['category'], 'the_club')
        self.assertEqual(item['age_anchor_field'], 'timestamp')
        self.assertEqual(item['age_anchor'], (START + timedelta(hours=3)).isoformat())

    def test_review_excluded(self):
        self.ingest(category='review')
        self.assertEqual(self.item()['status'], 'excluded')
        self.assertEqual(self.result()['due_ids'], [])

    def test_skip_and_reject_override(self):
        self.ingest()
        for choice in ('skip','reject'):
            with self.db:decide(self.db, VIDEO, choice, 'test')
            self.assertEqual(self.item()['reason'], 'blocked')

    def test_manual_approval_in_window(self):
        self.ingest(category='review')
        with self.db:decide(self.db, VIDEO, 'approve', 'test')
        self.assertTrue(self.item()['due'])
        self.assertEqual(self.item()['selection'], 'approved')

    def test_manual_approval_cannot_bypass_cutoff(self):
        self.ingest(category='review')
        with self.db:decide(self.db, VIDEO, 'approve', 'test')
        item = self.item(days=16)
        self.assertFalse(item['due'])
        self.assertEqual(item['reason'], 'collection_window_expired')
        self.assertTrue(item['refresh']['manual_allowed'])

    def test_historical_approval_cannot_bypass_forward_range(self):
        old = START - 60 * DAY
        self.ingest(category='review', release_timestamp=old.timestamp(), timestamp=old.timestamp())
        with self.db:decide(self.db, VIDEO, 'approve', 'test')
        self.assertEqual(self.item()['reason'], 'outside_forward_range')

    def test_historical_automatic_excluded(self):
        old = START - 60 * DAY
        self.ingest(release_timestamp=old.timestamp())
        self.assertEqual(self.item()['reason'], 'outside_range')

    def test_retained_daily_classification_requires_completed_status(self):
        self.ingest()
        self.ingest(live_status='is_live', title='Retitled')
        self.assertEqual(self.item()['reason'], 'completed_livestream_required')

    def test_upcoming_approved_excluded(self):
        self.ingest(category='review', live_status='is_upcoming')
        with self.db:decide(self.db, VIDEO, 'approve', 'test')
        self.assertEqual(self.item()['reason'], 'video_not_completed')

    def test_missing_catalog_not_initialized(self):
        self.ingest()
        self.assertEqual(self.result()['comment_catalog'], 'missing')
        self.assertFalse(self.store.exists())

    def test_empty_catalog(self):
        self.ingest()
        with CommentStore(self.store):pass
        self.assertTrue(self.item()['due'])
        self.assertIsNone(self.item()['last_collection_at'])

    def test_invalid_catalog_fails_without_migration(self):
        self.ingest();self.store.mkdir()
        path = self.store / 'comments.sqlite3'
        with sqlite3.connect(path):pass
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Unsupported'):self.result()
        self.assertEqual(path.read_bytes(), before)

    def test_target_awaiting_confirmation(self):
        self.ingest();self.record(1.5,target=True)
        item = self.item()
        self.assertFalse(item['due'])
        self.assertEqual(item['collection_kind'], 'confirmation')
        self.assertEqual(item['refresh']['phase'], 'confirmation')
        self.assertEqual(item['refresh']['confirmation_due_at'], (START + 2.5 * DAY).isoformat())
        self.assertEqual(item['first_collection_at'], (START + 1.5 * DAY).isoformat())

    def test_uncertain_confirmation_completes_even_target_absent(self):
        self.ingest();self.record(0,target=True);self.record(1)
        item = self.item()
        self.assertEqual(item['status'], 'completed')
        self.assertEqual(item['reason'], 'confirmation_complete')
        self.assertIsNotNone(item['refresh']['confirmation_attempt_id'])
        self.assertIsNotNone(item['refresh']['targets'][0]['first_seen_at'])

    def test_day14_final_then_suspension(self):
        self.ingest();self.record(1)
        self.assertEqual(self.item(days=10)['reason'], 'waiting_for_final_check')
        self.assertTrue(self.item(days=14)['due'])
        self.record(14)
        self.assertEqual(self.item(days=14)['status'], 'completed')
        self.assertFalse(self.item(days=15)['due'])

    def test_backoff_and_cooldown(self):
        self.ingest();self.record(2,outcome='failed')
        self.assertEqual(self.item()['reason'], 'retry_backoff')
        self.assertFalse(self.item()['due'])
        self.assertEqual(self.item(cooldown_until=START + 3 * DAY)['reason'], 'collector_cooldown')

    def test_date_precision(self):
        self.ingest(release_timestamp=None, timestamp=None)
        item = self.item()
        self.assertEqual(item['age_anchor_field'], 'release_date')
        self.assertEqual(item['age_anchor_precision'], 'date')
        self.assertEqual(item['age_anchor'], '2026-11-01T00:00:00+00:00')

    def test_livestream_does_not_use_availability_as_start(self):
        self.ingest(release_timestamp=None)
        self.assertEqual(self.item()['age_anchor_field'], 'release_date')

    def test_missing_and_malformed_publication_fail_closed(self):
        for value in (None, 'not-a-date'):
            self.ingest(release_timestamp=None, timestamp=None, release_date=value, upload_date=None)
            item = self.item()
            self.assertFalse(item['due'])
            self.assertEqual(item['status'], 'excluded')
            self.assertEqual(item['age_anchor_field'], 'first_seen')

    def test_numeric_timestamp_without_date_is_eligible(self):
        self.ingest(release_date=None,upload_date=None)
        self.assertTrue(self.item()['due'])

    def test_repeatable_and_readonly_no_network_or_importer(self):
        self.ingest();self.record(0,target=True)
        before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(socket, 'socket', side_effect=AssertionError('network')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('subprocess')), \
             patch('dls_video_downloader.inventory.import_document', side_effect=AssertionError('import')), \
             patch('dls_video_downloader.inventory.reclassify', side_effect=AssertionError('classify')):
            one=self.result();two=self.result()
        self.assertEqual(one,two)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_cli_json_and_human(self):
        self.ingest()
        args=['--db',str(self.path),'comments','plan','--store-root',str(self.store),'--now',(START+DAY).isoformat()]
        for extra in (['--json'],[]):
            out=io.StringIO()
            with redirect_stdout(out):self.assertEqual(main(args+extra),0)
            if extra:self.assertEqual(json.loads(out.getvalue())['due_ids'],[VIDEO])
            else:
                self.assertIn('UTC',out.getvalue());self.assertIn('release_timestamp',out.getvalue())
        self.assertFalse(self.store.exists())

    def test_naive_cli_time_fails_without_files(self):
        self.ingest()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--db',str(self.path),'comments','plan','--store-root',str(self.store),'--now','2026-11-02T00:00:00']),1)
        self.assertFalse(self.store.exists())

    def test_daily_exact_cutoff_and_late_initial(self):
        self.ingest()
        self.assertTrue(self.item(days=10)['due'])
        self.assertTrue(self.item(days=14.999)['due'])
        item = self.item(days=15)
        self.assertFalse(item['due'])
        self.assertEqual(item['refresh']['deadline_at'], (START+15*DAY).isoformat())

    def test_existing_history_prevents_duplicate_initial(self):
        self.ingest();self.record(2)
        item = self.item()
        self.assertFalse(item['due'])
        self.assertEqual(item['collection_kind'], 'refresh')
        self.assertEqual(item['reason'], 'daily_collection')

    def test_explicit_readonly_connections_reject_writes(self):
        self.ingest()
        from dls_video_downloader import comment_planning
        original = comment_planning._plan
        def inspect(inventory, comments, *args):
            with self.assertRaises(sqlite3.OperationalError):
                inventory.execute("UPDATE videos SET latest_title='changed'")
            with self.assertRaises(sqlite3.OperationalError):
                comments.execute("DELETE FROM collection_attempts")
            return original(inventory, comments, *args)
        with CommentStore(self.store):pass
        with patch.object(comment_planning, '_plan', side_effect=inspect):
            self.assertTrue(self.item()['due'])

    def test_missing_inventory_cli_never_creates_database_or_logs(self):
        absent = self.root / 'absent.sqlite3'
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--db',str(absent),'comments','plan',
                                   '--store-root',str(self.store)]),1)
        self.assertFalse(absent.exists())
        self.assertFalse(self.store.exists())
        self.assertFalse((self.root / 'runner-fatal.log').exists())

    def test_render_reports_observed_not_exhaustive(self):
        self.ingest();self.record(0,target=True);self.record(1)
        text=render(self.result())
        self.assertIn('IrishWeegee=observed',text)
        self.assertIn('does not establish exhaustive',text)


if __name__ == '__main__':
    unittest.main()
