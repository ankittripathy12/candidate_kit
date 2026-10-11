# Employee Attendance & Analytics API

A REST API for tracking employee attendance (punch-in / punch-out, corrections with an audit trail) and for monthly analytics. Built with **FastAPI** and **MongoDB**.

## Features
- Employee management with validation and a unique `emp_code`.
- Punch-in / punch-out with automatic late, overtime, work-hours and half-day calculation.
- Regularization (manual correction) with a full change history per record.
- Paginated, filterable attendance listing.
- Analytics built as MongoDB aggregation pipelines: employee monthly summary, department summary, late leaderboard, department trend.
- `/admin/explain/{endpoint}` shows the MongoDB query plan behind each read endpoint.

## Requirements
- Python 3.10+
- MongoDB 6.0+ (5.0 is the minimum: the analytics use `$setWindowFields`)

## Setup

```bash
# 1. create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate            # Git Bash on Windows: source .venv/Scripts/activate
                                     # PowerShell: .venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -r requirements.txt
```

### Configuration
The app reads two environment variables. Set them in the terminal, or put them in a `.env` file next to `requirements.txt` (real environment variables take priority over `.env`).

| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

```
# .env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Quick local MongoDB with Docker: `docker run -d -p 27017:27017 --name mongo mongo:7`

### Run
```bash
uvicorn app.main:app --port 8000
```
- Health check: http://localhost:8000/health
- Interactive docs (Swagger): http://localhost:8000/docs

Indexes are created automatically at startup.

Optional sample data: `python sample_seed.py`

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + MongoDB ping (503 if the database is down) |
| POST | `/employees` | Create an employee (409 on duplicate `emp_code`) |
| GET | `/employees` | List employees (`department`, `page`, `page_size`) |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Close the latest open record |
| GET | `/attendance` | List records (`emp_code`, `date_from`, `date_to`, `status`, paging) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record (audit trail in `history`) |
| GET | `/analytics/employees/{emp_code}/monthly?month=YYYY-MM` | Employee monthly summary |
| GET | `/analytics/departments/summary?month=YYYY-MM[&department=]` | Per-department totals |
| GET | `/analytics/leaderboard/late?month=YYYY-MM[&limit=][&department=]` | Late-minutes ranking |
| GET | `/analytics/departments/{department}/trend?from=&to=` | Daily attendance rate + 7-day moving average (max 92 days) |
| GET | `/admin/explain/{endpoint}` | MongoDB `explain` for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend` |

Common status codes: `201` created, `404` unknown employee/record, `409` conflict (duplicate or already punched out), `422` validation error, `503` database unavailable.

### Example
```bash
curl -X POST localhost:8000/employees -H 'content-type: application/json' -d '{
  "emp_code":"EMP0101","name":"Asha","email":"asha@example.com",
  "department":"Eng","joined_on":"2026-01-05"}'

curl -X POST localhost:8000/attendance/punch-in -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0101"}'

curl -X POST localhost:8000/attendance/punch-out -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0101"}'
```
Timestamps are **epoch milliseconds** (integers). Seconds, floats and strings are rejected with 422.

## Business rules
- **Attendance date** is the calendar date in IST (UTC+5:30). For overnight shifts (`shift_end <= shift_start`), a punch-in before `shift_end` belongs to the previous day.
- **Timestamps** are truncated to whole seconds.
- **Late:** punch-in strictly more than 10 minutes after shift start; `late_minutes` = whole minutes since shift start, otherwise 0.
- **Overtime:** whole minutes after shift end, only when at least 30, otherwise 0.
- **Work hours:** seconds / 3600, rounded to 2 decimals half-up.
- **Half day:** `work_hours < 4.5`.
- **Punch-out** must be after the punch-in and within 24 hours of it.
- **Present** statuses are `PRESENT`, `WFH`, `ON_DUTY`; a half day counts as 0.5 present day, and only Mon-Fri count towards `present_days` and `attendance_pct`.
- **Rounding** of reported numbers is half-up.

## Design notes
- **Race-proof writes:** a unique index on `(emp_code, date)` guarantees one record per employee per day, so concurrent punch-ins resolve to one 201 and the rest 409. Punch-out and regularize use conditional updates (compare-and-set).
- **Indexes:** unique `emp_code`; `(department, emp_code)`; `joined_on`; unique `(emp_code, date)`; `(date desc, emp_code)`; `(status, date desc, emp_code)`; `(date, emp_code, late_minutes)`; `(emp_code, punch_in desc)`. List and analytics queries are served by these indexes.
- **Analytics** run inside MongoDB (`$group`, `$lookup`, `$setWindowFields` with `$rank` and a 7-row moving average); Python only formats and rounds results.
- **Ties** in the leaderboard share a rank (1, 2, 2, 4) and the limit applies to the rank.

More detail: [`DECISIONS.md`](DECISIONS.md) (design choices) and [`REVIEW.md`](REVIEW.md) (defects found in the starter code).

## Project layout
```
app/
  __init__.py
  main.py          # the whole application
requirements.txt
README.md
REVIEW.md
DECISIONS.md
```

## Verification status
- [x] Business rules, validation, punch-in/out, regularize and listing tested with `mongomock`.
- [x] Analytics pipelines and `/admin/explain/*` run against a real MongoDB (verified manually through `/docs`).

## Troubleshooting
- **`/` returns 404:** expected, the API has no root page. Use `/docs` or `/health`.
- **`/health` returns 503 or startup is slow:** MongoDB is unreachable; check `MONGO_URI` and that MongoDB is running (Atlas: check the IP allow-list).
- **404 on punch-in:** the `emp_code` does not exist; create it first with `POST /employees`.
- **422 on create:** `emp_code` must look like `EMP0001`, dates `YYYY-MM-DD`, shifts `HH:MM`, and `shift_start` must differ from `shift_end`.