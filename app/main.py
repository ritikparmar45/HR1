"""Employee Attendance & Analytics API.

Official implementation for HROne Software Engineer Trainee assignment.
Contract: openapi.yaml (v2.0.0)
Data Model: DATA_MODEL.md
Rules: R1 - R10
"""
from __future__ import annotations

import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load environment variables (system env takes precedence over local .env)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required MongoDB indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Math & Conversion Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float | Decimal, decimals: int) -> float:
    """R8: Round numbers half-up to specified decimals."""
    d = Decimal(str(value))
    q = Decimal("10") ** (-decimals)
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def instant_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """Validate, truncate to whole seconds (R1), and return UTC datetime."""
    # Truncate instant to whole seconds per R1
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def parse_time_str(time_str: str) -> dt_time:
    """Parse HH:MM string into time object."""
    h, m = map(int, time_str.split(":"))
    return dt_time(hour=h, minute=m)


def determine_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """R1: Determine attendance date (YYYY-MM-DD IST) considering overnight shifts."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_time = parse_time_str(shift_start_str)
    end_time = parse_time_str(shift_end_str)

    # Overnight shift check: shift_end <= shift_start
    if end_time <= start_time:
        # A punch-in earlier than shift_end belongs to the shift that started the previous day
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, attendance_date_str: str) -> int:
    """R2: Grace period 10 min. Late if strictly > 10:00 after shift_start."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start_str.split(":"))
    att_date = dt_date.fromisoformat(attendance_date_str)
    shift_start_ist = datetime.combine(att_date, dt_time(hour=h, minute=m), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    # Grace period: strictly more than 10 minutes (600 seconds)
    if diff_sec > 600:
        return math.floor(diff_sec / 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start_str: str, shift_end_str: str, attendance_date_str: str) -> int:
    """R3: Whole minutes from shift_end to punch_out, only if >= 30, else 0."""
    punch_out_ist = punch_out_utc.astimezone(IST)
    start_time = parse_time_str(shift_start_str)
    end_time = parse_time_str(shift_end_str)
    att_date = dt_date.fromisoformat(attendance_date_str)

    # For overnight shift, shift_end is on the next calendar day
    if end_time <= start_time:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_ist = datetime.combine(shift_end_date, end_time, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    ot_minutes = math.floor(diff_sec / 60)
    return ot_minutes if ot_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours_val = Decimal(diff_sec) / Decimal(3600)
    return float(hours_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MongoDB document to API schema response, removing internal _id."""
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)
    if "created_at" in res and isinstance(res["created_at"], datetime):
        res["created_at"] = instant_to_epoch_ms(res["created_at"])
    if "punch_in" in res:
        res["punch_in"] = instant_to_epoch_ms(res["punch_in"])
    if "punch_out" in res:
        res["punch_out"] = instant_to_epoch_ms(res["punch_out"])
    if "history" in res and isinstance(res["history"], list):
        formatted_history = []
        for h in res["history"]:
            h_copy = dict(h)
            if "at" in h_copy and isinstance(h_copy["at"], datetime):
                h_copy["at"] = instant_to_epoch_ms(h_copy["at"])
            if "changes" in h_copy and isinstance(h_copy["changes"], dict):
                changes_copy = {}
                for k, v in h_copy["changes"].items():
                    change_item = dict(v)
                    if k in ("punch_in", "punch_out"):
                        if isinstance(change_item.get("from"), datetime):
                            change_item["from"] = instant_to_epoch_ms(change_item["from"])
                        if isinstance(change_item.get("to"), datetime):
                            change_item["to"] = instant_to_epoch_ms(change_item["to"])
                    changes_copy[k] = change_item
                h_copy["changes"] = changes_copy
            formatted_history.append(h_copy)
        res["history"] = formatted_history
    else:
        res["history"] = res.get("history") or []
    if "half_day" not in res or res["half_day"] is None:
        res["half_day"] = False
    return res


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid date format, expected YYYY-MM-DD")
        return v

    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["body", "shift_end"], "msg": "shift_start and shift_end must differ", "type": "value_error"}],
            )


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces unique emp_code via unique index."""
    body.validate_shifts()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in. Creates the day's record (R1, R2). Concurrency safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        in_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        # Current time truncated to whole seconds per R1
        now_ts = int(datetime.now(timezone.utc).timestamp())
        in_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    att_date = determine_attendance_date(in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return serialize_doc(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes most recent punch-in at/before punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        out_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        out_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_utc}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    # If already punched out, return 409
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    in_utc = record["punch_in"]
    diff_sec = (out_utc - in_utc).total_seconds()

    # Validation: punched_at <= punch_in or > 24 hours
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    work_hrs = compute_work_hours(in_utc, out_utc)
    ot_min = compute_overtime(out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    half_d = is_half_day(work_hrs)

    # Atomic update: only update if punch_out is still None (prevents race condition)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be <= date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record and append audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    new_in_utc = epoch_ms_to_utc_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_out_utc = epoch_ms_to_utc_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Contract Rule: setting status ABSENT or LEAVE clears both punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied for ABSENT or LEAVE",
            )
        new_in_utc = None
        new_out_utc = None
    else:
        # Presence status requires punch_in
        if new_in_utc is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )
        # punch_in must stay on record's attendance date per R1
        derived_date = determine_attendance_date(new_in_utc, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not match attendance date",
            )
        # punch_out check
        if new_out_utc is not None:
            diff_sec = (new_out_utc - new_in_utc).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_in_utc, emp["shift_start"], date)
        if new_out_utc is not None:
            new_work_hours = compute_work_hours(new_in_utc, new_out_utc)
            new_overtime_minutes = compute_overtime(new_out_utc, emp["shift_start"], emp["shift_end"], date)
            new_half_day = is_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine changes
    changes: Dict[str, Dict[str, Any]] = {}
    if new_status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": new_status}
    if new_in_utc != record.get("punch_in"):
        changes["punch_in"] = {"from": record.get("punch_in"), "to": new_in_utc}
    if new_out_utc != record.get("punch_out"):
        changes["punch_out"] = {"from": record.get("punch_out"), "to": new_out_utc}
    if new_late_minutes != (record.get("late_minutes") or 0):
        changes["late_minutes"] = {"from": record.get("late_minutes") or 0, "to": new_late_minutes}
    if new_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_overtime_minutes != (record.get("overtime_minutes") or 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes") or 0, "to": new_overtime_minutes}
    if new_half_day != (record.get("half_day") or False):
        changes["half_day"] = {"from": record.get("half_day") or False, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request produces no changes to the attendance record",
        )

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update ensuring existing history is not silently overwritten
    current_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in_utc,
                "punch_out": new_out_utc,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected on attendance history",
        )

    return serialize_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_month_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Monday-Friday working days in the month on or after joined_on."""
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    month_start = dt_date(year, month, 1)
    month_end = dt_date(year, month, num_days)
    joined_date = dt_date.fromisoformat(joined_on_str)

    start_date = max(month_start, joined_date)
    if start_date > month_end:
        return 0

    count = 0
    cur = start_date
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee (MongoDB aggregation)."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = get_month_working_days(emp["joined_on"], month)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
        {
            "$addFields": {
                # Determine day of week from date: 1=Sun, 2=Mon, 6=Fri, 7=Sat
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "isWeekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "isPresence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$isWeekday", "$isPresence"]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month (cross-collection aggregation)."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "logDayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
                "logIsWeekday": {
                    "$in": [
                        {
                            "$cond": [
                                {"$ifNull": ["$logs.date", False]},
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                None,
                            ]
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "logIsPresence": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "logs_present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$logIsWeekday", "$logIsPresence"]},
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$logIsPresence",
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "logs_late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "logs_total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "logs_leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "logs_on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$logs_present_days"},
                "all_work_hours": {"$push": "$valid_work_hours"},
                "late_count": {"$sum": "$logs_late_count"},
                "total_late_minutes": {"$sum": "$logs_total_late_minutes"},
                "leave_count": {"$sum": "$logs_leave_count"},
                "on_duty_count": {"$sum": "$logs_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        # Flatten all_work_hours arrays to calculate true mean across records
        flat_hours = [h for sublist in doc["all_work_hours"] for h in sublist if h is not None]
        if flat_hours:
            avg_wh = round_half_up(sum(flat_hours) / len(flat_hours), 2)
        else:
            avg_wh = None

        items.append({
            "department": doc["_id"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month with standard competition ranking."""
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking: ties share rank, next rank skipped (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling and 7-day moving average."""
    # 404 if department has no employees
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_d = dt_date.fromisoformat(from_date)
    end_d = dt_date.fromisoformat(to_date)
    if end_d < start_d:
        raise HTTPException(status_code=422, detail="to must be >= from")

    num_days = (end_d - start_d).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Aggregation for daily logs matching department
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
    ]

    log_map = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(pipeline)}

    # Fetch all department employees' joined_on dates to determine headcount per day
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    # Generate daily series (including weekends and empty days)
    items = []
    rates_history: List[Optional[float]] = []

    cur = start_d
    while cur <= end_d:
        cur_str = cur.isoformat()
        is_working = cur.weekday() < 5
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= cur_str)

        log_data = log_map.get(cur_str, {})
        pres_count = float(log_data.get("present_count", 0.0))
        late_count = int(log_data.get("late_count", 0))

        if is_working and headcount > 0:
            rate = round_half_up(pres_count / headcount, 4)
        else:
            rate = None

        rates_history.append(rate)

        # 7-day moving average: window of current day and up to 6 preceding days inside range
        window_rates = [r for r in rates_history[-7:] if r is not None]
        if window_rates:
            m_avg = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            m_avg = None

        items.append({
            "date": cur_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": pres_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs MongoDB explain with executionStats verbosity for an endpoint's main query."""
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        plan = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": 1},
                    "leave_days": {"$sum": 1},
                    "late_count": {"$sum": 1},
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_end_date = f"{month}-{last_day:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_date}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    raise HTTPException(status_code=400, detail="Unknown endpoint")
