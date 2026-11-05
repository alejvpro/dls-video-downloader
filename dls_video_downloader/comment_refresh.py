"""Pure comment refresh decisions. No clock reads, database access, or collection."""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
import re

from .comments import IRISHWEEGEE, OUTCOMES


DAY = timedelta(days=1)


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Policy times must be timezone-aware datetimes")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RefreshPolicy:
    daily_interval: timedelta = DAY
    daily_window: timedelta = 8 * DAY  # Day 0 initial, then days 1 through 7.
    final_check: timedelta = 14 * DAY
    final_window: timedelta = DAY
    disabled_checks: tuple = (7 * DAY, 14 * DAY)
    retry_delays: tuple = (timedelta(hours=1), timedelta(hours=6), DAY)
    max_failures: int = 5
    final_recoveries: int = 1
    confirmation_recoveries: int = 1

    def __post_init__(self):
        for name in ("daily_interval", "daily_window", "final_check", "final_window"):
            if not isinstance(getattr(self, name), timedelta) or getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        for name in ("disabled_checks", "retry_delays"):
            values = getattr(self, name)
            if (not isinstance(values, tuple) or not values or
                    any(not isinstance(v, timedelta) or v <= timedelta(0) for v in values) or
                    tuple(sorted(set(values))) != values):
                raise ValueError(f"{name} must be increasing positive timedeltas")
        if (self.daily_window != 8 * DAY or self.final_check != 14 * DAY or
                self.final_window != DAY or self.disabled_checks != (7 * DAY, 14 * DAY)):
            raise ValueError("Automatic collection must use the fixed day-7/day-14 windows")
        if (self.daily_interval < DAY or self.final_recoveries not in (0, 1)
                or self.confirmation_recoveries not in (0, 1)):
            raise ValueError("Routine interval must be >=24h; at most one recovery per completion cycle")
        for name in ("final_recoveries", "confirmation_recoveries", "max_failures"):
            if type(getattr(self, name)) is not int or getattr(self, name) < (1 if name == "max_failures" else 0):
                raise ValueError(f"Invalid {name}")


@dataclass(frozen=True)
class CommentEvidence:
    comment_id: str
    author_id: str
    text_hash: str


@dataclass(frozen=True)
class CollectionEvidence:
    attempt_id: str
    collected_at: datetime
    outcome: str
    comments: tuple = ()  # CommentEvidence; no text bodies or source timestamps.
    retrieval_state: str | None = None
    finished_at: datetime | None = None

    @property
    def unsuccessful(self):
        if self.retrieval_state == "unavailable" or self.outcome == "disabled":
            return False
        return self.outcome in ("partial", "failed", "invalid") or self.retrieval_state in (
            "timeout", "interrupted", "output_limit", "launch_failed")

    @property
    def completed_at(self):
        return self.finished_at or self.collected_at


@dataclass(frozen=True)
class TargetPresence:
    author_id: str
    first_seen_at: datetime | None = None
    observed_comment_ids: tuple = ()


@dataclass(frozen=True)
class RefreshDecision:
    due: bool
    next_due_at: datetime | None
    reason: str
    suspended: bool
    manual_allowed: bool
    needs_review: bool
    anchor: datetime | None
    anchor_source: str | None
    consecutive_unsuccessful: int
    targets: tuple
    phase: str
    last_attempt_id: str | None
    last_outcome: str | None
    deadline_at: datetime | None
    confirmation_due_at: datetime | None
    confirmation_attempt_id: str | None
    confirmation_recovery_attempt_id: str | None
    monitoring_complete: bool


def history_from_rows(attempt_rows, comment_rows):
    """Adapt already-read rows for ONE video, without opening any database.

    Attempts come from collection_attempts. Comment rows are an attempt_comments /
    comment_versions join exposing attempt_id, comment_id, author_id, text_hash.
    Coverage JSON supplies retrieval_state and finished_at. No payload is read.
    """
    attempts = [dict(row) for row in attempt_rows]
    if len({row["video_id"] for row in attempts}) > 1:
        raise ValueError("History must describe one video")
    sightings = {row["attempt_id"]: [] for row in attempts}
    if len(sightings) != len(attempts):
        raise ValueError("Duplicate attempt ID")
    for row in comment_rows:
        row = dict(row)
        if row["attempt_id"] not in sightings:
            raise ValueError("Sighting references an unknown attempt")
        if row["author_id"] is not None:
            sightings[row["attempt_id"]].append(CommentEvidence(
                row["comment_id"], row["author_id"], row["text_hash"]))
    result = []
    for row in attempts:
        coverage = json.loads(row["coverage_json"])
        result.append(CollectionEvidence(
            row["attempt_id"], utc(datetime.fromisoformat(row["collected_at"])), row["outcome"],
            tuple(sightings[row["attempt_id"]]), coverage.get("retrieval_state"),
            utc(datetime.fromisoformat(coverage["finished_at"])) if coverage.get("finished_at") else None))
    return tuple(result)


