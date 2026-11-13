"""Focused tests for the rules most likely to regress.

Run from the repository root with: pytest -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from solution.normalizers import normalize_date, normalize_phone
from solution.reporting import (
    classify_bucket,
    get_effective_disposition,
)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("9876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+91 98765-43210", "+919876543210"),
    ],
)
def test_normalize_phone_valid_indian_numbers(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["1234567890", "987654321", "abcdefghij", ""])
def test_normalize_phone_rejects_invalid_numbers(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-10-14", "2026-10-14"),
        ("14/10/2026", "2026-10-14"),
        ("14-10-2026", "2026-10-14"),
        ("14-Oct-2026", "2026-10-14"),
        ("14-oCT-2026", "2026-10-14"),
    ],
)
def test_normalize_date_supported_formats(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "not-a-date"])
def test_normalize_date_rejects_impossible_or_invalid_dates(raw):
    with pytest.raises(ValueError):
        normalize_date(raw)


def _event(event_id, event_type, code, occurred_at):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": "motor_renewal",
        "timestamp": datetime.fromisoformat(occurred_at).replace(tzinfo=IST),
        "payload": {"disposition_code": code},
    }


def test_latest_override_takes_precedence_over_disposition_set():
    events = [
        _event("e1", "disposition.set", "callback_requested", "2026-10-14T10:00:00"),
        _event("e2", "disposition.override", "renewed", "2026-10-14T10:05:00"),
        _event("e3", "disposition.override", "not_interested", "2026-10-14T10:06:00"),
    ]
    catalogue = {
        ("motor_renewal", "callback_requested"): {
            "is_terminal": False, "is_success": False
        },
        ("motor_renewal", "renewed"): {
            "is_terminal": True, "is_success": True
        },
        ("motor_renewal", "not_interested"): {
            "is_terminal": True, "is_success": False
        },
    }
    assert get_effective_disposition(events, catalogue) == "not_interested"


def test_latest_disposition_set_used_when_no_override_exists():
    events = [
        _event("e1", "disposition.set", "callback_requested", "2026-10-14T10:00:00"),
        _event("e2", "disposition.set", "renewed", "2026-10-14T10:05:00"),
    ]
    catalogue = {
        ("motor_renewal", "callback_requested"): {
            "is_terminal": False, "is_success": False
        },
        ("motor_renewal", "renewed"): {
            "is_terminal": True, "is_success": True
        },
    }
    assert get_effective_disposition(events, catalogue) == "renewed"


def test_bucket_yet_to_reach_out_when_no_calls():
    lead = {"workflow_id": "motor_renewal", "calls": {}}
    assert classify_bucket(lead, {}) == "yet_to_reach_out"


def test_bucket_completed_when_any_call_has_terminal_disposition():
    lead = {
        "workflow_id": "motor_renewal",
        "calls": {
            "c1": {"effective_disposition": "renewed", "events": []},
            "c2": {"effective_disposition": None, "events": []},
        },
    }
    catalogue = {
        ("motor_renewal", "renewed"): {
            "is_terminal": True, "is_success": True
        }
    }
    assert classify_bucket(lead, catalogue) == "completed"


def test_bucket_telephony_failed_only_when_every_call_failed():
    failed = {
        "effective_disposition": None,
        "events": [{"event_type": "call.telephony_failed"}],
    }
    lead = {"workflow_id": "motor_renewal", "calls": {"c1": failed, "c2": failed}}
    assert classify_bucket(lead, {}) == "in_progress_telephony_failed"


def test_bucket_dialed_when_at_least_one_call_did_not_fail():
    failed = {
        "effective_disposition": None,
        "events": [{"event_type": "call.telephony_failed"}],
    }
    connected = {
        "effective_disposition": None,
        "events": [{"event_type": "call.connected"}],
    }
    lead = {"workflow_id": "motor_renewal", "calls": {"c1": failed, "c2": connected}}
    assert classify_bucket(lead, {}) == "in_progress_dialed"
