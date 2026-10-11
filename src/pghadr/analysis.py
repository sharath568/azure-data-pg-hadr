"""Turn a list of Samples into the numbers a drill is for.

Definitions (they matter more than the code):

* **Outage**: a maximal run of consecutive failed attempts.
* **Write gap**: for an outage, the time from the last acknowledged write
  before it to the first acknowledged write after it. This is what an
  application that writes continuously experiences as "the database was
  down". It is an upper bound on the true unavailability, off by at most one
  probe interval plus one write latency.
* **Switch**: two consecutive successful writes answered by different nodes
  (different server address or different postmaster start time).
* **Lost write**: a write the server acknowledged (COMMIT returned) that is not
  in the table afterwards. With zone-redundant HA this should be zero, because
  Azure commits synchronously to the standby. The drill checks it instead of
  assuming it.
* **In doubt**: a write that was sent but failed. The server may or may not have
  committed it; either outcome is correct behaviour, so it is never counted as
  lost. The report says how many of them did land.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from pghadr.events import Sample


@dataclass(frozen=True)
class Outage:
    first_fail_t: float
    last_fail_t: float
    failed_attempts: int
    last_ok_before_t: float | None  # ack time of the last good write before it
    first_ok_after_t: float | None  # ack time of the first good write after it
    errors: tuple[tuple[str, int], ...]

    @property
    def recovered(self) -> bool:
        return self.first_ok_after_t is not None

    @property
    def write_gap_s(self) -> float | None:
        if self.last_ok_before_t is None or self.first_ok_after_t is None:
            return None
        return round(self.first_ok_after_t - self.last_ok_before_t, 3)


@dataclass(frozen=True)
class Switch:
    t: float
    before: tuple[str | None, str | None]
    after: tuple[str | None, str | None]


@dataclass(frozen=True)
class LossCheck:
    acknowledged: int
    present: int
    lost: tuple[int, ...]
    in_doubt: int
    in_doubt_committed: int

    @property
    def ok(self) -> bool:
        return not self.lost


@dataclass(frozen=True)
class DrCheck:
    """Cross-region DR: which writes acknowledged by the primary made it to the replica.

    ``cutoff_t`` is when the promote was requested. A write acknowledged before
    that and missing on the promoted replica is data you would lose in a real
    regional failover. ``rpo_s`` is the age of the oldest such write at cutoff
    (0 when nothing is missing).

    A requested promote does not cut replication at once: the primary keeps
    shipping WAL until the replica actually stops. ``replication_stopped_t`` is
    the ack time of the last write (before or after the cutoff) that reached
    the replica, and ``holes`` are acknowledged writes before that point that
    did not. No holes means the replica was a clean prefix of the primary when
    replication stopped, so its lag was below one probe interval. That lag, not
    the cutoff, is what a real regional loss would cost you.
    """

    cutoff_t: float
    acknowledged_before_cutoff: int
    present_on_replica: int
    missing: tuple[int, ...]
    rpo_s: float
    last_replicated_t: float | None
    replication_stopped_t: float | None = None
    holes: tuple[int, ...] = ()

    @property
    def replicated_after_cutoff_s(self) -> float | None:
        if self.replication_stopped_t is None:
            return None
        return round(max(0.0, self.replication_stopped_t - self.cutoff_t), 3)


@dataclass
class Summary:
    attempts: int
    ok: int
    failed: int
    duration_s: float
    interval_s: float
    latency_ms: dict[str, float]
    outages: list[Outage] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)

    @property
    def availability_pct(self) -> float:
        return round(100.0 * self.ok / self.attempts, 3) if self.attempts else 0.0

    @property
    def max_write_gap_s(self) -> float | None:
        gaps = [o.write_gap_s for o in self.outages if o.write_gap_s is not None]
        return max(gaps) if gaps else None


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile, ``p`` in [0, 100]. Empty input gives NaN."""
    if not values:
        return math.nan
    xs = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(xs)))
    return xs[min(k, len(xs)) - 1]


def find_outages(samples: list[Sample]) -> list[Outage]:
    out: list[Outage] = []
    last_ok: Sample | None = None
    run: list[Sample] = []
    for s in samples:
        if s.ok:
            if run:
                out.append(_outage(run, last_ok, s))
                run = []
            last_ok = s
        else:
            run.append(s)
    if run:
        out.append(_outage(run, last_ok, None))
    return out


