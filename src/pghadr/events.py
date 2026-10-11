"""What the probe records: one Sample per attempted write."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sample:
    """One probe tick.

    ``t`` is seconds since the probe started (monotonic clock), taken just before
    the attempt. ``latency_ms`` covers the whole attempt, including a reconnect
    when the previous one failed. ``sent`` says whether the INSERT reached the
    wire: a failure before that (connect refused, DNS, timeout) cannot have
    written anything, a failure after it is "in doubt" (the server may have
    committed and the reply was lost).
    """

    seq: int
    t: float
    wall: str
    ok: bool
    sent: bool
    latency_ms: float
    server: str | None = None
    started: str | None = None
    error: str | None = None

    @property
    def t_done(self) -> float:
        """When the attempt finished, in probe seconds."""
        return self.t + self.latency_ms / 1000.0

    @property
    def node(self) -> tuple[str | None, str | None]:
        """Identifies the server that answered: its address and postmaster start time.

        After an Azure HA failover the address of the answering node changes; after
        a plain restart only the start time does. Either change counts as a switch.
        """
        return (self.server, self.started)


def write_jsonl(samples: list[Sample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(asdict(s), separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[Sample]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(Sample(**json.loads(line)))
    return out
