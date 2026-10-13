"""Tests for analytics aggregation endpoints and admin explain functionality."""
import json
import pathlib
from bson import json_util
from app.main import db


def seed_sample_data():
    """Load the official candidate kit sample dataset."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    sample_dir = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)

    emp_docs = json_util.loads((sample_dir / "employees.json").read_text(), json_options=opts)
    db.employees.insert_many(emp_docs)

    att_docs = json_util.loads((sample_dir / "attendance_logs.json").read_text(), json_options=opts)
    db.attendance_logs.insert_many(att_docs)


def test_employee_monthly_analytics(client_app):
    """Test GET /analytics/employees/{emp_code}/monthly against seeded dataset."""
    seed_sample_data()

    # EMP0001 in month 2026-07
    res = client_app.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0


def test_department_summary_analytics(client_app):
    """Test GET /analytics/departments/summary counts 0-log employees in headcount."""
    seed_sample_data()

    res = client_app.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = {item["department"]: item for item in data["items"]}

    # Support department has EMP0005 and EMP0006 (headcount = 2)
    assert "Support" in items
    assert items["Support"]["headcount"] == 2
    assert "Engineering" in items
    assert items["Engineering"]["headcount"] == 2


def test_late_leaderboard_with_ties(client_app):
    """Test GET /analytics/leaderboard/late uses competition ranking."""
    seed_sample_data()

    res = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    if data["items"]:
        # First entry must have rank 1
        assert data["items"][0]["rank"] == 1


def test_department_trend(client_app):
    """Test GET /analytics/departments/{department}/trend gap-filling and moving average."""
    seed_sample_data()

    res = client_app.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    # Exact calendar span of 7 days
    assert len(data["items"]) == 7
    # Includes weekend
    sunday = next(i for i in data["items"] if i["date"] == "2026-07-12")
    assert sunday["is_working_day"] is False


def test_admin_explain_endpoints(client_app):
    """Test GET /admin/explain/{endpoint} returns raw explain plan without collscan."""
    seed_sample_data()

    endpoints = [
        "/admin/explain/attendance_list?emp_code=EMP0001",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-12",
    ]

    for ep in endpoints:
        res = client_app.get(ep)
        assert res.status_code == 200
        data = res.json()
        assert "explain" in data
        assert "collection" in data
