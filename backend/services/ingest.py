# backend/services/ingest.py
import csv
import os
from datetime import datetime, time, timezone
from dateutil import parser as date_parser
from sqlalchemy.orm import Session
from sqlalchemy import delete
from backend.models import Poll, BusinessHour, StoreTimezone

BULK_CHUNK = 10000
DEFAULT_TZ = "America/Chicago"


def _parse_utc_timestamp(ts_str: str) -> datetime:
    dt = date_parser.parse(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _parse_local_time(timestr: str) -> time:
    return date_parser.parse(timestr).time()


def _bulk_save_in_chunks(session: Session, objs):
    """Save objects in reasonable chunks to avoid memory / transaction explosion."""
    total = 0
    n = len(objs)
    for i in range(0, n, BULK_CHUNK):
        chunk = objs[i : i + BULK_CHUNK]
        session.bulk_save_objects(chunk)
        session.commit()
        total += len(chunk)
    return total


def load_polls(session: Session, csv_path: str, replace: bool = True):
    if replace:
        session.execute(delete(Poll))
        session.commit()

    rows = []
    with open(csv_path, newline='') as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            store_id = r.get("store_id")
            status = r.get("status")
            ts_raw = r.get("timestamp_utc")
            if not (store_id and status and ts_raw):
                continue
            try:
                ts = _parse_utc_timestamp(ts_raw)
            except Exception as e:
                print(f"Skipping poll row due to parse error: {r} -> {e}")
                continue
            rows.append(Poll(store_id=store_id, timestamp_utc=ts, status=status))

    if rows:
        added = _bulk_save_in_chunks(session, rows)
    else:
        added = 0
    print(f"Loaded {added} poll rows from {csv_path}")


def load_business_hours(session: Session, csv_path: str, replace: bool = True):
    if replace:
        session.execute(delete(BusinessHour))
        session.commit()

    rows = []
    with open(csv_path, newline='') as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            try:
                store_id = r.get("store_id")
                dow_raw = r.get("dayOfWeek") or r.get("day_of_week")
                start_raw = r.get("start_time_local")
                end_raw = r.get("end_time_local")
                if not (store_id and dow_raw and start_raw and end_raw):
                    continue
                dow = int(dow_raw)
                start_time = _parse_local_time(start_raw)
                end_time = _parse_local_time(end_raw)
                rows.append(
                    BusinessHour(
                        store_id=store_id,
                        day_of_week=dow,
                        start_time_local=start_time,
                        end_time_local=end_time,
                    )
                )
            except Exception as e:
                print(f"Skipping business-hour row {r} due to parse error: {e}")
                continue

    if rows:
        added = _bulk_save_in_chunks(session, rows)
    else:
        added = 0
    print(f"Loaded {added} business-hour rows from {csv_path}")


def load_timezones(session: Session, csv_path: str, replace: bool = True):
    if replace:
        session.execute(delete(StoreTimezone))
        session.commit()

    rows = []
    with open(csv_path, newline='') as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            store_id = r.get("store_id")
            tz = r.get("timezone_str") or DEFAULT_TZ
            if not store_id:
                continue
            rows.append(StoreTimezone(store_id=store_id, timezone_str=tz))

    if rows:
        added = _bulk_save_in_chunks(session, rows)
    else:
        added = 0
    print(f"Loaded {added} timezone rows from {csv_path}")
