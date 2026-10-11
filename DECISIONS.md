# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for list and trend; `joined_on` for the summary. `attendance_logs`: unique `(emp_code, date)` (one record per day, race-proof punch-in, monthly, joins); `(date desc, emp_code)` for the list sort; `(status, date desc, emp_code)` for status filters; `(date, emp_code, late_minutes)` so the leaderboard is a covered scan; `(emp_code, punch_in desc)` for punch-out. I rejected a separate `late_minutes`-only index: it cannot narrow by month.

2. **Punch-in race.** Both requests compute the same date and try `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.

3. **Ties.** Ranking uses `$rank` (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned and the list can exceed `limit`. Rows are ordered by minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s logs per employee, so no logs means an empty array, not a missing employee.

5. **At 100x.** I would stop doing the per-employee `$lookup` and precompute monthly per-employee totals in a rollup collection, updated on punch/regularize.