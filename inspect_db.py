from solution.database import get_connection, init_db

init_db()

with get_connection() as conn:
    print("\nLEADS BY WORKFLOW")
    rows = conn.execute("""
        SELECT workflow_id, COUNT(*) AS total
        FROM leads
        GROUP BY workflow_id
    """).fetchall()

    for row in rows:
        print(dict(row))

    print("\nEVENTS BY WORKFLOW AND TYPE")
    rows = conn.execute("""
        SELECT workflow_id, event_type, COUNT(*) AS total
        FROM events
        GROUP BY workflow_id, event_type
        ORDER BY workflow_id, event_type
    """).fetchall()

    for row in rows:
        print(dict(row))

    print("\nSAMPLE EVENTS")
    rows = conn.execute("""
        SELECT event_id, event_type, workflow_id,
               call_id, occurred_at, phone_hash, payload
        FROM events
        LIMIT 5
    """).fetchall()

    for row in rows:
        print(dict(row))

    print("\nDATA FILES")
    from pathlib import Path

    for path in sorted(Path("data").rglob("*")):
        if path.is_file():
            print(path)