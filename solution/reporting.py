import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solution.database import get_connection, init_db


IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_FILE = SCHEMA_DIR / "dispositions.csv"

REPORT_HEADERS = {
    "batch_summary.csv": [
        "workflow_id",
        "batch_id",
        "total_leads",
        "yet_to_reach_out",
        "in_progress_telephony_failed",
        "in_progress_dialed",
        "completed",
    ],
    "daily_overview.csv": [
        "workflow_id",
        "date",
        "total_calls",
        "connected_calls",
        "connectivity_rate",
        "success_leads",
    ],
    "lead_report.csv": [
        "workflow_id",
        "external_ref",
        "batch_id",
        "phone_masked",
        "bucket",
        "total_calls",
        "connected_calls",
        "last_call_at",
        "current_disposition",
        "ever_success",
    ],
    "rejections.csv": [
        "workflow_id",
        "batch_id",
        "row_number",
        "reason",
    ],
}


def parse_timestamp(value):
    if not value:
        return None

    try:
        value = str(value).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)

        return parsed.astimezone(IST)
    except (ValueError, TypeError, OverflowError):
        return None


def load_workflow_schemas():
    if not SCHEMA_DIR.exists():
        raise FileNotFoundError(f"Schema directory not found: {SCHEMA_DIR}")

    schemas = {}
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as file:
            schemas[path.stem] = json.load(file)

    return schemas


def get_schema_fields(schema):
    if isinstance(schema, list):
        return schema
    return schema.get("fields", [])


def get_reference_fields(schemas):
    references = {}

    for workflow_id, schema in schemas.items():
        for field in get_schema_fields(schema):
            if field.get("reference") is True:
                references[workflow_id] = field["name"]
                break

    return references


def load_dispositions():
    if not DISPOSITIONS_FILE.exists():
        raise FileNotFoundError(
            f"Dispositions file not found: {DISPOSITIONS_FILE}"
        )

    catalogue = {}
    with DISPOSITIONS_FILE.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.DictReader(file)

        required_columns = {
            "workflow_id",
            "code",
            "label",
            "is_terminal",
            "is_success",
        }
        if not reader.fieldnames or not required_columns.issubset(
            set(reader.fieldnames)
        ):
            raise ValueError("dispositions.csv is missing required columns")

        for row in reader:
            workflow_id = (row.get("workflow_id") or "").strip()
            code = (row.get("code") or "").strip()
            if not workflow_id or not code:
                continue

            catalogue[(workflow_id, code)] = {
                "label": (row.get("label") or "").strip(),
                "is_terminal": (
                    (row.get("is_terminal") or "").strip().lower() == "true"
                ),
                "is_success": (
                    (row.get("is_success") or "").strip().lower() == "true"
                ),
            }

    return catalogue


def load_leads(conn):
    leads = {}
    rows = conn.execute(
        """
        SELECT workflow_id, phone_hash, batch_id, phone_masked, lead_data
        FROM leads
        ORDER BY workflow_id, batch_id, phone_hash
        """
    ).fetchall()

    for row in rows:
        workflow_id = row["workflow_id"]
        phone_hash = row["phone_hash"]
        try:
            lead_data = json.loads(row["lead_data"] or "{}")
        except (json.JSONDecodeError, TypeError):
            lead_data = {}

        leads[(workflow_id, phone_hash)] = {
            "workflow_id": workflow_id,
            "phone_hash": phone_hash,
            "batch_id": row["batch_id"],
            "phone_masked": row["phone_masked"],
            "data": lead_data,
            "events": [],
            "calls": {},
            "bucket": None,
        }

    return leads


