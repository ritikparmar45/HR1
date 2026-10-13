"""Tests for punch-in, punch-out, and attendance listing."""
from datetime import datetime, timezone, timedelta
from app.main import db, IST


def test_punch_in_and_punch_out(client_app):
    """Test normal punch-in and punch-out lifecycle (R1 - R5)."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    emp_payload = {
        "emp_code": "EMP2001",
        "name": "Kavita Sen",
        "email": "kavita@example.com",
        "department": "Design",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    client_app.post("/employees", json=emp_payload)

    # 1. Punch-in inside grace period (09:35 IST on 2026-07-06)
    # 09:35 IST = 04:05 UTC -> 2026-07-06 04:05:00 UTC
    dt_in = datetime(2026, 7, 6, 4, 5, 0, tzinfo=timezone.utc)
    in_ms = int(dt_in.timestamp() * 1000)

    res_in = client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP2001", "punched_at": in_ms, "status": "PRESENT"},
    )
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["status"] == "PRESENT"
    assert in_data["punch_in"] == in_ms
    assert in_data["punch_out"] is None
    assert in_data["late_minutes"] == 0
    assert in_data["half_day"] is False

    # 2. Duplicate punch-in on same day returns 409
    res_in_dup = client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP2001", "punched_at": in_ms, "status": "PRESENT"},
    )
    assert res_in_dup.status_code == 409

    # 3. Punch-out at 19:15 IST (13:45 UTC) -> 45 min overtime (>= 30 threshold)
    dt_out = datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc)
    out_ms = int(dt_out.timestamp() * 1000)

    res_out = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP2001", "punched_at": out_ms},
    )
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["punch_out"] == out_ms
    assert out_data["work_hours"] == 9.67
    assert out_data["overtime_minutes"] == 45
    assert out_data["half_day"] is False

    # 4. Repeated punch-out returns 409
    res_out_dup = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP2001", "punched_at": out_ms},
    )
    assert res_out_dup.status_code == 409


def test_overnight_shift_punch_in_and_out(client_app):
    """Test overnight shift (22:00 - 06:00) punch-in after midnight (R1)."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    emp_payload = {
        "emp_code": "EMP2002",
        "name": "Nitin Verma",
        "email": "nitin@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
    }
    client_app.post("/employees", json=emp_payload)

    # Punch-in at 01:00 IST on 2026-07-07 (belongs to shift started on 2026-07-06)
    # 01:00 IST on 2026-07-07 = 19:30 UTC on 2026-07-06
    dt_in = datetime(2026, 7, 6, 19, 30, 0, tzinfo=timezone.utc)
    in_ms = int(dt_in.timestamp() * 1000)

    res_in = client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP2002", "punched_at": in_ms, "status": "PRESENT"},
    )
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"  # Assigned to previous calendar date per R1

    # Punch-out at 06:40 IST on 2026-07-07 (01:10 UTC on 2026-07-07)
    # 40 min overtime past 06:00
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    out_ms = int(dt_out.timestamp() * 1000)

    res_out = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP2002", "punched_at": out_ms},
    )
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["overtime_minutes"] == 40
    assert out_data["work_hours"] == 5.67
