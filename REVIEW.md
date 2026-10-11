# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace test is done on *floored minutes* (`minutes > 10`), so 09:40:01 to 09:40:59 gives 0 instead of 10 (R2 says strictly more than 10:00). | Shift 09:30, punch 09:40:01, expect 10, get 0. | Compare whole seconds: `seconds > 600`, then `seconds // 60`. |
| 2 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour=..)` in the punch's own (UTC/naive) zone, not IST, and on the punch's own calendar day. Wrong by 5.5 h; wrong again for overnight shifts. | Punch 09:28 IST (03:58 UTC) -> start becomes 09:30 UTC. | Shift start is an aware IST datetime anchored on the attendance date (`shift_window`). |
| 3 | `compute_work_hours` | Python `round()` on a float: not half-up (banker's and binary-float effects). Sub-second parts are not truncated. | 9.125 h style values round to 9.12. | Integer seconds -> `Decimal` -> `ROUND_HALF_UP` (R4). |
| 4 | `compute_overtime` | No 30-minute minimum (R3); ignores overnight shifts (end is on the next day). | 5 min over shift end returns 5; 22:00-06:00 shift gives nonsense. | `shift_window` puts an overnight end on the next day; floor minutes; return 0 below 30. |
| 5 | `create_employee` | Check-then-insert race; no unique index, so concurrent POSTs create duplicates. | Fire 20 parallel creates: more than one 201. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 6 | `create_employee` | `created_at = datetime.now()` is naive local time (PyMongo treats it as UTC) and is returned as a datetime, not epoch ms. | Server not on UTC: stored instant is off; response has an ISO string. | UTC, millisecond precision; converted to epoch ms at the boundary. |
| 7 | `EmployeeIn` | No validation: bad `emp_code`, email, `"25:99"` shifts, `shift_start == shift_end`, `joined_on: "abc"`. Bad shift strings later crash helpers with a 500. | POST with `shift_start: "9"`. | Pydantic patterns/lengths, real-date check, shift-differs validator -> 422. |
| 8 | `list_employees` | `skip = page * page_size` skips the first page; `total` counts the whole collection, ignoring the filter; no sort (unstable paging); `page=0` gives a negative skip (500); `page_size` unbounded. | `page=1` returns the 2nd page; `?department=X` total is wrong. | `(page-1)*page_size`, filtered `count_documents`, sort by `emp_code`, `page>=1`, `1<=page_size<=100`. |
| 9 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` -> 500. | Punch-in for `EMP9999`. | 404. |
| 10 | `punch_in` | Timestamp handling: `fromtimestamp` uses server-local naive time; seconds-looking values accepted (1970 dates); `if body.punched_at` treats 0 as missing; milliseconds not truncated; date taken from server-local date, not IST; overnight rule missing. | Send `punched_at` in seconds; punch at 23:30 UTC. | Strict epoch-ms model (422 otherwise), UTC datetimes truncated to seconds, IST date via `attendance_date` (R1). |
| 11 | `punch_in` | Check-then-insert race: two simultaneous requests both pass `find_one` and both get 201. | Parallel identical punch-ins. | Unique index (`emp_code`,`date`); `DuplicateKeyError` -> 409. |
| 12 | `punch_in` | `status` is not validated (ABSENT, LEAVE, garbage accepted). | `status: "XYZ"`. | `Literal["PRESENT","WFH","ON_DUTY"]` -> 422. |
| 13 | `punch_in` | Response leaks the ObjectId as `id`, returns raw datetimes. | Look at the response. | `record_out` returns only contract fields, instants as epoch ms. |
| 14 | `list_attendance` | Loads every matching document, sorts and slices in Python (100k rows per request); sort lacks the `emp_code` tie-break; no indexes; `page=0` gives a negative slice; unbounded `page_size`; bad dates / `date_from > date_to` / bad `status` are not rejected; legacy docs without `history`/`half_day` are returned as-is. | Slow at 100k docs; explain shows COLLSCAN. | Server-side sort/skip/limit, supporting indexes, validation, `count_documents`, defaults for missing fields. |
| 15 | `/health` | Always "ok", even if MongoDB is down. | Stop MongoDB, still 200. | `ping`, 503 on failure. |
| 16 | `MongoClient` setup | Naive datetimes everywhere; no index creation at startup; default 30 s server-selection wait. | Datetimes without tzinfo. | `tz_aware=True`, `ensure_indexes()` in lifespan, 10 s timeout. |

## Looked at and judged fine
* `load_dotenv()` does not override real environment variables (its default), so the "real env wins" rule holds.
* `IST = timezone(timedelta(hours=5, minutes=30))` is correct (India has no DST).
* Plain `def` endpoints run in FastAPI's thread pool; PyMongo's client is thread-safe, so no change needed.
* Projection `{"_id": 0}` in `list_employees` was fine in itself; I replaced it only to convert `created_at`.