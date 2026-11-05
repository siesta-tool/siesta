"""Tests for the working-time calendar (siesta.modules.analyser.business_time)
and its use by loop detection and bottlenecks.

Working window 07:45-15:15 (7.5h), weekends Sat/Sun, plus one holiday
(Tue 2026-01-06). Timestamps are wall-clock values in UTC fields.
"""
import calendar as _cal
from datetime import datetime

import pytest

from siesta.modules.analyser.business_time import format_working_duration
from siesta.modules.analyser.loop_detection import _find_loops

CAL = {"holidays": ["2026-01-06"], "work_start": "07:45", "work_end": "15:15"}
DAY = 7.5 * 3600


def ts(text):
    return _cal.timegm(datetime.strptime(text, "%Y-%m-%d %H:%M").timetuple())


class TestFormatWorkingDuration:

    def test_units(self):
        assert format_working_duration(None, DAY) == "n/a"
        assert format_working_duration(30, DAY) == "30.0s"
        assert format_working_duration(5400, DAY) == "1.5h"
        assert format_working_duration(DAY, DAY) == "1.0wd"            # 7.5h, not "7.5h"
        assert format_working_duration(10 * DAY, DAY) == "2.0ww"       # 5 working days a week


class TestLoopOffHoursCount:

    def test_off_hours_events_counted_inside_loop_span_only(self):
        seq = [
            {"activity": "A", "position": 0, "start_timestamp": 0, "off_hours": False},
            {"activity": "B", "position": 1, "start_timestamp": 10, "off_hours": True},
            {"activity": "A", "position": 2, "start_timestamp": 20, "off_hours": False},
            {"activity": "C", "position": 3, "start_timestamp": 30, "off_hours": True},
        ]
        loop = [r for r in _find_loops(seq, 0) if r["pattern"] == "A -> B -> A"][0]
        assert loop["off_hours_events"] == 1   # B inside the span; C is outside it

    def test_missing_flag_defaults_to_zero(self):
        seq = [{"activity": a, "position": i, "start_timestamp": i} for i, a in enumerate("ABA")]
        assert _find_loops(seq, 0)[0]["off_hours_events"] == 0


# ---------------------------------------------------------------------------
# Spark-backed: the clock mapping and the analysers that consume it
# ---------------------------------------------------------------------------

def _rows(spark, events):
    from siesta.model.DataModel import Event
    return spark.createDataFrame(
        [{"trace_id": t, "activity": a, "position": p, "start_timestamp": s, "attributes": {}}
         for t, a, p, s in events],
        schema=Event.get_schema())


@pytest.fixture
def spark(siesta_app):
    from siesta.core.sparkManager import get_spark_session
    return get_spark_session()


def _clock(spark, events):
    from siesta.modules.analyser.business_time import apply_business_calendar
    out = apply_business_calendar(_rows(spark, events), CAL).orderBy("trace_id", "position").collect()
    return [(r["start_timestamp"], r["off_hours"]) for r in out]


class TestApplyBusinessCalendar:

    def test_weekend_is_skipped(self, spark):
        # Fri 14:15 -> Mon 08:45: 1h left on Friday + 1h on Monday = 2h, not 2d18.5h.
        (fri, _), (mon, _) = _clock(spark, [("t", "A", 0, ts("2026-01-09 14:15")),
                                            ("t", "B", 1, ts("2026-01-12 08:45"))])
        assert mon - fri == 2 * 3600

    def test_holiday_is_skipped(self, spark):
        # Mon 15:00 -> Wed 08:00 across holiday Tue 01-06: 15m + 15m.
        (mon, _), (wed, _) = _clock(spark, [("t", "A", 0, ts("2026-01-05 15:00")),
                                            ("t", "B", 1, ts("2026-01-07 08:00"))])
        assert wed - mon == 30 * 60

    def test_off_hours_events_are_kept_clamped_and_flagged(self, spark):
        clock = _clock(spark, [("t", "A", 0, ts("2026-01-08 10:00")),    # Thu, in hours
                               ("t", "B", 1, ts("2026-01-08 18:00")),    # Thu, after hours
                               ("t", "C", 2, ts("2026-01-10 11:00")),    # Saturday
                               ("t", "D", 3, ts("2026-01-12 06:00"))])   # Mon, before hours
        (a, a_off), (b, b_off), (c, c_off), (d, d_off) = clock
        assert (a_off, b_off, c_off, d_off) == (False, True, True, True)
        assert b - a == 5 * 3600 + 15 * 60        # clamped to Thu 15:15
        assert c - b == DAY                       # Friday's full working day
        assert d == c                             # nothing works Sat/Sun/Mon 06:00


class TestAnalysersWithCalendar:

    def test_bottleneck_durations_and_off_hours_share(self, spark):
        from siesta.modules.analyser.bottlenecks import compute_bottlenecks
        events = [("t1", "A", 0, ts("2026-01-09 14:15")), ("t1", "B", 1, ts("2026-01-12 08:45")),
                  ("t2", "A", 0, ts("2026-01-08 09:00")), ("t2", "B", 1, ts("2026-01-08 11:00"))]
        rows = compute_bottlenecks(_rows(spark, events), trace_count=2,
                                   business_calendar=CAL).collect()
        pair = rows[0]
        assert (pair["source"], pair["target"]) == ("A", "B")
        assert pair["avg_duration_sec"] == 2 * 3600           # both occurrences: 2 working hours
        assert pair["off_hours_share"] == 0.0
        assert pair["avg_duration_human"] == "2.0h"

    def test_bottleneck_without_calendar_unchanged(self, spark):
        from siesta.modules.analyser.bottlenecks import compute_bottlenecks
        events = [("t1", "A", 0, ts("2026-01-09 14:15")), ("t1", "B", 1, ts("2026-01-12 08:45"))]
        row = compute_bottlenecks(_rows(spark, events), trace_count=1).collect()[0]
        assert row["avg_duration_sec"] == ts("2026-01-12 08:45") - ts("2026-01-09 14:15")
        assert "off_hours_share" not in row.asDict()

    def test_bottleneck_rejects_end_time_with_calendar(self, spark):
        from siesta.modules.analyser.bottlenecks import compute_bottlenecks
        with pytest.raises(ValueError):
            compute_bottlenecks(_rows(spark, [("t", "A", 0, 0)]), trace_count=1,
                                end_time="end", business_calendar=CAL)

    def test_loop_time_and_off_hours_share(self, spark):
        from siesta.modules.analyser.loop_detection import compute_loop_detection
        events = [("t1", "A", 0, ts("2026-01-09 14:15")), ("t1", "B", 1, ts("2026-01-09 20:00")),
                  ("t1", "A", 2, ts("2026-01-12 08:45")),
                  ("t2", "A", 0, ts("2026-01-08 09:00")), ("t2", "B", 1, ts("2026-01-08 10:00")),
                  ("t2", "A", 2, ts("2026-01-08 11:00"))]
        result = compute_loop_detection(_rows(spark, events), include_repeats=False,
                                        business_calendar=CAL)
        loop = [e for e in result["non_self_loops"] if e["pattern"] == "A -> B -> A"][0]
        assert loop["group_count"] == 2
        assert loop["avg_time_consumed_sec"] == 2 * 3600       # 2h working time in both traces
        assert loop["off_hours_share"] == 0.5                  # only t1's loop has an off-hours event
        assert loop["avg_time_consumed_human"] == "2.0h"
