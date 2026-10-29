"""Inventory CLI with explicit single-video acquisition. Status/plan are read-only."""
import argparse
from datetime import date
import json
import sqlite3
import sys
import io
from contextlib import redirect_stdout, nullcontext
from pathlib import Path

from .classification import DEFAULT_START
from .inventory import connect, decide, import_file, reclassify, report, seed
from .operations import OperationBusy, cleanup_staging, runtime_report, fatal_diagnostic, write_log


def iso_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "state/inventory.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Import local JSON only")
    importer.add_argument("--live")
    importer.add_argument("--videos")
    importer.add_argument("--full")
    importer.add_argument("--other", help="Other local extraction")
    decision = commands.add_parser("decide")
    decision.add_argument("id")
    decision.add_argument("decision", choices=["approve", "reject", "skip", "clear"])
    decision.add_argument("--reason", required=True)
    commands.add_parser("reclassify", help="Reevaluate automatic rules without changing decisions")
    download = commands.add_parser("download", help="Explicit local single-video 398+251 MKV acquisition")
    download.add_argument("id")
    download.add_argument("--start-date", type=iso_date, default=DEFAULT_START)
    for name in ("discover", "run"):
        command = commands.add_parser(name, help="Bounded discovery" + (" and acquisition" if name == "run" else " only"))
        command.add_argument("--per-tab", type=int, help="Override both tab limits")
        command.add_argument("--streams-limit", type=int, default=25)
        command.add_argument("--videos-limit", type=int, default=75)
        command.add_argument("--max-enrich", type=int, default=8)
        command.add_argument("--max-downloads", type=int, default=2)
        command.add_argument("--stale-hours", type=int, default=72)
        command.add_argument("--retry-base-minutes", type=int, default=60)
        command.add_argument("--retry-max-hours", type=int, default=24)
        command.add_argument("--quiet", action="store_true", help="Only report errors; detailed records remain in state/runs")
        command.add_argument("--start-date", type=iso_date, default=DEFAULT_START)
        command.add_argument("--only-id", help="Supervised acquisition restriction; never changes discovery rules")
    cleanup = commands.add_parser("cleanup", help="Inspect stale staging cleanup; dry-run unless --execute")
    cleanup.add_argument("--execute", action="store_true")
    cleanup.add_argument("--stale-hours", type=int, default=72)
    cleanup.add_argument("--max-bytes", type=int, default=5 * 1024**3)
    cleanup.add_argument("--max-files", type=int, default=100)
    for name in ("status", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--start-date", type=iso_date, default=DEFAULT_START)
        command.add_argument("--end-date", type=iso_date)
        command.add_argument("--json", action="store_true")
        command.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "import" and not any((args.live, args.videos, args.full, args.other)):
        parser.error("import requires at least one local input")
    readonly = args.command in ("status", "plan")
    if readonly and args.end_date and args.end_date < args.start_date:
        parser.error("end date precedes start date")
    try:
        db = connect(args.db, readonly=readonly)
        try:
            if readonly:
                result = report(db, args.start_date, args.end_date)
                result['runtime'] = runtime_report(db, result['items'], args.start_date)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    for key in ("totals", "by_source", "classifications", "manual_decisions", "eligibility"):
                        print(f"{key}: {json.dumps(result[key], ensure_ascii=False)}")
                    print(f"Automatic date range: {args.start_date} through {args.end_date or 'open-ended'}")
                    print(f"Warnings: {sum(bool(i['warnings']) for i in result['items'])} records; {result['classifications'].get('review', 0)} ambiguous classifications. Use plan --json for complete evidence.")
                    print("Eligibility is planning authorization only; acquisition readiness has not been checked.")
                    runtime = result['runtime']
                    print(f"Verified acquisitions: {runtime['verified_acquisitions']}; pending forward: {len(runtime['pending_forward'])}; retry records: {len(runtime['retry_delays'])}")
                    for row in runtime['recent_runs']:
                        print(f"{row['started_at']} {row['status']} | new acquisitions={len(row['summary'].get('acquired') or [])} failures={len(row['summary'].get('failures') or [])} | {row['run_id']}")
                    for row in runtime['retry_delays'][:10]:
                        print(f"Retry {row['video_id']} {row['operation']}: {row['category']} until {row['next_retry']} ({row['failures']} failures)")
                    if args.details or args.command == "plan":
                        for item in result["items"]:
                            print(f"{item['id']} | {item['classification']} | manual={item['decision']} | {item['eligibility']} | {item['title']}\n  {item['classification_reason']}; {item['eligibility_reason']}")
                            for warning in item["warnings"]:
                                print(f"  Warning: {warning}")
            elif args.command in ("discover", "run"):
                from .discovery import workflow
                with redirect_stdout(io.StringIO()) if args.quiet else nullcontext():
                    result = workflow(db, acquire_media=args.command == "run", per_tab=args.per_tab,
                                      streams_limit=args.streams_limit, videos_limit=args.videos_limit,
                                      max_enrich=args.max_enrich, max_downloads=args.max_downloads,
                                      start=args.start_date, only_id=args.only_id, stale_hours=args.stale_hours,
                                      retry_base_minutes=args.retry_base_minutes, retry_max_hours=args.retry_max_hours)
                if not args.quiet:
                    print(json.dumps(result, indent=2))
                if result["failures"]:
                    if args.quiet:
                        print(f"{result['finished_at']} partial_failure: {len(result['failures'])} failures; {result['log']}", file=sys.stderr)
                    return 2
            elif args.command == 'cleanup':
                from .acquisition import PROJECT, operation_lock
                with operation_lock(PROJECT / 'state/acquisitions'):
                    print(json.dumps(cleanup_staging(db, PROJECT / 'state', execute=args.execute,
                          retention_hours=args.stale_hours, max_bytes=args.max_bytes, max_files=args.max_files), indent=2))
            elif args.command == "download":
                from .acquisition import acquire
                print(json.dumps(acquire(db, args.id, args.start_date), indent=2))
            else:
                with db:
                    if args.command == "import":
                        seed(db)
                        for source in ("live", "videos", "full", "other"):
                            filename = getattr(args, source)
                            if filename:
                                print(json.dumps(import_file(db, filename, "other" if source == "full" else source), ensure_ascii=False))
                    elif args.command == "decide":
                        decide(db, args.id, args.decision, args.reason)
                    else:
                        reclassify(db)
        finally:
            db.close()
    except OperationBusy as exc:
        from .inventory import now
        print(f"{now()} overlap: {exc}", file=sys.stderr)
        return 75
    except KeyboardInterrupt:
        from .inventory import now
        print(f"{now()} interrupted; acquisition was not marked complete unless verification committed.", file=sys.stderr)
        return 130
    except Exception as exc:
        from .inventory import now
        diagnostic = f"{now()} fatal_failure: {fatal_diagnostic(exc)}"
        print(diagnostic, file=sys.stderr)
        # Pre-initialization errors have no run directory. Preserve the latest
        # fatal diagnostic beside the database, without turning status into a writer.
        parent = Path(args.db).resolve().parent
        if not readonly and parent.is_relative_to(Path(__file__).resolve().parent.parent):
            try:
                write_log(parent / 'runner-fatal.log', diagnostic)
            except OSError:
                pass  # stderr remains authoritative if local logging also fails.
        return 1
    return 0
