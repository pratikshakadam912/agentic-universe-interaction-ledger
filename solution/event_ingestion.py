
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from solution.database import get_connection, hash_pii, init_db
from solution.normalizers import normalize_phone


IST = ZoneInfo("Asia/Kolkata")


def normalize_timestamp(value):
    """Convert UTC, offset-aware and naive timestamps into IST ISO format."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_timestamp")

    value = value.strip()

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        # Naive timestamps are interpreted as IST.
        parsed = parsed.replace(tzinfo=IST)

    return parsed.astimezone(IST).isoformat()


def process_event_file(file_path):
    """Ingest one JSONL event file into the append-only ledger."""

    init_db()

    path = Path(file_path)
    source_file = str(path.resolve())

    with get_connection() as conn:

        already_processed = conn.execute(
            """
            SELECT 1
            FROM processed_event_files
            WHERE source_file = ?
            """,
            (source_file,)
        ).fetchone()

        if already_processed:
            return {
                "already_processed": True,
                "events_total": 0,
                "malformed_events": 0,
                "duplicate_events_dropped": 0,
            }

        events_total = 0
        malformed_events = 0
        duplicate_events = 0

        with path.open("r", encoding="utf-8-sig") as file:

            for line in file:

                # Empty lines do not count.
                if not line.strip():
                    continue

                events_total += 1

                #  Parse JSON and validate event_id.
                try:
                    event = json.loads(line)

                    if not isinstance(event, dict):
                        raise ValueError("Event must be an object")

                    event_id = event.get("event_id")

                    if event_id is None or not str(event_id).strip():
                        raise ValueError("Missing event_id")

                    event_id = str(event_id).strip()

                except (
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                ):
                    malformed_events += 1
                    continue

                #  Check duplicate event ID immediately.
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM events
                    WHERE event_id = ?
                    """,
                    (event_id,)
                ).fetchone()

                if existing:
                    duplicate_events += 1
                    continue

                #  Validate remaining event fields.
                try:
                    required_fields = (
                        "event_type",
                        "workflow_id",
                        "call_id",
                        "occurred_at",
                    )

                    for field in required_fields:
                        value = event.get(field)

                        if (
                            not isinstance(value, str)
                            or not value.strip()
                        ):
                            raise ValueError(
                                f"Missing or invalid {field}"
                            )

                    if not isinstance(event.get("payload"), dict):
                        raise ValueError("Payload must be an object")

                    event_type = event["event_type"].strip()
                    workflow_id = event["workflow_id"].strip()
                    call_id = event["call_id"].strip()

                    occurred_at = normalize_timestamp(
                        event["occurred_at"]
                    )

                    # Normalize and hash phone.

                    phone_hash = None

                    if event.get("phone") not in (None, ""):
                        try:
                            canonical_phone = normalize_phone(
                                str(event["phone"])
                            )

                            phone_hash = hash_pii(canonical_phone)

                        except ValueError:
                            phone_hash = None

                    # Keep payload data.

                    payload = event["payload"]

                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    OverflowError,
                ):
                    malformed_events += 1
                    continue

               # Append event to the ledger.
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at,
                        phone_hash,
                        payload,
                        source_file
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at,
                        phone_hash,
                        json.dumps(payload),
                        source_file,
                    )
                )

        stats = {
            "events_total": events_total,
            "malformed_events": malformed_events,
            "duplicate_events_dropped": duplicate_events,
        }

        # Record completed file for idempotent ingestion
        conn.execute(
            """
            INSERT INTO processed_event_files (
                source_file,
                stats
            )
            VALUES (?, ?)
            """,
            (source_file, json.dumps(stats))
        )

    #  Resolve links and quarantine orphan events
   
    resolve_event_links()

    return stats


def process_event_directory(directory):
    """Process all JSONL files in deterministic filename order."""

    directory = Path(directory)
    results = []

    for path in sorted(directory.glob("*.jsonl")):
        results.append(process_event_file(path))

    resolve_event_links()

    return results


def get_linked_phone_hash(conn, event):
    """
    Resolve the phone hash for an event.

    Regular events use their own phone hash.
    Overrides inherit the phone hash from another event
    belonging to the same workflow and call.
    """

    if event["event_type"] != "disposition.override":
        return event["phone_hash"]

    linked_event = conn.execute(
        """
        SELECT phone_hash
        FROM events
        WHERE workflow_id = ?
          AND call_id = ?
          AND event_id != ?
          AND phone_hash IS NOT NULL
        ORDER BY occurred_at, event_id
        LIMIT 1
        """,
        (
            event["workflow_id"],
            event["call_id"],
            event["event_id"],
        )
    ).fetchone()

    return linked_event["phone_hash"] if linked_event else None


def resolve_event_links():
    """
    Resolve ledger events against currently available leads.

    Previously orphaned events are linked when their leads
    arrive in a later drop.

    Events remain append-only. Only relationship and
    quarantine tables are updated.
    """

    init_db()

    linked = 0
    orphaned = 0

    with get_connection() as conn:

        events = conn.execute(
            """
            SELECT *
            FROM events
            ORDER BY occurred_at, event_id
            """
        ).fetchall()

        for event in events:

            phone_hash = get_linked_phone_hash(conn, event)

            lead = None

            if phone_hash:

                lead = conn.execute(
                    """
                    SELECT 1
                    FROM leads
                    WHERE workflow_id = ?
                      AND phone_hash = ?
                    """,
                    (
                        event["workflow_id"],
                        phone_hash,
                    )
                ).fetchone()

            if lead:

                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_lead_links (
                        event_id,
                        workflow_id,
                        phone_hash
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["workflow_id"],
                        phone_hash,
                    )
                )

                conn.execute(
                    """
                    DELETE FROM event_quarantine
                    WHERE event_id = ?
                    """,
                    (event["event_id"],)
                )

                linked += 1

            else:

                orphaned += 1

                reason = (
                    "missing_phone"
                    if not phone_hash
                    else "lead_not_found"
                )

                conn.execute(
                    """
                    INSERT INTO event_quarantine (
                        event_id,
                        reason
                    )
                    VALUES (?, ?)
                    ON CONFLICT(event_id)
                    DO UPDATE SET reason = excluded.reason
                    """,
                    (
                        event["event_id"],
                        reason,
                    )
                )

    return {
        "linked_events": linked,
        "orphan_events": orphaned,
    }
