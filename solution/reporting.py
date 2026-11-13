
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
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
    """Parse UTC, offset-aware and naive IST timestamps."""
    if not value:
        return None

    value = str(value).strip()

    try:
        if value.endswith("Z"):
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00"
            )
        else:
            parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)

        return parsed.astimezone(IST)

    except (ValueError, TypeError):
        return None


def load_workflow_schemas():
    schemas = {}

    if not SCHEMA_DIR.exists():
        raise FileNotFoundError(
            f"Schema directory not found: {SCHEMA_DIR}"
        )

    for path in SCHEMA_DIR.glob("*.json"):
        with path.open("r", encoding="utf-8") as file:
            schemas[path.stem] = json.load(file)

    return schemas


def get_schema_fields(schema):
    """Support the workflow schema's fields list."""
    if isinstance(schema, list):
        return schema

    return schema.get("fields", [])


def get_reference_field(schemas):
    references = {}

    for workflow_id, schema in schemas.items():
        for field in get_schema_fields(schema):
            if field.get("reference") is True:
                references[workflow_id] = field["name"]
                break

    return references


def load_dispositions():
    catalogue = {}

    if not DISPOSITIONS_FILE.exists():
        raise FileNotFoundError(
            f"Dispositions file not found: {DISPOSITIONS_FILE}"
        )

    with DISPOSITIONS_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            workflow_id = row["workflow_id"]
            code = row["code"]

            catalogue[(workflow_id, code)] = {
                "label": row["label"],
                "is_terminal": str(
                    row["is_terminal"]
                ).strip().lower() == "true",
                "is_success": str(
                    row["is_success"]
                ).strip().lower() == "true",
            }

    return catalogue


