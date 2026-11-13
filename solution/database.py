
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "interaction_ledger.db"
)


@contextmanager
def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_pii(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def get_masked_phone(phone):
    return "+91******" + phone[-4:]


def init_db():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                lead_data TEXT NOT NULL,
                PRIMARY KEY (workflow_id, phone_hash)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                phone_hash TEXT,
                payload TEXT NOT NULL,
                source_file TEXT NOT NULL
            );

            -- Keep the event ledger append-only.
            CREATE TRIGGER IF NOT EXISTS prevent_event_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS prevent_event_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;

            CREATE INDEX IF NOT EXISTS idx_events_call
            ON events(workflow_id, call_id);

            CREATE INDEX IF NOT EXISTS idx_events_phone
            ON events(workflow_id, phone_hash);

            CREATE TABLE IF NOT EXISTS event_lead_links (
                event_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                FOREIGN KEY(event_id)
                    REFERENCES events(event_id),
                FOREIGN KEY(workflow_id, phone_hash)
                    REFERENCES leads(workflow_id, phone_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_links_lead
            ON event_lead_links(workflow_id, phone_hash);

            CREATE TABLE IF NOT EXISTS event_quarantine (
                event_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                FOREIGN KEY(event_id)
                    REFERENCES events(event_id)
            );

            CREATE TABLE IF NOT EXISTS processed_lead_files (
                source_file TEXT PRIMARY KEY,
                stats TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_event_files (
                source_file TEXT PRIMARY KEY,
                stats TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_file_stats (
                source_file TEXT PRIMARY KEY,
                stats TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_rejections (
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                source_file TEXT NOT NULL
            );
        """)


def store_leads(leads):
    from solution.lead_ingestion import load_schema

    init_db()
    duplicates = 0

    with get_connection() as conn:
        schema_cache = {}

        for lead in leads:
            workflow_id = lead["workflow_id"]
            phone = lead["phone"]
            phone_hash = hash_pii(phone)
            masked_phone = get_masked_phone(phone)

            if workflow_id not in schema_cache:
                schema_cache[workflow_id] = load_schema(workflow_id)

            schema = schema_cache[workflow_id]
            safe_lead = {}

            for field in schema["fields"]:
                name = field["name"]
                value = lead.get(name)

                if value is None:
                    safe_lead[name] = None
                elif field.get("pii"):
                    if field["type"] == "phone":
                        safe_lead[name] = masked_phone
                    else:
                        safe_lead[name] = hash_pii(value)
                else:
                    safe_lead[name] = value

            existing = conn.execute(
                """
                SELECT lead_data
                FROM leads
                WHERE workflow_id = ? AND phone_hash = ?
                """,
                (workflow_id, phone_hash)
            ).fetchone()

            if existing:
                duplicates += 1
                old_data = json.loads(existing["lead_data"])

                old_data.update({
                    key: value
                    for key, value in safe_lead.items()
                    if value is not None
                })

                for field in schema["fields"]:
                    name = field["name"]
                    if old_data.get(name) is None and "default" in field:
                        old_data[name] = field["default"]

                conn.execute(
                    """
                    UPDATE leads
                    SET lead_data = ?
                    WHERE workflow_id = ? AND phone_hash = ?
                    """,
                    (
                        json.dumps(old_data),
                        workflow_id,
                        phone_hash
                    )
                )

            else:
                for field in schema["fields"]:
                    name = field["name"]
                    if safe_lead.get(name) is None and "default" in field:
                        safe_lead[name] = field["default"]

                conn.execute(
                    """
                    INSERT INTO leads (
                        workflow_id,
                        phone_hash,
                        batch_id,
                        phone_masked,
                        lead_data
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        phone_hash,
                        lead["batch_id"],
                        masked_phone,
                        json.dumps(safe_lead)
                    )
                )

    return duplicates
