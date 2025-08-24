# backend/scripts/load_csvs.py
import argparse
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.db import DATABASE_URL, SessionLocal, engine
from backend.models import Base
from backend.services.ingest import load_polls, load_business_hours, load_timezones

def main(data_dir: str, replace: bool = True):
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        polls_csv = os.path.join(data_dir, "store_status.csv")
        hours_csv = os.path.join(data_dir, "menu_hours.csv")
        tz_csv = os.path.join(data_dir, "timezones.csv")

        if os.path.exists(polls_csv):
            load_polls(session, polls_csv, replace=replace)
        else:
            print("No store_status.csv found at", polls_csv)

        if os.path.exists(hours_csv):
            load_business_hours(session, hours_csv, replace=replace)
        else:
            print("No menu_hours.csv found at", hours_csv)

        if os.path.exists(tz_csv):
            load_timezones(session, tz_csv, replace=replace)
        else:
            print("No timezones.csv found at", tz_csv)

    finally:
        session.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="path to folder containing CSV files (inside container)")
    parser.add_argument("--no-replace", action="store_true", help="do not truncate existing tables before load")
    args = parser.parse_args()
    main(args.dir, replace=not args.no_replace)
