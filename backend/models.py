from sqlalchemy import Column, Integer, String, DateTime, Time, SmallInteger, Text, Index, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Poll(Base):
    __tablename__ = 'polls'
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(String, index=True, nullable=False)
    timestamp_utc = Column(DateTime(timezone=True), index=True)
    status = Column(String)

    __table_args__ = (
        Index('ix_polls_store_ts', 'store_id', 'timestamp_utc'),
    )


class BusinessHour(Base):
    __tablename__ = 'business_hours'
    id = Column(Integer, primary_key=True)
    store_id = Column(String, index=True, nullable=False)
    day_of_week = Column(SmallInteger)  # 0=Mon
    start_time_local = Column(Time)
    end_time_local = Column(Time)

    __table_args__ = (
        Index('ix_bh_store_dow', 'store_id', 'day_of_week'),
    )


class StoreTimezone(Base):
    __tablename__ = 'store_timezones'
    store_id = Column(String, primary_key=True)
    timezone_str = Column(String)


class Report(Base):
    __tablename__ = 'reports'
    report_id = Column(String, primary_key=True)
    status = Column(String, nullable=False)
    percent_complete = Column(Float, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    csv_path = Column(Text, nullable=True)
