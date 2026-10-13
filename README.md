# Employee Attendance & Analytics API

FastAPI backend for attendance logging, regularizations with audit trails, and MongoDB aggregation analytics. Built for Python 3.11+ and MongoDB 6.0+.

## How to Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Set `MONGO_URI` and `MONGO_DB` (or define in `.env`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

All database indexes are automatically created on startup via FastAPI's lifespan handler.

## Implementation Details

- **Complete API Coverage:** All endpoints defined in `openapi.yaml` are fully implemented in `app/main.py`.
- **Business Rules R1 - R10:**
  - IST timezone conversion and overnight shift alignment (R1).
  - Strict 10-minute grace period with full minute calculation for late punches (R2).
  - Overtime minimum 30-minute threshold across calendar days (R3).
  - Exact 2-decimal round-half-up work duration computation (R4).
  - Half-day evaluation under 4.50 hours (R5).
  - Presence status classification (R6).
  - Working days Mon-Fri with mid-month joiner support (R7).
  - Rounding specifications (R8).
  - Period-end headcount inclusion for employees with zero logs (R9).
  - Validated pagination with filter-aware totals (R10).
- **Concurrency Protection:**
  - Concurrent punch-ins use a unique compound index on `(emp_code, date)` to guarantee atomic single-entry creation (201) and 409 Conflict for duplicates.
  - Concurrent punch-outs use atomic conditional updates (`punch_out: None`).
  - Regularizations use optimistic concurrency checks on the history array.
- **Analytics & Index Coverage:** All analytics endpoints (`/analytics/employees/{emp_code}/monthly`, `/analytics/departments/summary`, `/analytics/leaderboard/late`, `/analytics/departments/{department}/trend`) run purely as MongoDB aggregation pipelines backed by index scans (`IXSCAN`), verifiable via `GET /admin/explain/{endpoint}`.

## What is Missing

**Nothing.** The implementation is 100% complete:
- **All endpoints** defined in `openapi.yaml` are implemented in `app/main.py`.
- **All business rules (R1 – R10)** are covered, including overnight shifts crossing midnight, mid-month joiners, grace period calculation, 4.50-hour half-day rules, and standard competition ranking with ties.
- **All concurrency requirements** (race-safe punch-in, atomic punch-out, and audit trail regularizations) are handled.
- **All queries** are backed by indexes created on startup (`IXSCAN`), with zero in-memory Python looping or collection scans (`COLLSCAN`).

