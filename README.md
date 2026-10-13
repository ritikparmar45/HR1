# Employee Attendance & Analytics API

Official take-home assignment implementation for the **HROne Software Engineer Trainee** position.

This repository provides a high-performance, contract-compliant backend service built using **Python 3.11+**, **FastAPI**, and **MongoDB 6.0+**. It implements employee attendance management, shift regularizations with complete audit trails, and reporting analytics powered entirely by MongoDB aggregation pipelines.

---

## 1. Technology Stack

- **Language:** Python 3.11+ (tested on Python 3.14)
- **Framework:** FastAPI 0.110+
- **ASGI Server:** Uvicorn 0.29+
- **Database:** MongoDB 6.0+ (compatible with MongoDB 7.0 & 8.0)
- **Driver:** PyMongo 4.7+
- **Data Validation:** Pydantic 2.6+
- **Testing:** Pytest 8.0+ & HTTPX 0.27+

---

## 2. Prerequisites

- Python 3.11 or newer
- MongoDB instance (version 6.0 or higher) running locally or remotely (e.g., MongoDB Atlas)

---

## 3. Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd <repository_directory>
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## 4. Environment Variables

The application reads configuration from the environment (or a local `.env` file if present):

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Target database name | `attendance_db` |

Real environment variables take precedence over `.env` files.

---

## 5. Seed Sample Data

To load the 9 reference documents into MongoDB:

```bash
python sample_seed.py
```

*Note: The test suite and grader evaluate against expanded datasets containing edge cases (ties, mid-month joiners, overnight shifts, 0-log headcounts).*

---

## 6. Running the Application

Start the development server with Uvicorn:

```bash
uvicorn app.main:app --port 8000 --reload
```

The application creates all required database indexes idempotently during startup lifespan.

Once started, open your browser to access the interactive Swagger documentation:
- **Interactive OpenAPI UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 7. Running the Tests

Execute the comprehensive automated test suite:

```bash
python -m pytest -v
```

All tests run against isolated test collections and verify:
- Employee creation, validation, unique constraints, and pagination.
- Punch-in and punch-out transitions, race conditions, and overnight shifts.
- Manual regularizations (PATCH) and audit trail preservation.
- Analytics aggregations (monthly employee stats, department summary, late leaderboard with ties, and daily trends with gap-filling).
- Admin explain query plans.

---

## 8. API Endpoint Summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe; checks MongoDB connectivity (200 or 503). |
| `POST` | `/employees` | Registers new employee; enforces unique `emp_code`. |
| `GET` | `/employees` | Paginated listing sorted by `emp_code` ascending. |
| `POST` | `/attendance/punch-in` | Records punch-in for shift (race-safe). |
| `POST` | `/attendance/punch-out` | Closes punch-in; computes work hours, overtime, and half day. |
| `GET` | `/attendance` | Paginated attendance records sorted by `date` desc, `emp_code` asc. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularizes attendance day and appends to `history` audit trail. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary via aggregation pipeline. |
| `GET` | `/analytics/departments/summary` | Department summary including 0-log employees via cross-collection pipeline. |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard using standard competition ranking (`$rank`). |
| `GET` | `/analytics/departments/{department}/trend` | Daily trend series with gap-filling and 7-day moving average. |
| `GET` | `/admin/explain/{endpoint}` | Raw MongoDB execution plan verifying index usage (`IXSCAN`). |

---

## 9. Implemented Business Rules (R1 – R10)

- **R1:** IST (UTC+05:30) date handling; overnight shifts (`shift_end <= shift_start`) assign punch-ins prior to `shift_end` to previous day's shift. Instants truncated to whole seconds.
- **R2:** 10-minute grace period. Late only if strictly > 10:00; `late_minutes = floor((punch_in - shift_start) / 60)`.
- **R3:** Overtime counted only when >= 30 minutes past shift end; otherwise 0.
- **R4:** Work hours rounded to 2 decimals using decimal half-up (`ROUND_HALF_UP`).
- **R5:** Half day set to true if rounded work hours < 4.50 (counts as 0.5 present day).
- **R6:** `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **R7:** Monday–Friday working days; mid-month joiners count from `joined_on`. Weekend records do not increment `present_days` but contribute to late and overtime totals.
- **R8:** Rounding of reported values to 2 decimals half-up; rates to 4 decimals half-up.
- **R9:** Department headcount includes all employees with `joined_on` <= period end, even with zero logs.
- **R10:** 1-based pagination with max `page_size = 100`.

---

## 10. Submission Files

This repository contains all five required assignment artifacts:
1. `app/main.py` — Complete application and aggregation pipelines in the required single file.
2. `requirements.txt` — Project dependencies.
3. `REVIEW.md` — Code review of all 16 defects in the starter code with reproduction examples and fixes.
4. `DECISIONS.md` — Explanations for indexes, concurrency safety, ranking ties, headcount, and 100x scalability.
5. `README.md` — This file.
