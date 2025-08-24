# backend/main.py
import time
import os
from uuid import uuid4
from fastapi import FastAPI, BackgroundTasks
from sqlalchemy.exc import OperationalError
from backend.db import engine, SessionLocal  
from backend.models import Base, Report
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

def wait_for_db(max_retries: int = 30, delay_seconds: float = 1.0):
    """
    Retry connecting to the DB until success or max_retries reached.
    Uses conn.exec_driver_sql('SELECT 1') which works with SQLAlchemy 2.x.
    """
    for attempt in range(1, max_retries + 1):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            print("DB reachable")
            return
        except SQLAlchemyError as e:
            print(f"DB not reachable (attempt {attempt}/{max_retries}): {e}")
            time.sleep(delay_seconds)
    raise RuntimeError("Database not reachable after retries")

wait_for_db()
Base.metadata.create_all(bind=engine)

app = FastAPI()

@app.post('/trigger_report')
def trigger_report(background_tasks: BackgroundTasks):
    report_id = str(uuid4())
    db = SessionLocal()
    rep = Report(report_id=report_id, status='Running')
    db.add(rep)
    db.commit()
    out_path = f"/app/data/reports/{report_id}.csv"
    background_tasks.add_task(_run_and_store, report_id, out_path)
    return {"report_id": report_id}

def _run_and_store(report_id: str, out_path: str):
    from backend.services.report_runner import generate_report
    from backend.models import Report

    db = SessionLocal()
    try:
        generate_report(db, out_path, report_id=report_id)
        rep = db.query(Report).get(report_id)
        rep.status = 'Complete'
        rep.csv_path = out_path
        rep.percent_complete = 100.0
        rep.finished_at = datetime.now(tz=ZoneInfo("UTC"))
        db.commit()
    except Exception as e:
        try:
            rep = db.query(Report).get(report_id)
            if rep:
                rep.status = 'Failed'
                rep.percent_complete = 100.0
                rep.finished_at = datetime.now(tz=ZoneInfo("UTC"))
                db.commit()
        except Exception:
            pass
        print("Report generation failed:", e)
        raise
    finally:
        db.close()    
    
@app.get('/get_report')
def get_report(report_id: str):
    db = SessionLocal()
    rep = db.query(Report).get(report_id)
    if not rep:
        return {"status": "NotFound"}
    if rep.status != 'Complete':
        return {"status": rep.status}
    return {"status": "Complete", "csv_path": rep.csv_path}
