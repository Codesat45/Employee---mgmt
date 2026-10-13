"""HROne Employee Attendance & Analytics API."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENT_OR_LEAVE = {"ABSENT", "LEAVE"}
VALID_STATUSES = PRESENCE_STATUSES | ABSENT_OR_LEAVE

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def _validate_epoch_ms(value: Optional[int], *, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer epoch millisecond value")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError(f"{field_name} must be between 2001-09-09 and 2100-01-01 in epoch milliseconds")
    return value


def _validate_date_str(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field_name} must be a valid ISO date YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - pydantic catches it
        raise ValueError(f"{field_name} must be a valid ISO date YYYY-MM-DD") from exc
    return value


def _require_date(value: str, *, field_name: str) -> date:
    try:
        _validate_date_str(value, field_name=field_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return date.fromisoformat(value)


def _require_month(value: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")


def _validate_date_range(from_date: str, to_date: str, *, max_days: int | None = None) -> tuple[date, date]:
    start = _require_date(from_date, field_name="from")
    end = _require_date(to_date, field_name="to")
    if start > end:
        raise HTTPException(status_code=422, detail="from must be on or before to")
    if max_days is not None and (end - start).days + 1 > max_days:
        raise HTTPException(status_code=422, detail=f"date range must not exceed {max_days} days")
    return start, end


def _validate_hhmm(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f"{field_name} must be a valid HH:MM time")
    return value


def _to_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_ist_datetime(value: datetime | int) -> datetime:
    if isinstance(value, int):
        value = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _dt_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _truncate_epoch_ms(ms: int) -> int:
    return ms // 1000 * 1000


def _to_epoch_ms(value: int | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _dt_to_epoch_ms(value)
    return _truncate_epoch_ms(int(value))


def _shift_time(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def _shift_window_for_instant(punch_in_ms: int, shift_start: str, shift_end: str):
    punch_in_ist = _to_ist_datetime(punch_in_ms)
    attendance_date = punch_in_ist.date()
    if shift_end <= shift_start:
        if punch_in_ist.time() < _shift_time(shift_end):
            attendance_date -= timedelta(days=1)
    shift_start_dt = datetime.combine(attendance_date, _shift_time(shift_start), tzinfo=IST)
    end_date = attendance_date + (timedelta(days=1) if shift_end <= shift_start else timedelta(0))
    shift_end_dt = datetime.combine(end_date, _shift_time(shift_end), tzinfo=IST)
    return attendance_date, shift_start_dt, shift_end_dt


def _attendance_date_for_shift(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    attendance_date, _, _ = _shift_window_for_instant(punch_in_ms, shift_start, shift_end)
    return attendance_date.isoformat()


def _compute_late_minutes(punch_in_ms: int, shift_start: str, shift_end: str) -> int:
    _, shift_start_dt, _ = _shift_window_for_instant(punch_in_ms, shift_start, shift_end)
    delta_seconds = _to_ist_datetime(punch_in_ms) - shift_start_dt
    total_seconds = delta_seconds.total_seconds()
    if total_seconds <= 600:
        return 0
    return int(total_seconds // 60)


def _compute_overtime_minutes(punch_in_ms: int, punch_out_ms: int, shift_start: str, shift_end: str) -> int:
    _, _, shift_end_dt = _shift_window_for_instant(punch_in_ms, shift_start, shift_end)
    delta_seconds = _to_ist_datetime(punch_out_ms) - shift_end_dt
    if delta_seconds.total_seconds() < 1800:
        return 0
    return int(delta_seconds.total_seconds() // 60)


def _compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    delta_seconds = _to_utc_datetime(_ms_to_dt(punch_out_ms)) - _to_utc_datetime(_ms_to_dt(punch_in_ms))
    value = Decimal(str(delta_seconds.total_seconds())) / Decimal("3600")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rounded_decimal(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _month_bounds(month: str):
    year, month_num = map(int, month.split("-"))
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month_num + 1, 1)
    return start.isoformat(), end.isoformat()


def _serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = _to_epoch_ms(out.get("created_at"))
    return out


def _history_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _dt_to_epoch_ms(value)
    return value


def _serialize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in history or []:
        changes: dict[str, Any] = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": _history_value(change.get("from")),
                "to": _history_value(change.get("to")),
            }
        rows.append({
            "at": _dt_to_epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return rows


def _serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k not in {"_id"}}
    out.setdefault("punch_in", None)
    out.setdefault("punch_out", None)
    out.setdefault("work_hours", None)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    for field in ("punch_in", "punch_out"):
        if field in out:
            out[field] = _dt_to_epoch_ms(out.get(field))
    out["history"] = _serialize_history(out.get("history") or [])
    return out


def _ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@app.on_event("startup")
def startup_event() -> None:
    _ensure_indexes()


class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("name", "email", "department")
    @classmethod
    def validate_nonempty(cls, value: str, info) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time(cls, value: str, info) -> str:
        return _validate_hhmm(value, field_name=info.field_name)

    @field_validator("joined_on")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return _validate_date_str(value, field_name="joined_on")

    @model_validator(mode="after")
    def validate_shift_order(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return _validate_epoch_ms(value, field_name="punched_at")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY when punching in")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return _validate_epoch_ms(value, field_name="punched_at")


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_STATUSES:
            raise ValueError("status must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_field(cls, value: Optional[int], info) -> Optional[int]:
        return _validate_epoch_ms(value, field_name=info.field_name)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover - environment dependent
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    items = [_serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _normalize_record_for_update(doc: dict[str, Any], employee: dict[str, Any]) -> dict[str, Any]:
    status = doc.get("status")
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    if status in ABSENT_OR_LEAVE:
        punch_in = None
        punch_out = None
    elif punch_in is not None:
        punch_in = _to_utc_datetime(punch_in)
        if punch_out is not None:
            punch_out = _to_utc_datetime(punch_out)
    late_minutes = 0
    overtime_minutes = 0
    work_hours = None
    half_day = False
    if status in PRESENCE_STATUSES and punch_in is not None:
        late_minutes = _compute_late_minutes(_dt_to_epoch_ms(punch_in), employee.get("shift_start"), employee.get("shift_end"))
        if punch_out is not None:
            work_hours = _compute_work_hours(_dt_to_epoch_ms(punch_in), _dt_to_epoch_ms(punch_out))
            overtime_minutes = _compute_overtime_minutes(_dt_to_epoch_ms(punch_in), _dt_to_epoch_ms(punch_out), employee.get("shift_start"), employee.get("shift_end"))
            rounded_hours = Decimal(str(work_hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            half_day = rounded_hours < Decimal("4.50")
    if status in ABSENT_OR_LEAVE:
        late_minutes = 0
        overtime_minutes = 0
        work_hours = None
        half_day = False
    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at_ms = _truncate_epoch_ms(body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000))
    punch_in_dt = _ms_to_dt(punched_at_ms)
    record_date = _attendance_date_for_shift(punched_at_ms, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _compute_late_minutes(punched_at_ms, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return _serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at_ms = _truncate_epoch_ms(body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000))
    punch_out_dt = _ms_to_dt(punched_at_ms)
    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if existing.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    punch_in_dt = _to_utc_datetime(existing["punch_in"])
    delta_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if delta_seconds <= 0 or delta_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    work_hours = _compute_work_hours(_dt_to_epoch_ms(punch_in_dt), punched_at_ms)
    rounded_hours = Decimal(str(work_hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    record = db.attendance_logs.find_one_and_update(
        {"_id": existing["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": _compute_overtime_minutes(_dt_to_epoch_ms(punch_in_dt), punched_at_ms, employee["shift_start"], employee["shift_end"]),
            "half_day": rounded_hours < Decimal("4.50"),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if record is None:
        raise HTTPException(status_code=409, detail="record already punched out")
    return _serialize_attendance(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = _attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    items = [_serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    _require_date(date, field_name="date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="attendance record not found")

    final_status = body.status if body.status is not None else record.get("status")
    if final_status in ABSENT_OR_LEAVE and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot include punch times")

    effective_punch_in = record.get("punch_in")
    effective_punch_out = record.get("punch_out")
    if body.punch_in is not None:
        effective_punch_in = _ms_to_dt(_truncate_epoch_ms(body.punch_in))
    if body.punch_out is not None:
        effective_punch_out = _ms_to_dt(_truncate_epoch_ms(body.punch_out))
    if final_status in ABSENT_OR_LEAVE:
        effective_punch_in = None
        effective_punch_out = None
    elif final_status in PRESENCE_STATUSES and effective_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if effective_punch_in is not None:
        proposed_date = _attendance_date_for_shift(_dt_to_epoch_ms(effective_punch_in), employee["shift_start"], employee["shift_end"])
        if proposed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if effective_punch_in is not None and effective_punch_out is not None:
        if effective_punch_out <= effective_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (effective_punch_out - effective_punch_in).total_seconds() > 86400:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    proposed = _normalize_record_for_update({
        "status": final_status,
        "punch_in": effective_punch_in,
        "punch_out": effective_punch_out,
    }, employee)
    original = _normalize_record_for_update(record, employee)

    if original == proposed:
        raise HTTPException(status_code=422, detail="no changes were made")

    change_map: dict[str, Any] = {}
    for field in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(field)
        new_value = proposed.get(field)
        if old_value != new_value:
            change_map[field] = {
                "from": _history_value(old_value),
                "to": _history_value(new_value),
            }

    update = {
        "status": final_status,
        "punch_in": effective_punch_in,
        "punch_out": effective_punch_out,
        "work_hours": proposed["work_hours"],
        "late_minutes": proposed["late_minutes"],
        "overtime_minutes": proposed["overtime_minutes"],
        "half_day": proposed["half_day"],
    }
    entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": change_map,
    }

    history_match = {"history": record["history"]} if "history" in record else {"history": {"$exists": False}}
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_match},
        {"$set": {**update, "history": (record.get("history") or []) + [entry]}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="conflict while updating attendance record")
    return _serialize_attendance(result)


def _attendance_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    attendance_status: str | None,
) -> dict[str, Any]:
    if date_from is not None:
        _require_date(date_from, field_name="date_from")
    if date_to is not None:
        _require_date(date_to, field_name="date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if attendance_status is not None and attendance_status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")

    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if attendance_status is not None:
        query["status"] = attendance_status
    return query


def _employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    is_weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            [2, 3, 4, 5, 6],
        ]
    }
    is_present = {"$in": ["$status", sorted(PRESENCE_STATUSES)]}
    month_start = {"$dateFromString": {"dateString": start_date}}
    month_end = {"$dateFromString": {"dateString": end_date}}
    effective_start = {"$max": [
        {"$dateFromString": {"dateString": "$joined_on"}},
        month_start,
    ]}
    weekday_range = {"$map": {
        "input": {"$range": [0, {"$max": [
            0,
            {"$dateDiff": {"startDate": effective_start, "endDate": month_end, "unit": "day"}},
        ]}]},
        "as": "offset",
        "in": {"$dateAdd": {"startDate": effective_start, "unit": "day", "amount": "$$offset"}},
    }}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "working_days": {"$size": {"$filter": {
                "input": weekday_range,
                "as": "day",
                "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
            }}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start_date]},
                    {"$lt": ["$date", end_date]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [is_present, is_weekday]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0.0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ],
            "as": "attendance_stats",
        }},
        {"$set": {"attendance_stats": {"$ifNull": [{"$arrayElemAt": ["$attendance_stats", 0]}, {}]}}},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": 1,
            "present_days": {"$ifNull": ["$attendance_stats.present_days", 0]},
            "leave_days": {"$ifNull": ["$attendance_stats.leave_days", 0]},
            "late_count": {"$ifNull": ["$attendance_stats.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$attendance_stats.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$attendance_stats.total_overtime_minutes", 0]},
        }},
    ]


def _department_summary_pipeline(start_date: str, end_date: str, department: str | None) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lt": end_date}}
    if department is not None:
        employee_match["department"] = department

    safe_date = {"$ifNull": ["$logs.date", "1970-01-03"]}
    is_weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": safe_date}}},
            [2, 3, 4, 5, 6],
        ]
    }
    is_present = {"$in": ["$logs.status", sorted(PRESENCE_STATUSES)]}
    has_work_hours = {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start_date]},
                    {"$lt": ["$date", end_date]},
                ]}}},
                {"$project": {
                    "status": 1,
                    "date": 1,
                    "half_day": 1,
                    "work_hours": 1,
                    "late_minutes": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, is_weekday]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_total": {"$sum": {"$cond": [
                {"$and": [is_present, has_work_hours]},
                "$logs.work_hours",
                0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [is_present, has_work_hours]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_total": {"$sum": "$work_hours_total"},
            "work_hours_count": {"$sum": "$work_hours_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_total", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def _late_leaderboard_pipeline(
    start_date: str,
    end_date: str,
    limit: int,
    department: str | None,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_date, "$lt": end_date}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def _department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    start_instant = {"$dateFromString": {"dateString": from_date}}
    day_index = {"$range": [
        0,
        {"$add": [{"$dateDiff": {
            "startDate": start_instant,
            "endDate": {"$dateFromString": {"dateString": to_date}},
            "unit": "day",
        }}, 1]},
    ]}
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": None,
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
        }},
        {"$set": {"dates": {"$map": {
            "input": day_index,
            "as": "offset",
            "in": {"$dateAdd": {"startDate": start_instant, "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$dates"},
        {"$set": {
            "date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}},
            "active_employees": {"$filter": {
                "input": "$employees",
                "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}}]},
            }},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {
                "day": "$date_string",
                "employee_codes": {"$map": {
                    "input": "$active_employees",
                    "as": "employee",
                    "in": "$$employee.emp_code",
                }},
            },
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$day"]},
                    {"$in": ["$emp_code", "$$employee_codes"]},
                ]}}},
                {"$project": {"status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "headcount": {"$size": "$active_employees"},
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {
                    "input": "$logs",
                    "as": "log",
                    "cond": {"$in": ["$$log.status", sorted(PRESENCE_STATUSES)]},
                }},
                "as": "log",
                "in": {"$cond": [{"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]}, 0.5, 1.0]},
            }}},
            "late_count": {"$size": {"$filter": {
                "input": "$logs",
                "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
            }}},
            "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$dates"}, [1, 7]]}]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"dates": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, "current"]},
            }},
        }},
        {"$project": {
            "_id": 0,
            "date": "$date_string",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


def _rounded_number(value: Any, places: str) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    _require_month(month)
    start_date, end_date = _month_bounds(month)
    stats = next(iter(db.employees.aggregate(
        _employee_monthly_pipeline(emp_code, start_date, end_date),
        allowDiskUse=True,
    )), None)
    if stats is None:
        raise HTTPException(status_code=404, detail="employee not found")
    present_days = Decimal(str(stats.get("present_days", 0)))
    working_days = int(stats["working_days"])
    attendance_pct = None
    if working_days:
        attendance_pct = _rounded_number(present_days / Decimal(working_days) * Decimal(100), "0.0001")

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _rounded_number(present_days, "0.01"),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    _require_month(month)
    start_date, end_date = _month_bounds(month)
    pipeline = _department_summary_pipeline(start_date, end_date, department)
    items = []
    for item in db.employees.aggregate(pipeline, allowDiskUse=True):
        item["present_days"] = _rounded_number(item.get("present_days", 0), "0.01")
        item["avg_work_hours"] = _rounded_number(item.get("avg_work_hours"), "0.01")
        items.append(item)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    _require_month(month)
    start_date, end_date = _month_bounds(month)
    pipeline = _late_leaderboard_pipeline(start_date, end_date, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = _validate_date_range(from_date, to_date, max_days=92)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="department not found")

    pipeline = _department_trend_pipeline(department, start.isoformat(), end.isoformat())
    items = []
    for item in db.employees.aggregate(pipeline, allowDiskUse=True):
        item["present_count"] = _rounded_number(item.get("present_count", 0), "0.01")
        item["attendance_rate"] = _rounded_number(item.get("attendance_rate"), "0.0001")
        item["moving_avg_7d"] = _rounded_number(item.get("moving_avg_7d"), "0.0001")
        items.append(item)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attendance_status: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = _attendance_query(emp_code, date_from, date_to, attendance_status)
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        _require_month(month)
        start_date, end_date = _month_bounds(month)
        pipeline = _employee_monthly_pipeline(emp_code, start_date, end_date)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate("employees", pipeline),
        }

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        _require_month(month)
        start_date, end_date = _month_bounds(month)
        pipeline = _department_summary_pipeline(start_date, end_date, department)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate("employees", pipeline),
        }

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        _require_month(month)
        start_date, end_date = _month_bounds(month)
        pipeline = _late_leaderboard_pipeline(start_date, end_date, limit, department)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _explain_aggregate("attendance_logs", pipeline),
        }

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start, end = _validate_date_range(from_date, to_date, max_days=92)
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(status_code=404, detail="department not found")
        pipeline = _department_trend_pipeline(department, start.isoformat(), end.isoformat())
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate("employees", pipeline),
        }

    raise HTTPException(status_code=404, detail="endpoint not found")
