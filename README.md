# Employee Attendance & Analytics API

A FastAPI application for employee attendance management and analytics, backed by MongoDB.

## Features

- Employee creation and listing with pagination.
- Attendance punch-in, punch-out, and regularization.
- Monthly employee attendance analytics.
- Department summaries and attendance trends.
- Late-arrival leaderboard.
- MongoDB indexes and query-explain support.

## Requirements

- Python 3.10 or newer.
- MongoDB 6.0 or newer.

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` as environment variables for your MongoDB instance.

## Run

From the project root:

```bash
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs` to explore the API.

## Documentation

See `REVIEW.md` for the code review and `DECISIONS.md` for implementation decisions.