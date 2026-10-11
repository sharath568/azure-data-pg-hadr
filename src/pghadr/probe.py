"""The write probe: one small committed INSERT every ``interval`` seconds.

It behaves like a well-configured application, not like a benchmark:

* one connection, reused while it works, dropped and reopened after any error;
* libpq TCP keepalives and ``tcp_user_timeout`` so a connection to a node that
  vanished fails in seconds instead of hanging until the kernel gives up;
* a ``statement_timeout`` so a write can't wait forever on a node that is
  shutting down;
* a fresh DNS lookup on every reconnect (libpq does this), which is how a client
  follows the Azure server name to the new primary after a failover.

Ticks run on a fixed schedule. If an attempt runs long (a 5 second connect
timeout, say), the missed ticks are skipped, not queued, so the probe never
bursts after an outage.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pghadr.events import Sample

INSERT_SQL = (
    "INSERT INTO pghadr_heartbeat (run_id, seq, client_ts) VALUES (%s, %s, %s) "
    "RETURNING host(inet_server_addr()), pg_postmaster_start_time()::text"
)

# libpq settings. Values are strings because libpq takes them that way.
CONNECT_KWARGS: dict[str, Any] = {
    "autocommit": True,
    "application_name": "pghadr-probe",
    "keepalives": "1",
    "keepalives_idle": "2",
    "keepalives_interval": "1",
    "keepalives_count": "3",
    "tcp_user_timeout": "5000",
}


def error_kind(exc: BaseException) -> str:
    """A short, stable label for an error, good for grouping in a report."""
    name = type(exc).__name__
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 120:
        msg = msg[:117] + "..."
    return f"{name}: {msg}" if msg else name


def _connect_default(dsn: str, connect_timeout: float, statement_timeout_ms: int):
    import psycopg

    return psycopg.connect(
        dsn,
        connect_timeout=max(1, round(connect_timeout)),
        options=f"-c statement_timeout={statement_timeout_ms}",
        **CONNECT_KWARGS,
    )


class Probe:
    """Runs the write loop in a background thread and keeps every Sample."""

    def __init__(
        self,
        dsn: str,
        run_id: str,
        *,
        interval: float = 0.2,
        connect_timeout: float = 3.0,
        statement_timeout_ms: int = 5000,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_sample: Callable[[Sample], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.dsn = dsn
        self.run_id = run_id
        self.interval = interval
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self._connect = connect or _connect_default
        self._clock = clock
        self._sleep = sleep
        self._on_sample = on_sample
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._t0: float | None = None
        self.samples: list[Sample] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ timing

    def now(self) -> float:
        """Seconds since the probe started (0 before it starts)."""
        return 0.0 if self._t0 is None else self._clock() - self._t0

    # ----------------------------------------------------------------- control

    def start(self, t0: float | None = None) -> None:
        """Start the background loop. Pass ``t0`` (a value of ``clock``) to share a
        time origin with another probe, so their ``t`` values line up."""
        self._t0 = self._clock() if t0 is None else t0
        self._thread = threading.Thread(target=self._loop, name="pghadr-probe", daemon=True)
        self._thread.start()

    def stop(self) -> list[Sample]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.connect_timeout + self.statement_timeout_ms / 1000 + 5)
        self._close()
        return self.snapshot()

    def snapshot(self) -> list[Sample]:
        with self._lock:
            return list(self.samples)

    def run_for(self, seconds: float) -> list[Sample]:
        """Run in the calling thread for ``seconds`` (used by ``pghadr probe``)."""
        self._t0 = self._clock()
        deadline = self._t0 + seconds
        seq = 0
        next_tick = self._t0
        while self._clock() < deadline and not self._stop.is_set():
            seq += 1
            self._record(self.tick(seq))
            next_tick = self._next_tick(next_tick)
            self._sleep(max(0.0, next_tick - self._clock()))
        self._close()
        return self.snapshot()

    # -------------------------------------------------------------------- loop

    def _loop(self) -> None:
        seq = 0
        assert self._t0 is not None
        next_tick = self._t0
        while not self._stop.is_set():
            seq += 1
            self._record(self.tick(seq))
            next_tick = self._next_tick(next_tick)
            wait = next_tick - self._clock()
            if wait > 0:
                self._stop.wait(wait)

    def _next_tick(self, prev: float) -> float:
        nxt = prev + self.interval
        now = self._clock()
        if nxt < now:  # fell behind: skip missed ticks instead of bursting
            missed = int((now - nxt) / self.interval) + 1
            nxt += missed * self.interval
        return nxt

    def _record(self, s: Sample) -> None:
        with self._lock:
            self.samples.append(s)
        if self._on_sample is not None:
            self._on_sample(s)

    def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def tick(self, seq: int) -> Sample:
        """One attempt: (re)connect if needed, then one committed INSERT."""
        t = self.now()
        wall_dt = datetime.now(timezone.utc)
        wall = wall_dt.isoformat(timespec="milliseconds")
        start = self._clock()
        sent = False
        try:
            if self._conn is None:
                self._conn = self._connect(
                    self.dsn, self.connect_timeout, self.statement_timeout_ms
                )
            sent = True
            row = self._conn.execute(INSERT_SQL, (self.run_id, seq, wall_dt)).fetchone()
            latency = (self._clock() - start) * 1000
            server, started = (row[0], row[1]) if row else (None, None)
            return Sample(seq, round(t, 4), wall, True, True, round(latency, 2), server, started)
        except Exception as exc:  # any failure is data, never a crash
            latency = (self._clock() - start) * 1000
            self._close()
            return Sample(
                seq, round(t, 4), wall, False, sent, round(latency, 2), error=error_kind(exc)
            )
