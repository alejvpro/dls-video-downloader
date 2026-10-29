"""Bounded channel discovery and explicit bounded acquisition orchestration."""
from collections import Counter
import json
from pathlib import Path
import re
import uuid

from . import acquisition as media
from .classification import CHANNEL, DEFAULT_START
from .inventory import FIELDS, encoded, import_document, now, report


def compact(document, flat=False):
    def entry(value):
        result = {k: value[k] for k in (*FIELDS, "id", "_type") if value.get(k) is not None}
        return result
    result = entry(document)
    if document.get("epoch") is not None:
        result["epoch"] = document["epoch"]
    if flat:
        result["entries"] = [entry(e) for e in document["entries"]]
    return result


def workflow(db, *, acquire_media=False, per_tab=10, max_enrich=3,
             max_downloads=1, start=DEFAULT_START, only_id=None, state=None):
    if not 1 <= per_tab <= 50 or not 0 <= max_enrich <= 20 or not 1 <= max_downloads <= 3:
        raise ValueError("Limits: per-tab 1..50, max-enrich 0..20, max-downloads 1..3")
    if only_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", only_id):
        raise ValueError("Invalid supervised video ID")
    state = (state or media.PROJECT / "state").resolve()
    if not state.is_relative_to(media.PROJECT):
        raise ValueError("Workflow state must remain inside the project")
    with media.operation_lock(state / "acquisitions"):
        run_id = now().replace(":", "") + "-" + uuid.uuid4().hex[:8]
        directory = state / "runs" / run_id
        directory.mkdir(parents=True)
        events = directory / "events.jsonl"
        result = {"run_id": run_id, "mode": "discovery+acquisition" if acquire_media else "discovery-only",
                  "start_date": start, "limits": {"per_tab": per_tab, "max_enrich": max_enrich, "max_downloads": max_downloads},
                  "encountered": {}, "new_ids": [], "enriched": [], "failures": [], "acquired": [],
                  "deferred": [], "already_verified": [], "warnings": [], "log": str(events)}

        def event(kind, **values):
            with events.open("a") as stream:
                stream.write(encoded({"at": now(), "event": kind, **values}) + "\n")
                stream.flush()

        def failure(operation, exc, video_id=None):
            item = {"operation": operation, "id": video_id,
                    "error": re.sub(r"https?://\S+", "[URL omitted]", str(exc))}
            result["failures"].append(item)
            event("failure", **item)

        common = [media.YT_DLP, "--ignore-config", "--no-cache-dir", "--skip-download",
                  "--js-runtimes", "node:" + media.NODE, "--no-write-comments", "--no-write-subs",
                  "--no-write-auto-subs", "--no-write-thumbnail", "--no-write-info-json",
                  "--socket-timeout", "30", "--retries", "0", "--extractor-retries", "0", "--dump-single-json"]
        before = {r[0] for r in db.execute("SELECT id FROM videos")}
        positions = {}
        event("start", mode=result["mode"], limits=result["limits"], only_id=only_id)
        for source, tab in (("live", "streams"), ("videos", "videos")):
            try:
                document = json.loads(media.run(common + ["--flat-playlist", "--playlist-end", str(per_tab),
                    f"https://www.youtube.com/channel/{CHANNEL}/{tab}"], directory / f"{source}.stderr.log", 300))
                entries = document.get("entries")
                if document.get("channel_id") != CHANNEL or not isinstance(entries, list) or len(entries) > per_tab:
                    raise ValueError("Unexpected channel identity or unbounded/missing playlist entries")
                with db:
                    imported = import_document(db, compact(document, flat=True), source, f"channel:{CHANNEL}/{tab}")
                result["encountered"][source] = len(entries)
                if len(entries) == per_tab:
                    result["warnings"].append(f"{source} reached the {per_tab}-entry cap; older unseen items may exist")
                for position, item in enumerate(entries):
                    positions[item["id"]] = min(position, positions.get(item["id"], per_tab))
                event("tab_imported", source=source, entries=len(entries), observations=imported["new_observations"])
            except (ValueError, OSError, TypeError, KeyError) as exc:
                failure(source, exc)
        items = {i["id"]: i for i in report(db, start)["items"]}
        result["new_ids"] = sorted(set(positions) - before)
        verified = {r[0] for r in db.execute("SELECT video_id FROM acquisitions")}
        enrich = []
        for video_id, position in positions.items():
            item = items[video_id]
            live = db.execute("SELECT 1 FROM sources WHERE video_id=? AND source='live'", (video_id,)).fetchone()
            if video_id not in verified and item["decision"] not in ("reject", "skip") and (
                    item["decision"] == "approve" or item["classification"] in ("daily_show", "the_club")
                    or (live and item["classification"] == "review")):
                enrich.append((position, video_id))
        enrich.sort()
        enriched_metadata = {}
        failed_enrichment = set()
        for _, video_id in enrich[:max_enrich]:
            try:
                document = json.loads(media.run(common + ["--no-playlist", "https://www.youtube.com/watch?v=" + video_id],
                    directory / f"{video_id}.stderr.log", 180))
                if document.get("id") != video_id or document.get("channel_id") != CHANNEL or not document.get("live_status"):
                    raise ValueError("Missing full metadata or unexpected video/channel identity")
                with db:
                    imported = import_document(db, compact(document), "other", f"full:{video_id}")
                enriched_metadata[video_id] = document
                result["enriched"].append(video_id)
                event("enriched", id=video_id, observations=imported["new_observations"])
            except (ValueError, OSError, TypeError, KeyError) as exc:
                failed_enrichment.add(video_id)
                failure("enrichment", exc, video_id)
        items = report(db, start)["items"]
        for item in items:
            if item["id"] in positions or item["eligibility"] == "eligible":
                event("classification", id=item["id"], classification=item["classification"],
                      reason=item["classification_reason"], decision=item["decision"], eligibility=item["eligibility"])
        result["classifications"] = dict(Counter(i["classification"] for i in items if i["id"] in positions))
        result["new_classifications"] = dict(Counter(i["classification"] for i in items if i["id"] in result["new_ids"]))
        result["pending_reviews"] = [i["id"] for i in items if i["id"] in positions and i["classification"] == "review" and not i["decision"]]
        result["active_reviews"] = [i["id"] for i in items if i["classification"] == "review" and not i["decision"] and i["publication_date"] and i["publication_date"] >= start]
        result["eligible"] = [i["id"] for i in items if i["eligibility"] == "eligible"]
        attempts = 0
        for item in sorted(items, key=lambda i: (i["publication_date"] or "9999", i["id"])):
            if item["eligibility"] != "eligible":
                continue
            video_id = item["id"]
            if video_id in verified:
                result["already_verified"].append(video_id)
                continue
            fresh = enriched_metadata.get(video_id)
            if video_id in failed_enrichment or (fresh and (fresh.get("live_status") not in ("was_live", "not_live") or fresh.get("is_live"))):
                result["deferred"].append({"id": video_id, "reason": "metadata failed or broadcast not ready"})
                continue
            if not acquire_media or (only_id and video_id != only_id) or attempts >= max_downloads:
                result["deferred"].append({"id": video_id, "reason": "discovery-only, supervised restriction, or run limit"})
                continue
            attempts += 1
            try:
                outcome = media.acquire(db, video_id, start, root=state / "acquisitions", _lock_held=True)
                result["acquired"].append({"id": video_id, "status": outcome["status"], "sha256": outcome["sha256"]})
                event("acquired", **result["acquired"][-1])
            except (ValueError, OSError, TypeError, KeyError) as exc:
                failure("acquisition", exc, video_id)
        result["download_attempts"] = attempts
        event("finished", failures=len(result["failures"]), acquisitions=len(result["acquired"]))
        (directory / "summary.json").write_text(encoded(result))
        return result