def load_linked_events(conn):
    rows = conn.execute(
        """
        SELECT
            e.event_id,
            e.event_type,
            e.workflow_id,
            e.call_id,
            e.occurred_at,
            e.payload,
            l.phone_hash
        FROM events AS e
        INNER JOIN event_lead_links AS l
            ON e.event_id = l.event_id
        ORDER BY e.occurred_at, e.event_id
        """
    ).fetchall()

    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (json.JSONDecodeError, TypeError):
            payload = {}

        events.append(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "workflow_id": row["workflow_id"],
                "call_id": row["call_id"],
                "occurred_at": row["occurred_at"],
                "timestamp": parse_timestamp(row["occurred_at"]),
                "payload": payload,
                "phone_hash": row["phone_hash"],
            }
        )

    return events


def event_sort_key(event):
    timestamp = event["timestamp"]
    return (
        timestamp is not None,
        timestamp or datetime.min.replace(tzinfo=IST),
        event["event_id"],
    )


def get_effective_disposition(call_events, catalogue):
    overrides = []
    disposition_sets = []

    for event in call_events:
        event_type = event["event_type"]
        if event_type not in ("disposition.override", "disposition.set"):
            continue

        code = event["payload"].get("disposition_code")
        if not code:
            continue

        code = str(code).strip()
        if (event["workflow_id"], code) not in catalogue:
            continue

        if event_type == "disposition.override":
            overrides.append(event)
        else:
            disposition_sets.append(event)

    candidates = overrides if overrides else disposition_sets
    if not candidates:
        return None

    selected = max(candidates, key=event_sort_key)
    return str(selected["payload"]["disposition_code"]).strip()


def classify_bucket(lead, catalogue):
    calls = lead["calls"]

    if not calls:
        return "yet_to_reach_out"

    for call in calls.values():
        code = call["effective_disposition"]
        disposition = catalogue.get((lead["workflow_id"], code))
        if disposition and disposition["is_terminal"]:
            return "completed"

    every_call_failed = all(
        any(
            event["event_type"] == "call.telephony_failed"
            for event in call["events"]
        )
        for call in calls.values()
    )

    if every_call_failed:
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


def build_call_data(leads, events, catalogue):
    for event in events:
        lead_key = (event["workflow_id"], event["phone_hash"])
        lead = leads.get(lead_key)
        if lead is not None:
            lead["events"].append(event)

    for lead in leads.values():
        grouped_events = defaultdict(list)

        for event in lead["events"]:
            if event["call_id"]:
                grouped_events[event["call_id"]].append(event)

        for call_id, call_events in grouped_events.items():
            initiated_events = [
                event
                for event in call_events
                if event["event_type"] == "call.initiated"
            ]
            if not initiated_events:
                continue

            initiated_event = max(initiated_events, key=event_sort_key)
            connected = any(
                event["event_type"] == "call.connected"
                for event in call_events
            )
            disposition = get_effective_disposition(
                call_events, catalogue
            )

            lead["calls"][call_id] = {
                "call_id": call_id,
                "initiated_at": initiated_event["timestamp"],
                "connected": connected,
                "events": call_events,
                "effective_disposition": disposition,
            }

        lead["bucket"] = classify_bucket(lead, catalogue)


def build_batch_summary(leads):
    bucket_names = [
        "yet_to_reach_out",
        "in_progress_telephony_failed",
        "in_progress_dialed",
        "completed",
    ]
    grouped = defaultdict(Counter)

    for lead in leads.values():
        key = (lead["workflow_id"], lead["batch_id"])
        grouped[key]["total_leads"] += 1
        grouped[key][lead["bucket"]] += 1

    rows = []
    for workflow_id, batch_id in sorted(grouped):
        counts = grouped[(workflow_id, batch_id)]
        row = {
            "workflow_id": workflow_id,
            "batch_id": batch_id,
            "total_leads": counts["total_leads"],
        }
        for bucket in bucket_names:
            row[bucket] = counts[bucket]
        rows.append(row)

    return rows


