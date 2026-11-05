"""Unified age-window policy: synthetic history, no collection or operational IO."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dls_video_downloader.comment_refresh import (
    CollectionEvidence, CommentEvidence, RefreshPolicy, evaluate_refresh, history_from_rows,
)
from dls_video_downloader.comments import CommentStore, IRISHWEEGEE


START = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
OTHER = "UC" + "a" * 22


def at(days=0, hours=0):
    return START + days * DAY + hours * HOUR


def sighting(comment_id="Ugx-one", text="original", author=IRISHWEEGEE):
    return CommentEvidence(comment_id, author, hashlib.sha256(text.encode()).hexdigest())


def event(days=0, *, outcome="uncertain", comments=(), hours=0, name=None, state=None, finish=None):
    return CollectionEvidence(name or f"attempt-{days}-{hours}", at(days, hours), outcome,
                              tuple(comments), state, finish)


def cadence(decision):
    return (decision.due, decision.next_due_at, decision.reason, decision.suspended,
            decision.needs_review, decision.phase, decision.consecutive_unsuccessful)


class RefreshTests(unittest.TestCase):
    def evaluate(self, history=(), days=0, hours=0, **kwargs):
        return evaluate_refresh(history, **{"now": at(days, hours), "published_at": START, **kwargs})

    def test_new_video_immediately_due_independent_of_media(self):
        result = self.evaluate()
        self.assertTrue(result.due)
        self.assertEqual(result.next_due_at, START)
        self.assertEqual(result.reason, "initial_collection")
        self.assertTrue(result.manual_allowed)

    def test_initial_is_immediate_on_any_day_inside_window(self):
        for day in range(15):
            with self.subTest(day=day):
                self.assertTrue(self.evaluate(days=day).due)

    def test_initial_in_pause_is_not_followed_by_routine_checks(self):
        for day in range(8, 14):
            with self.subTest(day=day):
                self.assertTrue(self.evaluate(days=day).due)
                result = self.evaluate([event(day)], days=day, hours=1)
                self.assertFalse(result.due)
                self.assertEqual(result.next_due_at, at(14))

    def test_daily_checks_exact_boundaries_through_day_seven(self):
        history = [event()]
        for day in range(1, 8):
            with self.subTest(day=day):
                before = self.evaluate(history, now=at(day) - timedelta(microseconds=1))
                self.assertFalse(before.due)
                self.assertEqual(before.next_due_at, at(day))
                self.assertTrue(self.evaluate(history, days=day).due)
                history.append(event(day))
                self.assertFalse(self.evaluate(history, days=day, hours=12).due)
        self.assertEqual(self.evaluate(history, days=8).next_due_at, at(14))

    def test_other_author_does_not_trigger_irish_confirmation(self):
        for day in range(1, 8):
            missing = [event(d) for d in range(day)]
            found = [replace(e, comments=(sighting(author=OTHER),)) for e in missing]
            self.assertEqual(cadence(self.evaluate(missing, days=day)), cadence(self.evaluate(found, days=day)))

    def test_no_routine_checks_days_eight_through_thirteen(self):
        for day in range(8, 14):
            for comments in ((), (sighting(author=OTHER),)):
                with self.subTest(day=day, found=bool(comments)):
                    result = self.evaluate([event(7, comments=comments)], days=day)
                    self.assertFalse(result.due)
                    self.assertEqual(result.next_due_at, at(14))

    def test_day_fourteen_final_check_once_with_or_without_target(self):
        for comments in ((), (sighting(),)):
            self.assertTrue(self.evaluate([event(7, comments=comments)], days=14).due)
            history = [event(7, comments=comments), event(14, comments=comments)]
            for day, hour in ((14, 0), (14, 6), (15, 0), (30, 0), (90, 0)):
                result = self.evaluate(history, days=day, hours=hour)
                self.assertTrue(result.suspended)
                self.assertFalse(result.due)
                self.assertIsNone(result.next_due_at)
                self.assertTrue(result.manual_allowed)

    def test_final_window_end_is_exclusive_for_every_video(self):
        for history in ([], [event(comments=[sighting()])], [event(outcome="failed")]):
            self.assertTrue(self.evaluate(history, now=at(15) - timedelta(microseconds=1)).due)
            result = self.evaluate(history, days=15)
            self.assertTrue(result.suspended)
            self.assertEqual(result.reason, "collection_window_expired")
            self.assertEqual(result.deadline_at, at(15))

    def test_final_check_can_be_delayed_within_its_slot(self):
        history = [event(13, hours=12)]
        result = self.evaluate(history, days=14)
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(14, 12))
        self.assertTrue(self.evaluate(history, days=14, hours=12).due)

    def test_late_initial_collection_does_not_replay_missed_slots(self):
        self.assertTrue(self.evaluate(days=6).due)
        result = self.evaluate([event(6)], days=6, hours=4)
        self.assertEqual(result.next_due_at, at(7))
        self.assertFalse(result.due)
        self.assertEqual(self.evaluate([event(6)], days=12).next_due_at, at(14))

    def test_first_discovery_after_cutoff_never_starts_automatic_collection(self):
        for day in (15, 16, 30, 60, 90, 600):
            result = self.evaluate(days=day, first_observed_at=at(day))
            self.assertTrue(result.suspended)
            self.assertFalse(result.due)
            self.assertTrue(result.manual_allowed)
            self.assertEqual(result.anchor, START)

    def test_daily_spacing_uses_completion_plus_twenty_four_hours(self):
        result = self.evaluate([event(1, hours=20, finish=at(1, 21))], days=2)
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(2, 21))

    def test_target_detection_switches_to_confirmation(self):
        missing = [event(), event(5)]
        found = [event(), event(5, comments=[sighting()])]
        result = self.evaluate(found, days=5)
        self.assertEqual(result.phase, 'confirmation')
        self.assertEqual(result.next_due_at, at(6))
        self.assertEqual(self.evaluate(missing, days=6).next_due_at, at(6))
        found.append(event(6))
        self.assertTrue(self.evaluate(found, days=6).monitoring_complete)

    def test_seen_target_absent_later_is_not_deleted(self):
        result = self.evaluate([event(comments=[sighting()]), event(1)], days=2)
        self.assertIsNone(result.next_due_at)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.targets[0].observed_comment_ids, ("Ugx-one",))
        self.assertEqual(result.targets[0].first_seen_at, START)

    def test_multiple_target_comments_share_one_confirmation(self):
        cs = [sighting("one"), sighting("two")]
        history = [event(comments=cs), event(1, comments=[sighting("three")])]
        result = self.evaluate(history, days=1)
        self.assertIsNone(result.next_due_at)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.confirmation_attempt_id, history[1].attempt_id)
        self.assertEqual(result.targets[0].observed_comment_ids, ("one", "three", "two"))

    def test_configured_authors_cannot_change_cadence(self):
        history = [event(comments=[sighting()]), event(1, comments=[sighting(author=OTHER)])]
        one = self.evaluate(history, days=2)
        many = self.evaluate(history, days=2, target_author_ids=[IRISHWEEGEE, OTHER])
        self.assertEqual(cadence(one), cadence(many))
        self.assertEqual({t.author_id for t in many.targets}, {IRISHWEEGEE, OTHER})

    def test_text_change_in_pause_does_not_add_followup(self):
        same = [event(comments=[sighting()]), event(10, comments=[sighting()])]
        changed = [event(comments=[sighting()]), event(10, comments=[sighting(text="edited")])]
        self.assertEqual(cadence(self.evaluate(same, days=10)), cadence(self.evaluate(changed, days=10)))
        self.assertIsNone(self.evaluate(changed, days=10).next_due_at)
        self.assertTrue(self.evaluate(changed, days=10).monitoring_complete)

    def test_text_change_during_final_check_does_not_add_followup(self):
        history = [event(comments=[sighting()]), event(14, comments=[sighting(text="edited")])]
        self.assertTrue(self.evaluate(history, days=14).suspended)

    def test_repeated_text_edits_do_not_restart_or_extend_schedule(self):
        history = [event(d, comments=[sighting(text=str(d))]) for d in range(8)]
        self.assertTrue(self.evaluate(history, days=8).monitoring_complete)
        self.assertIsNone(self.evaluate(history, days=8).next_due_at)
        history.append(event(14, comments=[sighting(text="final edit")]))
        for day in (15, 30, 90):
            self.assertTrue(self.evaluate(history, days=day).suspended)

    def test_no_day_thirty_or_ninety_monitoring_after_target_detected(self):
        for detection in (0, 7, 14):
            history = [event(detection, comments=[sighting()])]
            for day in (30, 90, 120):
                result = self.evaluate(history, days=day)
                self.assertFalse(result.due)
                self.assertIsNone(result.next_due_at)

    def test_late_manual_sighting_and_edit_cannot_restart_automatic_collection(self):
        history = [event(14), event(30, comments=[sighting()]), event(90, comments=[sighting(text="late edit")])]
        result = self.evaluate(history, days=90)
        self.assertTrue(result.suspended)
        self.assertTrue(result.manual_allowed)
        self.assertEqual(result.targets[0].first_seen_at, at(30))

    def test_successful_empty_and_uncertain_are_not_failures(self):
        for outcome in ("success", "empty", "uncertain"):
            with self.subTest(outcome=outcome):
                result = self.evaluate([event(outcome=outcome)], hours=1)
                self.assertEqual(result.consecutive_unsuccessful, 0)
                self.assertEqual(result.next_due_at, at(1))

    def test_partial_failed_invalid_timeout_and_interruption_back_off(self):
        for outcome, state in (("partial", "returned_nonempty"), ("failed", None), ("invalid", None),
                               ("failed", "timeout"), ("failed", "interrupted")):
            result = self.evaluate([event(outcome=outcome, state=state)])
            self.assertEqual(result.next_due_at, at(hours=1))
            self.assertEqual(result.reason, "retry_backoff")

    def test_partial_positive_sighting_gets_one_confirmation_not_extra_retries(self):
        missing = self.evaluate([event(outcome="partial")])
        found = self.evaluate([event(outcome="partial", comments=[sighting()])])
        self.assertEqual(missing.next_due_at, at(hours=1))
        self.assertEqual(found.next_due_at, at(1))
        self.assertEqual(found.consecutive_unsuccessful, 1)
        self.assertEqual(found.targets[0].observed_comment_ids, ("Ugx-one",))

    def test_progressive_backoff_and_five_failure_review(self):
        history = []
        for index, hour in enumerate((0, 1, 7, 31, 55)):
            history.append(event(hours=hour, outcome="failed"))
            result = self.evaluate(history, hours=hour)
            self.assertEqual(result.consecutive_unsuccessful, index + 1)
            if index < 4:
                self.assertEqual(result.next_due_at, at(hours=hour + (1 if index == 0 else 6 if index == 1 else 24)))
            else:
                self.assertTrue(result.suspended)
                self.assertTrue(result.needs_review)
                self.assertEqual(result.reason, "failure_limit")
                self.assertTrue(result.manual_allowed)

    def test_uncertain_success_resets_failure_streak(self):
        result = self.evaluate([event(outcome="failed"), event(hours=1)], hours=2)
        self.assertEqual(result.consecutive_unsuccessful, 0)
        self.assertEqual(result.next_due_at, at(1, 1))

    def test_retry_measured_from_finish_not_start(self):
        result = self.evaluate([event(outcome="failed", finish=at(hours=2))], hours=2)
        self.assertEqual(result.next_due_at, at(hours=3))

    def test_retry_does_not_cross_routine_pause(self):
        result = self.evaluate([event(7, hours=23.5, outcome="failed")], days=8)
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(14))

    def test_initial_failure_in_pause_waits_until_final(self):
        result = self.evaluate([event(10, outcome="failed")], days=10)
        self.assertEqual(result.next_due_at, at(14))

    def test_final_failure_allows_only_one_recovery(self):
        history = [event(14, outcome="failed")]
        result = self.evaluate(history, days=14)
        self.assertEqual(result.next_due_at, at(14, 1))
        self.assertEqual(result.reason, "final_recovery")
        history.append(event(14, hours=1, outcome="partial"))
        result = self.evaluate(history, days=14, hours=2)
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.reason, "final_recovery_exhausted")

    def test_successful_final_recovery_suspends_even_if_target_newly_found(self):
        history = [event(14, outcome="partial"), event(14, hours=1, comments=[sighting()])]
        result = self.evaluate(history, days=14, hours=2)
        self.assertTrue(result.suspended)
        self.assertTrue(result.manual_allowed)

    def test_final_failure_near_end_cannot_extend_deadline(self):
        result = self.evaluate([event(14, hours=23.5, outcome="failed")], days=14, hours=23.5)
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.reason, "next_check_exceeds_window")

    def test_expired_failed_final_exposes_last_attempt_for_review(self):
        result = self.evaluate([event(14, outcome="failed", name="failed-final")], days=15)
        self.assertEqual(result.reason, "collection_window_expired")
        self.assertTrue(result.needs_review)
        self.assertEqual(result.last_attempt_id, "failed-final")
        self.assertEqual(result.last_outcome, "failed")
        self.assertIsNone(result.next_due_at)

    def test_disabled_checks_seven_fourteen_then_stop(self):
        history = [event(outcome="disabled")]
        self.assertEqual(self.evaluate(history).next_due_at, at(7))
        history.append(event(7, outcome="disabled"))
        self.assertEqual(self.evaluate(history, days=7).next_due_at, at(14))
        history.append(event(14, outcome="disabled"))
        self.assertTrue(self.evaluate(history, days=14).suspended)

    def test_disabled_followup_failure_waits_until_final(self):
        result = self.evaluate([event(outcome="disabled"), event(7, outcome="failed")], days=7)
        self.assertEqual(result.next_due_at, at(14))

    def test_unavailable_uses_limited_followups_without_permanent_claim(self):
        result = self.evaluate([event(outcome="failed", state="unavailable")])
        self.assertEqual(result.phase, "disabled")
        self.assertEqual(result.next_due_at, at(7))
        self.assertEqual(result.consecutive_unsuccessful, 0)
        self.assertTrue(result.manual_allowed)

    def test_disabled_retains_target_presence_and_cannot_monitor_later(self):
        history = [event(comments=[sighting()]), event(1, outcome="disabled")]
        self.assertEqual(self.evaluate(history, days=1).targets[0].observed_comment_ids, ("Ugx-one",))
        self.assertTrue(self.evaluate(history, days=30).suspended)

    def test_usable_result_clears_disabled_mode_but_not_age_cutoff(self):
        history = [event(outcome="disabled"), event(7, comments=[sighting(author=OTHER)])]
        result = self.evaluate(history, days=7)
        self.assertEqual(result.phase, "ordinary")
        self.assertEqual(result.next_due_at, at(14))
        history.append(event(30, comments=[sighting()]))
        self.assertTrue(self.evaluate(history, days=30).suspended)

    def test_cooldown_defers_both_fallback_and_confirmation(self):
        missing = self.evaluate([event()], days=1, cooldown_until=at(2))
        found = self.evaluate([event(comments=[sighting()])], days=1, cooldown_until=at(2))
        self.assertEqual(missing.next_due_at, found.next_due_at)
        self.assertFalse(found.due)
        self.assertEqual(found.next_due_at, at(2))

    def test_cooldown_cannot_move_work_beyond_final_slot(self):
        self.assertEqual(self.evaluate([event()], days=1, cooldown_until=at(10)).next_due_at, at(14))
        for found in (False, True):
            result = self.evaluate([event(comments=[sighting()] if found else [])], days=14, cooldown_until=at(15))
            self.assertTrue(result.suspended)
            self.assertEqual(result.reason, "cooldown_exceeds_window")

    def test_rate_limit_failure_respects_longer_collector_cooldown(self):
        result = self.evaluate([event(outcome="failed", state="rate_limited")], cooldown_until=at(2))
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(2))

    def test_first_ever_collection_in_pause_still_honors_cooldown(self):
        result = self.evaluate(days=10, cooldown_until=at(10, 12))
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(10, 12))

    def test_missing_publication_uses_only_explicit_first_observed_fallback(self):
        result = self.evaluate(published_at=None, first_observed_at=START)
        self.assertTrue(result.due)
        self.assertEqual(result.anchor_source, "first_observed")
        missing = self.evaluate(published_at=None)
        self.assertTrue(missing.suspended)
        self.assertTrue(missing.needs_review)
        self.assertEqual(missing.reason, "missing_time_anchor")

    def test_found_target_cannot_substitute_a_detection_based_window(self):
        result = self.evaluate([event(comments=[sighting()])], published_at=None)
        self.assertTrue(result.suspended)
        self.assertEqual(result.reason, "missing_time_anchor")

    def test_fallback_window_also_stops_at_exclusive_end(self):
        result = self.evaluate([event(comments=[sighting()])], days=15,
                               published_at=None, first_observed_at=START)
        self.assertTrue(result.suspended)
        self.assertEqual(result.reason, "collection_window_expired")

    def test_verified_old_publication_is_not_replaced_by_recent_discovery(self):
        result = self.evaluate(days=20, first_observed_at=at(20))
        self.assertTrue(result.suspended)
        self.assertEqual(result.anchor, START)

    def test_date_only_publication_has_explicit_utc_day_resolution(self):
        result = self.evaluate(published_at=date(2026, 10, 26))
        self.assertEqual(result.anchor, datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(result.anchor_source, "publication_date")

    def test_future_publication_is_deferred(self):
        result = self.evaluate(published_at=at(1))
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(1))

    def test_utc_and_fall_dst_use_elapsed_twenty_four_hours(self):
        pacific = ZoneInfo("America/Los_Angeles")
        publication = datetime(2026, 10, 31, 12, tzinfo=pacific)
        history = [CollectionEvidence("dst", publication, "uncertain")]
        result = evaluate_refresh(history, now=publication, published_at=publication)
        self.assertEqual(result.next_due_at, datetime(2026, 11, 1, 19, tzinfo=timezone.utc))
        self.assertEqual(result.next_due_at.astimezone(pacific).hour, 11)

    def test_spring_dst_uses_elapsed_twenty_four_hours(self):
        pacific = ZoneInfo("America/Los_Angeles")
        publication = datetime(2026, 3, 7, 12, tzinfo=pacific)
        result = evaluate_refresh([CollectionEvidence("dst", publication, "uncertain")], now=publication, published_at=publication)
        self.assertEqual(result.next_due_at.astimezone(pacific).hour, 13)

    def test_invalid_times_duplicate_attempts_and_sightings_rejected(self):
        for kwargs in ({"now": datetime(2026, 1, 1)}, {"published_at": datetime(2026, 1, 1)},
                       {"cooldown_until": datetime(2026, 1, 1)}, {"first_observed_at": at(1)}):
            with self.assertRaises(ValueError):
                self.evaluate(**kwargs)
        for history in ([event(1)], [event(), event()], [event(outcome="wrong")],
                        [event(comments=[sighting(), sighting()])], [event(outcome="failed", comments=[sighting()])]):
            with self.assertRaises(ValueError):
                self.evaluate(history)

    def test_identical_inputs_are_deterministic_order_independent_and_immutable(self):
        history = [event(comments=[sighting()]), event(1), event(3, comments=[sighting(text="edited")])]
        before = deepcopy(history)
        result = self.evaluate(history, days=4)
        self.assertEqual(result, self.evaluate(history, days=4))
        self.assertEqual(result, self.evaluate(list(reversed(history)), days=4))
        self.assertEqual(before, history)
        with self.assertRaises(FrozenInstanceError):
            result.due = False

    def test_overdue_daily_time_is_stable_before_pause(self):
        self.assertEqual(self.evaluate([event()], days=2).next_due_at, at(1))
        self.assertEqual(self.evaluate([event()], days=5).next_due_at, at(1))

    def test_large_gaps_never_replay_missed_daily_or_final_checks(self):
        history = [event()]
        self.assertEqual(self.evaluate(history, days=10).next_due_at, at(14))
        self.assertTrue(self.evaluate(history, days=30).suspended)
        self.assertTrue(self.evaluate(history, days=90).suspended)

    def test_explicit_operator_suspension_keeps_manual_available(self):
        result = self.evaluate(automatic_suspended=True)
        self.assertTrue(result.suspended)
        self.assertFalse(result.due)
        self.assertTrue(result.manual_allowed)
        self.assertEqual(result.reason, "operator_suspended")

    def test_fixed_deadline_cannot_be_reconfigured_or_bypassed(self):
        for kwargs in ({"final_check": 30 * DAY}, {"final_window": 2 * DAY}, {"daily_window": 14 * DAY},
                       {"final_recoveries": 2}, {"daily_interval": HOUR}):
            with self.assertRaises(ValueError):
                replace(RefreshPolicy(), **kwargs)
        with self.assertRaises(TypeError):
            RefreshPolicy(edit_checks=(DAY, 90 * DAY))
        result = self.evaluate(days=30, automatic_suspended=True)
        self.assertEqual(result.reason, "collection_window_expired")

    def test_retry_intervals_and_failure_budget_remain_configurable(self):
        policy = replace(RefreshPolicy(), daily_interval=2 * DAY,
                         retry_delays=(2 * HOUR, 8 * HOUR, DAY), final_recoveries=0)
        self.assertEqual(self.evaluate([event()], policy=policy).next_due_at, at(2))
        self.assertEqual(self.evaluate([event(outcome="failed")], policy=policy).next_due_at, at(hours=2))
        self.assertTrue(self.evaluate([event(14, outcome="failed")], days=14, policy=policy).suspended)
        for kwargs in ({"max_failures": 0}, {"retry_delays": ()}, {"retry_delays": (DAY, HOUR)}):
            with self.assertRaises(ValueError):
                replace(RefreshPolicy(), **kwargs)

    def test_frequent_runner_simulation_stops_early_only_after_irish_confirmation(self):
        for present in (False, True):
            history, collected = [], []
            for hour in range(0, 100 * 24, 6):
                result = self.evaluate(history, hours=hour)
                if result.due:
                    collected.append(hour / 24)
                    history.append(event(hours=hour, comments=[sighting(text=str(hour))] if present else []))
            self.assertEqual(collected, [0, 1] if present else [0, 1, 2, 3, 4, 5, 6, 7, 14])

    def test_failure_simulation_cannot_continue_indefinitely(self):
        for initial_hour in (0, 14 * 24):
            history, collected = [], []
            for hour in range(initial_hour, 90 * 24):
                result = self.evaluate(history, hours=hour)
                if result.due:
                    collected.append(hour)
                    history.append(event(hours=hour, outcome="failed"))
            self.assertLessEqual(len(collected), 5 if initial_hour == 0 else 2)
            self.assertTrue(all(h < 15 * 24 for h in collected))

    def test_no_candidate_can_cross_deadline_for_any_target_or_cooldown(self):
        histories = [[], [event()], [event(comments=[sighting()])], [event(outcome="failed")],
                     [event(outcome="partial", comments=[sighting()])], [event(outcome="disabled")]]
        for history in histories:
            for day in (0, 7, 8, 13, 14, 14.99, 15, 30, 90):
                for cooldown in (None, at(14, 12), at(15), at(90)):
                    result = self.evaluate(history, days=day, cooldown_until=cooldown)
                    if result.next_due_at is not None:
                        self.assertLess(result.next_due_at, at(15))
                    if day >= 15:
                        self.assertTrue(result.suspended)
                        self.assertTrue(result.manual_allowed)

    def test_first_detection_days_zero_three_seven_thirteen_fourteen(self):
        for day in (0, 3, 7, 13, 14):
            with self.subTest(day=day):
                result = self.evaluate([event(day, comments=[sighting()])], days=day)
                if day == 14:
                    self.assertTrue(result.suspended)
                    self.assertEqual(result.reason, 'confirmation_exceeds_window')
                    self.assertIsNone(result.confirmation_due_at)
                else:
                    self.assertEqual(result.next_due_at, at(day + 1))
                    self.assertEqual(result.confirmation_due_at, at(day + 1))
                    self.assertEqual(result.phase, 'confirmation')

    def test_confirmation_uses_first_positive_completion_not_latest_sighting(self):
        history = [event(3, comments=[sighting()], finish=at(3, 2)),
                   event(3, hours=12, comments=[sighting(text='early correction')])]
        result = self.evaluate(history, days=3, hours=13)
        self.assertEqual(result.next_due_at, at(4, 2))
        self.assertEqual(result.targets[0].first_seen_at, at(3))

    def test_confirmation_due_at_exact_twenty_four_hour_boundary(self):
        history = [event(3, comments=[sighting()])]
        self.assertFalse(self.evaluate(history, now=at(4) - timedelta(microseconds=1)).due)
        self.assertTrue(self.evaluate(history, days=4).due)
        history.append(event(4))
        result = self.evaluate(history, days=4)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.confirmation_attempt_id, history[-1].attempt_id)
        self.assertIsNone(result.next_due_at)

    def test_early_manual_collection_does_not_consume_confirmation(self):
        history = [event(3, comments=[sighting()]), event(3, hours=23)]
        result = self.evaluate(history, days=3, hours=23)
        self.assertIsNone(result.confirmation_attempt_id)
        self.assertEqual(result.next_due_at, at(4))
        self.assertFalse(result.monitoring_complete)

    def test_confirmation_is_allowed_during_routine_pause(self):
        history = [event(7, comments=[sighting()])]
        self.assertTrue(self.evaluate(history, days=8).due)
        history.append(event(8))
        self.assertTrue(self.evaluate(history, days=9).monitoring_complete)
        self.assertIsNone(self.evaluate(history, days=14).next_due_at)

    def test_confirmation_failure_has_one_bounded_recovery(self):
        history = [event(3, comments=[sighting()]), event(4, outcome='failed')]
        result = self.evaluate(history, days=4)
        self.assertEqual(result.reason, 'confirmation_recovery')
        self.assertEqual(result.next_due_at, at(4, 1))
        self.assertEqual(result.confirmation_attempt_id, history[-1].attempt_id)
        history.append(event(4, hours=1))
        result = self.evaluate(history, days=4, hours=1)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.reason, 'confirmation_recovered')
        self.assertEqual(result.confirmation_recovery_attempt_id, history[-1].attempt_id)

    def test_failed_confirmation_recovery_stops_and_exposes_evidence(self):
        history = [event(3, comments=[sighting()]), event(4, outcome='failed'),
                   event(4, hours=1, outcome='partial')]
        result = self.evaluate(history, days=4, hours=2)
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        self.assertFalse(result.monitoring_complete)
        self.assertEqual(result.reason, 'confirmation_recovery_exhausted')
        self.assertEqual(result.confirmation_recovery_attempt_id, history[-1].attempt_id)
        self.assertIsNone(self.evaluate(history, days=14).next_due_at)

    def test_confirmation_recovery_also_allowed_during_pause(self):
        history = [event(7, comments=[sighting()]), event(8, outcome='failed')]
        self.assertEqual(self.evaluate(history, days=8).next_due_at, at(8, 1))

    def test_confirmation_recovery_uses_progressive_failure_count(self):
        history = [event(3, outcome='partial', comments=[sighting()]), event(4, outcome='failed')]
        self.assertEqual(self.evaluate(history, days=4).next_due_at, at(4, 6))

    def test_confirmation_failure_recovery_cannot_exceed_deadline(self):
        history = [event(13, hours=23, comments=[sighting()]),
                   event(14, hours=23.5, outcome='failed')]
        result = self.evaluate(history, days=14, hours=23.5)
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.reason, 'confirmation_exceeds_window')
        self.assertTrue(self.evaluate(history, days=15).suspended)

    def test_confirmation_can_fit_just_before_but_not_at_cutoff(self):
        history = [event(13, hours=23, comments=[sighting()])]
        self.assertEqual(self.evaluate(history, days=13, hours=23).next_due_at, at(14, 23))
        too_late = self.evaluate([event(14, comments=[sighting()])], days=14)
        self.assertTrue(too_late.suspended)
        self.assertIsNone(too_late.confirmation_due_at)

    def test_confirmation_cooldown_respects_pause_exception_and_cutoff(self):
        history = [event(7, comments=[sighting()])]
        result = self.evaluate(history, days=8, cooldown_until=at(10))
        self.assertEqual(result.next_due_at, at(10))
        self.assertTrue(self.evaluate(history, days=8, cooldown_until=at(15)).suspended)

    def test_confirmation_completion_latches_across_edits_absence_and_manual_failures(self):
        history = [event(comments=[sighting()]), event(1, comments=[sighting(text='corrected')]),
                   event(3), event(4, outcome='failed'), event(7, comments=[sighting(text='edited again')])]
        result = self.evaluate(history, days=7)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.confirmation_attempt_id, history[1].attempt_id)
        self.assertIsNone(result.confirmation_recovery_attempt_id)
        self.assertIsNone(result.next_due_at)

    def test_monitoring_complete_does_not_claim_complete_extraction(self):
        history = [event(comments=[sighting()]), event(1, outcome='uncertain')]
        result = self.evaluate(history, days=1)
        self.assertTrue(result.monitoring_complete)
        self.assertEqual(result.last_outcome, 'uncertain')
        self.assertTrue(result.manual_allowed)

    def test_post_cutoff_manual_collection_does_not_count_as_confirmation(self):
        history = [event(comments=[sighting()]), event(30, comments=[sighting(text='manual')])]
        result = self.evaluate(history, days=30)
        self.assertTrue(result.suspended)
        self.assertFalse(result.monitoring_complete)
        self.assertIsNone(result.confirmation_attempt_id)

    def test_explicit_disabled_confirmation_finishes_the_cycle(self):
        history = [event(comments=[sighting()]), event(1, outcome='disabled')]
        result = self.evaluate(history, days=1)
        self.assertTrue(result.monitoring_complete)
        self.assertIsNone(result.next_due_at)
        self.assertTrue(result.manual_allowed)

    def test_confirmation_recovery_can_be_disabled_but_not_expanded(self):
        history = [event(comments=[sighting()]), event(1, outcome='failed')]
        result = self.evaluate(history, days=1, policy=replace(RefreshPolicy(), confirmation_recoveries=0))
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        with self.assertRaises(ValueError):
            RefreshPolicy(confirmation_recoveries=2)

    def test_confirmation_five_failure_guard_still_applies(self):
        history = [event(hours=h, outcome='failed') for h in (0, 1, 7, 31)]
        history.append(event(hours=55, outcome='partial', comments=[sighting()]))
        result = self.evaluate(history, hours=55)
        self.assertTrue(result.suspended)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.reason, 'failure_limit')

    def test_confirmation_dst_boundaries_use_elapsed_twenty_four_hours(self):
        pacific = ZoneInfo('America/Los_Angeles')
        for publication, expected_hour in ((datetime(2026, 10, 31, 12, tzinfo=pacific), 11),
                                           (datetime(2026, 3, 7, 12, tzinfo=pacific), 13)):
            history = [CollectionEvidence('dst-positive', publication, 'uncertain', (sighting(),))]
            result = evaluate_refresh(history, now=publication, published_at=publication)
            self.assertEqual(result.next_due_at - publication.astimezone(timezone.utc), DAY)
            self.assertEqual(result.next_due_at.astimezone(pacific).hour, expected_hour)

    def test_premiere_sighting_does_not_allow_collection_before_publication(self):
        result = self.evaluate([event(comments=[sighting()])], published_at=at(2))
        self.assertFalse(result.due)
        self.assertEqual(result.next_due_at, at(2))

    def test_fallback_monitoring_completion_survives_cutoff_and_late_manual_sighting(self):
        history = [event(), event(14), event(30, comments=[sighting()])]
        result = self.evaluate(history, days=30)
        self.assertTrue(result.monitoring_complete)
        self.assertTrue(result.suspended)
        self.assertIsNone(result.confirmation_attempt_id)

    def test_explicit_backfill_intent_cannot_bypass_forward_cutoff(self):
        with self.assertRaises(TypeError):
            self.evaluate(days=100, collection_intent='historical_backfill')
        result = self.evaluate(days=100, first_observed_at=at(100))
        self.assertTrue(result.suspended)
        self.assertTrue(result.manual_allowed)

    def test_backfill_within_window_reuses_existing_collection_history(self):
        history = [event(3, comments=[sighting(author=OTHER)])]
        result = self.evaluate(history, days=3, first_observed_at=at(3))
        self.assertEqual(result.reason, 'daily_collection')
        self.assertEqual(result.next_due_at, at(4))
        self.assertEqual(result.last_attempt_id, history[0].attempt_id)
        self.assertFalse(result.due)

    def test_old_backfilled_video_and_manual_refresh_never_reset_original_age(self):
        history = [event(100, comments=[sighting()])]
        result = self.evaluate(history, days=100, first_observed_at=at(100))
        self.assertTrue(result.suspended)
        self.assertIsNone(result.confirmation_due_at)
        self.assertEqual(result.anchor, START)
        self.assertTrue(result.manual_allowed)

    def test_no_io_collection_or_wall_clock_side_effects(self):
        with patch("builtins.open", side_effect=AssertionError("File IO")), \
                patch("sqlite3.connect", side_effect=AssertionError("Database IO")), \
                patch("socket.socket.connect", side_effect=AssertionError("Network")), \
                patch("dls_video_downloader.comment_collection.collect", side_effect=AssertionError("Collection")), \
                patch("dls_video_downloader.comments.timestamp", side_effect=AssertionError("Wall clock")):
            self.assertTrue(self.evaluate().due)


class HistoryAdapterTests(unittest.TestCase):
    def test_confirmation_preserves_full_source_set_and_uses_stored_finish(self):
        with tempfile.TemporaryDirectory() as root, CommentStore(root) as store:
            initial = [{"id": "Ugx-one", "text": "original\n  ", "author_id": IRISHWEEGEE},
                       {"id": "Ugx-two", "text": "second target", "author_id": IRISHWEEGEE},
                       {"id": "Ugx-other", "text": "other author", "author_id": OTHER}]
            first = store.record('abcdefghijk', initial, outcome='uncertain',
                                 collected_at=at(3).isoformat(), coverage={'finished_at': at(3, 1).isoformat()})
            attempts = store.db.execute('SELECT * FROM collection_attempts').fetchall()
            rows = store.db.execute('''SELECT a.attempt_id,v.comment_id,v.author_id,v.text_hash
                FROM attempt_comments a JOIN comment_versions v USING(video_id,comment_id,payload_hash)''').fetchall()
            decision = evaluate_refresh(history_from_rows(attempts, rows), now=at(3, 1), published_at=START)
            self.assertEqual(decision.next_due_at, at(4, 1))
            self.assertEqual(len(store.load_snapshot('abcdefghijk', first['snapshot_hash'])['comments']), 3)
            corrected = [dict(c, text=c['text'] + ' correction') for c in initial]
            confirmation = store.record('abcdefghijk', corrected, outcome='uncertain', collected_at=at(4, 1).isoformat())
            attempts = store.db.execute('SELECT * FROM collection_attempts').fetchall()
            rows = store.db.execute('''SELECT a.attempt_id,v.comment_id,v.author_id,v.text_hash
                FROM attempt_comments a JOIN comment_versions v USING(video_id,comment_id,payload_hash)''').fetchall()
            before = store.db.total_changes
            decision = evaluate_refresh(history_from_rows(attempts, rows), now=at(4, 1), published_at=START)
            self.assertTrue(decision.monitoring_complete)
            self.assertEqual(store.db.total_changes, before)
            self.assertEqual(store.load_snapshot('abcdefghijk', first['snapshot_hash'])['comments'],
                             sorted(initial, key=lambda c: c['id']))
            self.assertEqual(store.load_snapshot('abcdefghijk', confirmation['snapshot_hash'])['comments'],
                             sorted(corrected, key=lambda c: c['id']))

    def test_original_text_snapshots_history_and_missing_comments_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            with CommentStore(root) as store:
                results = []
                for day, likes, text in ((0, 1, "original\n  "), (1, 2, "original\n  "),
                                         (7, 3, "edited\n  "), (14, 4, "final\n  ")):
                    results.append(store.record("abcdefghijk", [{"id": "Ugx-one", "text": text,
                        "author_id": IRISHWEEGEE, "author": "Mutable display", "timestamp": day,
                        "like_count": likes}], outcome="uncertain", collected_at=at(day).isoformat()))
                store.record("abcdefghijk", [], outcome="uncertain", collected_at=at(30).isoformat())
            before = {str(p): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}
            with CommentStore(root) as store:
                attempts = store.db.execute("SELECT * FROM collection_attempts").fetchall()
                sightings = store.db.execute("""SELECT a.attempt_id,v.comment_id,v.author_id,v.text_hash
                    FROM attempt_comments a JOIN comment_versions v
                    USING(video_id,comment_id,payload_hash)""").fetchall()
                changes = store.db.total_changes
                history = history_from_rows(attempts, sightings)
                result = evaluate_refresh(history, now=at(30), published_at=START)
                self.assertTrue(result.suspended)
                self.assertTrue(result.manual_allowed)
                self.assertEqual(result.targets[0].observed_comment_ids, ("Ugx-one",))
                self.assertEqual(store.db.total_changes, changes)
                self.assertEqual(store.db.execute('SELECT count(*) FROM comment_versions').fetchone()[0], 4)
                self.assertEqual(history[0].comments[0].text_hash, history[1].comments[0].text_hash)
                for saved, expected in zip(results, ("original\n  ", "original\n  ", "edited\n  ", "final\n  ")):
                    self.assertEqual(store.load_snapshot('abcdefghijk', saved['snapshot_hash'])['comments'][0]['text'], expected)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()})

    def test_adapter_retains_failure_evidence_without_operational_writes(self):
        with tempfile.TemporaryDirectory() as root, CommentStore(root) as store:
            attempt = store.record('abcdefghijk', outcome='failed', error='Timeout',
                                   collected_at=at(14).isoformat(), coverage={'retrieval_state': 'timeout'})
            rows = store.db.execute('SELECT * FROM collection_attempts').fetchall()
            result = evaluate_refresh(history_from_rows(rows, []), now=at(15), published_at=START)
            self.assertEqual(result.last_attempt_id, attempt['attempt_id'])
            self.assertTrue(result.needs_review)
            self.assertEqual(store.db.execute('SELECT error FROM collection_attempts').fetchone()[0], 'Timeout')

    def test_adapter_checks_video_scope_and_sighting_references(self):
        with self.assertRaises(ValueError):
            history_from_rows([{"video_id": "a"}, {"video_id": "b"}], [])
        with self.assertRaises(ValueError):
            history_from_rows([], [{"attempt_id": "unknown", "author_id": IRISHWEEGEE}])


if __name__ == "__main__":
    unittest.main()