def _history(history, now):
    result, ids = [], set()
    for event in history:
        if (not isinstance(event, CollectionEvidence) or not isinstance(event.attempt_id, str)
                or not event.attempt_id or event.attempt_id in ids):
            raise ValueError("Expected distinct collection evidence")
        ids.add(event.attempt_id)
        start = utc(event.collected_at)
        finish = utc(event.completed_at)
        if finish < start or finish > now:
            raise ValueError("Collection history must have completed by now")
        if not isinstance(event.outcome, str) or event.outcome not in OUTCOMES:
            raise ValueError("Unknown collection outcome")
        seen = set()
        for c in event.comments:
            if (not isinstance(c, CommentEvidence) or not isinstance(c.comment_id, str)
                    or not c.comment_id or c.comment_id in seen or
                    not isinstance(c.author_id, str) or not c.author_id or not isinstance(c.text_hash, str) or
                    not re.fullmatch(r"[a-f0-9]{64}", c.text_hash)):
                raise ValueError("Malformed comment sighting or duplicate comment ID")
            seen.add(c.comment_id)
        if event.comments and event.outcome in ("disabled", "invalid", "failed"):
            raise ValueError("Unusable outcomes cannot contain preserved comment sightings")
        result.append(replace(event, collected_at=start, finished_at=finish,
                              comments=tuple(event.comments)))
    return tuple(sorted(result, key=lambda e: (e.collected_at, e.completed_at, e.attempt_id)))


def _target_presence(author, history):
    first, comment_ids = None, set()
    for event in history:
        for c in event.comments:
            if c.author_id != author:
                continue
            if first is None:
                first = event.collected_at
            comment_ids.add(c.comment_id)
    return TargetPresence(author, first, tuple(sorted(comment_ids)))


def _disabled(history):
    for event in reversed(history):
        if event.outcome == "disabled" or event.retrieval_state == "unavailable":
            return True
        if event.comments or event.outcome in ("success", "empty", "uncertain") or event.retrieval_state in (
                "returned_nonempty", "returned_empty"):
            return False
    return False


def _schedule(history, anchor, disabled, failures, now, policy):
    if anchor is None:
        return None, "missing_time_anchor"
    final = anchor + policy.final_check
    end = final + policy.final_window
    if now >= end:
        return None, "collection_window_expired"
    final_attempts = [e for e in history if final <= e.collected_at < end]
    if final_attempts:
        if any(not e.unsuccessful for e in final_attempts[:1 + policy.final_recoveries]):
            return None, "final_check_finished"
        latest = final_attempts[-1]
        if len(final_attempts) > policy.final_recoveries:
            return None, "final_recovery_exhausted"
        due = latest.completed_at + policy.retry_delays[min(failures - 1, len(policy.retry_delays) - 1)]
        reason = "final_recovery"
    elif failures:
        last = history[-1]
        due = last.completed_at + policy.retry_delays[min(failures - 1, len(policy.retry_delays) - 1)]
        reason = "retry_backoff"
    elif disabled:
        last = history[-1].collected_at if history else None
        due = next((anchor + offset for offset in policy.disabled_checks
                    if last is None or anchor + offset > last), None)
        if due is None:
            return None, "disabled_checks_finished"
        reason = "disabled_followup"
    elif not history:
        due, reason = anchor, "initial_collection"
    else:
        due, reason = history[-1].completed_at + policy.daily_interval, "daily_collection"
    due = max(due, anchor)
    # Retries also respect the days 8–13 pause. The first-ever collection is
    # immediate even in that pause; it is not a replay of routine daily checks.
    # Disabled mode never turns failed follow-ups into aggressive hourly polling.
    if disabled and not final_attempts:
        due = max(due, next((anchor + offset for offset in policy.disabled_checks
                            if anchor + offset > history[-1].collected_at), final))
    if history and anchor + policy.daily_window <= max(now, due) < final:
        due, reason = final, "waiting_for_final_check"
    if not final_attempts and now >= final:
        due, reason = max(due, final), "final_collection"
    if due >= end:
        return None, "next_check_exceeds_window"
    return due, reason


def _confirmation(history, positive, failures, anchor, deadline, policy):
    """A single cycle anchored to first positive completion, never text changes.

    Any collection starting at/after the original due time can satisfy the
    confirmation, including a manual collection. Earlier attempts do not consume
    it or reset its clock. The first qualifying attempt and at most one recovery
    are authoritative even if later manual collections fail or omit the author.
    """
    scheduled = max(positive.completed_at + DAY, anchor)
    if scheduled >= deadline:
        return None, "confirmation_exceeds_window", None, None, None
    attempts = [e for e in history if scheduled <= e.collected_at < deadline]
    primary = attempts[0] if attempts else None
    recovery = attempts[1] if len(attempts) > 1 else None
    if primary is not None and not primary.unsuccessful:
        return None, "confirmation_complete", scheduled, primary, None
    if primary is not None:
        if recovery is not None or not policy.confirmation_recoveries:
            reason = ("confirmation_recovered" if recovery is not None and not recovery.unsuccessful
                      else "confirmation_recovery_exhausted")
            return None, reason, scheduled, primary, recovery
        due = primary.completed_at + policy.retry_delays[min(failures - 1, len(policy.retry_delays) - 1)]
        reason = "confirmation_recovery"
    else:
        due, reason = scheduled, "confirmation_pending"
    if due >= deadline:
        return None, "confirmation_exceeds_window", scheduled, primary, recovery
    return due, reason, scheduled, primary, recovery


