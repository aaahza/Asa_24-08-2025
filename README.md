# Store Monitoring — README

## Contents

```
.
├─ docker-compose.yml
├─ backend/
│  ├─ main.py
│  ├─ db.py
│  ├─ models.py
│  ├─ services/
│  │  ├─ ingest.py
│  │  └─ report_runner.py
│  └─ scripts/
│     └─ load_csvs.py
├─ data/
│  ├─ store-monitoring-data/   # input CSVs (provided)
│  └─ reports/                 # output CSVs produced by the app
└─ README.md
```

---

## What this does

* Loads CSVs into Postgres:

  * `store_status.csv` → `polls` (polls are UTC timestamps + `active`/`inactive`)
  * `menu_hours.csv` → `business_hours` (local business hours: dayOfWeek, start/end times)
  * `timezones.csv` → `store_timezones` (IANA strings like `America/Chicago`)
* Exposes two endpoints:

  * `POST /trigger_report` → returns `{ "report_id": "<uuid>" }` and starts background job
  * `GET  /get_report?report_id=<uuid>` → returns `{"status":"Running"}` or `{"status":"Complete","csv_path":"/app/data/reports/<id>.csv"}`
* Report CSV columns:

  ```
  store_id,
  uptime_last_hour_minutes,
  uptime_last_day_hours,
  uptime_last_week_hours,
  downtime_last_hour_minutes,
  downtime_last_day_hours,
  downtime_last_week_hours
  ```

---

## Architecture & purpose of main components (plain language)

* **Docker / docker-compose** — runs the whole stack with one command (Postgres, FastAPI app, pgAdmin).
* **Postgres** — persistent store for polls/business hours/timezones and `reports` status rows.
* **FastAPI + Uvicorn** — web API server and auto Swagger UI (`/docs`) for quick testing.
* **SQLAlchemy ORM** — maps Python classes to database tables and manages DB connections.
* **pgAdmin** — browser GUI you can use to inspect the DB and tables.
* **Pandas** — used to write the final CSV file (simple and reliable).
* **ZoneInfo (stdlib)** — converts local business hours to UTC for correct overlap arithmetic.

---

## How the uptime/downtime logic works (important)

1. **Interpolation (midpoint)**
   Polls are discrete (≈1/hour). We turn consecutive polls into continuous status intervals by using the midpoint between timestamps as the boundary. This is simple and robust: a poll at `t0` applies from midpoint(previous, t0) to midpoint(t0, next).

2. **Business hours handling**
   Business hours are stored with a `dayOfWeek` and local `start_time_local` / `end_time_local`. We:

   * Convert the relevant local-day intervals to UTC using the store timezone,
   * Handle midnight-crossing schedules (e.g., 22:00 — 02:00),
   * Intersect business-hour intervals with the requested window (last hour / last 24 hours / last 7 days).

3. **Uptime calculation**
   For each business interval, compute overlap (seconds) with `active` status intervals. Sum active seconds → uptime. Downtime = total business seconds − uptime.

4. **Defaults / edge cases**

   * No business hours row → treat as open 24×7.
   * Missing timezone → assume `America/Chicago`.
   * No polls for a store → current code conservatively treats as **full downtime** (0 uptime). You can change this behavior if you prefer a different default.

---

## Setup / run (Docker-first)

> Works on Linux/macOS/Windows with Docker + docker-compose installed. All commands assume repository root.

### 1) Start services

```bash
# build and start (shows logs)
sudo docker-compose up --build
# or to run in background
sudo docker-compose up -d --build
```

This starts three containers:

* `db` (Postgres) — port `5432` inside container (mapped to host by compose)
* `backend` (FastAPI) — serves on port `8000` (host `http://localhost:8000`)
* `pgadmin` (optional GUI) — default mapping in this repo: `http://localhost:5050`

> If you have permission issues with `docker-compose`, run as your local user or use `sudo` as shown.

### 2) Load CSVs into Postgres (one-time per start)

The repo includes a small loader script that truncates and loads CSVs. Run it after containers are up:

```bash
sudo docker-compose exec backend python /app/backend/scripts/load_csvs.py --dir /app/data/store-monitoring-data
```

Expected prints:

```
Loaded 1849837 poll rows from /app/data/store-monitoring-data/store_status.csv
Loaded 35457 business-hour rows from /app/data/store-monitoring-data/menu_hours.csv
Loaded 4559 timezone rows from /app/data/store-monitoring-data/timezones.csv
```

