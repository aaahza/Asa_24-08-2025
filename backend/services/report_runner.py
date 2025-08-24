# backend/services/report_runner.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple, Dict
import os
import math
import concurrent.futures
import traceback

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models import Poll, BusinessHour, StoreTimezone, Report
from backend.db import SessionLocal  

import os
MAX_WORKERS = min(4, max(1, (os.cpu_count() or 2)))  

DEFAULT_TZ = "America/Chicago"

@dataclass
class TimeInterval:
    start: datetime  
    end: datetime    

    def overlap_seconds(self, other: "TimeInterval") -> float:
        s = max(self.start, other.start)
        e = min(self.end, other.end)
        delta = (e - s).total_seconds()
        return max(0.0, delta)


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))


def _get_now_from_db(session: Session) -> datetime:
    max_ts = session.query(func.max(Poll.timestamp_utc)).scalar()
    if max_ts is None:
        return datetime.now(tz=ZoneInfo("UTC"))
    return _ensure_aware_utc(max_ts)


def _gather_store_ids(session: Session) -> List[str]:
    ids = set()
    for row in session.query(Poll.store_id).distinct():
        ids.add(row[0])
    for row in session.query(BusinessHour.store_id).distinct():
        ids.add(row[0])
    for row in session.query(StoreTimezone.store_id).distinct():
        ids.add(row[0])
    return sorted(ids)


def _fetch_polls_for_store(session: Session, store_id: str, window_start: datetime, window_end: datetime, margin: timedelta):
    q = (
        session.query(Poll.timestamp_utc, Poll.status)
        .filter(Poll.store_id == store_id)
        .filter(Poll.timestamp_utc >= (window_start - margin))
        .filter(Poll.timestamp_utc <= (window_end + margin))
        .order_by(Poll.timestamp_utc.asc())
    )
    results = [( _ensure_aware_utc(r[0]), r[1] ) for r in q.all()]
    return results


def _build_status_intervals_for_window(polls: List[Tuple[datetime, str]], window_start: datetime, window_end: datetime, margin: timedelta) -> List[Tuple[datetime, datetime, str]]:
    """
    Build status intervals that are well-defined for [window_start, window_end].
    We use midpoint interpolation between polls. For edges:
      - if there are polls before window, we extend the nearest poll to window_start
      - if there are polls after window, we extend the nearest poll to window_end
      - if there are no polls at all, return [] (caller will decide)
    """
    if not polls:
        return []

    n = len(polls)
    mids = []
    for i in range(n - 1):
        mids.append(polls[i][0] + (polls[i + 1][0] - polls[i][0]) / 2)

    intervals = []
    for i in range(n):
        start = mids[i-1] if i > 0 else (polls[i][0] - margin)
        end = mids[i] if i < n-1 else (polls[i][0] + margin)
        intervals.append((start, end, polls[i][1]))

    clipped = []
    window_ext_start = window_start - margin
    window_ext_end = window_end + margin
    for s,e,status in intervals:
        s2 = max(s, window_ext_start)
        e2 = min(e, window_ext_end)
        if e2 > s2:
            clipped.append((s2,e2,status))

    return clipped


def _get_timezone_for_store(session: Session, store_id: str) -> str:
    tz_row = session.query(StoreTimezone).filter(StoreTimezone.store_id == store_id).first()
    return tz_row.timezone_str if tz_row and tz_row.timezone_str else DEFAULT_TZ


def _get_business_intervals_for_window(session: Session, store_id: str, window_start_utc: datetime, window_end_utc: datetime, tz_str: Optional[str]) -> List[TimeInterval]:
    tzname = tz_str or DEFAULT_TZ
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)

    bh_rows = session.query(BusinessHour).filter(BusinessHour.store_id == store_id).all()
    if not bh_rows:
        return [TimeInterval(window_start_utc, window_end_utc)]

    bh_by_dow: Dict[int, List[BusinessHour]] = {}
    for bh in bh_rows:
        bh_by_dow.setdefault(bh.day_of_week, []).append(bh)

    local_start = window_start_utc.astimezone(tz)
    local_end = window_end_utc.astimezone(tz)
    start_date = (local_start.date() - timedelta(days=1))
    end_date = (local_end.date() + timedelta(days=1))

    biz_intervals = []
    cur_date = start_date
    while cur_date <= end_date:
        dow = cur_date.weekday()
        rows = bh_by_dow.get(dow, [])
        for bh in rows:
            s_local = datetime.combine(cur_date, bh.start_time_local)
            e_local = datetime.combine(cur_date, bh.end_time_local)
            if e_local <= s_local:
                e_local = e_local + timedelta(days=1)

            s_local = s_local.replace(tzinfo=tz)
            e_local = e_local.replace(tzinfo=tz)
            s_utc = s_local.astimezone(ZoneInfo("UTC"))
            e_utc = e_local.astimezone(ZoneInfo("UTC"))

            interval_start = max(s_utc, window_start_utc)
            interval_end = min(e_utc, window_end_utc)
            if interval_end > interval_start:
                biz_intervals.append(TimeInterval(interval_start, interval_end))
        cur_date += timedelta(days=1)

    if not biz_intervals:
        return []
    biz_intervals.sort(key=lambda x: x.start)
    merged = [biz_intervals[0]]
    for it in biz_intervals[1:]:
        last = merged[-1]
        if it.start <= last.end:
            last.end = max(last.end, it.end)
        else:
            merged.append(it)
    return merged


def _seconds_to_hours(sec: float) -> float:
    return sec / 3600.0


