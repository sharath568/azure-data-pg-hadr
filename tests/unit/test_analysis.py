import math

import pytest

from conftest import fail, ok
from pghadr import analysis


def test_outage_and_write_gap(failover_samples):
    outs = analysis.find_outages(failover_samples)
    assert len(outs) == 1
    o = outs[0]
    assert o.failed_attempts == 3
    assert o.first_fail_t == 2.2
    assert o.last_fail_t == 8.2
    # last ok write acked at 2.0 + 2 ms, first ok after acked at 11.2 + 2 ms
    assert o.last_ok_before_t == pytest.approx(2.002)
    assert o.first_ok_after_t == pytest.approx(11.202)
    assert o.write_gap_s == pytest.approx(9.2)
    assert o.recovered
    assert dict(o.errors)["OperationalError: x"] == 2


def test_outage_that_never_recovers():
    outs = analysis.find_outages([ok(1, 0.0), fail(2, 0.2), fail(3, 0.4)])
    assert len(outs) == 1
    assert not outs[0].recovered
    assert outs[0].write_gap_s is None


def test_outage_at_start_has_no_gap():
    outs = analysis.find_outages([fail(1, 0.0), ok(2, 0.2)])
    assert outs[0].last_ok_before_t is None
    assert outs[0].write_gap_s is None


def test_no_outage():
    assert analysis.find_outages([ok(1, 0.0), ok(2, 0.2)]) == []


def test_switch_detected_on_address_or_start_time(failover_samples):
    sw = analysis.find_switches(failover_samples)
    assert len(sw) == 1
    assert sw[0].before == ("10.0.0.4", "s1")
    assert sw[0].after == ("10.0.0.5", "s2")
    # a plain restart: same address, new postmaster start time
    restart = [ok(1, 0.0, started="a"), ok(2, 0.2, started="b")]
    assert len(analysis.find_switches(restart)) == 1


def test_summary(failover_samples):
    s = analysis.summarize(failover_samples, 0.2)
    assert s.attempts == 18
    assert s.ok == 15
    assert s.failed == 3
    assert s.availability_pct == pytest.approx(83.333)
    assert s.max_write_gap_s == pytest.approx(9.2)
    assert s.latency_ms["p50"] == 2.0
    assert len(s.switches) == 1


def test_summary_empty():
    s = analysis.summarize([], 0.2)
    assert s.attempts == 0
    assert s.availability_pct == 0.0
    assert math.isnan(s.latency_ms["p50"])
    assert s.max_write_gap_s is None


def test_loss_none_lost_and_in_doubt(failover_samples):
    acked = {s.seq for s in failover_samples if s.ok}
    present = acked | {11}  # the in-doubt write did commit
    loss = analysis.check_loss(failover_samples, present)
    assert loss.ok
    assert loss.acknowledged == 15
    assert loss.in_doubt == 1
    assert loss.in_doubt_committed == 1


def test_loss_detects_missing_acknowledged_write(failover_samples):
    acked = {s.seq for s in failover_samples if s.ok}
    loss = analysis.check_loss(failover_samples, acked - {9, 10})
    assert not loss.ok
    assert loss.lost == (9, 10)
    assert loss.present == 13


def test_unsent_failures_are_not_in_doubt(failover_samples):
    loss = analysis.check_loss(failover_samples, set())
    assert loss.in_doubt == 1  # seq 11 only; 12 and 13 never reached the server


def test_dr_no_loss():
    samples = [ok(i, i * 0.2) for i in range(1, 11)]
    dr = analysis.check_dr(samples, cutoff_t=1.5, replica_seqs=set(range(1, 11)))
    assert dr.acknowledged_before_cutoff == 7  # acked at 0.202 .. 1.402
    assert dr.missing == ()
    assert dr.rpo_s == 0.0
    assert dr.last_replicated_t == pytest.approx(1.402)


def test_dr_replication_lag_is_rpo():
    samples = [ok(i, i * 0.2) for i in range(1, 11)]
    # replica only has 1..4; 5, 6, 7 were acknowledged before the cutoff
    dr = analysis.check_dr(samples, cutoff_t=1.5, replica_seqs={1, 2, 3, 4})
    assert dr.missing == (5, 6, 7)
    assert dr.rpo_s == pytest.approx(1.5 - 1.002)
    assert dr.last_replicated_t == pytest.approx(0.802)


def test_dr_ignores_writes_after_cutoff():
    samples = [ok(i, i * 0.2) for i in range(1, 11)]
    dr = analysis.check_dr(samples, cutoff_t=0.5, replica_seqs={1, 2})
    assert dr.acknowledged_before_cutoff == 2
    assert dr.missing == ()


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [([1, 2, 3, 4], 50, 2), ([1, 2, 3, 4], 100, 4), ([5], 99, 5), ([3, 1, 2], 0, 1)],
)
def test_percentile(values, p, expected):
    assert analysis.percentile(values, p) == expected


def test_percentile_empty_is_nan():
    assert math.isnan(analysis.percentile([], 50))


def test_timeline(failover_samples):
    line, bucket = analysis.timeline(failover_samples)
    assert bucket == 1.0
    # bucket 2 holds the last good write (2.0 s) and the first failure (2.2 s);
    # empty buckets are spaces, failed ones dots
    assert line == "##:  .  .  ##"


def test_timeline_mixed_bucket_and_folding():
    line, _ = analysis.timeline([ok(1, 0.1), fail(2, 0.5)])
    assert line == ":"
    many = [ok(i, float(i)) for i in range(1000)]
    folded, bucket = analysis.timeline(many, width=100)
    assert len(folded) <= 101
    assert bucket > 1


def test_timeline_empty():
    assert analysis.timeline([]) == ("", 1.0)


def test_dr_replication_keeps_running_after_the_cutoff():
    samples = [ok(i, i * 0.2) for i in range(1, 21)]  # acked at 0.202 .. 4.002
    # promote requested at 1.5, but the replica got everything up to seq 12 (2.402)
    dr = analysis.check_dr(samples, cutoff_t=1.5, replica_seqs=set(range(1, 13)))
    assert dr.missing == ()
    assert dr.replication_stopped_t == pytest.approx(2.402)
    assert dr.replicated_after_cutoff_s == pytest.approx(0.902)
    assert dr.holes == ()


def test_dr_holes_before_the_stop_point():
    samples = [ok(i, i * 0.2) for i in range(1, 11)]
    dr = analysis.check_dr(samples, cutoff_t=1.5, replica_seqs={1, 2, 4, 5})
    assert dr.holes == (3,)
    assert dr.missing == (3, 6, 7)


def test_dr_nothing_replicated():
    samples = [ok(i, i * 0.2) for i in range(1, 5)]
    dr = analysis.check_dr(samples, cutoff_t=1.0, replica_seqs=set())
    assert dr.replication_stopped_t is None
    assert dr.replicated_after_cutoff_s is None
    assert dr.holes == ()
