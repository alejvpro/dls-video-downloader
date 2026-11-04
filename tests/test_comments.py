"""Synthetic fixtures only. No production store or network access."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dls_video_downloader.comments import CommentStore, IRISHWEEGEE, encoded


VIDEO = "abcdefghijk"
OTHER_VIDEO = "lmnopqrstuv"
OTHER_AUTHOR = "UC" + "a" * 22
EARLY = "2026-11-01T10:00:00+00:00"
LATE = "2026-11-02T10:00:00+00:00"


def comment(comment_id="Ugx-first", **changes):
    return {"id": comment_id, "text": "  Original\n\n[0:00] literal text  \n\tΩ\n",
            "author": "@IrishWeegee", "author_id": IRISHWEEGEE,
            "author_url": "https://www.youtube.com/@IrishWeegee", "parent": "root",
            "timestamp": 1791936000, "_time_text": "3 weeks ago (edited)",
            "like_count": 92, "is_pinned": False, "is_favorited": False,
            "author_is_verified": False, "author_is_uploader": False,
            "extra_source_field": {"nested": [True, None, "  untouched  "]}, **changes}


class CommentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "comments"
        self.store = CommentStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, comments=None, **kwargs):
        defaults = {"outcome": "success", "coverage": {"assessment": "complete",
                    "evidence": "synthetic exhausted traversal"}, "collected_at": EARLY,
                    "tool": {"name": "yt-dlp", "version": "fixture"},
                    "configuration": {"comment_sort": "new"}, "cap": 1000}
        defaults.update(kwargs)
        return self.store.record(VIDEO, [comment()] if comments is None else comments, **defaults)

    def rows(self, table):
        return [dict(r) for r in self.store.db.execute(f"SELECT * FROM {table}")]

    def snapshot(self, result):
        return self.store.load_snapshot(VIDEO, result["snapshot_hash"])

    def test_initial_collection_and_provenance(self):
        result = self.record()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(self.snapshot(result)["comments"], [comment()])
        attempt = self.rows("collection_attempts")[0]
        self.assertEqual((attempt["retrieved_count"], attempt["top_level_count"], attempt["reply_count"]), (1, 1, 0))
        self.assertEqual(attempt["cap"], 1000)
        self.assertEqual(json.loads(attempt["tool_json"])["version"], "fixture")
        self.assertNotEqual(attempt["recorded_at"], attempt["collected_at"])
        self.assertEqual(json.loads(attempt["coverage_json"])["effective_assessment"], "complete")
        self.assertIsNone(self.rows("refresh_state")[0]["next_due_at"])

    def test_identical_results_reuse_snapshot_and_version_but_record_attempt(self):
        first = self.record()
        second = self.record(collected_at=LATE)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.assertEqual(len(self.rows("collection_attempts")), 2)
        self.assertEqual(len(self.rows("attempt_comments")), 2)
        version = self.rows("comment_versions")[0]
        self.assertEqual((version["first_observed_at"], version["last_observed_at"]), (EARLY, LATE))

    def test_reordered_results_same_snapshot_original_order_preserved(self):
        a, b = comment("Ugx-z"), comment("Ugx-a", author_id=OTHER_AUTHOR)
        original = deepcopy([a, b])
        first = self.record(original)
        second = self.record([b, a], collected_at=LATE)
        self.assertEqual(original, [a, b])
        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertEqual([c["id"] for c in self.snapshot(first)["comments"]], ["Ugx-a", "Ugx-z"])
        for result, expected in ((first, ["Ugx-z", "Ugx-a"]), (second, ["Ugx-a", "Ugx-z"])):
            order = self.store.db.execute("SELECT comment_id FROM attempt_comments WHERE attempt_id=? ORDER BY position", (result["attempt_id"],))
            self.assertEqual([r[0] for r in order], expected)

    def test_metadata_and_text_changes_have_separate_hashes(self):
        first = self.record()
        second = self.record([comment(like_count=93)], collected_at=LATE)
        third = self.record([comment(timestamp=1791936010, _time_text="4 weeks ago (edited)")])
        fourth = self.record([comment(text="Actually changed\n  ")])
        versions = self.rows("comment_versions")
        self.assertEqual(len(versions), 4)
        self.assertEqual(len({v["payload_hash"] for v in versions}), 4)
        self.assertEqual(len({v["text_hash"] for v in versions[:3]}), 1)
        self.assertNotEqual(versions[0]["text_hash"], versions[3]["text_hash"])
        self.assertEqual(len({r["snapshot_hash"] for r in (first, second, third, fourth)}), 4)
        self.assertEqual(self.snapshot(first)["comments"][0], comment())

    def test_exact_multiline_whitespace_and_all_source_fields(self):
        source = comment()
        result = self.record([source])
        saved = self.snapshot(result)["comments"][0]
        self.assertEqual(saved, source)
        self.assertEqual(saved["text"].encode(), source["text"].encode())
        version = self.rows("comment_versions")[0]
        self.assertEqual(version["text_hash"], hashlib.sha256(source["text"].encode()).hexdigest())
        self.assertEqual(version["payload_hash"], hashlib.sha256(encoded(source)).hexdigest())
        raw = (self.root / VIDEO / "snapshots" / f"{result['snapshot_hash']}.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["snapshot_hash"])

    def test_replies_unknown_parents_and_multiple_author_comments(self):
        unknown = comment("Ugx-unknown")
        del unknown["parent"]
        result = self.record([comment(), comment("Ugx-first.reply", parent="Ugx-first"), unknown])
        self.assertEqual(len(result["target_matches"][IRISHWEEGEE]), 3)
        attempt = self.rows("collection_attempts")[0]
        self.assertEqual((attempt["top_level_count"], attempt["reply_count"], attempt["unknown_parent_count"]), (1, 1, 1))
        self.assertEqual(self.rows("comment_versions")[1]["parent_id"], "Ugx-first")

    def test_target_stable_id_not_display_name_and_configurable_targets(self):
        no_name = comment("Ugx-no-name")
        del no_name["author"]
        cs = [no_name, comment("Ugx-renamed", author="Another name"),
              comment("Ugx-impostor", author_id=OTHER_AUTHOR)]
        result = self.record(cs, target_author_ids=[IRISHWEEGEE, OTHER_AUTHOR])
        self.assertEqual(result["target_matches"][IRISHWEEGEE], ["Ugx-no-name", "Ugx-renamed"])
        self.assertEqual(result["target_matches"][OTHER_AUTHOR], ["Ugx-impostor"])
        self.assertEqual(len(self.snapshot(result)["comments"]), 3)

    def test_missing_match_does_not_erase_target_presence_or_comments(self):
        first = self.record()
        self.record([comment("Ugx-other", author_id=OTHER_AUTHOR)], collected_at=LATE)
        target = self.rows("target_presence")[0]
        self.assertEqual((target["first_observed_at"], target["last_observed_at"]), (EARLY, EARLY))
        self.assertEqual(self.snapshot(first)["comments"], [comment()])
        self.assertEqual(len(self.rows("comment_versions")), 2)
        missing = self.rows("collection_attempts")[1]
        self.assertEqual(json.loads(missing["target_matches_json"])[IRISHWEEGEE], [])

    def test_empty_does_not_replace_nonempty_evidence(self):
        first = self.record()
        empty = self.record([], collected_at=LATE)
        self.assertEqual(empty["outcome"], "empty")
        self.assertEqual(self.snapshot(empty)["comments"], [])
        state = self.rows("refresh_state")[0]
        self.assertEqual(state["last_nonempty_snapshot_hash"], first["snapshot_hash"])
        self.assertEqual(state["last_successful_attempt_id"], empty["attempt_id"])
        self.assertEqual(len(self.rows("comment_versions")), 1)

    def test_disabled_is_not_empty_or_failure(self):
        result = self.store.record(VIDEO, outcome="disabled", coverage={"message": "Comments disabled"})
        self.assertEqual(result["outcome"], "disabled")
        self.assertIsNone(result["snapshot_hash"])
        row = self.rows("collection_attempts")[0]
        self.assertIsNone(row["retrieved_count"])
        self.assertIsNone(self.rows("refresh_state")[0]["last_successful_attempt_id"])

    def test_success_without_explicit_coverage_is_uncertain(self):
        for cs in ([comment()], []):
            with self.subTest(comments=bool(cs)):
                result = self.record(cs, coverage={})
                self.assertEqual(result["outcome"], "uncertain")
                self.assertIsNotNone(result["snapshot_hash"])
        self.assertIsNone(self.rows("refresh_state")[0]["last_successful_attempt_id"])

    def test_cap_overrides_complete_claim_and_stores_evidence(self):
        result = self.record(cap=1)
        self.assertEqual(result["outcome"], "partial")
        attempt = self.rows("collection_attempts")[0]
        evidence = json.loads(attempt["coverage_json"])
        self.assertTrue(evidence["cap_reached"])
        self.assertEqual(evidence["assessment"], "complete")
        self.assertEqual(evidence["effective_assessment"], "partial")
        self.assertIsNone(self.rows("refresh_state")[0]["last_successful_attempt_id"])

    def test_partial_and_uncertain_preserve_usable_comments(self):
        for kwargs in ({"outcome": "partial"}, {"outcome": "uncertain"},
                       {"coverage": {"assessment": "partial", "reason": "page failed"}}):
            result = self.record(**kwargs)
            self.assertIn(result["outcome"], ("partial", "uncertain"))
            self.assertEqual(self.snapshot(result)["comments"], [comment()])
        self.assertEqual(len(self.rows("snapshots")), 1)

    def test_failed_retrieval_keeps_prior_success_and_records_error(self):
        first = self.record()
        failed = self.store.record(VIDEO, outcome="failed", error="Timed out", warnings=["Network failure"], collected_at=LATE)
        self.assertEqual(failed["outcome"], "failed")
        self.assertIsNone(failed["snapshot_hash"])
        state = self.rows("refresh_state")[0]
        self.assertEqual(state["last_successful_attempt_id"], first["attempt_id"])
        self.assertEqual(state["last_nonempty_snapshot_hash"], first["snapshot_hash"])
        self.assertEqual(state["failure_count"], 1)
        row = self.rows("collection_attempts")[1]
        self.assertEqual(row["error"], "Timed out")
        self.assertEqual(json.loads(row["warnings_json"]), ["Network failure"])

    def test_failed_extraction_with_usable_comments_is_partial(self):
        result = self.record(outcome="failed", error="Later page failed")
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(self.snapshot(result)["comments"], [comment()])
        self.assertEqual(self.rows("collection_attempts")[0]["requested_outcome"], "failed")
        self.assertEqual(self.rows("refresh_state")[0]["failure_count"], 1)

    def test_malformed_and_duplicate_comments_record_invalid_without_subset(self):
        bad = [{"id": "bad id", "text": "x"}, {"text": "no ID"}, comment(text=None),
               comment(parent="bad parent"), comment(author_id=23),
               comment(extra=float("nan")), comment(extra={1: "lossy key"})]
        collections = [[comment(), value] for value in bad] + [[comment(), comment()], {"comments": []}]
        for cs in collections:
            with self.subTest(comments=repr(cs)[:80]):
                result = self.record(cs)
                self.assertEqual(result["outcome"], "invalid")
                self.assertIsNone(result["snapshot_hash"])
        self.assertFalse(self.rows("snapshots"))
        self.assertFalse(self.rows("comment_versions"))
        self.assertTrue(self.rows("refresh_state")[0]["needs_review"])
        self.assertTrue(all("Invalid input:" in row["error"] for row in self.rows("collection_attempts")))

    def test_invalid_api_arguments_do_not_create_attempts_or_paths(self):
        for kwargs in ({"cap": True}, {"cap": 0}, {"outcome": "unknown"},
                       {"outcome": []}, {"collected_at": 100},
                       {"coverage": {"assessment": "exhaustive"}}, {"tool": []},
                       {"collected_at": "2026-11-01T00:00:00"},
                       {"warnings": "warning"}, {"target_author_ids": ["IrishWeegee"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.record(**kwargs)
        with self.assertRaises(ValueError):
            self.store.record("../escape", outcome="failed")
        self.assertEqual(self.rows("collection_attempts"), [])

    def test_snapshot_publication_failure_rolls_back_and_can_retry(self):
        with patch.object(self.store, "_publish", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.rows("collection_attempts"), [])
        self.assertEqual(self.rows("refresh_state"), [])
        self.assertEqual(self.record()["outcome"], "success")

    def test_catalog_failure_after_file_publication_rolls_back_safe_retry(self):
        self.store.db.execute("""CREATE TRIGGER fail_version BEFORE INSERT ON comment_versions
            BEGIN SELECT RAISE(ABORT, 'test catalog failure'); END""")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.record()
        for table in ("collection_attempts", "snapshots", "comment_versions", "attempt_comments", "refresh_state", "target_presence"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.store.db.execute("DROP TRIGGER fail_version")
        self.store.db.commit()
        self.record()
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.assertEqual(len(self.rows("collection_attempts")), 1)

    def test_corrupt_existing_snapshot_is_never_replaced(self):
        result = self.record()
        path = self.root / VIDEO / "snapshots" / f"{result['snapshot_hash']}.json"
        path.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            self.record()
        with self.assertRaises(ValueError):
            self.snapshot(result)
        self.assertEqual(path.read_bytes(), b"damaged")
        self.assertEqual(len(self.rows("collection_attempts")), 1)

    def test_reopen_persists_history_and_return_to_old_payload(self):
        first = self.record()
        self.record([comment(text="changed")], collected_at=LATE)
        self.store.close()
        self.store = CommentStore(self.root)
        self.record()
        self.assertEqual(len(self.rows("collection_attempts")), 3)
        self.assertEqual(len(self.rows("comment_versions")), 2)
        self.assertEqual(len(self.rows("snapshots")), 2)
        self.assertEqual(self.snapshot(first)["comments"], [comment()])
        self.assertFalse(self.store.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_video_namespaces_and_missing_author(self):
        self.record()
        c = comment()
        del c["author_id"]
        other = self.store.record(OTHER_VIDEO, [c], outcome="success", coverage={"assessment": "complete"})
        self.assertEqual(other["target_matches"][IRISHWEEGEE], [])
        self.assertEqual(len(self.rows("comment_versions")), 2)
        self.assertEqual(len(self.rows("refresh_state")), 2)

    def test_older_observations_do_not_regress_times_or_refresh_pointer(self):
        newer = self.record(collected_at=LATE)
        self.record(collected_at=EARLY)
        state = self.rows("refresh_state")[0]
        self.assertEqual(state["last_attempt_id"], newer["attempt_id"])
        self.assertEqual(state["last_successful_attempt_id"], newer["attempt_id"])
        version = self.rows("comment_versions")[0]
        self.assertEqual((version["first_observed_at"], version["last_observed_at"]), (EARLY, LATE))

    def test_reserved_refresh_fields_are_not_scheduled_or_unsuspended(self):
        self.record()
        self.store.db.execute("UPDATE refresh_state SET next_due_at=?,suspended=1,needs_review=1", (LATE,))
        self.store.db.commit()
        self.record(collected_at=LATE)
        state = self.rows("refresh_state")[0]
        self.assertEqual((state["next_due_at"], state["suspended"], state["needs_review"]), (LATE, 1, 1))

    def test_late_import_of_older_nonempty_result_after_newer_empty(self):
        newer = self.record([], collected_at=LATE)
        older = self.record(collected_at=EARLY)
        state = self.rows("refresh_state")[0]
        self.assertEqual(state["last_nonempty_snapshot_hash"], older["snapshot_hash"])
        self.assertEqual(state["last_attempt_id"], newer["attempt_id"])
        self.assertEqual(state["last_successful_attempt_id"], newer["attempt_id"])

    def test_failed_empty_and_disabled_keep_successful_snapshot(self):
        first = self.record()
        self.record([], outcome="failed", error="No usable response", collected_at=LATE)
        self.store.record(VIDEO, outcome="disabled", collected_at=LATE)
        state = self.rows("refresh_state")[0]
        self.assertEqual(state["last_successful_attempt_id"], first["attempt_id"])
        self.assertEqual(state["last_nonempty_snapshot_hash"], first["snapshot_hash"])
        self.assertEqual(len(self.rows("snapshots")), 1)

    def test_canonical_object_key_order_and_input_detachment(self):
        original = comment()
        result = self.record([original])
        reordered = dict(reversed(list(original.items())))
        again = self.record([reordered])
        self.assertEqual(result["snapshot_hash"], again["snapshot_hash"])
        original["extra_source_field"]["nested"].append("later caller mutation")
        self.assertEqual(self.snapshot(result)["comments"], [comment()])

    def test_multiple_catalog_handles_share_valid_schema_and_snapshots(self):
        with CommentStore(self.root) as other:
            first = self.record()
            again = other.record(VIDEO, [comment()], outcome="success", coverage={"assessment": "complete"})
            self.assertEqual(first["snapshot_hash"], again["snapshot_hash"])
        self.assertEqual(len(self.rows("snapshots")), 1)
        self.assertEqual(len(self.rows("collection_attempts")), 2)

    def test_persistence_has_no_network_inventory_or_media_lock_side_effects(self):
        forbidden = AssertionError("Comment persistence must remain offline and independent")
        with patch("socket.socket.connect", side_effect=forbidden), \
                patch("subprocess.Popen", side_effect=forbidden), \
                patch("dls_video_downloader.inventory.connect", side_effect=forbidden), \
                patch("dls_video_downloader.acquisition.operation_lock", side_effect=forbidden), \
                patch("dls_video_downloader.inventory.reclassify", side_effect=forbidden):
            result = self.record()
            self.assertEqual(self.snapshot(result)["comments"], [comment()])

    def test_refuses_unrelated_database_and_future_schema(self):
        with tempfile.TemporaryDirectory() as name:
            db = sqlite3.connect(Path(name) / "comments.sqlite3")
            db.execute("CREATE TABLE videos(id TEXT)")
            db.commit()
            db.close()
            before = (Path(name) / "comments.sqlite3").read_bytes()
            with self.assertRaises(ValueError):
                CommentStore(name)
            self.assertEqual((Path(name) / "comments.sqlite3").read_bytes(), before)
        self.store.db.execute("PRAGMA user_version=99")
        self.store.db.commit()
        with self.assertRaises(ValueError):
            CommentStore(self.root)


if __name__ == "__main__":
    unittest.main()
