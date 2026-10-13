"""Tests for employee endpoints and validations."""
from datetime import datetime, timezone
import pytest
from app.main import db


def test_health_check(client_app):
    """GET /health returns 200 and status ok when DB is responsive."""
    res = client_app.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_and_list_employees(client_app):
    """POST /employees creates employee and GET /employees lists paginated items."""
    db.employees.delete_many({})

    payload = {
        "emp_code": "EMP1001",
        "name": "Arjun Sharma",
        "email": "arjun@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    res = client_app.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP1001"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate creation returns 409
    res_dup = client_app.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Validation: shift_start == shift_end yields 422
    payload_bad_shift = dict(payload, emp_code="EMP1002", shift_end="09:30")
    res_bad = client_app.post("/employees", json=payload_bad_shift)
    assert res_bad.status_code == 422

    # List employees with department filter
    list_res = client_app.get("/employees?department=Engineering&page=1&page_size=10")
    assert list_res.status_code == 200
    body = list_res.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["emp_code"] == "EMP1001"
