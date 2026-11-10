import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import patch

from dls_video_downloader import acquisition, format_inspection as inspector, youtube_safety as safety
from dls_video_downloader.classification import CHANNEL

VIDEO = "kEMi9MO8dSI"
BOT = "ERROR: Sign in to confirm you're not a bot. LOGIN_REQUIRED"


def metadata(formats=None, **extra):
    return {"id": VIDEO, "channel_id": CHANNEL, "title": "The Club fixture",
            "live_status": "not_live", "was_live": False, "upload_date": "20261106",
            "duration": 447, "formats": formats if formats is not None else [
                {"format_id": "398", "vcodec": "av01.0.05M.08", "acodec": "none",
                 "width": 1280, "height": 720, "fps": 30, "has_drm": False,
                 "filesize": 1000, "url": "https://signed.example/video?token=secret"},
                {"format_id": "251", "vcodec": "none", "acodec": "opus", "has_drm": False,
                 "filesize_approx": 200, "http_headers": {"Authorization": "secret"}},
            ], **extra}


class FormatInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.calls = []
        self.current_metadata = metadata()

        def fake_run(argv, log, timeout):
            self.calls.append((list(argv), Path(log), timeout))
            Path(log).write_text("synthetic extractor stderr")
            return json.dumps(self.current_metadata)

        self.fake_run = fake_run

    def inspect(self, **kwargs):
        with safety.scope(self.state, "format-inspection"), patch.object(acquisition, "run", side_effect=self.fake_run):
            return inspector.inspect(VIDEO, self.state, execute=True, **kwargs)

    def test_default_plan_is_network_and_write_free(self):
        result = inspector.inspect(VIDEO, self.state)
        self.assertEqual(result["mode"], "plan")
        self.assertFalse(result["network"]); self.assertFalse(result["writes"])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state.exists())

    def test_cli_proposed_syntax_is_plan_by_default(self):
        from dls_video_downloader.cli import main
        output = io.StringIO()
        with redirect_stdout(output), patch.object(acquisition, "run") as run:
            code = main(["--db", str(self.state / "inventory.sqlite3"), "formats", "inspect", VIDEO, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "plan")
        run.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_valid_pair_returns_sanitized_allowlisted_metadata(self):
        result = self.inspect()
        self.assertTrue(result["selection"]["available"])
        self.assertEqual(result["format_listing"]["count"], 2)
        self.assertEqual(result["video"]["id"], VIDEO)
        serialized = json.dumps(result)
        self.assertNotIn("signed.example", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("url", serialized)
        self.assertNotIn("http_headers", serialized)
        self.assertEqual(len(self.calls), 1)

    def test_inspection_uses_shared_alternative_audio_selector(self):
        self.current_metadata["formats"] = [self.current_metadata["formats"][0], {
            "format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "language": "en",
            "format_note": "English original", "filesize": 200, "abr": 128,
            "url": "https://signed.invalid/?token=private"}]
        result = self.inspect()
        self.assertEqual(result["selection"]["audio_format_id"], "140")
        self.assertEqual(result["selection"]["audio_codec"], "aac")
        self.assertNotIn("signed.invalid", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_command_is_single_video_metadata_only_and_has_no_retries(self):
        result = self.inspect(timeout=17)
        argv, _, timeout = self.calls[0]
        self.assertEqual(timeout, 17)
        self.assertEqual(argv[0], acquisition.YT_DLP)
        self.assertIn("node:" + acquisition.NODE, argv)
        self.assertIn("--ignore-config", argv)
        self.assertIn("--dump-single-json", argv)
        self.assertIn("--skip-download", argv)
        self.assertIn("--retries", argv); self.assertEqual(argv[argv.index("--retries") + 1], "0")
        self.assertIn("--extractor-retries", argv); self.assertEqual(argv[argv.index("--extractor-retries") + 1], "0")
        self.assertEqual(argv[-1], "https://www.youtube.com/watch?v=" + VIDEO)
        for forbidden in ("--playlist-items", "--write-subs", "--write-auto-subs", "-f", "--format", "--download-archive"):
            self.assertNotIn(forbidden, argv)
        self.assertNotIn("media.mkv", str(result))

    def test_missing_required_video_format(self):
        self.current_metadata = metadata([metadata()["formats"][1]])
        result = self.inspect()
        self.assertFalse(result["selection"]["available"])
        self.assertIn("video_398_missing", result["selection"]["reasons"])

    def test_missing_required_audio_format(self):
        self.current_metadata = metadata([metadata()["formats"][0]])
        result = self.inspect()
        self.assertFalse(result["selection"]["available"])
        self.assertIn("original_audio_unavailable_or_unverified", result["selection"]["reasons"])

    def test_codec_and_property_mismatch_are_reported(self):
        rows = self.current_metadata["formats"]
        rows[0].update(vcodec="vp9", height=1080, fps=60)
        rows[1]["acodec"] = "aac"
        result = self.inspect()
        self.assertIn("video_398_height_mismatch", result["selection"]["reasons"])
        self.assertIn("video_398_fps_mismatch", result["selection"]["reasons"])
        self.assertIn("video_398_codec_mismatch", result["selection"]["reasons"])
        self.assertEqual(result["selection"]["audio_codec"], "aac")

    def test_drm_protected_pair_fails(self):
        rows = self.current_metadata["formats"]
        rows[0]["has_drm"] = True
        rows[1]["has_drm"] = True
        result = self.inspect()
        self.assertFalse(result["selection"]["available"])
        self.assertIn("video_398_drm", result["selection"]["reasons"])
        self.assertIn("original_audio_unavailable_or_unverified", result["selection"]["reasons"])

    def test_empty_format_listing_is_explicit_incomplete(self):
        self.current_metadata = metadata([])
        result = self.inspect()
        self.assertEqual(result["format_listing"]["status"], "empty")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("format_list_empty", result["selection"]["reasons"])

    def test_missing_format_listing_is_not_reported_as_permanent_absence(self):
        self.current_metadata = metadata()
        self.current_metadata.pop("formats")
        result = self.inspect()
        self.assertEqual(result["format_listing"]["status"], "empty")
        self.assertFalse(result["selection"]["available"])
        self.assertEqual(result["exit_code"], 2)

    def test_shared_block_prevents_subprocess_launch(self):
        gate = safety.Safety(self.state)
        gate.observe(BOT, component="test", operation="prior")
        with safety.scope(self.state), patch.object(acquisition, "run") as run:
            with self.assertRaises(safety.Blocked):
                inspector.inspect(VIDEO, self.state, execute=True)
        run.assert_not_called()

    def test_live_challenge_is_classified_and_journaled(self):
        class Process:
            pid = 999999
            returncode = 1
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def communicate(self, timeout=None): return "", BOT

        with safety.scope(self.state, "format-inspection"), patch.object(acquisition.subprocess, "Popen", return_value=Process()):
            with self.assertRaises(safety.Blocked):
                inspector.inspect(VIDEO, self.state, execute=True)
        status = safety.Safety(self.state).status()
        self.assertEqual(status["reason"], "authentication_challenge")
        self.assertEqual(status["component"], "format-inspection")
        self.assertIn(VIDEO, status["operation"])
        self.assertTrue(status["operation"].endswith(".stderr.log"))

    def test_timeout_is_bounded_and_does_not_create_block(self):
        class Process:
            pid = 999999
            returncode = -9
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def communicate(self, timeout=None):
                if timeout is not None: raise subprocess.TimeoutExpired("yt-dlp", timeout)
                return "", ""

        with safety.scope(self.state, "format-inspection"), patch.object(acquisition.subprocess, "Popen", return_value=Process()), \
             patch.object(acquisition.os, "killpg"):
            with self.assertRaisesRegex(ValueError, "timed out"):
                inspector.inspect(VIDEO, self.state, execute=True, timeout=5)
        self.assertTrue(safety.Safety(self.state).status()["network_allowed"])

    def test_local_launch_failure_does_not_create_block_or_retry(self):
        with safety.scope(self.state, "format-inspection"), patch.object(acquisition.subprocess, "Popen", side_effect=OSError("local launch failure")):
            with self.assertRaisesRegex(OSError, "local launch failure"):
                inspector.inspect(VIDEO, self.state, execute=True)
        self.assertTrue(safety.Safety(self.state).status()["network_allowed"])
        self.assertFalse((self.state / "retry.json").exists())

    def test_invalid_metadata_identity_rejected_without_echoing_payload(self):
        self.current_metadata = metadata()
        self.current_metadata["id"] = "wrongwrong1"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.inspect()

    def test_timeout_bounds_and_id_validation(self):
        with self.assertRaises(ValueError): inspector.inspect("bad", self.state)
        with self.assertRaises(ValueError): inspector.inspect(VIDEO, self.state, execute=True, timeout=181)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
