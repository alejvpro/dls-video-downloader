"""Bounded channel discovery and explicit bounded acquisition orchestration."""
from collections import Counter
import json
from pathlib import Path
import re
import uuid

from . import acquisition as media
from .classification import CHANNEL, DEFAULT_START
from .inventory import FIELDS, encoded, import_document, now, report
from .operations import (LOG_LIMIT, cleanup_staging, forward_allowed, retry_pending,
                         retry_failure, retry_success, run_record, safe_text)
from .operations import OperationBusy, record_overlap


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


def workflow(db, *, acquire_media=False, per_tab=None, streams_limit=25, videos_limit=75,
             max_enrich=8, max_downloads=2, start=DEFAULT_START, only_id=None, state=None,
             stale_hours=72, retry_base_minutes=60, retry_max_hours=24):
    if per_tab is not None:
        streams_limit = videos_limit = per_tab
    if (not 1 <= streams_limit <= 200 or not 1 <= videos_limit <= 200
            or not 0 <= max_enrich <= 20 or not 1 <= max_downloads <= 3
            or stale_hours < 24 or not 1 <= retry_base_minutes <= 1440
            or not 1 <= retry_max_hours <= 168):
        raise ValueError("Invalid limits: tabs 1..200, enrichment 0..20, downloads 1..3, retention >=24h")
    if only_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", only_id):
        raise ValueError("Invalid supervised video ID")
    state = (state or media.PROJECT / "state").resolve()
    if not state.is_relative_to(media.PROJECT):
        raise ValueError("Workflow state must remain inside the project")
    try:
        with media.operation_lock(state / "acquisitions"):
            return _workflow_locked(db, acquire_media, streams_limit, videos_limit, max_enrich,
                                    max_downloads, start, only_id, state, stale_hours,
                                    retry_base_minutes, retry_max_hours)
    except OperationBusy:
        record_overlap(db, 'overlap-' + uuid.uuid4().hex)
        raise


