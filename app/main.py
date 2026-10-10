"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from typing import Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from pydantic import ConfigDict
from pydantic import BaseModel, StrictInt, Field

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    try:
        db.employees.create_index(
            [("emp_code", 1)],
            unique=True,
            name="uq_employee_code",
        )
        db.employees.create_index(
            [("department", 1), ("joined_on", 1)],
            name="department_joined",
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)],
            unique=True,
            name="uq_attendance_employee_date",
        )
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)],
            name="attendance_date_employee",
        )
        db.attendance_logs.create_index(
            [("date", 1), ("status", 1), ("emp_code", 1)],
            name="attendance_date_status_employee",
        )
    except Exception as exc:
        print(f"WARNING: Could not create MongoDB indexes: {exc}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp() * 1000)




def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_in_ist = punch_in.astimezone(IST)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_start_minutes = start_h * 60 + start_m
    shift_end_minutes = end_h * 60 + end_m
    punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute

    start = punch_in_ist.replace(
        hour=start_h, minute=start_m, second=0, microsecond=0
    )

    is_overnight = shift_end_minutes <= shift_start_minutes

    if is_overnight and punch_minutes < shift_end_minutes:
        start -= timedelta(days=1)

    minutes = int((punch_in_ist - start).total_seconds() // 60)
    return minutes if minutes > 10 else 0



def compute_work_hours(punch_in: datetime, punch_out: datetime,) -> float:
    total_seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = total_seconds / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str,
) -> int:
    # Normalize punch-out to IST.
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    punch_out = punch_out.astimezone(IST)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    # Build the scheduled shift-end instant.
    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    # If shift_end <= shift_start, the shift ends the next day.
    if (end_h, end_m) <= (start_h, start_m):
        shift_end_dt += timedelta(days=1)

    # Calculate complete overtime minutes.
    overtime_seconds = (punch_out - shift_end_dt).total_seconds()
    overtime_minutes = int(overtime_seconds // 60)

    # Overtime is counted only when it reaches 30 minutes.
    if overtime_minutes < 30:
        return 0

    return overtime_minutes




# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    reason: str
    regularized_by: str


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_: Optional[str | int | float | bool] = Field(
        default=None, alias="from"
    )
    to: Optional[str | int | float | bool] = None

class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, FieldChange]

class AttendanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int = 0
    overtime_minutes: int = 0
    half_day: bool = False
    history: list[HistoryEntry] = Field(default_factory=list)


class AttendanceListResponse(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")
    #ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)

    # d = ts.astimezone(IST).date().isoformat()
    local_ts = ts.astimezone(IST)
    attendance_date = local_ts.date()
    shift_start = tuple(map(int, emp["shift_start"].split(":")))
    shift_end = tuple(map(int, emp["shift_end"].split(":")))
    local_time = (local_ts.hour, local_ts.minute)
    # For overnight shifts, early-morning punches belong
    # to the shift that started the previous day.
    if shift_end <= shift_start and local_time < shift_end:
        attendance_date -= timedelta(days=1)
    d = attendance_date.isoformat()

    # if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
    #     raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], emp["shift_end"]
        ),
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
    doc.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        doc[field] = to_epoch_ms(doc.get(field))

    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = (
        datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )
        if body.punched_at is not None
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "no open punch-in found")

    punch_in_time = record["punch_in"].replace(tzinfo=timezone.utc)

    if ts <= punch_in_time or ts > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "invalid punch-out time")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        new_punch_out,
        emp["shift_end"],
        date,
        emp["shift_start"],
    )

    updated = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.5,
            }
        },
    )

    if updated.modified_count == 0:
        raise HTTPException(409, "record already punched out")

    result = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0}
    )

    for field in ("punch_in", "punch_out"):
        result[field] = to_epoch_ms(result.get(field))

    for entry in result.get("history", []):
        entry["at"] = to_epoch_ms(entry.get("at"))

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                value = change.get(side)
                if isinstance(value, datetime):
                    change[side] = to_epoch_ms(value)

    return result


