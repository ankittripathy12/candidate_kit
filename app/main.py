"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. configuration, time helpers, business-rule helpers (R1 - R5, R8)
  2. request models
  3. employees, attendance (punch-in, punch-out, list, regularize)
  4. analytics (every one is a MongoDB aggregation pipeline built by a small function)
  5. explain endpoint (re-uses exactly the same builders as the endpoints)
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables always win
log = logging.getLogger("attendance")

# tz_aware=True: every datetime read from MongoDB is an aware UTC datetime (never naive).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    wanted = [
        # employees
        ("employees", [("emp_code", ASCENDING)], dict(unique=True, name="emp_code_unique")),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], dict(name="dept_emp_code")),
        ("employees", [("joined_on", ASCENDING)], dict(name="joined_on")),
        # attendance_logs
        # one record per (employee, day): race-proof punch-in, per-employee queries, monthly, $lookup joins
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], dict(unique=True, name="emp_date_unique")),
        # GET /attendance sort (date desc, emp_code asc) and date ranges without an emp_code filter
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], dict(name="date_desc_emp")),
        # GET /attendance?status=... keeps the same sort served by the index
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], dict(name="status_date_emp")),
        # late leaderboard: date range + late_minutes > 0, grouped by emp_code -> covered query
        ("attendance_logs", [("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", ASCENDING)], dict(name="date_emp_late")),
        # punch-out: latest record of an employee with punch_in <= t
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], dict(name="emp_punch_in_desc")),
    ]
    for coll, keys, opts in wanted:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError as exc:  # never keep the service from starting because of one index
            log.error("index %s on %s failed: %s", opts.get("name"), coll, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # pragma: no cover
        log.error("index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Small helpers: errors, instants, rounding
# --------------------------------------------------------------------------- #
def unprocessable(loc: tuple, msg: str):
    """Raise a 422 in FastAPI's default validation-error shape."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive; the client is tz_aware
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON dates hold milliseconds


def as_decimal(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal128):
        return v.to_decimal()
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(v)


def q(value: Any, places: int = 2) -> Optional[float]:
    """Round half-up (R8). Floats are first cleaned of binary noise (0.30000000000000004 -> 0.3)."""
    if value is None:
        return None
    if isinstance(value, float):
        d = Decimal(repr(round(value, 10)))
    else:
        d = as_decimal(value)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp: dict) -> bool:
    return _minutes(emp["shift_end"]) <= _minutes(emp["shift_start"])


def attendance_date(ts: datetime, emp: dict) -> Date:
    """R1: IST calendar date of the punch-in; overnight shifts: before shift_end -> previous day."""
    ist = ts.astimezone(IST)
    day = ist.date()
    if is_overnight(emp) and (ist.hour * 3600 + ist.minute * 60 + ist.second) < _minutes(emp["shift_end"]) * 60:
        day -= timedelta(days=1)
    return day


def shift_window(emp: dict, day: Date) -> tuple:
    """(shift start, shift end) as aware IST datetimes for the shift that belongs to attendance date `day`."""
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: strictly more than 10:00 after shift start -> whole minutes since shift start, else 0."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (exact decimal arithmetic, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: on the rounded work hours."""
    return Decimal(str(work_hours)) < HALF_DAY_BELOW


def derived_fields(emp: dict, day: Date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All stored derived values for a presence record (R2 - R5); defaults when a part is missing."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        return out
    start, end = shift_window(emp, day)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# Date / month parameter parsing
# --------------------------------------------------------------------------- #
def parse_date(value: str, loc: tuple) -> Date:
    if not DATE_RE.match(value):
        unprocessable(loc, "must be a date in YYYY-MM-DD format")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")


def month_bounds(month: str, loc: tuple = ("query", "month")) -> tuple:
    """-> (first day, first day of next month, last day) of 'YYYY-MM'."""
    if not re.match(MONTH_PATTERN, month):
        unprocessable(loc, "must be a month in YYYY-MM format")
    year, mon = int(month[:4]), int(month[5:7])
    if not 1 <= year <= 9998:
        unprocessable(loc, "year out of range")
    first = Date(year, mon, 1)
    nxt = Date(year + 1, 1, 1) if mon == 12 else Date(year, mon + 1, 1)
    return first, nxt, nxt - timedelta(days=1)


def weekday_strings(first: Date, nxt: Date) -> list:
    days, d = [], first
    while d < nxt:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def working_days(first: Date, last: Date, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for a mid-month joiner."""
    start = first
    try:
        start = max(first, Date.fromisoformat(joined_on))
    except (TypeError, ValueError):
        pass
    count, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        try:
            Date.fromisoformat(v)
        except ValueError:
            raise ValueError("not a valid calendar date")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (BSON -> API)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return to_ms(v) if isinstance(v, datetime) else v


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d.get("name"),
        "email": d.get("email"),
        "department": d.get("department"),
        "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"),
        "joined_on": d.get("joined_on"),
        "created_at": to_ms(d.get("created_at")),
    }


def record_out(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the only guard that is race-proof
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {"department": department} if department else {}
    total = db.employees.count_documents(flt)
    cur = db.employees.find(flt).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "history": [],
    }
    doc.update(derived_fields(emp, day, ts, None))
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if ts <= p_in:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if ts - p_in > MAX_SHIFT:
        unprocessable(("body", "punched_at"), "punch-out is more than 24 hours after the punch-in")
    d = derived_fields(emp, Date.fromisoformat(rec["date"]), p_in, ts)
    # The filter makes this a compare-and-set: of two simultaneous punch-outs only one still matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": d["work_hours"],
            "overtime_minutes": d["overtime_minutes"],
            "half_day": d["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Shared by GET /attendance and its explain: filter, sort, skip and limit."""
    d_from = parse_date(date_from, ("query", "date_from")) if date_from else None
    d_to = parse_date(date_to, ("query", "date_to")) if date_to else None
    if d_from and d_to and d_from > d_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if d_from or d_to:
        flt["date"] = {}
        if d_from:
            flt["date"]["$gte"] = d_from.isoformat()
        if d_to:
            flt["date"]["$lte"] = d_to.isoformat()
    if status:
        flt["status"] = status
    return {
        "filter": flt,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    flt = spec["filter"]
    total = db.attendance_logs.count_documents(flt) if flt else db.attendance_logs.estimated_document_count()
    cur = (
        db.attendance_logs.find(flt)
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ("path", "date"))
    emp = get_employee(emp_code)

    for _attempt in range(5):  # optimistic concurrency: re-read and retry if somebody else wrote first
        cur = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if cur is None:
            raise HTTPException(404, "no attendance record for that date")

        old_in, old_out = cur.get("punch_in"), cur.get("punch_out")
        new_status = body.status or cur["status"]

        if new_status in NON_PRESENCE:
            if body.punch_in is not None or body.punch_out is not None:
                unprocessable(("body",), f"status {new_status} clears punch times; do not supply punch_in/punch_out")
            new_in = new_out = None
            derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
        else:
            new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
            new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
            if body.punch_in is not None and attendance_date(new_in, emp).isoformat() != date:
                unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
            if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
            derived = derived_fields(emp, Date.fromisoformat(date), new_in, new_out)

        old = {
            "status": cur["status"], "punch_in": old_in, "punch_out": old_out,
            "work_hours": cur.get("work_hours"),
            "late_minutes": int(cur.get("late_minutes") or 0),
            "overtime_minutes": int(cur.get("overtime_minutes") or 0),
            "half_day": bool(cur.get("half_day") or False),
        }
        new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
        changes = {
            k: {"from": old[k], "to": new[k]}
            for k in new
            if (to_ms(old[k]) != to_ms(new[k]) if k in ("punch_in", "punch_out") else old[k] != new[k])
        }
        if not any(k in changes for k in ("status", "punch_in", "punch_out")):
            unprocessable(("body",), "the request changes nothing")

        entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        n = len(cur.get("history") or [])
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": cur["_id"],
                f"history.{n}": {"$exists": False},  # nobody appended since we read it
                "punch_out": old_out,                  # and no punch-out slipped in
            },
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders
# --------------------------------------------------------------------------- #
def _present_weight(weekdays: Optional[list] = None) -> dict:
    """1 for a present record, 0.5 for a present half day, 0 otherwise (optionally Mon-Fri only)."""
    conds: list = [{"$in": ["$status", list(PRESENCE)]}]
    if weekdays is not None:
        conds.append({"$in": ["$date", weekdays]})
    return {"$cond": [{"$and": conds}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}


_LATE_ONE = {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}


def employee_monthly_pipeline(emp_code: str, month: str) -> tuple:
    first, nxt, last = month_bounds(month)
    weekdays = weekday_strings(first, nxt)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weight(weekdays)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _LATE_ONE},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    return pipeline, first, last


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """employees (joined on/before month end) -> $lookup that month's logs per employee -> $group by department.
    Starting from employees is what makes people with zero logs count in the headcount (R9)."""
    first, nxt, last = month_bounds(month)
    weekdays = weekday_strings(first, nxt)
    has_hours = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$isNumber": "$work_hours"}]}
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": _present_weight(weekdays)},
                    "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, {"$toDecimal": 0}]}},
                    "wh_cnt": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": _LATE_ONE},
                    "late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "agg",
        }},
        {"$set": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {"$literal": {}}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$agg.present_days"},
            "wh_sum": {"$sum": "$agg.wh_sum"},
            "wh_cnt": {"$sum": "$agg.wh_cnt"},
            "late_count": {"$sum": "$agg.late_count"},
            "total_late_minutes": {"$sum": "$agg.late_minutes"},
            "leave_count": {"$sum": "$agg.leave_count"},
            "on_duty_count": {"$sum": "$agg.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    """Total late minutes per employee, then $setWindowFields/$rank (standard competition ranking), then cut by rank."""
    first, nxt, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: Date, d_to: Date) -> list:
    """One document per calendar day built with $range (gap filling), then a 7-row $setWindowFields average."""
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                {"$group": {"_id": "$date", "present_count": {"$sum": _present_weight()}, "late_count": {"$sum": _LATE_ONE}}},
            ],
            "as": "days",
        }},
        {"$project": {"_id": 0, "joined": 1, "days": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$set": {"date_dt": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$offset"}}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}}, "dow": {"$dayOfWeek": "$date_dt"}}},
        {"$set": {
            "day": {"$arrayElemAt": [{"$filter": {"input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}}, 0]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},  # $dayOfWeek: 1 = Sunday
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$day.present_count", 0]},
            "late_count": {"$ifNull": ["$day.late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date_dt": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"date_dt": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    pipeline, first, last = employee_monthly_pipeline(emp_code, month)
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {}
    present = as_decimal(r.get("present_days", 0))
    wd = working_days(first, last, emp.get("joined_on"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": q(present),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": q(present * 100 / wd) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    pipeline = department_summary_pipeline(month, department)
    items = []
    for r in db.employees.aggregate(pipeline):
        cnt = int(r["wh_cnt"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": q(as_decimal(r["present_days"])),
            "avg_work_hours": q(as_decimal(r["wh_sum"]) / cnt) if cnt else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = late_leaderboard_pipeline(month, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline)
    ]
    return {"month": month, "items": items}


def _trend_args(department: str, from_: str, to: str) -> tuple:
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        unprocessable(("query", "to"), "the range must not exceed 92 days")
    return department_trend_pipeline(department, d_from, d_to), d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    pipeline, _, _ = _trend_args(department, from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": q(as_decimal(r["present_count"])),
            "late_count": int(r["late_count"]),
            "attendance_rate": q(r.get("attendance_rate"), 4),
            "moving_avg_7d": q(r.get("moving_avg_7d"), 4),
        }
        for r in db.employees.aggregate(pipeline)
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain (uses the very same builders as the endpoints above)
# --------------------------------------------------------------------------- #
def _need(**params):
    for name, value in params.items():
        if value is None:
            unprocessable(("query", name), "field required")


def _jsonable(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        inner = {"find": collection, **spec}
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            pipeline = employee_monthly_pipeline(emp_code, month)[0]
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            _need(month=month)
            pipeline = department_summary_pipeline(month, department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            _need(month=month)
            pipeline = late_leaderboard_pipeline(month, limit, department)
            collection = "attendance_logs"
        else:  # department_trend
            _need(department=department, **{"from": from_}, to=to)
            pipeline = _trend_args(department, from_, to)[0]
            collection = "employees"
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}