Validate in the DB:

```bash
# run psql inside db container (example creds used in this project)
sudo docker-compose exec db psql -U loop -d store_monitoring -c "SELECT count(*) FROM polls;"
```

### 3) Use the APIs (Swagger UI)

* Swagger / OpenAPI UI: `http://localhost:8000/docs`
  Use it to call `POST /trigger_report` and `GET /get_report`.

* Example `curl`:

```bash
# trigger a report
RID=$(curl -s -X POST http://localhost:8000/trigger_report | jq -r .report_id)
echo "Report ID: $RID"

# poll status every 3 seconds
while true; do
  curl -s "http://localhost:8000/get_report?report_id=$RID" | jq .
  sleep 3
done
```

When complete, the response will include the CSV path like:

```json
{"status":"Complete","csv_path":"/app/data/reports/<report_id>.csv"}
```

Generated CSVs are written to the host folder: `./data/reports/`.

---

## Where files appear on disk

* Inside container: `/app/data/reports/<report_id>.csv`
* On host (mounted): `./data/reports/<report_id>.csv`

To view a generated file on the host:

```bash
ls -lh ./data/reports
head -n 20 ./data/reports/<report_id>.csv
```

---

## Common issues & diagnostics (what to try first)

### Backend cannot reach DB on startup

* Symptoms: `DB not reachable` or `could not translate host name "db" to address`
* Fixes:

  * Make sure all containers are up: `sudo docker-compose ps`
  * If `db` is still initializing, wait and try again — backend has a retry loop.
  * Restart stack: `sudo docker-compose down && sudo docker-compose up --build`

### `NameError: func` / missing imports

* Cause: using `func.now()` or similar in `main.py` without `from sqlalchemy import func`.
* Fix: either import `func` or prefer to use `datetime.now(tz=ZoneInfo("UTC"))` when writing application-level timestamps.

### `QueuePool limit of size X overflow Y reached`

* Cause: too many simultaneous DB connections (report runner + web requests + pgAdmin).
* Fixes:

  * Reduce parallelism in your code (set worker count to 1).
  * Increase SQLAlchemy pool size or Postgres max connections in `docker-compose.yml` environment (if needed).
  * Ensure sessions are closed properly (use `session.close()` or context manager).

### pgAdmin not showing tables

* Ensure you added a server in pgAdmin pointing to the `db` container with the correct credentials and database name (`store_monitoring` in this repo). Use the server address `db` if connecting from other containers, or `localhost` + port if connecting from your host (and port is mapped).

---

## How to validate correctness (simple checks)

1. After loading CSVs: check counts via `psql` (sample shown earlier).
2. Trigger a small report using Swagger.
3. Open one store’s polls directly in the DB and visually verify:

   * Get the latest N polls for that store:

     ```sql
     SELECT timestamp_utc, status FROM polls
     WHERE store_id = '<SOME_STORE_ID>' ORDER BY timestamp_utc DESC LIMIT 10;
     ```
   * Manually compute overlap for a short window and confirm the CSV values are reasonable.

---

## Things I recommend improving before submitting (real, prioritized)

1. **Streaming CSV output** — write the CSV row-by-row so memory does not spike for many stores.
2. **Progress updates** — store progress in the `reports` row periodically (e.g., every 100 stores) and return percent complete. Avoid excessive DB writes.
3. **Unit tests** — add pytest tests for:

   * day boundary crossing business hours,
   * single poll behavior,
   * missing timezone / missing business-hours assumptions.
4. **Graceful concurrency** — if you parallelize per-store computation, use a worker pool with a small size (1–4) and ensure sessions are short-lived.
5. **Better handling of no-polls** — decide whether to treat as unknown, fully-down, or assume active; document it.
6. **Add health endpoint** — `/health` that quickly checks the DB connection and returns useful diagnostics for CI.

---

## Useful commands

```bash
# show logs (follow)
sudo docker-compose logs -f backend
sudo docker-compose logs -f db

# rebuild backend only
sudo docker-compose build backend
sudo docker-compose up -d backend

# run CSV loader
sudo docker-compose exec backend python /app/backend/scripts/load_csvs.py --dir /app/data/store-monitoring-data

# run psql inside db container
sudo docker-compose exec db psql -U loop -d store_monitoring -c "SELECT count(*) FROM polls;"

# trigger a report (curl)
curl -X POST http://localhost:8000/trigger_report

# check report
curl "http://localhost:8000/get_report?report_id=<uuid>"
```