@app.get("/attendance", response_model=AttendanceListResponse)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    # Validate and apply date filters.
    for date_value in (date_from, date_to):
        if date_value is not None:
            try:
                datetime.strptime(date_value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="Dates must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    if date_from or date_to:
        date_filter = {}

        if date_from:
            date_filter["$gte"] = date_from

        if date_to:
            date_filter["$lte"] = date_to

        q["date"] = date_filter

    # Validate status.
    if status is not None:
        allowed_statuses = {
            "PRESENT",
            "ABSENT",
            "LEAVE",
            "WFH",
            "ON_DUTY",
        }

        if status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status",
            )

        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    # Convert timestamps to epoch milliseconds.
    for record in page_docs:
        for field in ("punch_in", "punch_out"):
            record[field] = to_epoch_ms(record.get(field))

        for entry in record.get("history", []):
            entry["at"] = to_epoch_ms(entry.get("at"))

            for change in entry.get("changes", {}).values():
                for side in ("from", "to"):
                    value = change.get(side)
                    if isinstance(value, datetime):
                        change[side] = to_epoch_ms(value)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecord
)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # 1. Verify the employee and attendance record exist.
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if not record:
        raise HTTPException(404, "attendance record not found")

    # 2. Validate the requested status and punch-time changes.
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    if body.status is not None and body.status not in allowed_statuses:
        raise HTTPException(422, "invalid attendance status")

    if body.status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "ABSENT or LEAVE cannot have punch times"
            )

    # MongoDB's naive datetimes represent UTC instants.
    def as_utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(microsecond=0)


    # Convert supplied epoch milliseconds to UTC.
    new_punch_in = (
        datetime.fromtimestamp(
            body.punch_in / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)
        if body.punch_in is not None
        else as_utc(record.get("punch_in"))
    )

    new_punch_out = (
        datetime.fromtimestamp(
            body.punch_out / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)
        if body.punch_out is not None
        else as_utc(record.get("punch_out"))
    )

    final_status = body.status or record["status"]

    # ABSENT and LEAVE must have no punch times.
    if final_status in {"ABSENT", "LEAVE"}:
        new_punch_in = None
        new_punch_out = None

    # Presence statuses require a punch-in.
    if final_status in {"PRESENT", "WFH", "ON_DUTY"}:
        if new_punch_in is None:
            raise HTTPException(
                422, "presence status requires punch_in"
            )

    # Punch-in must belong to the attendance day in IST.
    if new_punch_in is not None:
        punch_in_ist = new_punch_in.astimezone(IST)
        # Punch-in must belong to the attendance day in IST.
        if new_punch_in is not None:
            punch_in_ist = new_punch_in.astimezone(IST)
            punch_in_date = punch_in_ist.date()

            shift_start = tuple(map(int, emp["shift_start"].split(":")))
            shift_end = tuple(map(int, emp["shift_end"].split(":")))
            punch_time = (punch_in_ist.hour, punch_in_ist.minute)

            is_overnight = shift_end <= shift_start

            if is_overnight and punch_time < shift_end:
                punch_in_date -= timedelta(days=1)

            if punch_in_date.isoformat() != date:
                raise HTTPException(
                    422, "punch_in must be on attendance date"
                )


    # Punch-out must follow punch-in and be within 24 hours.
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")

        if new_punch_out > new_punch_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be within 24 hours")

    # 3. Recompute the derived fields from the final values.
    if new_punch_in is not None and new_punch_out is not None:
        work_hours = compute_work_hours(new_punch_in, new_punch_out)
    else:
        work_hours = None

    late_minutes = (
        compute_late_minutes(
            new_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        )
        if new_punch_in is not None
        else 0
    )

    if new_punch_out is not None:
        overtime_minutes = compute_overtime(
            new_punch_out,
            emp["shift_end"],
            date,
            emp["shift_start"],
        )
    else:
        overtime_minutes = 0


    half_day = work_hours is not None and work_hours < 4.5

    # 4. Record only fields whose values actually changed.
    old_values = {
        "status": record.get("status"),
        "punch_in": as_utc(record.get("punch_in")),
        "punch_out": as_utc(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": final_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = old_values[field]
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "request does not change anything")

    # 5. Append one audit entry and update the record atomically.
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_fields = dict(new_values)

    # Compare the original history during the update so concurrent
    # corrections cannot silently overwrite one another.
    if "history" in record:
        concurrency_filter = {
            "_id": record["_id"],
            "history": record["history"],
        }
    else:
        concurrency_filter = {
            "_id": record["_id"],
            "history": {"$exists": False},
        }

    result = db.attendance_logs.update_one(
        concurrency_filter,
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(
            409, "attendance record changed; reload and retry"
        )

    # 6. Return timestamps as epoch milliseconds, as required by the API.
    updated_record = db.attendance_logs.find_one({
        "_id": record["_id"],
    })

    response = {
        key: value
        for key, value in updated_record.items()
        if key != "_id"
    }
    # response["id"] = str(updated_record["_id"])

    for field in ("punch_in", "punch_out"):
        response[field] = to_epoch_ms(updated_record.get(field))

    for entry in response.get("history", []):
        entry["at"] = to_epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_epoch_ms(change[side])

    return response


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Verify the employee exists.
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    # Validate month format: YYYY-MM.
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "month must be in YYYY-MM format")

    if month_start.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be in YYYY-MM format")

    next_month = (
        month_start.replace(
            year=month_start.year + 1, month=1
        )
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )

    # Calculate working weekdays, starting from the employee's join date.
    joined_on = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    first_day = max(month_start, joined_on)

    working_days = 0
    current_day = first_day
    while current_day < next_month:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Use MongoDB aggregation to summarize attendance records.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
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
                                    {
                                        "$lte": [
                                            {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            5
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
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

    result = list(db.attendance_logs.aggregate(pipeline))
    summary = result[0] if result else {}

    present_days = summary.get("present_days", 0)
    attendance_pct = (
        round(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate the requested month.
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "month must be in YYYY-MM format")

    if month_start.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be in YYYY-MM format")

    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )

    # Employees who joined by the end of the requested month.
    employee_match = {
        "joined_on": {"$lt": next_month.isoformat()}
    }
    if department:
        employee_match["department"] = department

    # Aggregate employees with attendance records, preserving employees
    # who have no logs for the month.
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
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
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [
                                        {"$isoDayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$logs.date",
                                                "onError": None,
                                            }
                                        }},
                                        5,
                                    ]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [
                            {"$size": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]},
                                }
                            }},
                            0,
                        ]},
                        {"$avg": {
                            "$filter": {
                                "input": "$work_hours_values",
                                "as": "hours",
                                "cond": {"$ne": ["$$hours", None]},
                            }
                        }},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "items": [
            {
                **item,
                "avg_work_hours": (
                    round(item["avg_work_hours"], 2)
                    if item["avg_work_hours"] is not None
                    else None
                ),
            }
            for item in items
            if item["headcount"] > 0
        ],
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    # Validate month.
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "month must be in YYYY-MM format")

    if month_start.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be in YYYY-MM format")

    if not 1 <= limit <= 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    next_month = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    )

    # Join logs with employees so orphaned logs are ignored.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: ties share a rank; next rank is skipped.
    ranked = []
    previous_total = None
    current_rank = 0

    for position, row in enumerate(rows, start=1):
        total = row["total_late_minutes"]

        if total != previous_total:
            current_rank = position
            previous_total = total

        row["rank"] = current_rank
        ranked.append(row)

    # Apply limit to the rank, retaining all ties at the cutoff rank.
    items = [row for row in ranked if row["rank"] <= limit]

    return {
        "month": month,
        "items": items,
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    department_exists = db.employees.find_one(
        {"department": department},
        {"_id": 1},
    )

    if not department_exists:
        raise HTTPException(404, "unknown department")

    start_dt = datetime.combine(
        from_date, datetime.min.time(), tzinfo=timezone.utc
    )

    end_dt = datetime.combine(
        to_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    pipeline = [
        {"$match": {"department": department}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "joined_on": 1,
            }
        },
        {
            "$set": {
                "trend_date": start_dt,
            }
        },
        {
            "$densify": {
                "field": "trend_date",
                "partitionByFields": ["emp_code"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$set": {
                "trend_date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$trend_date",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "attendance_date": "$trend_date_str",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$eq": [
                                            "$date",
                                            "$$attendance_date",
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "daily_attendance",
            }
        },
        {
            "$unwind": {
                "path": "$daily_attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$trend_date",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$joined_on", "$trend_date_str"]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$daily_attendance.status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$daily_attendance.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$daily_attendance.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$_id"},
                        5,
                    ]
                },
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {
                                    "$lte": [
                                        {"$isoDayOfWeek": "$_id"},
                                        5,
                                    ]
                                },
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = float(
                Decimal(str(item["moving_avg_7d"])).quantize(
                    Decimal("0.0001"),
                    rounding=ROUND_HALF_UP,
                )
            )

    return {
        "department": department,
        "items": items,
    }


#   GET   /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required for employee_monthly",
            )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                422,
                "month is required for department_summary",
            )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                422,
                "month is required for late_leaderboard",
            )

    elif endpoint == "department_trend":
        if not department or from_date is None or to_date is None:
            raise HTTPException(
                422,
                "department, from, and to are required for department_trend",
            )
        if to_date < from_date:
            raise HTTPException(422, "to must be on or after from")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")

    if endpoint == "attendance_list":
        q = {}

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

        explain_result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == "employee_monthly":
        #month_start = datetime.strptime(month, "%Y-%m").date()
        try:
            month_start = datetime.strptime(month, "%Y-%m").date()
            if month_start.strftime("%Y-%m") != month:
                raise ValueError("Invalid month format")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must be a valid month in YYYY-MM format",
            )

        if month_start.strftime("%Y-%m") != month:
            raise HTTPException(422, "month must be in YYYY-MM format")

        next_month = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
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
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                {
                                                    "$isoDayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                5,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
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

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == "department_summary":
        month_start = datetime.strptime(month, "%Y-%m").date()

        if month_start.strftime("%Y-%m") != month:
            raise HTTPException(422, "month must be in YYYY-MM format")

        next_month = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )

        employee_match = {
            "joined_on": {"$lt": next_month.isoformat()}
        }
        if department:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$gte": ["$date", month_start.isoformat()]},
                                        {"$lt": ["$date", next_month.isoformat()]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                {
                                                    "$isoDayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date",
                                                            "onError": None,
                                                        }
                                                    }
                                                },
                                                5,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "work_hours_values": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {
                                "$gt": [
                                    {
                                        "$size": {
                                            "$filter": {
                                                "input": "$work_hours_values",
                                                "as": "hours",
                                                "cond": {"$ne": ["$$hours", None]},
                                            }
                                        }
                                    },
                                    0,
                                ]
                            },
                            {
                                "$avg": {
                                    "$filter": {
                                        "input": "$work_hours_values",
                                        "as": "hours",
                                        "cond": {"$ne": ["$$hours", None]},
                                    }
                                }
                            },
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_result,
        }

    elif endpoint == "late_leaderboard":
        month_start = datetime.strptime(month, "%Y-%m").date()

        if month_start.strftime("%Y-%m") != month:
            raise HTTPException(422, "month must be in YYYY-MM format")

        if not 1 <= limit <= 50:
            raise HTTPException(422, "limit must be between 1 and 50")

        next_month = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == "department_trend":
        if to_date < from_date:
            raise HTTPException(422, "to must be on or after from")

        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")

        department_exists = db.employees.find_one(
            {"department": department},
            {"_id": 1},
        )

        if not department_exists:
            raise HTTPException(404, "unknown department")

        start_dt = datetime.combine(
            from_date, datetime.min.time(), tzinfo=timezone.utc
        )
        end_dt = datetime.combine(
            to_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        # Reuse the exact pipeline from department_trend().
        pipeline = [
            {"$match": {"department": department}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": 1,
                    "joined_on": 1,
                }
            },
            {"$set": {"trend_date": start_dt}},
            {
                "$densify": {
                    "field": "trend_date",
                    "partitionByFields": ["emp_code"],
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_dt],
                    },
                }
            },
            {
                "$set": {
                    "trend_date_str": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$trend_date",
                            "timezone": "UTC",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "employee_code": "$emp_code",
                        "attendance_date": "$trend_date_str",
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code",
                                            ]
                                        },
                                        {
                                            "$eq": [
                                                "$date",
                                                "$$attendance_date",
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "daily_attendance",
                }
            },
            {
                "$unwind": {
                    "path": "$daily_attendance",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$trend_date",
                    "headcount": {
                        "$sum": {
                            "$cond": [
                                {"$lte": ["$joined_on", "$trend_date_str"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$daily_attendance.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$daily_attendance.half_day",
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$daily_attendance.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$set": {
                    "is_working_day": {
                        "$lte": [{"$isoDayOfWeek": "$_id"}, 5]
                    },
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$lte": [
                                            {"$isoDayOfWeek": "$_id"},
                                            5,
                                        ]
                                    },
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
                    "sortBy": {"_id": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_id",
                            "timezone": "UTC",
                        }
                    },
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {"$ne": ["$attendance_rate", None]},
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$attendance_rate",
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_result,
        }



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend

# --------------------------------------------------------------------------- #
