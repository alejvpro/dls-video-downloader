"""Read-only inventory/comment planning. No discovery or collection imports."""
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

from . import youtube_safety as safety
from .classification import DEFAULT_START, eligibility
from .comments import APPLICATION_ID
from .comment_refresh import evaluate_refresh, history_from_rows, utc
from .inventory import connect, report


PROJECT = Path(__file__).resolve().parent.parent


def instant(value):
    if isinstance(value, str):
        return utc(datetime.fromisoformat(value))
    return utc(value)


def age_anchor(metadata, daily):
    """Return source evidence, never a download time or inferred broadcast end."""
    fields = ("release_timestamp",) if daily else ("timestamp", "release_timestamp")
    for field in fields:
        value = metadata.get(field)
        if type(value) in (int, float):
            try:
                return datetime.fromtimestamp(value, timezone.utc), field, "second"
            except (ValueError, OverflowError, OSError):
                pass
    for field in ("release_date", "upload_date"):
        try:
            return date.fromisoformat(metadata[field]), field, "date"
        except (KeyError, TypeError, ValueError):
            pass
    return None, None, "unknown"


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


def plan(inventory_path, comment_root=PROJECT / "state/comments", *, now,
         start=DEFAULT_START, cooldown_until=None):
    """Read stable database views; return deterministic decisions for all known IDs.

    A missing comment catalog means no history and is never initialized. Existing
    invalid catalogs fail closed. Caller supplies time and any global cooldown.
    """
    now = instant(now)
    start = date.fromisoformat(start).isoformat()
    catalog = Path(comment_root) / "comments.sqlite3"
    with closing(connect(inventory_path, readonly=True, timeout=1)) as inventory:
        inventory.execute("BEGIN")
        if catalog.exists():
            with closing(sqlite3.connect(catalog.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as comments:
                comments.row_factory = sqlite3.Row
                comments.execute("PRAGMA query_only=ON")
                comments.execute("BEGIN")
                identity = (comments.execute("PRAGMA application_id").fetchone()[0],
                            comments.execute("PRAGMA user_version").fetchone()[0])
                if identity != (APPLICATION_ID, 1):
                    raise ValueError("Unsupported comment catalog")
                return safety.apply_plan(_plan(inventory, comments, now, start, cooldown_until),Path(inventory_path).resolve().parent,now)
        return safety.apply_plan(_plan(inventory, None, now, start, cooldown_until),Path(inventory_path).resolve().parent,now)


def selection_view(inventory, *, now, start=DEFAULT_START):
    """Reuse inventory selection/provenance under caller's read transaction.

    No comment catalog is read. Consumers use eligibility evidence, not the empty
    comment-history refresh decisions attached by the existing view builder.
    """
    return _plan(inventory, None, instant(now), date.fromisoformat(start).isoformat(), None)


def _plan(inventory, comments, now, start, cooldown):
    items = []
    for selected in report(inventory, start)["items"]:
        video_id = selected["id"]
        row = inventory.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        metadata = json.loads(row["metadata"])
        provenance = json.loads(row["field_provenance"])
        daily = selected["classification"] == "daily_show"
        anchor, field, precision = age_anchor(metadata, daily or metadata.get("was_live") is True or metadata.get("live_status") == "was_live")
        # Numeric source evidence also provides a verified UTC date to the existing
        # eligibility rules; retain its actual field separately in the output.
        effective_metadata = dict(metadata)
        if anchor is not None:
            effective_metadata["release_date"] = anchor.isoformat()[:10]
        eligible, selection_reason = eligibility(effective_metadata, selected["classification"],
                                                  selected["decision"], start)
        exclusion = None
        if eligible != "eligible":
            exclusion = eligible
        elif anchor is None:
            exclusion = "publication_unknown"
        elif anchor.isoformat()[:10] < start:
            exclusion = "outside_forward_range"
        elif daily and metadata.get("live_status") != "was_live":
            exclusion = "completed_livestream_required"
        elif metadata.get("live_status") in ("is_upcoming", "is_live", "post_live"):
            exclusion = "video_not_completed"
        attempts, versions = [], []
        if comments is not None:
            attempts = comments.execute("SELECT * FROM collection_attempts WHERE video_id=? ORDER BY collected_at,attempt_id", (video_id,)).fetchall()
            versions = comments.execute("""SELECT a.attempt_id,a.comment_id,v.author_id,v.text_hash
                FROM attempt_comments a JOIN comment_versions v
                ON a.video_id=v.video_id AND a.comment_id=v.comment_id AND a.payload_hash=v.payload_hash
                WHERE a.video_id=?""", (video_id,)).fetchall()
        history = history_from_rows(attempts, versions)
        decision = evaluate_refresh(history, now=now, published_at=anchor,
                                    first_observed_at=instant(row["first_seen"]),
                                    cooldown_until=cooldown)
        completed = inventory.execute("SELECT observed_at,imported_at,metadata FROM observations WHERE video_id=? ORDER BY imported_at,observation_id", (video_id,)).fetchall()
        completed = next((o for o in completed if json.loads(o["metadata"]).get("live_status") == "was_live"), None)
        state = ("excluded" if exclusion else "due" if decision.due else
                 "completed" if decision.monitoring_complete else
                 "suspended" if decision.suspended else "deferred")
        items.append({"id": video_id, "title": selected["title"],
                      "category": selected["classification"], "manual_decision": selected["decision"],
                      "selection": "approved" if selected["decision"] == "approve" else selected["classification"],
                      "selection_reason": selection_reason, "eligible": exclusion is None,
                      "status": state, "reason": exclusion or decision.reason,
                      "due": exclusion is None and decision.due,
                      "age_anchor": decision.anchor, "age_anchor_field": field or "first_seen",
                      "age_anchor_precision": precision, "age_anchor_provenance": provenance.get(field),
                      "first_observed_at": row["first_seen"],
                      "inventory_live_status": metadata.get("live_status"),
                      "inventory_live_status_provenance": provenance.get("live_status"),
                      "first_completed_observation": ({"observed_at": completed["observed_at"], "imported_at": completed["imported_at"]} if completed else None),
                      "source_timing": {k: {"value": metadata[k], "provenance": provenance.get(k)}
                                        for k in ("release_timestamp", "timestamp", "release_date", "upload_date") if k in metadata},
                      "first_collection_at": min((h.collected_at for h in history), default=None),
                      "last_collection_at": max((h.collected_at for h in history), default=None),
                      "confirmation_collection_at": next((h.collected_at for h in history if h.attempt_id == decision.confirmation_attempt_id), None),
                      "collection_kind": "confirmation" if decision.phase == "confirmation" else "initial" if not history else "refresh",
                      "refresh": asdict(decision)})
    # Oldest due time first prevents starvation; initial work wins ties only.
    due = sorted((i for i in items if i["due"]), key=lambda i: (
        i["refresh"]["next_due_at"], i["collection_kind"] != "initial", i["id"]))
    return _json({"evaluated_at": now, "start_date": start, "comment_catalog": "present" if comments is not None else "missing",
                  "due_ids": [i["id"] for i in due], "items": items})


def render(result, *, include_excluded=False):
    lines = [f"Comment plan at {result['evaluated_at']} (UTC); due={len(result['due_ids'])}",
             "Planning only; monitoring completion does not establish exhaustive retrieval."]
    excluded = sum(i["status"] == "excluded" for i in result["items"])
    lines.append(f"Excluded={excluded}; use --details or --json for exclusions. All times below are UTC.")
    for item in result["items"]:
        if item["status"] == "excluded" and not include_excluded:
            continue
        r = item["refresh"]
        target = r["targets"][0]
        lines.extend([f"{item['id']} | {item['status']} | {item['selection']} | {item['title']}",
                      f"  {item['reason']} | {item['collection_kind']} | phase={r['phase']} | IrishWeegee={'observed' if target['first_seen_at'] else 'not observed'}",
                      f"  anchor={item['age_anchor']} ({item['age_anchor_field']}, {item['age_anchor_precision']}); next={r['next_due_at']}; cutoff={r['deadline_at']}",
                      f"  inventory first={item['first_observed_at']}; last collection={item['last_collection_at']}"])
    return "\n".join(lines)
