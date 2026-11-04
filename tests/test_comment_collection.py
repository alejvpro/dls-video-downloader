"""Offline manual-collector tests: synthetic JSON, temporary stores, no YouTube."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader import comment_collection as cc
from dls_video_downloader.cli import main
from dls_video_downloader.comments import CommentStore, IRISHWEEGEE


VIDEO = "abcdefghijk"
VERSION = b"[debug] yt-dlp version stable@2026.10.26 fixture\n[debug] JS runtimes: node-22.23.4\n"


def source(comment_id="Ugx-fixture", **fields):
    return {"id": comment_id, "text": "  Source\n\n[0:00] untouched  \n",
            "author_id": IRISHWEEGEE, "author": "@IrishWeegee", "parent": "root",
            "timestamp": 1791936000, "_time_text": "3 weeks ago (edited)",
            "like_count": 10, **fields}


def execution(comments, *, stderr=VERSION, returncode=0, termination=None, **fields):
    payload = {"id": VIDEO, "comments": comments,
               "comment_count": len(comments) if isinstance(comments, list) else None,
               "formats": [{"url": "https://playback.example/expiring-secret"}],
               "description": "unrelated-video-payload", **fields}
    return cc.Execution(json.dumps(payload).encode(), stderr, returncode, termination)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "comments"

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, result=None, **kwargs):
        with patch.object(cc, "run_extractor", return_value=result or execution([source()])) as run:
            collected = cc.collect(VIDEO, root=self.root, **kwargs)
        return collected, run

    def attempt(self, result):
        with sqlite3.connect(self.root / "comments.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM collection_attempts WHERE attempt_id=?", (result["attempt_id"],)).fetchone())

    def test_collection_persistence_version_and_portable_record(self):
        result, _ = self.collect()
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(result["retrieval_state"], "returned_nonempty")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["retrieved_count"], 1)
        self.assertEqual(result["target_matches"][IRISHWEEGEE], ["Ugx-fixture"])
        portable = json.loads(Path(result["collection_record"]).read_bytes())
        self.assertEqual(portable["asset_type"], "comments")
        self.assertEqual(portable["source"]["video_id"], VIDEO)
        self.assertEqual(portable["collection"]["tool"]["version"], "stable@2026.10.26 fixture")
        self.assertEqual(portable["collection"]["tool"]["js_runtimes"], "node-22.23.4")
        raw = Path(result["snapshot_path"]).read_bytes()
        self.assertEqual(json.loads(raw)["comments"], [source()])
        self.assertEqual(portable["snapshot"]["size"], len(raw))
        self.assertEqual(portable["snapshot"]["sha256"], result["snapshot_hash"])
        self.assertEqual(portable["comment_order"], ["Ugx-fixture"])
        files = list(self.root.rglob("*.json"))
        text = "\n".join(p.read_text() for p in files)
        with sqlite3.connect(self.root / "comments.sqlite3") as db:
            text += "\n".join(db.iterdump())
        self.assertNotIn("expiring-secret", text)
        self.assertNotIn("unrelated-video-payload", text)
        self.assertNotIn("formats", text)

    def test_safe_metadata_only_command_and_budgets(self):
        _, run = self.collect()
        argv, timeout, fd = run.call_args.args
        self.assertEqual(argv[0], "/usr/local/bin/yt-dlp-new")
        self.assertEqual(argv[-1], f"https://www.youtube.com/watch?v={VIDEO}")
        for flag in ("--skip-download", "--no-write-info-json", "--no-write-subs", "--no-write-auto-subs",
                     "--no-write-thumbnail", "--no-cache-dir", "--ignore-config", "--no-plugin-dirs", "--no-playlist"):
            self.assertIn(flag, argv)
        self.assertIn("node:" + cc.NODE, argv)
        self.assertIn("youtube:max_comments=1000,all,all,all,all;comment_sort=new", argv)
        self.assertEqual(timeout, 600)
        for retry in ("--retries", "--extractor-retries", "--fragment-retries"):
            self.assertEqual(argv[argv.index(retry) + 1], "0")

    def test_empty_success_remains_uncertain_coverage(self):
        result, _ = self.collect(execution([]))
        self.assertEqual(result["retrieval_state"], "returned_empty")
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(result["retrieved_count"], 0)
        self.assertEqual(json.loads(Path(result["snapshot_path"]).read_bytes())["comments"], [])

    def test_explicit_disabled_is_distinct_from_null_without_evidence(self):
        result, _ = self.collect(execution(None, stderr=VERSION + b"WARNING: [youtube] Comments are disabled for this video\n"))
        self.assertEqual(result["outcome"], "disabled")
        self.assertIsNone(result["snapshot_path"])
        unknown, _ = self.collect(execution(None))
        self.assertEqual(unknown["outcome"], "failed")

    def test_explicit_turned_off_message_is_disabled(self):
        result, _ = self.collect(execution(None, stderr=VERSION + b"WARNING: YouTube said: Comments are turned off.\n"))
        self.assertEqual(result["outcome"], "disabled")

    def test_null_or_conflicting_extracted_counts_are_partial(self):
        for count in (None, 10):
            with self.subTest(count=count):
                result, _ = self.collect(execution([source()], comment_count=count))
                self.assertEqual(result["outcome"], "partial")
                self.assertIsNotNone(result["snapshot_path"])

    def test_absent_author_is_only_unobserved(self):
        found, _ = self.collect()
        absent, _ = self.collect(execution([source(author_id="UC" + "b" * 22)]))
        self.assertEqual(absent["target_matches"][IRISHWEEGEE], [])
        with sqlite3.connect(self.root / "comments.sqlite3") as db:
            self.assertIsNotNone(db.execute("SELECT first_observed_at FROM target_presence WHERE author_id=?", (IRISHWEEGEE,)).fetchone()[0])
        self.assertTrue(Path(found["snapshot_path"]).exists())

    def test_capped_and_incomplete_results(self):
        result, _ = self.collect(max_comments=1)
        self.assertEqual(result["outcome"], "partial")
        self.assertTrue(result["coverage"]["cap_reached"])
        self.assertEqual(result["exit_code"], 2)
        partial, _ = self.collect(execution([source()], stderr=VERSION + b"WARNING: Incomplete data received\n"))
        self.assertEqual(partial["outcome"], "partial")

    def test_warning_is_retained_sanitized_and_not_complete(self):
        result, _ = self.collect(execution([source()], stderr=VERSION + b"WARNING: Something changed https://private.example/playback?token=secret\n"))
        self.assertEqual(result["outcome"], "uncertain")
        self.assertIn("[URL omitted]", result["warnings"][0])
        self.assertNotIn("private.example", Path(result["collection_record"]).read_text())

    def test_failure_and_rate_limit_do_not_retry_or_damage_history(self):
        first, _ = self.collect()
        before = Path(first["snapshot_path"]).read_bytes()
        failed, run = self.collect(cc.Execution(stderr=b"ERROR: HTTP Error 429: Too Many Requests\n", returncode=1))
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["exit_code"], 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(Path(first["snapshot_path"]).read_bytes(), before)
        partial, _ = self.collect(execution([source(like_count=11)], returncode=1))
        self.assertEqual(partial["outcome"], "partial")
        self.assertIsNotNone(partial["snapshot_path"])

    def test_timeout_interruption_and_partial_json(self):
        for reason, exit_code in (("timeout", 2), ("interrupted", 130), ("output_limit", 2)):
            with self.subTest(reason=reason):
                result, _ = self.collect(cc.Execution(stdout=b'{"comments": [', termination=reason, returncode=-9))
                self.assertEqual(result["retrieval_state"], reason)
                self.assertEqual(result["outcome"], "failed")
                self.assertEqual(result["exit_code"], exit_code)
                self.assertIsNone(result["snapshot_path"])
        partial, _ = self.collect(execution([source()], termination="interrupted", returncode=-9))
        self.assertEqual(partial["outcome"], "partial")
        self.assertEqual(partial["exit_code"], 130)

    def test_launch_failure_and_early_keyboard_interrupt_are_recorded(self):
        for error, reason in ((FileNotFoundError("Missing tool"), "launch_failed"),
                              (KeyboardInterrupt(), "interrupted")):
            with patch.object(cc, "run_extractor", side_effect=error):
                result = cc.collect(VIDEO, root=self.root)
            self.assertEqual(result["retrieval_state"], reason)
            self.assertEqual(result["outcome"], "failed")

    def test_invalid_inputs_never_start_process_or_create_store(self):
        for video in ("bad", "../bad-path", "https://youtube.com/watch?v=" + VIDEO):
            with patch.object(cc, "run_extractor") as run, self.assertRaises(ValueError):
                cc.collect(video, root=self.root)
            run.assert_not_called()
        for kwargs in ({"max_comments": 1001}, {"timeout": 601}, {"max_comments": True}):
            with self.assertRaises(ValueError):
                cc.collect(VIDEO, root=self.root, **kwargs)
        self.assertFalse(self.root.exists())

    def test_repeated_manual_results_deduplicate_source_artifacts(self):
        first, _ = self.collect()
        again, _ = self.collect()
        self.assertEqual(first["snapshot_hash"], again["snapshot_hash"])
        self.assertNotEqual(first["attempt_id"], again["attempt_id"])
        self.assertEqual(len(list(self.root.rglob("snapshots/*.json"))), 1)
        self.assertEqual(len(list(self.root.rglob("collections/*.json"))), 2)

    def test_wrong_video_omitted_comments_and_malformed_payload_fail_closed(self):
        for data in ({"id": "other-video", "comments": [source()]}, {"id": VIDEO}, []):
            result, _ = self.collect(cc.Execution(stdout=json.dumps(data).encode(), returncode=0))
            self.assertEqual(result["outcome"], "failed")
            self.assertIsNone(result["snapshot_path"])
        result, _ = self.collect(execution("not a list"))
        self.assertEqual(result["outcome"], "invalid")
        duplicate, _ = self.collect(execution([source(), source()]))
        self.assertEqual(duplicate["outcome"], "invalid")

    def test_comments_lock_does_not_use_video_lock(self):
        with cc.collection_lock(self.root), patch.object(cc, "run_extractor") as run:
            with self.assertRaises(cc.CommentBusy):
                cc.collect(VIDEO, root=self.root)
        run.assert_not_called()

    def test_cli_bypasses_inventory_and_classification(self):
        output = io.StringIO()
        with patch.object(cc, "run_extractor", return_value=execution([source()])), \
                patch("dls_video_downloader.cli.connect", side_effect=AssertionError("Video inventory opened")), \
                patch("dls_video_downloader.cli.reclassify", side_effect=AssertionError("Classification invoked")), \
                patch("dls_video_downloader.acquisition.operation_lock", side_effect=AssertionError("Media lock used")), \
                redirect_stdout(output):
            code = main(["--db", str(self.root / "must-not-exist.sqlite3"), "comments", "collect", VIDEO,
                         "--store-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["video_id"], VIDEO)
        self.assertFalse((self.root / "must-not-exist.sqlite3").exists())

    def test_cli_validation_and_independent_error_codes(self):
        for arguments in (["comments", "collect", "bad"], ["comments", "collect", VIDEO, "--max-comments", "1001"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(arguments)
            self.assertEqual(exc.exception.code, 2)
        for error, code in ((cc.CommentBusy("Busy"), 75), (OSError("Disk full"), 1), (KeyboardInterrupt(), 130)):
            with patch.object(cc, "collect", side_effect=error), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["comments", "collect", VIDEO]), code)

    def test_export_is_offline_repeatable_and_failure_can_be_repaired(self):
        with patch.object(CommentStore, "export_attempt", side_effect=OSError("Export failed")):
            with self.assertRaisesRegex(ValueError, "committed, but portable export failed"):
                self.collect()
        with CommentStore(self.root) as store:
            attempt = store.db.execute("SELECT attempt_id FROM collection_attempts").fetchone()[0]
            first = store.export_attempt(attempt)
            before = first.read_bytes(), first.stat().st_mtime_ns
            self.assertEqual(first, store.export_attempt(attempt))
            self.assertEqual(before, (first.read_bytes(), first.stat().st_mtime_ns))
            first.write_bytes(b"damaged")
            with self.assertRaises(ValueError):
                store.export_attempt(attempt)

    def test_diagnostics_are_bounded_and_sensitive_lines_removed(self):
        raw = (b"WARNING: " + b"x" * 80000 + b"\nAuthorization: Bearer secret\n")
        result, _ = self.collect(execution([source()], stderr=raw))
        self.assertLessEqual(len(result["coverage"]["diagnostics"].encode()), cc.DIAGNOSTIC_LIMIT)
        self.assertNotIn("Bearer secret", result["coverage"]["diagnostics"])

    def test_diagnostic_truncation_keeps_version_and_marks_coverage(self):
        raw = VERSION + b"x\n" * 100000 + b"WARNING: tail warning\n"
        result, _ = self.collect(execution([source()], stderr=raw))
        self.assertTrue(result["coverage"]["diagnostics_truncated"])
        record = json.loads(Path(result["collection_record"]).read_bytes())
        self.assertEqual(record["collection"]["tool"]["version"], "stable@2026.10.26 fixture")


class ProcessTests(unittest.TestCase):
    """Tiny local Python processes exercise pipes and cleanup, never yt-dlp."""
    def run_local(self, program, timeout=3):
        with tempfile.TemporaryDirectory() as root, cc.collection_lock(Path(root)) as fd:
            return cc.run_extractor([sys.executable, "-B", "-c", program], timeout, fd)

    def test_pipe_draining_and_process_group_configuration(self):
        real = cc.subprocess.Popen
        with patch.object(cc.subprocess, "Popen", wraps=real) as popen:
            result = self.run_local("import sys; sys.stderr.write('diagnostic'); print('output')")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"output\n")
        self.assertEqual(result.stderr, b"diagnostic")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)

    def test_timeout_kills_process_group_and_reaps_parent(self):
        kill = os.killpg
        with patch.object(cc.os, "killpg", wraps=kill) as killed:
            result = self.run_local("import time; time.sleep(60)", timeout=0.1)
        self.assertEqual(result.termination, "timeout")
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(killed.call_args.args[1], signal.SIGKILL)

    def test_keyboard_interrupt_kills_process_group(self):
        with patch.object(cc.selectors.DefaultSelector, "select", side_effect=KeyboardInterrupt):
            result = self.run_local("import time; time.sleep(60)")
        self.assertEqual(result.termination, "interrupted")
        self.assertEqual(result.returncode, -signal.SIGKILL)

    def test_output_and_diagnostic_limits(self):
        with patch.object(cc, "STDOUT_LIMIT", 64):
            result = self.run_local("import sys; sys.stdout.write('x'*100000)")
        self.assertEqual(result.termination, "output_limit")
        self.assertLessEqual(len(result.stdout), 64)
        with patch.object(cc, "STDERR_LIMIT", 128):
            result = self.run_local("import sys; sys.stderr.write('x'*10000)")
        self.assertTrue(result.diagnostics_truncated)
        self.assertEqual(len(result.stderr), 128)


if __name__ == "__main__":
    unittest.main()
