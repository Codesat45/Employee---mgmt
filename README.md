# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

Start MongoDB (for example, `docker run -d --name attendance-mongo -p 127.0.0.1:27017:27017 -v attendance-mongo-data:/data/db mongo:7`),
then from this directory install dependencies and start the app:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` can be set in the environment; defaults are `mongodb://localhost:27017` and
`attendance_db`. `GET /health` checks database readiness. The app creates indexes on startup.

## Optional sample data

In the original candidate kit, `sample_seed.py` can load the supplied local fixtures. It permanently deletes all
documents in the configured `employees` and `attendance_logs` collections first. Use only a disposable local database.
Seeding requires an explicit flag:

```powershell
$env:MONGO_DB = "attendance_sample"
python sample_seed.py --confirm-destructive
```

The sample JSON files are local fixtures and are excluded from Git so they are not submitted as data dumps. Do not run
the seeder against a shared or production database.

## Tests

The automated integration tests use a randomly named database beginning with
`attendance_api_tests_<process-id>` and drop only that database at the end. They require MongoDB 6.0+ and can be run
with:

```powershell
python -m unittest discover -s tests -v
```

Set `MONGO_TEST_URI` to choose a test MongoDB server. The normal `MONGO_URI` / `MONGO_DB` settings are not used by the
test suite.

## Submission

Submit `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README in a public Git repository. Keep
the supplied `openapi.yaml`, `DATA_MODEL.md`, and `PROBLEM_STATEMENT.docx` available as contract/reference material.
Do not submit `.env`, secrets, `.venv/`, Python caches, sample-data dumps, or a Dockerfile.
