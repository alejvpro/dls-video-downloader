import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
import io

from dls_video_downloader.classification import CHANNEL, DEFAULT_START, classify, eligibility
from dls_video_downloader.inventory import connect, decide, import_file, reclassify, report, seed
from dls_video_downloader.cli import main

VIDEO = "abcdefghijk"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "inventory.sqlite3"
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest(self, metadata, source="live", epoch=100, flat=True):
        entry = {"id": VIDEO, **metadata}
        payload = {"channel_id": CHANNEL, "epoch": epoch, "entries": [entry]} if flat else {"channel_id": CHANNEL, "epoch": epoch, **entry}
        path = self.root / "input.json"
        path.write_text(json.dumps(payload))
        with self.db:
            return import_file(self.db, path, source)

    def item(self):
        return report(self.db, "2026-10-28")["items"][0]

    def daily(self):
        return {"title": "LIVE at 9am ET | 10/28/26 | The Dan Le Batard Show", "live_status": "was_live", "duration": 11000}

    def test_identity_reimport_and_cross_source(self):
        self.ingest(self.daily())
        before = report(self.db, "2026-10-28")
        self.assertTrue(self.ingest(self.daily())["unchanged"])
        self.assertEqual(before, report(self.db, "2026-10-28"))
        self.ingest(self.daily(), "videos")
        self.assertEqual(self.db.execute("SELECT count(*) FROM videos").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        self.assertEqual(len(report(self.db, "2026-10-28")["by_source"]), 2)

    def test_title_change_and_missing_fields(self):
        self.ingest({**self.daily(), "description": "original", "upload_date": "20261028"}, "full", flat=False)
        self.ingest({"title": "A descriptive new title", "description": None}, epoch=200)
        item = self.item()
        self.assertEqual(item["initial_title"], self.daily()["title"])
        self.assertEqual(item["title"], "A descriptive new title")
        metadata = json.loads(self.db.execute("SELECT metadata FROM videos").fetchone()[0])
        self.assertEqual(metadata["description"], "original")
        self.assertEqual(item["publication_date"], "2026-10-28")
        # Full-only source does not establish Live discovery; add it now.
        self.ingest(self.daily(), epoch=300)
        self.ingest({"title": "Retitled again"}, epoch=400)
        self.assertEqual(self.item()["classification"], "daily_show")

    def test_unknown_date_never_from_title(self):
        self.ingest(self.daily())
        self.assertEqual(self.item()["eligibility"], "date_unknown")
        self.assertIsNone(self.item()["publication_date"])

    def test_full_enrichment_new_week_and_default_plan(self):
        self.ingest(self.daily())
        full = {**self.daily(), "title": "Daily show current title", "release_date": "20261026",
                "upload_date": "20261027", "description": "Full description",
                "formats": [{"url": "temporary-playback-url"}]}
        self.ingest(full, "other", epoch=200, flat=False)
        result = report(self.db, DEFAULT_START)
        item = result["items"][0]
        self.assertEqual(item["initial_title"], self.daily()["title"])
        self.assertEqual(item["title"], full["title"])
        self.assertEqual(item["classification"], "daily_show")
        self.assertEqual(item["publication_date"], "2026-10-26")
        self.assertEqual(item["eligibility"], "eligible")
        self.assertTrue(any("dates differ" in w for w in item["warnings"]))
        self.assertEqual(result["totals"]["videos"], 1)
        self.assertEqual(result["totals"]["observations"], 2)
        self.assertTrue(self.ingest(full, "other", epoch=200, flat=False)["unchanged"])
        self.assertEqual(result, report(self.db, DEFAULT_START))
        self.assertNotIn("temporary-playback-url", self.db.execute("SELECT metadata FROM videos").fetchone()[0])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.path), "plan", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["start_date"], "2026-10-26")

    def test_description_revisions_and_distinct_times(self):
        self.ingest({"title": "Title", "description": "First"}, "full", epoch=100, flat=False)
        self.ingest({"title": "Title", "description": "Second"}, "full", epoch=200, flat=False)
        descriptions = [json.loads(r[0])["description"] for r in self.db.execute("SELECT metadata FROM observations ORDER BY observation_id")]
        self.assertEqual(descriptions, ["First", "Second"])
        observed = self.db.execute("SELECT observed_at FROM observations LIMIT 1").fetchone()[0]
        seen = self.db.execute("SELECT first_seen FROM videos").fetchone()[0]
        self.assertNotEqual(observed, seen)

    def test_rule_version_change_preserves_decision(self):
        self.ingest(self.daily())
        with self.db:
            decide(self.db, VIDEO, "reject", "persistent")
            with patch("dls_video_downloader.inventory.RULE_VERSION", "future-test-version"):
                reclassify(self.db)
        self.assertEqual(self.item()["eligibility"], "blocked")
        self.assertEqual(self.db.execute("SELECT count(*) FROM classifications").fetchone()[0], 2)

    def test_date_boundaries_and_older_approval(self):
        metadata = {"upload_date": "20261027"}
        self.assertEqual(eligibility(metadata, "daily_show", None)[0], "eligible")
        self.assertEqual(eligibility(metadata, "daily_show", None, "2026-10-28")[0], "outside_range")
        self.assertEqual(eligibility(metadata, "daily_show", None, "2026-10-27", "2026-10-27")[0], "eligible")
        self.assertEqual(eligibility(metadata, "review", "approve")[0], "eligible")
        self.assertEqual(eligibility(metadata, "daily_show", "reject")[0], "blocked")
        self.assertEqual(eligibility(metadata, "daily_show", "skip")[0], "blocked")

    def test_confirmed_program_override_and_idempotency(self):
        for title in ("#BecauseMiami: Episode", "The Hockey Show: Episode",
                      "MORALLY ABHORRENT: Preview", "Episode | Alley Oop"):
            with self.subTest(title=title):
                self.ingest({"title": title}, "videos", epoch=100)
                with self.db:
                    decide(self.db, VIDEO, "approve", "Keep this ID")
                    reclassify(self.db)
                self.assertEqual(self.item()["classification"], "skip")
                self.assertEqual(self.item()["eligibility"], "eligible")
                before = report(self.db, "2026-10-28")
                self.assertTrue(self.ingest({"title": title}, "videos", epoch=100)["unchanged"])
                self.assertEqual(before, report(self.db, "2026-10-28"))
                with self.db:
                    decide(self.db, VIDEO, "reject", "Skip this ID")
                self.assertEqual(self.item()["eligibility"], "blocked")
                # Independent fixture per program, preserving normal DB transactions.
                self.db.close()
                self.path.unlink()
                self.db = connect(self.path)

    def test_manual_decisions_survive_rule_and_title_changes(self):
        self.ingest(self.daily())
        with self.db:
            decide(self.db, VIDEO, "approve", "original")
            decide(self.db, VIDEO, "approve", "original")
        self.ingest({"title": "Mystery Crate"}, epoch=200)
        with self.db:
            reclassify(self.db)
        self.assertEqual(self.item()["classification"], "skip")
        self.assertEqual(self.item()["eligibility"], "eligible")
        self.assertEqual(self.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
        with self.db:
            decide(self.db, VIDEO, "reject", "changed my mind")
        self.assertEqual(self.item()["eligibility"], "blocked")

    def test_seeds_do_not_override_later_decisions(self):
        with self.db:
            seed(self.db)
            seed(self.db)
            decide(self.db, "Zvvx0Lcavkk", "reject", "new decision")
            seed(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 5)

    def test_no_playback_urls_or_null_erasure(self):
        self.ingest({"title": "First", "description": "keep", "formats": [{"url": "secret"}], "url": "secret"})
        self.ingest({"title": "Second", "description": None}, epoch=200)
        data = self.db.execute("SELECT metadata FROM videos").fetchone()[0]
        self.assertNotIn("secret", data)
        self.assertEqual(json.loads(data)["description"], "keep")
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_full_fields_not_downgraded_or_old_title_replayed(self):
        self.ingest({"title": "New", "description": "full", "duration": 120}, "full", epoch=200, flat=False)
        self.ingest({"title": "Old", "description": "flat", "duration": 110}, epoch=100)
        self.ingest({"title": "Latest", "description": "flat", "duration": 110}, epoch=300)
        m = json.loads(self.db.execute("SELECT metadata FROM videos").fetchone()[0])
        self.assertEqual((m["title"], m["description"], m["duration"]), ("Latest", "full", 120))

    def test_invalid_import_rolls_back(self):
        path = self.root / "bad.json"
        path.write_text(json.dumps({"entries": [{"id": VIDEO}, {"id": "bad"}]}))
        with self.assertRaises(ValueError):
            with self.db:
                import_file(self.db, path, "other")
        self.assertEqual(self.db.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

    def test_readonly_status_and_missing_db(self):
        self.ingest(self.daily())
        before = self.path.read_bytes()
        readonly = connect(self.path, readonly=True)
        report(readonly, "2026-10-28")
        with self.assertRaises(Exception):
            readonly.execute("DELETE FROM videos")
        readonly.close()
        self.assertEqual(before, self.path.read_bytes())
        missing = self.root / "missing.sqlite3"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(missing), "status"]), 1)
        self.assertFalse(missing.exists())