def _workflow_locked(db, acquire_media, streams_limit, videos_limit, max_enrich,
                     max_downloads, start, only_id, state, stale_hours,
                     retry_base_minutes, retry_max_hours):
    run_id = now().replace(":", "") + "-" + uuid.uuid4().hex[:8]
    directory = state / "runs" / run_id
    directory.mkdir(parents=True)
    events = directory / "events.jsonl"
    result = {"run_id": run_id, "mode": "discovery+acquisition" if acquire_media else "discovery-only",
              "start_date": start, "limits": {"streams": streams_limit, "videos": videos_limit, "max_enrich": max_enrich, "max_downloads": max_downloads},
              "encountered": {}, "new_ids": [], "enriched": [], "failures": [], "acquired": [],
              "deferred": [], "already_verified": [], "warnings": [], "log": str(events), "retry_deferred": [], "coverage": {}}

    def event(kind, **values):
        with events.open("a") as stream:
            if stream.tell() < LOG_LIMIT:
                stream.write(encoded({"at": now(), "event": kind, **values}) + "\n")
            stream.flush()

    def failure(operation, exc, video_id=None):
        item = {"operation": operation, "id": video_id,
                "error": safe_text(exc)[:1000]}
        result["failures"].append(item)
        event("failure", **item)

    with run_record(db, state, result):
        common = [media.YT_DLP, "--ignore-config", "--no-cache-dir", "--skip-download",
                  "--ffmpeg-location", media.FFMPEG,
                  "--js-runtimes", "node:" + media.NODE, "--no-write-comments", "--no-write-subs",
                  "--no-write-auto-subs", "--no-write-thumbnail", "--no-write-info-json",
                  "--socket-timeout", "30", "--retries", "0", "--extractor-retries", "0", "--dump-single-json"]
        result['cleanup'] = cleanup_staging(db, state, execute=True, retention_hours=stale_hours)
        event('cleanup', files=len(result['cleanup']['files']), bytes=result['cleanup']['bytes'])
        before = {r[0] for r in db.execute("SELECT id FROM videos")}
        positions = {}
        event("start", mode=result["mode"], limits=result["limits"], only_id=only_id)
        for source, tab, limit in (("live", "streams", streams_limit), ("videos", "videos", videos_limit)):
            try:
                document = json.loads(media.run(common + ["--flat-playlist", "--playlist-end", str(limit),
                    f"https://www.youtube.com/channel/{CHANNEL}/{tab}"], directory / f"{source}.stderr.log", 300))
                entries = document.get("entries")
                if document.get("channel_id") != CHANNEL or not isinstance(entries, list) or len(entries) > limit:
                    raise ValueError("Unexpected channel identity or unbounded/missing playlist entries")
                with db:
                    imported = import_document(db, compact(document, flat=True), source, f"channel:{CHANNEL}/{tab}")
                result["encountered"][source] = len(entries)
                frontier = db.execute("SELECT head_id FROM scan_frontiers WHERE source=?", (source,)).fetchone()
                ids = [e['id'] for e in entries]
                overlap = bool(frontier and frontier[0] in ids)
                result['coverage'][source] = {'prior_head_seen': overlap, 'initial_scan': not bool(frontier), 'cap_reached': len(entries) == limit}
                if frontier and not overlap:
                    failure('coverage', ValueError(f'{source}: previous scan head missing; possible discovery gap'))
                elif not frontier and len(entries) == limit:
                    result['warnings'].append(f'{source}: initial bounded baseline reached {limit} entries')
                if ids and (not frontier or overlap):
                    with db:
                        db.execute("INSERT INTO scan_frontiers VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET head_id=excluded.head_id,scanned_at=excluded.scanned_at", (source, ids[0], now()))
                for position, item in enumerate(entries):
                    positions[item["id"]] = min(position, positions.get(item["id"], limit))
                event("tab_imported", source=source, entries=len(entries), observations=imported["new_observations"])
            except (ValueError, OSError) as exc:
                failure(source, exc)
        items = {i["id"]: i for i in report(db, start)["items"]}
        result["new_ids"] = sorted(set(positions) - before)
        verified = {r[0] for r in db.execute("SELECT video_id FROM acquisitions")}
        enrich = []
        for video_id, position in positions.items():
            item = items[video_id]
            live = db.execute("SELECT 1 FROM sources WHERE video_id=? AND source='live'", (video_id,)).fetchone()
            if item['publication_date'] and item['publication_date'] < start:
                continue
            pending = retry_pending(db, video_id, 'enrichment')
            if pending:
                result['retry_deferred'].append(pending)
                continue
            if video_id not in verified and item["decision"] not in ("reject", "skip") and (
                    item["decision"] == "approve" or item["classification"] in ("daily_show", "the_club")
                    or (live and item["classification"] == "review")):
                enrich.append((position, video_id))
        enrich.sort()
        # Reserve the first opportunities for a daily show and a Club upload;
        # a large flat backlog on either tab cannot starve the other program.
        first_daily = next((entry for entry in enrich if items[entry[1]]['classification'] == 'daily_show'), None)
        first_club = next((entry for entry in enrich if items[entry[1]]['classification'] == 'the_club'), None)
        preferred = [entry for entry in (first_daily, first_club) if entry]
        enrich = preferred + [entry for entry in enrich if entry not in preferred]
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
                retry_success(db, video_id, 'enrichment')
                result["enriched"].append(video_id)
                event("enriched", id=video_id, observations=imported["new_observations"])
            except (ValueError, OSError) as exc:
                failed_enrichment.add(video_id)
                retry_failure(db, video_id, 'enrichment', exc, base_minutes=retry_base_minutes, max_hours=retry_max_hours)
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
        result["eligible"] = [i["id"] for i in items if forward_allowed(i, start)]
        attempts = 0
        for item in sorted(items, key=lambda i: (i["publication_date"] or "9999", i["id"])):
            if not forward_allowed(item, start):
                if item['eligibility'] == 'eligible':
                    result['deferred'].append({'id': item['id'], 'reason': 'scheduled policy requires verified forward-window date, even for approvals'})
                continue
            video_id = item["id"]
            if video_id in verified:
                result["already_verified"].append(video_id)
                continue
            pending = retry_pending(db, video_id, 'acquisition') or retry_pending(db, video_id, 'enrichment')
            if pending:
                result['retry_deferred'].append(pending)
                event('retry_deferred', id=video_id, next_retry=pending['next_retry'], category=pending['category'])
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
                outcome = media.acquire(db, video_id, start, root=state / "acquisitions", _lock_held=True,
                                        retry_base_minutes=retry_base_minutes, retry_max_hours=retry_max_hours, _forward_only=True)
                retry_success(db, video_id, 'acquisition')
                result["acquired"].append({"id": video_id, "status": outcome["status"], "sha256": outcome["sha256"]})
                event("acquired", **result["acquired"][-1])
            except (ValueError, OSError) as exc:
                if not retry_pending(db, video_id, 'acquisition'):
                    retry_failure(db, video_id, 'acquisition', exc, base_minutes=retry_base_minutes, max_hours=retry_max_hours)
                failure("acquisition", exc, video_id)
        result["download_attempts"] = attempts
        event("finished", failures=len(result["failures"]), acquisitions=len(result["acquired"]))
        return result
