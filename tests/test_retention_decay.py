"""
Unit tests for the retention policy's decayed counters (no Spark needed).

Counters used to be rounded on every decay step, which made them sticky:
with frequent evaluations round(n * factor) == n, so demand never decayed.
They are now floats, compared against integer thresholds with a
half-query tolerance.
"""

import time

import pytest

from siesta.model.PerspectiveModel import PairStats, PairStatus, PerspectiveStats
from siesta.modules.adaptive_index.retention import RetentionPolicy


def _aged(ps: PairStats, seconds: float) -> PairStats:
    """Pretend the last decay happened `seconds` ago."""
    ps.last_decay_ts = time.time() - seconds
    return ps


def test_frequent_evaluations_still_decay():
    policy = RetentionPolicy(half_life_seconds=60.0)
    ps = PairStats(query_count=3.0)
    # 120 evaluations one second apart: two half-lives in total.
    ps.last_decay_ts = time.time()
    for _ in range(120):
        ps.last_decay_ts -= 1.0
        policy.decay_pair(ps)
    assert ps.query_count == pytest.approx(0.75, rel=0.05)


def test_one_half_life_halves_the_counter():
    policy = RetentionPolicy(half_life_seconds=100.0)
    ps = _aged(PairStats(query_count=4.0, total_savings_ms=1000.0), 100.0)
    policy.decay_pair(ps)
    assert ps.query_count == pytest.approx(2.0, rel=1e-3)
    assert ps.total_savings_ms == pytest.approx(500.0, rel=1e-3)


def test_quick_repeated_queries_pass_the_demand_gate():
    policy = RetentionPolicy(half_life_seconds=3600.0, min_query_count=3, hysteresis=0.15)
    ps = PairStats(build_cost_ms=1000.0)
    ps.last_decay_ts = time.time() - 30.0
    # Three touches 30 s apart, each decayed before it is added (as the
    # catalog does): the decayed sum is slightly below 3.
    for _ in range(3):
        policy.decay_pair(ps)
        ps.query_count += 1
        ps.last_decay_ts -= 30.0
    ps.total_savings_ms = 2000.0  # two warm hits saved the build cost twice
    assert ps.query_count < 3.0
    assert policy.should_persist_pair(ps)


def test_demand_gate_blocks_two_queries():
    policy = RetentionPolicy(min_query_count=3)
    ps = PairStats(query_count=2.0, build_cost_ms=1000.0, total_savings_ms=10_000.0)
    assert not policy.should_persist_pair(ps)


def test_cost_gate_needs_savings_above_build_cost():
    policy = RetentionPolicy(min_query_count=3, hysteresis=0.15)
    ps = PairStats(query_count=3.0, build_cost_ms=1000.0, total_savings_ms=1100.0)
    assert not policy.should_persist_pair(ps)  # 1100 < 1.15 * 1000
    ps.total_savings_ms = 1200.0
    assert policy.should_persist_pair(ps)


def test_idle_pair_decays_to_zero_and_demotes():
    policy = RetentionPolicy(half_life_seconds=60.0)
    ps = PairStats(
        query_count=3.0, build_cost_ms=1000.0, total_savings_ms=3000.0,
        status=PairStatus.PERSISTENT, last_accessed_ts=time.time() - 600.0,
    )
    _aged(ps, 600.0)  # ten half-lives
    assert policy.should_demote_pair(ps)
    assert ps.query_count < 0.5


def test_active_pair_is_not_demoted():
    policy = RetentionPolicy(half_life_seconds=3600.0, hysteresis=0.15)
    ps = PairStats(
        query_count=5.0, build_cost_ms=1000.0, total_savings_ms=4000.0,
        total_maintenance_ms=500.0, maintenance_batch_count=5,
        status=PairStatus.PERSISTENT, last_accessed_ts=time.time(),
    )
    _aged(ps, 1.0)
    assert not policy.should_demote_pair(ps)


def test_perspective_counters_decay_as_floats():
    policy = RetentionPolicy(half_life_seconds=10.0)
    stats = PerspectiveStats(l1_query_count=5.0)
    stats.last_decay_ts = time.time() - 10.0
    policy.decay_perspective(stats)
    assert stats.l1_query_count == pytest.approx(2.5, rel=1e-3)
    assert isinstance(stats.l1_query_count, float)


def test_catalog_row_round_trip_keeps_float_counters_and_decay_state():
    from pyspark.sql import Row

    from siesta.modules.adaptive_index.catalog import _row_to_stats, _stats_to_row

    stats = PerspectiveStats(
        l1_query_count=2.4, last_decay_ts=123.0, l1_maintenance_batch_count=4.2,
    )
    stats.pairs[("A", "B")] = PairStats(
        query_count=2.6, build_cost_ms=10.0, status=PairStatus.PERSISTENT,
        last_decay_ts=456.0, maintenance_batch_count=3, total_maintenance_ms=9.0,
    )
    back = _row_to_stats(Row(**_stats_to_row("x", stats)))
    assert back.l1_query_count == pytest.approx(2.4)
    assert back.last_decay_ts == 123.0
    assert back.l1_maintenance_batch_count == pytest.approx(4.2)
    ps = back.pairs[("A", "B")]
    assert ps.query_count == pytest.approx(2.6)
    assert ps.last_decay_ts == 456.0
    assert ps.maintenance_batch_count == 3
    assert ps.status == PairStatus.PERSISTENT


def test_catalog_touch_decays_before_counting():
    stats = PerspectiveStats()
    ps = PairStats(query_count=2.0)
    ps.last_decay_ts = time.time() - 60.0
    stats.pairs[("A", "B")] = ps

    _bare_catalog(stats).record_query_touch(
        pid="p", pairs_touched=[("A", "B")], pair_savings_ms={},
        decay=RetentionPolicy(half_life_seconds=60.0),
    )
    # 2 decayed by one half-life, plus this query at full weight.
    assert ps.query_count == pytest.approx(2.0, rel=1e-3)