def load_leads(conn):
    leads = {}

    rows = conn.execute(
        """
        SELECT workflow_id, phone_hash, batch_id,
               phone_masked, lead_data
        FROM leads
        ORDER BY workflow_id, batch_id, phone_hash
        """
    ).fetchall()

    for row in rows:
        workflow_id = row["workflow_id"]
        phone_hash = row["phone_hash"]

        lead_data = json.loads(row["lead_data"] or "{}")

        key = (workflow_id, phone_hash)

        leads[key] = {
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
    """
    Load only linked events.

    Quarantined and unresolved orphan events must not
    contribute to reports.
    """
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
        FROM events e
        INNER JOIN event_lead_links l
            ON e.event_id = l.event_id
        ORDER BY e.event_id
        """
    ).fetchall()

    events = []

    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}

        events.append({
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "workflow_id": row["workflow_id"],
            "call_id": row["call_id"],
            "occurred_at": row["occurred_at"],
            "timestamp": parse_timestamp(row["occurred_at"]),
            "payload": payload,
            "phone_hash": row["phone_hash"],
        })

    return events


def get_effective_disposition(call_events, catalogue):
    """
    Latest override takes priority over every disposition.set.
    Otherwise use the latest disposition.set.
    """
    overrides = [
        event for event in call_events
        if event["event_type"] == "disposition.override"
    ]

    sets = [
        event for event in call_events
        if event["event_type"] == "disposition.set"
    ]

    candidates = overrides if overrides else sets

    if not candidates:
        return None

    candidates.sort(
        key=lambda event: (
            event["timestamp"] is not None,
            event["timestamp"] or datetime.min.replace(
                tzinfo=IST
            ),
            event["event_id"],
        )
    )

    selected = candidates[-1]

    code = selected["payload"].get("disposition_code")

    if not code:
        return None

    return code if (
        selected["workflow_id"],
        code
    ) in catalogue else None


def classify_bucket(lead, catalogue):
    calls = lead["calls"]

    if not calls:
        return "yet_to_reach_out"

    for call in calls.values():
        code = call["effective_disposition"]

        if code:
            disposition = catalogue.get(
                (lead["workflow_id"], code)
            )

            if disposition and disposition["is_terminal"]:
                return "completed"

    if all(
        any(
            event["event_type"] == "call.telephony_failed"
            for event in call["events"]
        )
        for call in calls.values()
    ):
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


def build_call_data(leads, events, catalogue):
    """
    Group linked events by lead and call.

    A call exists only when call.initiated is present.
    """
    for event in events:
        lead_key = (
            event["workflow_id"],
            event["phone_hash"],
        )

        lead = leads.get(lead_key)

        if lead is None:
            continue

        lead["events"].append(event)

    for lead in leads.values():
        grouped = defaultdict(list)

        for event in lead["events"]:
            if event["call_id"]:
                grouped[event["call_id"]].append(event)

        for call_id, call_events in grouped.items():
            initiated = [
                event for event in call_events
                if event["event_type"] == "call.initiated"
            ]

            if not initiated:
                continue

            initiated.sort(
                key=lambda event: (
                    event["timestamp"] is not None,
                    event["timestamp"] or datetime.min.replace(
                        tzinfo=IST
                    ),
                    event["event_id"],
                )
            )

            initiated_event = initiated[-1]

            connected = any(
                event["event_type"] == "call.connected"
                for event in call_events
            )

            disposition = get_effective_disposition(
                call_events,
                catalogue
            )

            lead["calls"][call_id] = {
                "call_id": call_id,
                "initiated_at": initiated_event["timestamp"],
                "connected": connected,
                "events": call_events,
                "effective_disposition": disposition,
            }

        lead["bucket"] = classify_bucket(
            lead,
            catalogue
        )


def build_batch_summary(leads):
    buckets = [
        "yet_to_reach_out",
        "in_progress_telephony_failed",
        "in_progress_dialed",
        "completed",
    ]

    grouped = defaultdict(Counter)

    for lead in leads.values():
        key = (
            lead["workflow_id"],
            lead["batch_id"],
        )

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

        for bucket in buckets:
            row[bucket] = counts[bucket]

        rows.append(row)

    return rows


def build_daily_overview(leads):
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

            key = (workflow_id, date)
            stats = daily[key]

            stats["total_calls"] += 1

            if call["connected"]:
                stats["connected_calls"] += 1

            code = call["effective_disposition"]

            if code:
                
                call["is_success"] = False

    return daily


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
            key = (workflow_id, date)

            stats = daily[key]
            stats["total_calls"] += 1

            if call["connected"]:
                stats["connected_calls"] += 1

            code = call["effective_disposition"]

            if code:
                disposition = catalogue.get(
                    (workflow_id, code)
                )

                if disposition and disposition["is_success"]:
                    stats["success_leads"].add(
                        lead["phone_hash"]
                    )

    rows = []

    for workflow_id, date in sorted(daily):
        stats = daily[(workflow_id, date)]

        total = stats["total_calls"]
        connected = stats["connected_calls"]

        rows.append({
            "workflow_id": workflow_id,
            "date": date,
            "total_calls": total,
            "connected_calls": connected,
            "connectivity_rate": f"{connected / total:.4f}",
            "success_leads": len(
                stats["success_leads"]
            ),
        })

    return rows


def build_lead_report(leads, catalogue, references):
    rows = []

    for lead in leads.values():
        workflow_id = lead["workflow_id"]
        calls = list(lead["calls"].values())

        calls.sort(
            key=lambda call: (
                call["initiated_at"] is not None,
                call["initiated_at"] or datetime.min.replace(
                    tzinfo=IST
                ),
                call["call_id"],
            )
        )

        total_calls = len(calls)

        connected_calls = sum(
            1 for call in calls if call["connected"]
        )

        last_call_at = ""
        current_disposition = ""

        calls_with_disposition = [
            call for call in calls
            if call["effective_disposition"]
        ]

        if calls:
            latest_call = calls[-1]

            if latest_call["initiated_at"]:
                last_call_at = (
                    latest_call["initiated_at"].isoformat()
                )

        if calls_with_disposition:
            current_disposition = (
                calls_with_disposition[-1][
                    "effective_disposition"
                ]
            )

        ever_success = False

        for call in calls:
            code = call["effective_disposition"]

            if code:
                disposition = catalogue.get(
                    (workflow_id, code)
                )

                if disposition and disposition["is_success"]:
                    ever_success = True
                    break

        external_ref = lead["data"].get(
            references.get(workflow_id, ""),
            ""
        )

        rows.append({
            "workflow_id": workflow_id,
            "external_ref": external_ref or "",
            "batch_id": lead["batch_id"],
            "phone_masked": lead["phone_masked"],
            "bucket": lead["bucket"],
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": current_disposition,
            "ever_success": str(ever_success).lower(),
        })

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


def collect_quality_stats(conn):
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

    def merge_stats(stats):
        if not isinstance(stats, dict):
            return

        aliases = {
            "lead_rows_total": [
                "lead_rows_total",
                "total_rows",
            ],
            "lead_rows_rejected": [
                "lead_rows_rejected",
                "rejected_rows",
            ],
            "duplicate_lead_rows_merged": [
                "duplicate_lead_rows_merged",
                "merged_duplicates",
            ],
            "field_warnings": [
                "field_warnings",
            ],
            "events_total": [
                "events_total",
                "total_events",
            ],
            "malformed_events": [
                "malformed_events",
            ],
            "duplicate_events_dropped": [
                "duplicate_events_dropped",
                "duplicate_events",
            ],
            "orphan_events": [
                "orphan_events",
            ],
            "unknown_disposition_events": [
                "unknown_disposition_events",
                "unknown_dispositions",
            ],
        }

        for target, keys in aliases.items():
            for key in keys:
                if key in stats:
                    result[target] += int(stats[key] or 0)
                    break

        reasons = stats.get("rejections_by_reason", {})

        if isinstance(reasons, dict):
            reason_counts.update({
                key: int(value or 0)
                for key, value in reasons.items()
            })

    reason_counts = Counter()

    for table in (
        "lead_file_stats",
        "processed_event_files",
    ):
        try:
            rows = conn.execute(
                f"SELECT stats FROM {table}"
            ).fetchall()

            for row in rows:
                try:
                    merge_stats(json.loads(row["stats"] or "{}"))
                except (ValueError, TypeError):
                    continue

        except Exception:
            
            continue

    try:
        rejection_rows = conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM lead_rejections
            GROUP BY reason
            """
        ).fetchall()

        reason_counts = Counter({
            row["reason"]: row["count"]
            for row in rejection_rows
        })

        result["lead_rows_rejected"] = sum(
            reason_counts.values()
        )

    except Exception:
        pass

    result["rejections_by_reason"] = dict(
        sorted(reason_counts.items())
    )

    return result


def write_csv(output_path, headers, rows):
    with output_path.open(
        "w",
        encoding="utf-8",
        newline=""
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=headers,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def generate_reports(output_directory="reports"):
    """Generate all five assignment-required reports."""
    output_directory = Path(output_directory)
    output_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    init_db()

    schemas = load_workflow_schemas()
    references = get_reference_field(schemas)
    catalogue = load_dispositions()

    with get_connection() as conn:
        leads = load_leads(conn)
        events = load_linked_events(conn)

        build_call_data(
            leads,
            events,
            catalogue
        )

        batch_rows = build_batch_summary(leads)

        daily_rows = build_daily_rows(
            leads,
            catalogue
        )

        lead_rows = build_lead_report(
            leads,
            catalogue,
            references
        )

        rejection_rows = build_rejections(conn)

        quality = collect_quality_stats(conn)


        quality["orphan_events"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM event_quarantine
            WHERE reason = 'orphan'
            """
        ).fetchone()[0]

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

    with quality_path.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            quality,
            file,
            indent=2,
        )
        file.write("\n")

    print("\nREPORT GENERATION COMPLETE")

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
        help="Output directory"
    )

    args = parser.parse_args()

    generate_reports(args.out)