def evaluate_refresh(history=(), *, now, published_at=None, first_observed_at=None,
                     target_author_ids=(IRISHWEEGEE,), policy=RefreshPolicy(),
                     cooldown_until=None, automatic_suspended=False):
    """Return publication-bounded collection/confirmation decisions; no IO.

    Days are elapsed UTC 24-hour periods from verified publication, or an explicit
    first-observed fallback. Final day 14 is [anchor+14d, anchor+15d). Manual
    collection always remains permitted by policy, even if automatic work stops.
    """
    now = utc(now)
    publication_source = "publication"
    if type(published_at) is date:
        published_at = datetime(published_at.year, published_at.month, published_at.day, tzinfo=timezone.utc)
        publication_source = "publication_date"
    published_at = utc(published_at) if published_at is not None else None
    first_observed_at = utc(first_observed_at) if first_observed_at is not None else None
    if first_observed_at is not None and first_observed_at > now:
        raise ValueError("First observation must not be in the future")
    cooldown_until = utc(cooldown_until) if cooldown_until is not None else None
    if not isinstance(policy, RefreshPolicy):
        raise ValueError("Expected RefreshPolicy")
    if (not isinstance(target_author_ids, (tuple, list)) or not target_author_ids or any(
            not isinstance(a, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", a)
            for a in target_author_ids)):
        raise ValueError("Expected target channel IDs")
    history = _history(history, now)
    anchor = published_at if published_at is not None else first_observed_at
    anchor_source = publication_source if published_at is not None else "first_observed" if anchor else None
    failures = 0
    for event in reversed(history):
        if not event.unsuccessful:
            break
        failures += 1
    disabled = _disabled(history)
    targets = tuple(_target_presence(author, history) for author in sorted(set(target_author_ids)))
    deadline = anchor + policy.final_check + policy.final_window if anchor else None
    positive = next((e for e in history if any(c.author_id == IRISHWEEGEE for c in e.comments)), None)
    phase = "confirmation" if positive is not None else "disabled" if disabled else "ordinary"
    confirmation_due, primary, recovery = None, None, None
    if positive is not None and anchor is not None:
        due, reason, confirmation_due, primary, recovery = _confirmation(
            history, positive, failures, anchor, deadline, policy)
    else:
        due, reason = _schedule(history, anchor, disabled, failures, now, policy)
    expired = deadline is not None and now >= deadline
    if expired:
        due, reason = None, "collection_window_expired"
    if due is not None and cooldown_until is not None and cooldown_until > due:
        due, reason = cooldown_until, "collector_cooldown"
        if phase != "confirmation" and history and anchor + policy.daily_window <= due < anchor + policy.final_check:
            due = anchor + policy.final_check
        if due >= anchor + policy.final_check + policy.final_window:
            due, reason = None, "cooldown_exceeds_window"
    # The publication-anchored deadline takes precedence even over failed final
    # attempts, late manual sightings, changed text, and operator/cache state.
    if not expired and due is not None:
        if automatic_suspended:
            due, reason = None, "operator_suspended"
        elif failures >= policy.max_failures:
            due, reason = None, "failure_limit"
    review_reasons = {"collection_window_expired", "next_check_exceeds_window",
                      "final_recovery_exhausted", "cooldown_exceeds_window",
                      "confirmation_exceeds_window", "confirmation_recovery_exhausted"}
    final_history = [e for e in history if anchor is not None and
                     anchor + policy.final_check <= e.collected_at < deadline]
    fallback_complete = ((positive is None or positive.collected_at >= deadline) and
                         any(not e.unsuccessful for e in final_history[:1 + policy.final_recoveries])) if deadline else False
    complete = (fallback_complete or reason in ("confirmation_complete", "confirmation_recovered", "final_check_finished")
                or (primary is not None and not primary.unsuccessful)
                or (recovery is not None and not recovery.unsuccessful))
    return RefreshDecision(
        due=due is not None and due <= now, next_due_at=due, reason=reason,
        suspended=due is None, manual_allowed=True,
        needs_review=(reason == "missing_time_anchor" or failures >= policy.max_failures
                      or bool(failures and reason in review_reasons)),
        anchor=anchor, anchor_source=anchor_source, consecutive_unsuccessful=failures,
        targets=targets, phase=phase,
        last_attempt_id=history[-1].attempt_id if history else None,
        last_outcome=history[-1].outcome if history else None,
        deadline_at=deadline, confirmation_due_at=confirmation_due,
        confirmation_attempt_id=primary.attempt_id if primary else None,
        confirmation_recovery_attempt_id=recovery.attempt_id if recovery else None,
        monitoring_complete=complete)
