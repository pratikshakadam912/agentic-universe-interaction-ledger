"""Command-line interface for Assignment A.

Run from the repository root:
    python -m solution.cli ingest data/drop_1
    python -m solution.cli ingest data/drop_2
    python -m solution.cli report --out reports/
"""
import argparse
import json
from pathlib import Path

from solution.database import get_connection, init_db, store_leads
from solution.lead_ingestion import process_lead_file
from solution.event_ingestion import process_event_directory, resolve_event_links
from solution.reporting import generate_reports


def _already_processed(source_file):
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM processed_lead_files WHERE source_file = ?",
            (source_file,),
        ).fetchone() is not None


def ingest_lead_file(csv_path):
    """Validate and persist one lead CSV exactly once."""
    csv_path = Path(csv_path)
    source_file = str(csv_path.resolve())
    workflow_id = csv_path.parent.name
    batch_id = csv_path.stem

    if _already_processed(source_file):
        print(f"SKIP already processed lead file: {csv_path}")
        return {"already_processed": True}

    result = process_lead_file(csv_path, workflow_id)
    valid_leads = result["valid_leads"]
    rejections = result["rejections"]
    stats = {
        "total_rows": int(result["total_rows"]),
        "rejected_rows": int(result["rejected_rows"]),
        "field_warnings": int(result["field_warnings"]),
    }

    # Keep the source filename as the first-seen batch identity.
    for lead in valid_leads:
        lead["batch_id"] = batch_id

    duplicates = store_leads(valid_leads)
    stats["duplicate_lead_rows_merged"] = int(duplicates)
    stats["merged_duplicates"] = int(duplicates)

    init_db()
    with get_connection() as conn:
        for rejection in rejections:
            conn.execute(
                """
                INSERT INTO lead_rejections
                    (workflow_id, batch_id, row_number, reason, source_file)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    batch_id,
                    int(rejection["row_number"]),
                    str(rejection["reason"]),
                    source_file,
                ),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO lead_file_stats (source_file, stats)
            VALUES (?, ?)
            """,
            (source_file, json.dumps(stats)),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_lead_files (source_file, stats)
            VALUES (?, ?)
            """,
            (source_file, json.dumps(stats)),
        )

    print(
        f"INGESTED {csv_path}: rows={stats['total_rows']}, "
        f"accepted={len(valid_leads)}, rejected={stats['rejected_rows']}, "
        f"merged_duplicates={duplicates}"
    )
    return stats


def ingest_drop(drop_directory):
    """Ingest all leads first, then events, in stable filename order."""
    drop_directory = Path(drop_directory)
    if not drop_directory.is_dir():
        raise FileNotFoundError(f"Drop directory not found: {drop_directory}")

    leads_root = drop_directory / "leads"
    events_root = drop_directory / "events"

    lead_results = []
    if leads_root.exists():
        for csv_path in sorted(leads_root.rglob("*.csv")):
            lead_results.append(ingest_lead_file(csv_path))

    # Also relink events quarantined during an earlier drop, even if this
    # drop contains no new event files.
    resolve_event_links()

    event_results = []
    if events_root.exists():
        event_results = process_event_directory(events_root)

    print(
        f"DROP COMPLETE: {drop_directory} "
        f"(lead_files={len(lead_results)}, event_files={len(event_results)})"
    )
    return {"lead_files": lead_results, "event_files": event_results}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="assignment-a",
        description="Interaction Ledger & Reporting Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest one data drop directory"
    )
    ingest_parser.add_argument("directory", help="Path such as data/drop_1")

    report_parser = subparsers.add_parser(
        "report", help="Generate the five required reports"
    )
    report_parser.add_argument(
        "--out", default="reports", help="Output directory (default: reports)"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    init_db()

    if args.command == "ingest":
        ingest_drop(args.directory)
    elif args.command == "report":
        generate_reports(args.out)


if __name__ == "__main__":
    main()