def build_daily_rows(leads, catalogue):
    daily = defaultdict(
        lambda: {
            "total_calls": 0,
            "connected_calls": 0,
            "success_leads": set(),
        }
    )

    for lead in leads.values():
        workflow_id = lead["workflow_id"]

        for call in lead["calls"].values():
            initiated_at = call["initiated_at"]
            if initiated_at is None:
                continue

            date = initiated_at.date().isoformat()
            stats = daily[(workflow_id, date)]
            stats["total_calls"] += 1

            if call["connected"]:
                stats["connected_calls"] += 1

            code = call["effective_disposition"]
            disposition = catalogue.get((workflow_id, code))
            if disposition and disposition["is_success"]:
                stats["success_leads"].add(lead["phone_hash"])

    rows = []
    for workflow_id, date in sorted(daily):
        stats = daily[(workflow_id, date)]
        total = stats["total_calls"]
        connected = stats["connected_calls"]

        rows.append(
            {
                "workflow_id": workflow_id,
                "date": date,
                "total_calls": total,
                "connected_calls": connected,
                "connectivity_rate": f"{connected / total:.4f}",
                "success_leads": len(stats["success_leads"]),
            }
        )

    return rows


def build_lead_report(leads, catalogue, references):
    rows = []

    for lead in leads.values():
        workflow_id = lead["workflow_id"]
        calls = sorted(
            lead["calls"].values(),
            key=lambda call: (
                call["initiated_at"] is not None,
                call["initiated_at"]
                or datetime.min.replace(tzinfo=IST),
                call["call_id"],
            ),
        )

        total_calls = len(calls)
        connected_calls = sum(
            1 for call in calls if call["connected"]
        )

        last_call_at = ""
        if calls and calls[-1]["initiated_at"] is not None:
            last_call_at = calls[-1]["initiated_at"].isoformat()

        calls_with_disposition = [
            call for call in calls if call["effective_disposition"]
        ]
        current_disposition = (
            calls_with_disposition[-1]["effective_disposition"]
            if calls_with_disposition
            else ""
        )

        ever_success = any(
            (
                disposition := catalogue.get(
                    (workflow_id, call["effective_disposition"])
                )
            )
            and disposition["is_success"]
            for call in calls
            if call["effective_disposition"]
        )

        reference_field = references.get(workflow_id)
        external_ref = (
            lead["data"].get(reference_field, "")
            if reference_field
            else ""
        )

        rows.append(
            {
                "workflow_id": workflow_id,
                "external_ref": external_ref or "",
                "batch_id": lead["batch_id"],
                "phone_masked": lead["phone_masked"],
                "bucket": lead["bucket"],
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "last_call_at": last_call_at,
                "current_disposition": current_disposition,
                "ever_success": str(bool(ever_success)).lower(),
            }
        )

    rows.sort(
        key=lambda row: (
            row["workflow_id"],
            str(row["external_ref"]),
        )
    )
    return rows


