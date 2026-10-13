"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required database indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])


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
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def to_epoch_millis(val: Any) -> Optional[int]:
    """Convert BSON datetime or int/float to epoch milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return int(val.timestamp() * 1000)
    return None


def from_epoch_millis(ms: int) -> datetime:
    """Convert epoch milliseconds (truncated to whole seconds) to UTC datetime."""
    sec = truncate_to_seconds(ms) // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_hh_mm(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    start_h, start_m = parse_hh_mm(shift_start)
    end_h, end_m = parse_hh_mm(shift_end)
    return (end_h, end_m) <= (start_h, start_m)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Round reported numbers half-up."""
    if val is None:
        return None
    d = Decimal(str(val)).quantize(Decimal("10") ** -decimals, rounding=ROUND_HALF_UP)
    return float(d)


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose
    IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    truncated_ms = truncate_to_seconds(punch_in_ms)
    dt_ist = datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).astimezone(IST)

    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hh_mm(shift_end)
        if (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
            prev_day = dt_ist.date() - timedelta(days=1)
            return prev_day.isoformat()

    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: Optional[int], shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    if punch_in_ms is None:
        return 0

    truncated_ms = truncate_to_seconds(punch_in_ms)
    punch_in_ist = datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).astimezone(IST)

    d = date.fromisoformat(date_str)
    start_h, start_m = parse_hh_mm(shift_start)
    shift_start_dt = datetime(d.year, d.month, d.day, start_h, start_m, 0, tzinfo=IST)

    delta_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if delta_sec > 600:
        return int(delta_sec // 60)
    return 0


def compute_overtime(punch_out_ms: Optional[int], shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    """
    if punch_out_ms is None:
        return 0

    truncated_ms = truncate_to_seconds(punch_out_ms)
    punch_out_ist = datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).astimezone(IST)

    d = date.fromisoformat(date_str)
    end_h, end_m = parse_hh_mm(shift_end)
    if is_overnight(shift_start, shift_end):
        end_day = d + timedelta(days=1)
    else:
        end_day = d

    shift_end_dt = datetime(end_day.year, end_day.month, end_day.day, end_h, end_m, 0, tzinfo=IST)
    delta_sec = (punch_out_ist - shift_end_dt).total_seconds()
    mins = int(delta_sec // 60)
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in_ms: Optional[int], punch_out_ms: Optional[int]) -> Optional[float]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    null until punch-out.
    """
    if punch_in_ms is None or punch_out_ms is None:
        return None

    in_sec = truncate_to_seconds(punch_in_ms) // 1000
    out_sec = truncate_to_seconds(punch_out_ms) // 1000
    diff_sec = out_sec - in_sec
    hours = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar. For mid-month joiners,
    working days are counted from joined_on.
    """
    year, month = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, num_days)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > month_end:
        return 0

    start_date = max(month_start, joined_date)
    working_days = 0
    curr = start_date
    while curr <= month_end:
        if curr.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Document Serializers (BSON <-> JSON)
# --------------------------------------------------------------------------- #
def serialize_history_entry(h: dict) -> dict:
    changes = {}
    for k, v in h.get("changes", {}).items():
        changes[k] = {
            "from": to_epoch_millis(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
            "to": to_epoch_millis(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
        }
    return {
        "at": to_epoch_millis(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employees Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces unique emp_code via unique database index."""
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")

    try:
        date.fromisoformat(body.joined_on)
    except ValueError:
        raise HTTPException(422, "Invalid joined_on date")

    now_utc = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
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
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {
        "items": [serialize_employee_doc(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Creates the day's record (R1, R2). Status defaults to PRESENT.
    Enforces concurrency via unique compound index (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_to_seconds(body.punched_at)
    else:
        punched_at_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    attendance_date = get_attendance_date(punched_at_ms, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punched_at_ms, emp["shift_start"], emp["shift_end"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": from_epoch_millis(punched_at_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_ms = truncate_to_seconds(body.punched_at)
    else:
        punch_out_ms = truncate_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = from_epoch_millis(punch_out_ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_ms = to_epoch_millis(record["punch_in"])
    if punch_in_ms is None or punch_out_ms <= punch_in_ms:
        raise HTTPException(422, "punch_out must be strictly greater than punch_in")

    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    overtime = compute_overtime(punch_out_ms, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Already punched out")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance_doc(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeRequest):
    """
    Regularize (manually correct) one day's record. Addresses record by (emp_code, date).
    Recomputes derived fields and appends an audit trail entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    # Setting ABSENT or LEAVE clears both punch times; supplying punches with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    final_status = body.status if body.status is not None else record["status"]

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in_ms = None
        final_punch_out_ms = None
        final_late = 0
        final_work_hours = None
        final_overtime = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in_ms = truncate_to_seconds(body.punch_in)
        else:
            final_punch_in_ms = to_epoch_millis(record.get("punch_in"))

        if body.punch_out is not None:
            final_punch_out_ms = truncate_to_seconds(body.punch_out)
        else:
            final_punch_out_ms = to_epoch_millis(record.get("punch_out"))

        if final_punch_in_ms is None:
            raise HTTPException(422, "A presence status requires punch_in")

        # punch_in must stay on record's attendance date per R1
        in_att_date = get_attendance_date(final_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if in_att_date != date_str:
            raise HTTPException(422, "punch_in must belong to attendance date")

        if final_punch_out_ms is not None:
            if final_punch_out_ms <= final_punch_in_ms:
                raise HTTPException(422, "punch_out must be strictly greater than punch_in")
            if (final_punch_out_ms - final_punch_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        final_late = compute_late_minutes(final_punch_in_ms, emp["shift_start"], emp["shift_end"], date_str)
        if final_punch_out_ms is not None:
            final_work_hours = compute_work_hours(final_punch_in_ms, final_punch_out_ms)
            final_overtime = compute_overtime(final_punch_out_ms, emp["shift_start"], emp["shift_end"], date_str)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime = 0
            final_half_day = False

    # Compute changes
    changes: Dict[str, dict] = {}
    old_in_ms = to_epoch_millis(record.get("punch_in"))
    old_out_ms = to_epoch_millis(record.get("punch_out"))

    if record["status"] != final_status:
        changes["status"] = {"from": record["status"], "to": final_status}

    if old_in_ms != final_punch_in_ms:
        changes["punch_in"] = {
            "from": record.get("punch_in"),
            "to": from_epoch_millis(final_punch_in_ms) if final_punch_in_ms is not None else None,
        }

    if old_out_ms != final_punch_out_ms:
        changes["punch_out"] = {
            "from": record.get("punch_out"),
            "to": from_epoch_millis(final_punch_out_ms) if final_punch_out_ms is not None else None,
        }

    if record.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": final_work_hours}

    if record.get("late_minutes", 0) != final_late:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": final_late}

    if record.get("overtime_minutes", 0) != final_overtime:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": final_overtime}

    if record.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check on history size
    history_len = len(record.get("history", []))
    match_query: Dict[str, Any] = {"_id": record["_id"]}
    if "history" in record:
        match_query["history"] = {"$size": history_len}
    else:
        match_query["history"] = {"$exists": False}

    update_doc = {
        "$set": {
            "status": final_status,
            "punch_in": from_epoch_millis(final_punch_in_ms) if final_punch_in_ms is not None else None,
            "punch_out": from_epoch_millis(final_punch_out_ms) if final_punch_out_ms is not None else None,
            "work_hours": final_work_hours,
            "late_minutes": final_late,
            "overtime_minutes": final_overtime,
            "half_day": final_half_day,
        },
        "$push": {"history": history_entry},
    }

    res = db.attendance_logs.find_one_and_update(
        match_query,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "Concurrent modification conflict")

    return serialize_attendance_doc(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        row = res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month via cross-collection aggregation."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "metrics": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {
                            "present_days": 0.0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                            "sum_work_hours": 0.0,
                            "count_work_hours": 0,
                        },
                        "in": {
                            "present_days": {
                                "$add": [
                                    "$$value.present_days",
                                    {
                                        "$let": {
                                            "vars": {
                                                "is_present": {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                "is_weekday": {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date",
                                                                    "format": "%Y-%m-%d",
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            },
                                            "in": {
                                                "$cond": [
                                                    {"$and": ["$$is_present", "$$is_weekday"]},
                                                    {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                                    0.0,
                                                ]
                                            },
                                        }
                                    },
                                ]
                            },
                            "late_count": {
                                "$add": [
                                    "$$value.late_count",
                                    {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                                ]
                            },
                            "total_late_minutes": {
                                "$add": [
                                    "$$value.total_late_minutes",
                                    {"$ifNull": ["$$this.late_minutes", 0]},
                                ]
                            },
                            "leave_count": {
                                "$add": [
                                    "$$value.leave_count",
                                    {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                                ]
                            },
                            "on_duty_count": {
                                "$add": [
                                    "$$value.on_duty_count",
                                    {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                                ]
                            },
                            "sum_work_hours": {
                                "$add": [
                                    "$$value.sum_work_hours",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            "$$this.work_hours",
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                            "count_work_hours": {
                                "$add": [
                                    "$$value.count_work_hours",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    },
                                ]
                            },
                        },
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$metrics.present_days"},
                "late_count": {"$sum": "$metrics.late_count"},
                "total_late_minutes": {"$sum": "$metrics.total_late_minutes"},
                "leave_count": {"$sum": "$metrics.leave_count"},
                "on_duty_count": {"$sum": "$metrics.on_duty_count"},
                "sum_work_hours": {"$sum": "$metrics.sum_work_hours"},
                "count_work_hours": {"$sum": "$metrics.count_work_hours"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$count_work_hours", 0]},
                        {"$divide": ["$sum_work_hours", "$count_work_hours"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = round_half_up(d.get("avg_work_hours"), 2)
        items.append(
            {
                "department": d["department"],
                "headcount": int(d["headcount"]),
                "present_days": float(d["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(d["late_count"]),
                "total_late_minutes": int(d["total_late_minutes"]),
                "leave_count": int(d["leave_count"]),
                "on_duty_count": int(d["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks employees by total late_minutes in the month (total > 0).
    Uses standard competition ranking (1, 2, 2, 4) with ties.
    Limit applies after ranking: returns every employee whose rank <= limit.
    """
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
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

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    """
    Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average.
    """
    try:
        d_from_date = date.fromisoformat(from_)
        d_to_date = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "Invalid from or to date format")

    if d_to_date < d_from_date:
        raise HTTPException(422, "'to' date cannot be before 'from' date")

    days_count = (d_to_date - d_from_date).days + 1
    if days_count > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # 404 if department has no employees
    emp_check = db.employees.find_one({"department": department})
    if not emp_check:
        raise HTTPException(404, "Department not found")

    d_from_dt = datetime(d_from_date.year, d_from_date.month, d_from_date.day, tzinfo=timezone.utc)
    d_to_end_dt = (datetime(d_to_date.year, d_to_date.month, d_to_date.day) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_obj": {"$literal": d_from_dt}}},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from_dt, d_to_end_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                    }
                },
                "dow": {"$dayOfWeek": "$date_obj"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"emp_code": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$d"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$dept_emps"},
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        rate = round_half_up(d.get("attendance_rate"), 4)
        mavg = round_half_up(d.get("moving_avg_7d"), 4)
        items.append(
            {
                "date": d["date"],
                "is_working_day": bool(d["is_working_day"]),
                "headcount": int(d["headcount"]),
                "present_count": float(d["present_count"]),
                "late_count": int(d["late_count"]),
                "attendance_rate": rate,
                "moving_avg_7d": mavg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint(
    endpoint_name: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """
    Runs MongoDB's explain with executionStats verbosity on the named endpoint's main query.
    """
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint_name not in valid_endpoints:
        raise HTTPException(422, f"Invalid endpoint: {endpoint_name}")

    if endpoint_name == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        try:
            year, m = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m)
        except Exception:
            raise HTTPException(422, "Invalid month format")

        start_date = f"{month}-01"
        end_date = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$project": {
                    "status": 1,
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                    "half_day": 1,
                    "dow": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint_name == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            year, m = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m)
        except Exception:
            raise HTTPException(422, "Invalid month format")

        start_date = f"{month}-01"
        end_date = f"{month}-{last_day:02d}"

        match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "metrics": {
                        "$reduce": {
                            "input": "$logs",
                            "initialValue": {
                                "present_days": 0.0,
                                "late_count": 0,
                                "total_late_minutes": 0,
                                "leave_count": 0,
                                "on_duty_count": 0,
                                "sum_work_hours": 0.0,
                                "count_work_hours": 0,
                            },
                            "in": {
                                "present_days": {
                                    "$add": [
                                        "$$value.present_days",
                                        {
                                            "$let": {
                                                "vars": {
                                                    "is_present": {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    "is_weekday": {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$this.date",
                                                                        "format": "%Y-%m-%d",
                                                                    }
                                                                }
                                                            },
                                                            [2, 3, 4, 5, 6],
                                                        ]
                                                    },
                                                },
                                                "in": {
                                                    "$cond": [
                                                        {"$and": ["$$is_present", "$$is_weekday"]},
                                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                                        0.0,
                                                    ]
                                                },
                                            }
                                        },
                                    ]
                                },
                                "late_count": {
                                    "$add": [
                                        "$$value.late_count",
                                        {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                                    ]
                                },
                                "total_late_minutes": {
                                    "$add": [
                                        "$$value.total_late_minutes",
                                        {"$ifNull": ["$$this.late_minutes", 0]},
                                    ]
                                },
                                "leave_count": {
                                    "$add": [
                                        "$$value.leave_count",
                                        {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                                    ]
                                },
                                "on_duty_count": {
                                    "$add": [
                                        "$$value.on_duty_count",
                                        {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                                    ]
                                },
                                "sum_work_hours": {
                                    "$add": [
                                        "$$value.sum_work_hours",
                                        {
                                            "$cond": [
                                                {
                                                    "$and": [
                                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                        {"$ne": ["$$this.work_hours", None]},
                                                    ]
                                                },
                                                "$$this.work_hours",
                                                0.0,
                                            ]
                                        },
                                    ]
                                },
                                "count_work_hours": {
                                    "$add": [
                                        "$$value.count_work_hours",
                                        {
                                            "$cond": [
                                                {
                                                    "$and": [
                                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                        {"$ne": ["$$this.work_hours", None]},
                                                    ]
                                                },
                                                1,
                                                0,
                                            ]
                                        },
                                    ]
                                },
                            },
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$metrics.present_days"},
                    "late_count": {"$sum": "$metrics.late_count"},
                    "total_late_minutes": {"$sum": "$metrics.total_late_minutes"},
                    "leave_count": {"$sum": "$metrics.leave_count"},
                    "on_duty_count": {"$sum": "$metrics.on_duty_count"},
                    "sum_work_hours": {"$sum": "$metrics.sum_work_hours"},
                    "count_work_hours": {"$sum": "$metrics.count_work_hours"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$count_work_hours", 0]},
                            {"$divide": ["$sum_work_hours", "$count_work_hours"]},
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint_name,
            "collection": "employees",
            "explain": json_util.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint_name == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        try:
            year, m = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m)
        except Exception:
            raise HTTPException(422, "Invalid month format")

        start_date = f"{month}-01"
        end_date = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
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

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {
                            "rank": {"$rank": {}},
                        },
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$emp.name",
                        "department": "$emp.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": json_util.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint_name == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required")
        try:
            d_from_date = date.fromisoformat(from_)
            d_to_date = date.fromisoformat(to)
        except ValueError:
            raise HTTPException(422, "Invalid from or to date format")

        if d_to_date < d_from_date:
            raise HTTPException(422, "'to' date cannot be before 'from' date")
        if (d_to_date - d_from_date).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        d_from_dt = datetime(d_from_date.year, d_from_date.month, d_from_date.day, tzinfo=timezone.utc)
        d_to_end_dt = (datetime(d_to_date.year, d_to_date.month, d_to_date.day) + timedelta(days=1)).replace(tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"date_obj": {"$literal": d_from_dt}}},
            {
                "$densify": {
                    "field": "date_obj",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [d_from_dt, d_to_end_dt],
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date_obj",
                        }
                    },
                    "dow": {"$dayOfWeek": "$date_obj"},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"d": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$d"]},
                                    ]
                                }
                            }
                        },
                        {"$project": {"emp_code": 1}},
                    ],
                    "as": "dept_emps",
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"d": "$date", "codes": "$dept_emps.emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$eq": ["$date", "$$d"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                    "headcount": {"$size": "$dept_emps"},
                    "present_count": {
                        "$reduce": {
                            "input": "$logs",
                            "initialValue": 0.0,
                            "in": {
                                "$add": [
                                    "$$value",
                                    {
                                        "$cond": [
                                            {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                        }
                    },
                    "late_count": {
                        "$reduce": {
                            "input": "$logs",
                            "initialValue": 0,
                            "in": {
                                "$add": [
                                    "$$value",
                                    {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                                ]
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"],
                            },
                        }
                    },
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint_name,
            "collection": "employees",
            "explain": json_util.loads(json_util.dumps(explain_doc)),
        }
