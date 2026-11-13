
import json
from pathlib import Path
from collections import Counter

from solution.database import get_connection, init_db, store_leads
from solution.lead_ingestion import process_lead_file
from solution.event_ingestion import resolve_event_links


def ingest_lead_file(csv_path):
    csv_path = Path(csv_path).resolve()
    source_file = str(csv_path)
    workflow_id = csv_path.parent.name

    init_db()

    
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT 1
            FROM processed_lead_files
            WHERE source_file = ?
            """,
            (source_file,)
        ).fetchone()

    if existing:
        print(f"Already processed: {csv_path.name}")
        return None

    # Process and validate the CSV file.
    result = process_lead_file(csv_path, workflow_id)

    valid_leads = result["valid_leads"]
    rejections = result["rejections"]
    total_rows = result["total_rows"]
    rejected_rows = result["rejected_rows"]
    field_warnings = result["field_warnings"]
    rejections_by_reason = result["rejections_by_reason"]

    # Store valid leads and merge duplicates.
    duplicate_count = store_leads(valid_leads)


    stats = {
        "lead_rows_total": total_rows,
        "lead_rows_rejected": rejected_rows,
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": duplicate_count,
        "field_warnings": field_warnings
    }

    stats_json = json.dumps(stats)

    with get_connection() as conn:
        for rejection in rejections:
            conn.execute(
                """
                INSERT INTO lead_rejections
                (workflow_id, batch_id, row_number, reason, source_file)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    rejection["workflow_id"],
                    rejection["batch_id"],
                    rejection["row_number"],
                    rejection["reason"],
                    source_file
                )
            )

        conn.execute(
            """
            INSERT INTO lead_file_stats (source_file, stats)
            VALUES (?, ?)
            """,
            (source_file, stats_json)
        )

        conn.execute(
            """
            INSERT INTO processed_lead_files (source_file, stats)
            VALUES (?, ?)
            """,
            (source_file, stats_json)
        )

    print(
        f"Processed {csv_path.name}: "
        f"rows={total_rows}, "
        f"rejected={rejected_rows}, "
        f"merged_duplicates={duplicate_count}, "
        f"warnings={field_warnings}"
    )

    return stats


def ingest_lead_directory(directory):
    directory = Path(directory).resolve()

    if not directory.exists():
        raise FileNotFoundError(
            f"Directory not found: {directory}"
        )

    csv_files = sorted(
        directory.rglob("*.csv"),
        key=lambda path: (path.parent.name, path.name)
    )

    if not csv_files:
        print(f"No CSV files found in {directory}")
        return {}

    totals = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "rejections_by_reason": Counter(),
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0
    }

    for csv_file in csv_files:
        stats = ingest_lead_file(csv_file)

        if stats is None:
            continue

        totals["lead_rows_total"] += stats["lead_rows_total"]
        totals["lead_rows_rejected"] += stats["lead_rows_rejected"]

        totals["rejections_by_reason"].update(
            stats["rejections_by_reason"]
        )

        totals["duplicate_lead_rows_merged"] += (
            stats["duplicate_lead_rows_merged"]
        )

        totals["field_warnings"] += stats["field_warnings"]

    
    link_stats = resolve_event_links()

    totals["rejections_by_reason"] = dict(
        totals["rejections_by_reason"]
    )

    print("\nLEAD INGESTION SUMMARY")
    print(json.dumps(totals, indent=2))

    print("\nEVENT LINK RECOVERY")
    print(json.dumps(link_stats, indent=2))

    return totals
