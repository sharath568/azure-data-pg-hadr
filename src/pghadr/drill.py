"""Drills: run the probe, do something disruptive, measure what the client saw.

Two kinds:

* ``run_ha_drill``: probe the primary, trigger an action (an HA failover, a
  restart, or any shell command), wait until writes are flowing again, then
  check that every acknowledged write is still in the table.
* ``run_dr_drill``: probe the primary, force-promote a cross-region read
  replica, and check which writes acknowledged by the primary made it to the
  replica (RPO) and how long until the replica accepted writes (RTO).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from pghadr import analysis
from pghadr.events import Sample
from pghadr.probe import CONNECT_KWARGS, Probe

Log = Callable[[str], None]


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"


def schema_sql() -> str:
    """The heartbeat DDL, from the installed package or the source tree."""
    try:
        return resources.files("pghadr").joinpath("sql/001_heartbeat.sql").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        root = Path(__file__).resolve().parents[2]
        return (root / "sql" / "001_heartbeat.sql").read_text()


def _connect(dsn: str, timeout: float = 5.0):
    import psycopg

    return psycopg.connect(dsn, connect_timeout=max(1, round(timeout)), **CONNECT_KWARGS)


def with_retry(fn: Callable[[], Any], *, timeout_s: float, what: str, log: Log) -> Any:
    """Call ``fn`` until it succeeds or ``timeout_s`` passes (right after a
    failover, the first connection or two can still fail)."""
    deadline = time.monotonic() + timeout_s
    delay = 1.0
    while True:
        try:
            return fn()
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{what} failed after {timeout_s:g}s: {exc}") from exc
            log(f"  {what}: {type(exc).__name__}, retrying in {delay:g}s")
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def init_schema(dsn: str) -> None:
    with _connect(dsn) as conn:
        conn.execute(schema_sql())


def fetch_seqs(dsn: str, run_id: str) -> set[int]:
    with _connect(dsn) as conn:
        rows = conn.execute(
            "SELECT seq FROM pghadr_heartbeat WHERE run_id = %s", (run_id,)
        ).fetchall()
    return {r[0] for r in rows}


@dataclass
class ActionRecord:
    name: str
    t_start: float | None = None
    t_end: float | None = None
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.t_start is None or self.t_end is None:
            return None
        return round(self.t_end - self.t_start, 3)


@dataclass
class DrillResult:
    kind: str  # "ha" or "dr"
    run_id: str
    started: str
    params: dict[str, Any]
    action: ActionRecord
    samples: list[Sample]
    summary: analysis.Summary
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    loss: analysis.LossCheck | None = None
    dr: analysis.DrCheck | None = None
    replica_samples: list[Sample] = field(default_factory=list)
    replica_writable_t: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def dr_rto_s(self) -> float | None:
        if self.replica_writable_t is None or self.action.t_start is None:
            return None
        return round(self.replica_writable_t - self.action.t_start, 3)


def _wait_until_recovered(
    probe: Probe, after_t: float, settle_s: float, need_ok: int, timeout_s: float
) -> bool:
    """Wait until ``need_ok`` consecutive successful writes after ``after_t`` and at
    least ``settle_s`` seconds have passed. False if ``timeout_s`` runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        tail = [s for s in probe.snapshot() if s.t >= after_t]
        streak = 0
        for s in reversed(tail):
            if not s.ok:
                break
            streak += 1
        if streak >= need_ok and probe.now() >= after_t + settle_s:
            return True
        time.sleep(0.25)
    return False


def _run_action(action: Callable[[], None] | None, rec: ActionRecord, clock, log: Log) -> None:
    rec.t_start = clock()
    if action is not None:
        try:
            action()
        except Exception as exc:  # recorded, the drill still measures what happened
            rec.error = f"{type(exc).__name__}: {exc}"
            log(f"  action failed: {rec.error}")
    rec.t_end = clock()


