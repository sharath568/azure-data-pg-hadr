"""The probe against a fake connection, so every failure path runs without a database."""

from __future__ import annotations

from itertools import pairwise

import pytest

from pghadr.probe import INSERT_SQL, Probe, error_kind


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, world: World) -> None:
        self.world = world
        self.closed = False

    def execute(self, sql, params):
        assert sql == INSERT_SQL
        self.world.clock.t += 0.002
        if self.world.write_fails:
            raise ConnectionError("server closed the connection unexpectedly")
        self.world.rows.add(params[1])
        return FakeCursor((self.world.server, self.world.started))

    def close(self):
        self.closed = True


class World:
    """What the fake server is doing right now."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.connect_fails = False
        self.write_fails = False
        self.server = "10.0.0.4"
        self.started = "s1"
        self.rows: set[int] = set()
        self.connects = 0

    def connect(self, dsn, connect_timeout, statement_timeout_ms):
        self.connects += 1
        if self.connect_fails:
            self.clock.t += connect_timeout
            raise TimeoutError("connection timeout expired")
        self.clock.t += 0.01
        return FakeConn(self)


@pytest.fixture
def world():
    return World(FakeClock())


def make_probe(world: World, **kw) -> Probe:
    return Probe(
        "host=fake",
        "run-1",
        interval=0.2,
        connect=world.connect,
        clock=world.clock,
        sleep=world.clock.sleep,
        **kw,
    )


def test_steady_state_reuses_one_connection(world):
    samples = make_probe(world).run_for(2.0)
    assert len(samples) == 10
    assert all(s.ok and s.sent for s in samples)
    assert world.connects == 1
    assert {s.seq for s in samples} == world.rows
    assert samples[0].server == "10.0.0.4"
    assert samples[1].t == pytest.approx(0.2)


def test_write_failure_is_in_doubt_and_forces_reconnect(world):
    p = make_probe(world)
    p._t0 = world.clock()
    assert p.tick(1).ok
    world.write_fails = True
    s = p.tick(2)
    assert not s.ok
    assert s.sent
    assert "server closed" in s.error
    world.write_fails = False
    world.server = "10.0.0.5"
    s3 = p.tick(3)
    assert s3.ok
    assert s3.server == "10.0.0.5"
    assert world.connects == 2


def test_connect_failure_is_not_sent(world):
    p = make_probe(world, connect_timeout=3.0)
    p._t0 = world.clock()
    world.connect_fails = True
    s = p.tick(1)
    assert not s.ok
    assert not s.sent
    assert s.latency_ms == pytest.approx(3000.0)
    assert s.error.startswith("TimeoutError")


def test_slow_attempts_skip_ticks_instead_of_bursting(world):
    world.connect_fails = True
    samples = make_probe(world, connect_timeout=1.0).run_for(3.0)
    # each attempt takes 1 s, so a 0.2 s schedule can't be kept: ~3 attempts, not 15
    assert 3 <= len(samples) <= 4
    gaps = [b.t - a.t for a, b in pairwise(samples)]
    assert all(g >= 0.99 for g in gaps)


def test_background_thread_start_stop(world):
    # real clock here; the fake connection still makes it fast and offline
    p = Probe("host=fake", "run-2", interval=0.01, connect=world.connect)
    p.start()
    import time

    time.sleep(0.15)
    samples = p.stop()
    assert len(samples) >= 3
    assert [s.seq for s in samples] == list(range(1, len(samples) + 1))


def test_shared_time_origin(world):
    p = make_probe(world)
    p.start(t0=world.clock() - 5.0)
    try:
        assert p.now() == pytest.approx(5.0, abs=0.1)
    finally:
        p.stop()


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        Probe("x", "r", interval=0)


def test_error_kind_is_short():
    assert error_kind(ValueError("a" * 500)).endswith("...")
    assert error_kind(ValueError("line1\nline2")) == "ValueError: line1"
    assert error_kind(KeyError()) == "KeyError"