def test_row_without_batch_count_column_loads_as_zero():
    from pyspark.sql import Row

    from siesta.modules.adaptive_index.catalog import _row_to_stats, _stats_to_row

    row = _stats_to_row("x", PerspectiveStats(l1_query_count=1.0))
    del row["l1_maintenance_batch_count"]
    back = _row_to_stats(Row(**row))  # catalog written before the column existed
    assert back.l1_maintenance_batch_count == 0.0


def test_row_at_removed_level_2_loads_as_l1():
    from pyspark.sql import Row

    from siesta.model.PerspectiveModel import PerspectiveLevel
    from siesta.modules.adaptive_index.catalog import _row_to_stats, _stats_to_row

    row = _stats_to_row("x", PerspectiveStats(level=PerspectiveLevel.L1_POS_FREE))
    row["level"] = 2  # written while the L2 positions overlay existed
    row["l2_build_cost_ms"] = 900.0  # and its now-unused columns
    assert _row_to_stats(Row(**row)).level == PerspectiveLevel.L1_POS_FREE


def test_batch_count_decays_with_query_counters():
    policy = RetentionPolicy(half_life_seconds=10.0)
    stats = PerspectiveStats(l1_maintenance_batch_count=8.0)
    stats.last_decay_ts = time.time() - 10.0
    policy.decay_perspective(stats)
    assert stats.l1_maintenance_batch_count == pytest.approx(4.0, rel=1e-3)


def test_pair_maintenance_decay_keeps_mean_cost():
    policy = RetentionPolicy(half_life_seconds=10.0)
    ps = _aged(PairStats(total_maintenance_ms=400.0, maintenance_batch_count=8.0), 10.0)
    policy.decay_pair(ps)
    assert ps.maintenance_batch_count == pytest.approx(4.0, rel=1e-3)
    assert ps.total_maintenance_ms / ps.maintenance_batch_count == pytest.approx(50.0)


def _bare_catalog(stats):
    import threading

    from siesta.modules.adaptive_index.catalog import PerspectiveCatalog

    cat = PerspectiveCatalog.__new__(PerspectiveCatalog)  # no Delta load
    cat._lock = threading.RLock()
    cat._dirty = set()
    cat._cache = {"p": stats}
    return cat


def test_catalog_batch_decays_before_counting():
    from siesta.model.PerspectiveModel import PerspectiveLevel

    stats = PerspectiveStats(level=PerspectiveLevel.L1_POS_FREE, l1_maintenance_batch_count=2.0)
    stats.last_decay_ts = time.time() - 60.0
    ps = _aged(PairStats(total_maintenance_ms=20.0, maintenance_batch_count=2.0), 60.0)
    stats.pairs[("A", "B")] = ps

    _bare_catalog(stats).record_batch_maintenance(
        pid="p", l1_ms=0.0, pair_ms={("A", "B"): 10.0},
        decay=RetentionPolicy(half_life_seconds=60.0),
    )
    # 2 decayed by one half-life, plus this batch at full weight.
    assert stats.l1_maintenance_batch_count == pytest.approx(2.0, rel=1e-3)
    assert ps.maintenance_batch_count == pytest.approx(2.0, rel=1e-3)
    assert ps.total_maintenance_ms == pytest.approx(20.0, rel=1e-3)


def test_l1_at_zero_cost_follows_demand_alone():
    policy = RetentionPolicy(half_life_seconds=60.0, min_query_count=3)
    stats = PerspectiveStats(l1_query_count=3.0)  # no savings, build or maintenance
    stats.last_decay_ts = time.time()
    assert policy.should_promote_l1(stats)
    assert not policy.should_demote_l1(stats)
    stats.last_decay_ts = time.time() - 600.0  # ten half-lives idle
    assert policy.should_demote_l1(stats)


def test_touch_credits_no_l1_saving():
    stats = PerspectiveStats()
    _bare_catalog(stats).record_query_touch(pid="p", pairs_touched=[], pair_savings_ms={})
    assert stats.l1_query_count == 1.0
    assert stats.l1_total_savings_ms == 0.0  # L1 materialises nothing


def test_steadily_used_pair_survives_long_ingestion():
    # One query (saving 50 ms) and one batch (costing 20 ms of maintenance)
    # every 10 s, for 100 half-lives.  Savings outpace maintenance, so the
    # pair must stay.  With an undecayed B the cost grew without bound and
    # the pair was eventually demoted however popular.
    policy = RetentionPolicy(half_life_seconds=100.0, hysteresis=0.15)
    ps = PairStats(build_cost_ms=500.0, status=PairStatus.PERSISTENT,
                   last_accessed_ts=time.time())
    ps.last_decay_ts = time.time()
    for _ in range(1000):
        ps.last_decay_ts -= 10.0
        policy.decay_pair(ps)
        ps.query_count += 1
        ps.total_savings_ms += 50.0
        ps.total_maintenance_ms += 20.0
        ps.maintenance_batch_count += 1
    assert not policy.should_demote_pair(ps)

    # The same history with maintenance above savings is demoted.
    ps.total_maintenance_ms = ps.maintenance_batch_count * 80.0
    assert policy.should_demote_pair(ps)


def test_pinned_pair_is_never_demoted():
    policy = RetentionPolicy(half_life_seconds=1.0)
    ps = _aged(PairStats(status=PairStatus.PERSISTENT, pinned=True), 1000.0)
    assert not policy.should_demote_pair(ps)