def run_ha_drill(
    dsn: str,
    *,
    action: Callable[[], None] | None,
    action_name: str,
    warmup_s: float = 30.0,
    settle_s: float = 30.0,
    interval: float = 0.2,
    recover_timeout_s: float = 600.0,
    state: Callable[[], dict[str, Any]] | None = None,
    run_id: str | None = None,
    log: Log = print,
) -> DrillResult:
    run_id = run_id or new_run_id("ha")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"run {run_id}: preparing schema")
    with_retry(lambda: init_schema(dsn), timeout_s=60, what="create heartbeat table", log=log)
    before = state() if state else None

    probe = Probe(dsn, run_id, interval=interval)
    probe.start()
    log(f"probe running every {interval:g}s; warming up for {warmup_s:g}s")
    time.sleep(warmup_s)

    rec = ActionRecord(action_name)
    log(f"action: {action_name}")
    _run_action(action, rec, probe.now, log)
    log(f"action finished after {rec.duration_s:g}s; waiting for writes to settle")
    assert rec.t_end is not None
    recovered = _wait_until_recovered(
        probe, rec.t_end, settle_s, need_ok=max(5, int(2 / interval)), timeout_s=recover_timeout_s
    )
    samples = probe.stop()
    after = state() if state else None

    log("verifying acknowledged writes")
    present = with_retry(
        lambda: fetch_seqs(dsn, run_id), timeout_s=120, what="read heartbeat rows", log=log
    )
    result = DrillResult(
        kind="ha",
        run_id=run_id,
        started=started,
        params={"interval_s": interval, "warmup_s": warmup_s, "settle_s": settle_s},
        action=rec,
        samples=samples,
        summary=analysis.summarize(samples, interval),
        state_before=before,
        state_after=after,
        loss=analysis.check_loss(samples, present),
    )
    if not recovered:
        result.notes.append(
            f"writes had not recovered {recover_timeout_s:g}s after the action finished"
        )
    return result


def run_dr_drill(
    primary_dsn: str,
    replica_dsn: str,
    *,
    promote: Callable[[], None] | None,
    action_name: str = "promote replica (forced)",
    warmup_s: float = 60.0,
    interval: float = 0.2,
    replica_interval: float = 1.0,
    writable_timeout_s: float = 900.0,
    state: Callable[[], dict[str, Any]] | None = None,
    run_id: str | None = None,
    log: Log = print,
) -> DrillResult:
    run_id = run_id or new_run_id("dr")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"run {run_id}: preparing schema on the primary")
    with_retry(
        lambda: init_schema(primary_dsn), timeout_s=60, what="create heartbeat table", log=log
    )
    before = state() if state else None

    t0 = time.monotonic()
    probe = Probe(primary_dsn, run_id, interval=interval)
    # Writes to the replica fail (read-only) until the promote finishes; the first
    # one that succeeds is the moment the DR copy became usable.
    rprobe = Probe(replica_dsn, f"{run_id}-replica", interval=replica_interval)
    probe.start(t0)
    log(f"probe running on the primary every {interval:g}s; warming up for {warmup_s:g}s")
    time.sleep(warmup_s)
    rprobe.start(t0)

    rec = ActionRecord(action_name)
    log(f"action: {action_name}")
    _run_action(promote, rec, probe.now, log)
    assert rec.t_start is not None
    cutoff = rec.t_start
    samples = probe.stop()
    log(f"promote returned after {rec.duration_s:g}s; waiting for the replica to accept writes")

    deadline = time.monotonic() + writable_timeout_s
    writable_t = None
    while time.monotonic() < deadline:
        oks = [s for s in rprobe.snapshot() if s.ok and s.t >= cutoff]
        if oks:
            writable_t = oks[0].t_done
            break
        time.sleep(0.5)
    rsamples = rprobe.stop()
    after = state() if state else None

    log("comparing the primary's acknowledged writes with the promoted replica")
    replica_seqs = with_retry(
        lambda: fetch_seqs(replica_dsn, run_id), timeout_s=120, what="read replica rows", log=log
    )
    result = DrillResult(
        kind="dr",
        run_id=run_id,
        started=started,
        params={
            "interval_s": interval,
            "warmup_s": warmup_s,
            "replica_interval_s": replica_interval,
        },
        action=rec,
        samples=samples,
        summary=analysis.summarize(samples, interval),
        state_before=before,
        state_after=after,
        dr=analysis.check_dr(samples, cutoff, replica_seqs),
        replica_samples=rsamples,
        replica_writable_t=writable_t,
    )
    if writable_t is None:
        result.notes.append(f"the replica did not accept a write within {writable_timeout_s:g}s")
    return result
