"""A thin wrapper over the Azure CLI (``az``) for the few calls a drill needs.

Shelling out to ``az`` keeps the dependency list short and means the drill
uses whatever login you already have (``az login``, a service principal in
CI, a managed identity). Every call goes through one ``runner`` so tests can
replace it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

Runner = Callable[[Sequence[str]], str]

FAILOVER_MODES = {"planned": "Planned", "forced": "Forced"}


class AzError(RuntimeError):
    pass


def run_az(args: Sequence[str]) -> str:
    exe = shutil.which("az")
    if exe is None:
        raise AzError("the Azure CLI (az) is not installed or not on PATH")
    proc = subprocess.run(
        [exe, *args, "--only-show-errors"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise AzError(f"az {' '.join(args[:3])} failed: {msg[-1] if msg else proc.returncode}")
    return proc.stdout


@dataclass(frozen=True)
class ServerState:
    name: str
    state: str | None
    location: str | None
    version: str | None
    sku: str | None
    tier: str | None
    zone: str | None
    ha_mode: str | None
    ha_state: str | None
    standby_zone: str | None
    fqdn: str | None
    replication_role: str | None

    @classmethod
    def from_show(cls, d: dict[str, Any]) -> ServerState:
        ha = d.get("highAvailability") or {}
        sku = d.get("sku") or {}
        return cls(
            name=d.get("name", ""),
            state=d.get("state"),
            location=d.get("location"),
            version=d.get("version"),
            sku=sku.get("name"),
            tier=sku.get("tier"),
            zone=d.get("availabilityZone"),
            ha_mode=ha.get("mode"),
            ha_state=ha.get("state"),
            standby_zone=ha.get("standbyAvailabilityZone"),
            fqdn=d.get("fullyQualifiedDomainName"),
            replication_role=d.get("replicationRole"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Az:
    def __init__(self, runner: Runner = run_az) -> None:
        self._run = runner

    def _json(self, args: Sequence[str]) -> Any:
        out = self._run([*args, "-o", "json"])
        return json.loads(out) if out.strip() else None

    def server(self, name: str, group: str) -> ServerState:
        d = self._json(["postgres", "flexible-server", "show", "-n", name, "-g", group])
        return ServerState.from_show(d or {})

    def restart(self, name: str, group: str, failover: str | None = None) -> None:
        """Restart, optionally with an HA failover (``planned`` or ``forced``).

        ``az`` waits for the operation to finish, so this returns once Azure
        reports the restart or failover as done.
        """
        args = ["postgres", "flexible-server", "restart", "-n", name, "-g", group]
        if failover is not None:
            if failover not in FAILOVER_MODES:
                raise ValueError(f"failover must be one of {sorted(FAILOVER_MODES)}")
            args += ["--failover", FAILOVER_MODES[failover]]
        self._run(args)

    def promote_replica(self, replica: str, group: str, option: str = "forced") -> None:
        """Promote a read replica to a standalone, writable server.

        ``forced`` promotes at once, without waiting for the replica to catch up.
        That is what you would do if the primary region were gone, and it is the
        mode that exposes replication lag as data loss.
        """
        if option not in ("forced", "planned"):
            raise ValueError("option must be 'forced' or 'planned'")
        self._run(
            [
                "postgres", "flexible-server", "replica", "promote",
                "-n", replica, "-g", group,
                "--promote-mode", "standalone", "--promote-option", option, "--yes",
            ]
        )  # fmt: skip
