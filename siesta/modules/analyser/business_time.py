"""Working-time clock for analyser durations.

A business calendar replaces each event's ``start_timestamp`` with a cumulative
*working* clock - seconds of working time elapsed since a fixed origin - so every
duration the analysers already compute as a timestamp difference (loop spans,
adjacent-pair gaps) becomes working time without touching their logic. Time
outside working hours, on weekends and on holidays does not advance the clock:
an event at 18:00 sits at that day's closing time, a Saturday event at Friday's
closing time, a 06:00 event at that day's opening time. Events are never
dropped; ``off_hours`` marks the ones that fall outside the working window so
callers can report them separately.

Calendar dict::

    {"holidays": ["2026-01-01", ...],   # extra non-working dates (ISO)
     "work_start": "07:45", "work_end": "15:15",
     "weekend_days": [5, 6]}            # Python weekday(): Monday=0

Timestamps are read as wall-clock seconds in UTC fields (no timezone shift), i.e.
an epoch value whose UTC rendering is the local time the event happened.
"""

from datetime import date, timedelta
from typing import Callable, Optional

from pyspark.sql import DataFrame, functions as F

_EPOCH = date(1970, 1, 1)
_DAY = 86400


def _hhmm_to_seconds(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def working_window(calendar: dict) -> tuple[int, int]:
    """(start, end) of the working window, in seconds since midnight."""
    start = _hhmm_to_seconds(calendar.get("work_start", "07:45"))
    end = _hhmm_to_seconds(calendar.get("work_end", "15:15"))
    if end <= start:
        raise ValueError(f"work_end must be after work_start, got {calendar!r}")
    return start, end


def working_day_seconds(calendar: dict) -> int:
    start, end = working_window(calendar)
    return end - start


def format_working_duration(seconds: Optional[float], day_seconds: int) -> str:
    """Like duration_format.format_duration, but days and weeks are *working*
    days (``day_seconds`` long) and working weeks (5 working days): ``wd``/``ww``.
    """
    if seconds is None:
        return "n/a"
    seconds = float(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{sign}{seconds:.1f}s"
    if seconds < 3600:
        return f"{sign}{seconds / 60:.1f}m"
    if seconds < day_seconds:
        return f"{sign}{seconds / 3600:.1f}h"
    if seconds < 5 * day_seconds:
        return f"{sign}{seconds / day_seconds:.1f}wd"
    return f"{sign}{seconds / (5 * day_seconds):.1f}ww"


def duration_formatter(calendar: dict) -> Callable[[Optional[float]], str]:
    day_seconds = working_day_seconds(calendar)
    return lambda seconds: format_working_duration(seconds, day_seconds)


def apply_business_calendar(events_df: DataFrame, calendar: dict) -> DataFrame:
    """Return ``events_df`` with ``start_timestamp`` on the working clock and a
    boolean ``off_hours`` column (non-working day, or before/after the window).
    """
    work_start, work_end = working_window(calendar)
    day_len = work_end - work_start
    weekend = set(calendar.get("weekend_days", [5, 6]))
    holidays = {date.fromisoformat(d) for d in calendar.get("holidays", [])}

    bounds = events_df.agg(F.min("start_timestamp").alias("lo"),
                           F.max("start_timestamp").alias("hi")).first()
    if bounds["lo"] is None:
        return events_df.withColumn("off_hours", F.lit(False))

    # One row per calendar day in the data's range: working days strictly before
    # it (the clock value at its opening) and whether it is itself a working day.
    rows, worked = [], 0
    for day in range(int(bounds["lo"]) // _DAY, int(bounds["hi"]) // _DAY + 1):
        current = _EPOCH + timedelta(days=day)
        is_working = current.weekday() not in weekend and current not in holidays
        rows.append((day, worked, is_working))
        worked += is_working
    lookup = events_df.sparkSession.createDataFrame(
        rows, "_bt_day long, _bt_days_before long, _bt_working boolean")

    ts_type = events_df.schema["start_timestamp"].dataType
    second_of_day = F.col("start_timestamp") - F.col("_bt_day") * _DAY
    within_day = F.when(
        F.col("_bt_working"),
        F.least(F.greatest(second_of_day - work_start, F.lit(0)), F.lit(day_len)),
    ).otherwise(F.lit(0))

    return (
        events_df
        .withColumn("_bt_day", F.floor(F.col("start_timestamp") / _DAY).cast("long"))
        .join(F.broadcast(lookup), on="_bt_day", how="left")
        # off_hours must be derived before start_timestamp is overwritten.
        .withColumn("off_hours", ~F.col("_bt_working")
                    | (second_of_day < work_start) | (second_of_day > work_end))
        .withColumn("start_timestamp",
                    (F.col("_bt_days_before") * day_len + within_day).cast(ts_type))
        .drop("_bt_day", "_bt_days_before", "_bt_working")
    )
