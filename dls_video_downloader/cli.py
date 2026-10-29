"""Inventory CLI with explicit single-video acquisition. Status/plan are read-only."""
import argparse
from datetime import date
import json
import sqlite3
import sys
from pathlib import Path

from .classification import DEFAULT_START
from .inventory import connect, decide, import_file, reclassify, report, seed


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
        command.add_argument("--per-tab", type=int, default=10)
        command.add_argument("--max-enrich", type=int, default=3)
        command.add_argument("--max-downloads", type=int, default=1)
        command.add_argument("--start-date", type=iso_date, default=DEFAULT_START)
        command.add_argument("--only-id", help="Supervised acquisition restriction; never changes discovery rules")
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
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    for key in ("totals", "by_source", "classifications", "manual_decisions", "eligibility"):
                        print(f"{key}: {json.dumps(result[key], ensure_ascii=False)}")
                    print(f"Automatic date range: {args.start_date} through {args.end_date or 'open-ended'}")
                    print(f"Warnings: {sum(bool(i['warnings']) for i in result['items'])} records; {result['classifications'].get('review', 0)} ambiguous classifications. Use plan --json for complete evidence.")
                    print("Eligibility is planning authorization only; acquisition readiness has not been checked.")
                    if args.details or args.command == "plan":
                        for item in result["items"]:
                            print(f"{item['id']} | {item['classification']} | manual={item['decision']} | {item['eligibility']} | {item['title']}\n  {item['classification_reason']}; {item['eligibility_reason']}")
                            for warning in item["warnings"]:
                                print(f"  Warning: {warning}")
            elif args.command in ("discover", "run"):
                from .discovery import workflow
                result = workflow(db, acquire_media=args.command == "run", per_tab=args.per_tab,
                                  max_enrich=args.max_enrich, max_downloads=args.max_downloads,
                                  start=args.start_date, only_id=args.only_id)
                print(json.dumps(result, indent=2))
                if result["failures"]:
                    return 2
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
    except KeyboardInterrupt:
        print("Interrupted; acquisition was not marked complete unless verification committed.", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, TypeError, AttributeError, OverflowError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0