def _outage(run: list[Sample], before: Sample | None, after: Sample | None) -> Outage:
    errors = Counter(s.error or "unknown" for s in run).most_common(3)
    return Outage(
        first_fail_t=run[0].t,
        last_fail_t=run[-1].t,
        failed_attempts=len(run),
        last_ok_before_t=round(before.t_done, 4) if before else None,
        first_ok_after_t=round(after.t_done, 4) if after else None,
        errors=tuple(errors),
    )


def find_switches(samples: Iterable[Sample]) -> list[Switch]:
    out: list[Switch] = []
    prev: Sample | None = None
    for s in samples:
        if not s.ok:
            continue
        if prev is not None and s.node != prev.node:
            out.append(Switch(s.t, prev.node, s.node))
        prev = s
    return out


def summarize(samples: list[Sample], interval_s: float) -> Summary:
    oks = [s for s in samples if s.ok]
    lat = [s.latency_ms for s in oks]
    duration = (samples[-1].t_done - samples[0].t) if samples else 0.0
    return Summary(
        attempts=len(samples),
        ok=len(oks),
        failed=len(samples) - len(oks),
        duration_s=round(duration, 3),
        interval_s=interval_s,
        latency_ms={
            "p50": round(percentile(lat, 50), 2) if lat else math.nan,
            "p95": round(percentile(lat, 95), 2) if lat else math.nan,
            "p99": round(percentile(lat, 99), 2) if lat else math.nan,
            "max": round(max(lat), 2) if lat else math.nan,
        },
        outages=find_outages(samples),
        switches=find_switches(samples),
    )


def check_loss(samples: list[Sample], present: set[int]) -> LossCheck:
    acked = {s.seq for s in samples if s.ok}
    doubt = {s.seq for s in samples if s.sent and not s.ok}
    return LossCheck(
        acknowledged=len(acked),
        present=len(acked & present),
        lost=tuple(sorted(acked - present)),
        in_doubt=len(doubt),
        in_doubt_committed=len(doubt & present),
    )


def check_dr(samples: list[Sample], cutoff_t: float, replica_seqs: set[int]) -> DrCheck:
    before = [s for s in samples if s.ok and s.t_done <= cutoff_t]
    acked = {s.seq for s in before}
    missing = sorted(acked - replica_seqs)
    replicated = [s for s in before if s.seq in replica_seqs]
    last_rep_t = max((s.t_done for s in replicated), default=None)
    if missing:
        by_seq = {s.seq: s for s in before}
        oldest_missing = min(by_seq[m].t_done for m in missing)
        rpo = round(cutoff_t - oldest_missing, 3)
    else:
        rpo = 0.0
    acked_all = [s for s in samples if s.ok]
    stopped_t = max((s.t_done for s in acked_all if s.seq in replica_seqs), default=None)
    holes = (
        sorted(s.seq for s in acked_all if s.t_done <= stopped_t and s.seq not in replica_seqs)
        if stopped_t is not None
        else []
    )
    return DrCheck(
        cutoff_t=round(cutoff_t, 4),
        acknowledged_before_cutoff=len(acked),
        present_on_replica=len(acked & replica_seqs),
        missing=tuple(missing),
        rpo_s=rpo,
        last_replicated_t=round(last_rep_t, 4) if last_rep_t is not None else None,
        replication_stopped_t=round(stopped_t, 4) if stopped_t is not None else None,
        holes=tuple(holes),
    )


def timeline(samples: list[Sample], bucket_s: float = 1.0, width: int = 120) -> tuple[str, float]:
    """One character per bucket: '#' all ok, '.' all failed, ':' mixed, ' ' no data.

    Long runs are folded so the line stays readable: when there are more than
    ``width`` buckets, the bucket size grows. Returns the line and the bucket size
    actually used, in seconds.
    """
    if not samples:
        return "", bucket_s
    end = samples[-1].t
    n = int(end // bucket_s) + 1
    if n > width:
        bucket_s = math.ceil(end / width) or 1
        n = int(end // bucket_s) + 1
    ok = [0] * n
    bad = [0] * n
    for s in samples:
        i = min(int(s.t // bucket_s), n - 1)
        if s.ok:
            ok[i] += 1
        else:
            bad[i] += 1
    chars = []
    for a, b in zip(ok, bad, strict=True):
        chars.append(" " if a == b == 0 else "#" if b == 0 else "." if a == 0 else ":")
    return "".join(chars), bucket_s