class ClassificationTests(unittest.TestCase):
    def test_default_acquisition_week_boundary(self):
        self.assertEqual(DEFAULT_START, "2026-10-26")
        for day in ("20261026", "20261027", "20261028"):
            self.assertEqual(eligibility({"release_date": day}, "daily_show", None)[0], "eligible")
        self.assertEqual(eligibility({"release_date": "20261025"}, "daily_show", None)[0], "outside_range")
        self.assertEqual(eligibility({"release_date": "20261025"}, "review", "approve")[0], "eligible")

    def classify(self, title, duration=600, **extra):
        return classify({"channel_id": CHANNEL, "title": title, "duration": duration, **extra}, ["videos"])[0]

    def test_club_short_and_exclusions(self):
        self.assertEqual(self.classify("Elizabeth Holmes In The Club | DLS", 465), "the_club")
        for title in ["Mystery Crate", "NPDS × Pitch Clock", "Because Miami", "MMA Hangout", "UFC 333 LIVE Watchalong", "Football America", "Canes Insight", "Suey Award Nominees Compilation"]:
            with self.subTest(title=title):
                self.assertEqual(self.classify(title), "skip")

    def test_clip_and_ambiguous_originals(self):
        for title in ["A funny excerpt from the show", "New Feud? | DLS", "Should the Jets Fire Their Coach?"]:
            self.assertEqual(self.classify(title), "skip")
        for title, duration in [("Extended interview | DLS", 2300), ("REACTION: Dolphins win?", 600), ("Juju and Jeremy Sit Down With Pelle Larsson", 561), ("Something original", 600), ("DLS special livestream", 11000)]:
            self.assertEqual(self.classify(title, duration), "review")
        self.assertEqual(classify({"title": "The Club"}, ["videos"])[0], "review")

    def test_completion_required_and_daily_retitle(self):
        m = {"channel_id": CHANNEL, "title": "LIVE at 9am ET | The Dan Le Batard Show"}
        self.assertEqual(classify(m, ["live"])[0], "review")
        m["live_status"] = "was_live"
        self.assertEqual(classify(m, ["live"])[0], "daily_show")
        m["title"] = "A descriptive title"
        self.assertEqual(classify(m, ["live"], ("daily_show", "evidence"))[0], "daily_show")

    def test_nine_missed_because_miami_records(self):
        records = {
            "d7om-eU4RXk": "#BecauseMiami: Miami Police Shootout | The Dan Le Batard Show",
            "eUp9q4AAF-g": "#BecauseMiami: Who the F-ck is Rob Piper? | The Dan Le Batard Show",
            "kaIHZdQpWWM": "#becausemiami: Get Off My Wife! | The Dan Le Batard Show",
            "biHDMFqzRRM": "#becausemiami: Miami Mayor Has a Problem With Black People | The Dan Le Batard Show",
            "-monWnYxyig": "#BecauseMiami: Secret Republican | The Dan Le Batard Show",
            "wFyEYDgD1YU": "#becausemiami: Neanderthal in City Hall | The Dan Le Batard Show ",
            "gU5EqqcCqEg": '#becausemiami | "There\'s Something About Jeffery Epstein" | The Dan Le Batard Show',
            "a2pRuIfKJA0": "#becausemiami : Escape From (Alligator) Alcatraz",
            "GRULXHJjFrU": '#becausemiami : "The Long Con" | The Dan Le Batard Show with Stugotz',
        }
        for video_id, title in records.items():
            with self.subTest(video_id=video_id):
                self.assertEqual(self.classify(title), "skip")

    def test_confirmed_program_branding(self):
        for title in ("BecauseMiami", "Because Miami: Episode", "#bEcAuSeMiAmI | Episode",
                      "The Hockey Show: Mike Babcock Is REALLY Getting Another NHL Job?! | The Dan Le Batard Show",
                      "MORALLY ABHORRENT: FIFA World Cup 2026 Week 2 Preview (ft. Nick Verlaney)",
                      "The Alley Oop: Episode", "Alley Oop — Episode",
                      "WEMBY VANISHES in Game 4 😳 What Went Wrong for the Spurs? | Alley Oop",
                      "Episode | The Alley Oop"):
            with self.subTest(title=title):
                self.assertEqual(self.classify(title), "skip")

    def test_incidental_mentions_preserve_positive_selections(self):
        for program in ("Because Miami", "#BecauseMiami", "BecauseMiami", "The Hockey Show", "MORALLY ABHORRENT", "The Alley Oop"):
            with self.subTest(program=program):
                title = f"Discussing {program} with Dan"
                self.assertEqual(self.classify(title, description=f"Watch {program}: episode"), "review")
                # Leading words that merely form part of a sentence are not branding.
                self.assertEqual(self.classify(f"{program} was mentioned today"), "review")
                daily = {"channel_id": CHANNEL, "title": f"LIVE at 9am ET | The Dan Le Batard Show | {program}",
                         "live_status": "was_live", "description": f"{program}: Episode"}
                self.assertEqual(classify(daily, ["live"])[0], "daily_show")
                daily["title"] = f"{program}: discussed today"
                self.assertEqual(classify(daily, ["live"], ("daily_show", "established"))[0], "daily_show")
                self.assertEqual(self.classify(f"Talking {program} In The Club", 450), "the_club")
                self.assertEqual(self.classify("Regular topical discussion", description=f"{program}: episode"), "review")

    def test_proposed_segment_rules_still_not_enabled(self):
        self.assertEqual(self.classify("Does Curry need to be traded? Are the warriors REALLY thinking about the future? SEGMENT", 673), "review")
        self.assertEqual(self.classify("Miami Heat's Newest PA Announcer joins the show! | The Dan Le Batard Show", 941), "review")


if __name__ == "__main__":
    unittest.main()
