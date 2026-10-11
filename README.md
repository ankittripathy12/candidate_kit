# Employee Attendance & Analytics API

FastAPI + MongoDB 6.0+. All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `REVIEW.md` lists the starter defects; `DECISIONS.md` answers the five questions.

**Status / honesty note.** Every endpoint is implemented. In my sandbox no MongoDB server was available, so the rules (R1-R5), validation, status codes, punch-in/out, regularize and list were tested with `mongomock`; the aggregation pipelines (monthly, summary, leaderboard, trend, explain) were built and BSON-encoded but **not executed against a real MongoDB**. Please run them once against Mongo 6/7. Pipelines use `$setWindowFields` (`$rank`, moving average), so MongoDB 5.0+ is needed; the trend gap-filling uses `$range` + `$dateAdd` instead of `$densify`.