def _seconds_to_minutes(sec: float) -> float:
    return sec / 60.0


def compute_uptime_for_store_internal(store_id: str, now_utc: datetime) -> Dict[str, float]:
    """
    This function runs inside a thread; it creates its own DB session.
    """
    session = SessionLocal()
    try:
        window_hour_start = now_utc - timedelta(hours=1)
        window_day_start = now_utc - timedelta(hours=24)
        window_week_start = now_utc - timedelta(days=7)
        margin = timedelta(hours=12)  

        tz_str = _get_timezone_for_store(session, store_id)

        overall_start = window_week_start - timedelta(days=1)
        overall_end = now_utc + timedelta(hours=1)
        polls = _fetch_polls_for_store(session, store_id, overall_start, overall_end, margin)
        status_intervals = _build_status_intervals_for_window(polls, overall_start, overall_end, margin)

        biz_hour = _get_business_intervals_for_window(session, store_id, window_hour_start, now_utc, tz_str)
        biz_day = _get_business_intervals_for_window(session, store_id, window_day_start, now_utc, tz_str)
        biz_week = _get_business_intervals_for_window(session, store_id, window_week_start, now_utc, tz_str)

        def _compute(biz_intervals):
            total_business_seconds = sum((it.end - it.start).total_seconds() for it in biz_intervals)
            if total_business_seconds <= 0:
                return 0.0, 0.0

            if not status_intervals:
                return 0.0, total_business_seconds

            uptime_seconds = 0.0
            for biz in biz_intervals:
                for s_start, s_end, s_status in status_intervals:
                    overlap = TimeInterval(s_start, s_end).overlap_seconds(biz)
                    if overlap <= 0:
                        continue
                    if s_status.lower() == "active":
                        uptime_seconds += overlap
            downtime_seconds = total_business_seconds - uptime_seconds
            return max(0.0, uptime_seconds), max(0.0, downtime_seconds)

        u_hr, d_hr = _compute(biz_hour)
        u_day, d_day = _compute(biz_day)
        u_week, d_week = _compute(biz_week)

        return {
            "store_id": store_id,
            "uptime_last_hour_minutes": round(_seconds_to_minutes(u_hr), 2),
            "uptime_last_day_hours": round(_seconds_to_hours(u_day), 2),
            "uptime_last_week_hours": round(_seconds_to_hours(u_week), 2),
            "downtime_last_hour_minutes": round(_seconds_to_minutes(d_hr), 2),
            "downtime_last_day_hours": round(_seconds_to_hours(d_day), 2),
            "downtime_last_week_hours": round(_seconds_to_hours(d_week), 2),
        }
    finally:
        session.close()


def generate_report(session: Session, out_csv_path: str, report_id: Optional[str] = None) -> str:
    """
    Generate report writing CSV at out_csv_path.
    If report_id is supplied, update the Report row's percent_complete during run.
    """
    os.makedirs(os.path.dirname(out_csv_path), exist_ok=True)
    now_utc = _get_now_from_db(session)

    store_ids = _gather_store_ids(session)
    total = len(store_ids)
    if total == 0:
        # write empty csv
        df = pd.DataFrame([], columns=[
            "store_id",
            "uptime_last_hour_minutes",
            "uptime_last_day_hours",
            "uptime_last_week_hours",
            "downtime_last_hour_minutes",
            "downtime_last_day_hours",
            "downtime_last_week_hours",
        ])
        df.to_csv(out_csv_path, index=False)
        return out_csv_path

    rows = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, total))) as ex:
        futures = {ex.submit(compute_uptime_for_store_internal, sid, now_utc): sid for sid in store_ids}
    rows = []

    done = 0
    update_every = max(1, min(5, total // 20 or 1))  # at most every 5 stores, or proportional
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, total))) as ex:
        futures = {ex.submit(compute_uptime_for_store_internal, sid, now_utc): sid for sid in store_ids}
        for fut in concurrent.futures.as_completed(futures):
            sid = futures[fut]
            try:
                result = fut.result()
                rows.append(result)
            except Exception as e:
                print(f"Exception computing store {sid}: {e}")
                rows.append({
                    "store_id": sid,
                    "uptime_last_hour_minutes": 0.0,
                    "uptime_last_day_hours": 0.0,
                    "uptime_last_week_hours": 0.0,
                    "downtime_last_hour_minutes": 0.0,
                    "downtime_last_day_hours": 0.0,
                    "downtime_last_week_hours": 0.0,
                })
            done += 1

            if report_id and (done % update_every == 0 or done == total):
                try:
                    s2 = SessionLocal()
                    rep = s2.query(Report).get(report_id)
                    if rep:
                        rep.percent_complete = (done / total) * 100.0
                        s2.commit()
                except Exception:
                    pass
                finally:
                    try:
                        s2.close()
                    except Exception:
                        pass


    df = pd.DataFrame(rows, columns=[
        "store_id",
        "uptime_last_hour_minutes",
        "uptime_last_day_hours",
        "uptime_last_week_hours",
        "downtime_last_hour_minutes",
        "downtime_last_day_hours",
        "downtime_last_week_hours",
    ])
    df.to_csv(out_csv_path, index=False)

    if report_id:
        try:
            s3 = SessionLocal()
            rep = s3.query(Report).get(report_id)
            if rep:
                rep.percent_complete = 100.0
                rep.finished_at = datetime.now(tz=ZoneInfo("UTC"))
                s3.commit()
        except Exception:
            pass
        finally:
            try:
                s3.close()
            except Exception:
                pass

    return out_csv_path
