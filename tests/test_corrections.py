"""Tests for attendance regularization (PATCH) and audit trail preservation."""
from datetime import datetime, timezone
from app.main import db


def test_regularize_attendance_and_audit_trail(client_app):
    """Test attendance regularization modifies record, recomputes derived fields, and appends history."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP3001",
            "name": "Priya Sen",
            "email": "priya@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Initial punch in at 10:05 IST (04:35 UTC) -> 35 min late
    dt_in = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    in_ms = int(dt_in.timestamp() * 1000)
    client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP3001", "punched_at": in_ms, "status": "PRESENT"},
    )

    # Punch out at 18:30 IST (13:00 UTC) -> 8.42 work hours
    dt_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    out_ms = int(dt_out.timestamp() * 1000)
    client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP3001", "punched_at": out_ms},
    )

    # Correct punch-in to 09:28 IST (03:58 UTC) -> 0 late minutes, work hours = 9.03
    dt_corr_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    corr_in_ms = int(dt_corr_in.timestamp() * 1000)

    patch_payload = {
        "punch_in": corr_in_ms,
        "reason": "Biometric terminal malfunctioned",
        "regularized_by": "hr.lead",
    }
    res_patch = client_app.patch("/attendance/EMP3001/2026-07-07", json=patch_payload)
    assert res_patch.status_code == 200
    data = res_patch.json()

    assert data["punch_in"] == corr_in_ms
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1

    entry = data["history"][0]
    assert entry["by"] == "hr.lead"
    assert entry["reason"] == "Biometric terminal malfunctioned"
    assert "punch_in" in entry["changes"]
    assert entry["changes"]["punch_in"]["from"] == in_ms
    assert entry["changes"]["punch_in"]["to"] == corr_in_ms
    assert entry["changes"]["late_minutes"]["from"] == 35
    assert entry["changes"]["late_minutes"]["to"] == 0

    # Sending a patch that produces zero changes returns 422
    res_no_change = client_app.patch("/attendance/EMP3001/2026-07-07", json=patch_payload)
    assert res_no_change.status_code == 422
