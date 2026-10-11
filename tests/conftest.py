from __future__ import annotations

import pytest

from pghadr.events import Sample


def ok(
    seq: int, t: float, server: str = "10.0.0.4", started: str = "s1", ms: float = 2.0
) -> Sample:
    return Sample(seq, t, f"w{seq}", True, True, ms, server, started)


def fail(seq: int, t: float, sent: bool = False, err: str = "OperationalError: x") -> Sample:
    return Sample(seq, t, f"w{seq}", False, sent, 3000.0 if not sent else 5.0, error=err)


@pytest.fixture
def failover_samples() -> list[Sample]:
    """10 ok writes on node A, 3 failures (one in doubt), then 5 ok writes on node B."""
    out = [ok(i, i * 0.2) for i in range(1, 11)]  # t 0.2 .. 2.0
    out += [
        fail(11, 2.2, sent=True, err="AdminShutdown: terminating connection"),
        fail(12, 5.2),
        fail(13, 8.2),
    ]
    out += [ok(i, 11.2 + (i - 14) * 0.2, server="10.0.0.5", started="s2") for i in range(14, 19)]
    return out