def build_rejections(conn):
    rows = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id, batch_id, row_number
        """
    ).fetchall()

    return [
        {
            "workflow_id": row["workflow_id"],
            "batch_id": row["batch_id"],
            "row_number": row["row_number"],
            "reason": row["reason"],
        }
        for row in rows
    ]


def read_ingestion_stats(conn, table_name):
    allowed_tables = {
        "lead_file_stats",
        "processed_event_files",
    }
    if table_name not in allowed_tables:
        raise ValueError("Unsupported statistics table")

    rows = conn.execute(
        f"SELECT stats FROM {table_name}"
    ).fetchall()

    stats_list = []
    for row in rows:
        try:
            stats = json.loads(row["stats"] or "{}")
            if isinstance(stats, dict):
                stats_list.append(stats)
        except (json.JSONDecodeError, TypeError):
            continue

    return stats_list


def collect_quality_stats(conn, linked_events, catalogue):
    result = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "rejections_by_reason": {},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }

    lead_stats = read_ingestion_stats(conn, "lead_file_stats")
    lead_stat_aliases = {
        "duplicate_lead_rows_merged": (
            "duplicate_lead_rows_merged",
            "merged_duplicates",
            "duplicate_rows_merged",
        ),
        "field_warnings": ("field_warnings", "warnings"),
    }

    for stats in lead_stats:
        for target, aliases in lead_stat_aliases.items():
            for alias in aliases:
                if alias in stats:
                    result[target] += int(stats[alias] or 0)
                    break

    event_stats = read_ingestion_stats(
        conn, "processed_event_files"
    )
    event_stat_aliases = {
        "events_total": ("events_total", "total_events"),
        "malformed_events": ("malformed_events",),
        "duplicate_events_dropped": (
            "duplicate_events_dropped",
            "duplicate_events",
        ),
    }

    for stats in event_stats:
        for target, aliases in event_stat_aliases.items():
            for alias in aliases:
                if alias in stats:
                    result[target] += int(stats[alias] or 0)
                    break

    rejection_rows = conn.execute(
        """
        SELECT reason, COUNT(*) AS count
        FROM lead_rejections
        GROUP BY reason
        ORDER BY reason
        """
    ).fetchall()

    rejection_counts = {
        row["reason"]: row["count"] for row in rejection_rows
    }
    result["lead_rows_rejected"] = sum(rejection_counts.values())
    result["rejections_by_reason"] = rejection_counts

    unique_leads = conn.execute(
        "SELECT COUNT(*) FROM leads"
    ).fetchone()[0]

    result["lead_rows_total"] = (
        unique_leads
        + result["duplicate_lead_rows_merged"]
        + result["lead_rows_rejected"]
    )

    result["orphan_events"] = conn.execute(
        "SELECT COUNT(*) FROM event_quarantine"
    ).fetchone()[0]

    unknown_count = 0
    for event in linked_events:
        if event["event_type"] not in (
            "disposition.set",
            "disposition.override",
        ):
            continue

        code = event["payload"].get("disposition_code")
        if not code or (
            event["workflow_id"], str(code).strip()
        ) not in catalogue:
            unknown_count += 1

    result["unknown_disposition_events"] = unknown_count
    return result


def write_csv(output_path, headers, rows):
    with output_path.open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=headers,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def generate_reports(output_directory="reports"):
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    init_db()
    schemas = load_workflow_schemas()
    references = get_reference_fields(schemas)
    catalogue = load_dispositions()

    with get_connection() as conn:
        leads = load_leads(conn)
        events = load_linked_events(conn)

        build_call_data(leads, events, catalogue)

        batch_rows = build_batch_summary(leads)
        daily_rows = build_daily_rows(leads, catalogue)
        lead_rows = build_lead_report(
            leads, catalogue, references
        )
        rejection_rows = build_rejections(conn)
        quality = collect_quality_stats(
            conn, events, catalogue
        )

    write_csv(
        output_directory / "batch_summary.csv",
        REPORT_HEADERS["batch_summary.csv"],
        batch_rows,
    )
    write_csv(
        output_directory / "daily_overview.csv",
        REPORT_HEADERS["daily_overview.csv"],
        daily_rows,
    )
    write_csv(
        output_directory / "lead_report.csv",
        REPORT_HEADERS["lead_report.csv"],
        lead_rows,
    )
    write_csv(
        output_directory / "rejections.csv",
        REPORT_HEADERS["rejections.csv"],
        rejection_rows,
    )

    quality_path = output_directory / "data_quality.json"
    with quality_path.open("w", encoding="utf-8") as file:
        json.dump(quality, file, indent=2)
        file.write("\n")

    print("REPORT GENERATION COMPLETE")
    for filename in (
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    ):
        print(f"Created: {output_directory / filename}")

    return quality


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate interaction ledger reports"
    )
    parser.add_argument(
        "--out",
        default="reports",
        help="Output directory",
    )
    args = parser.parse_args()
    generate_reports(args.out)
