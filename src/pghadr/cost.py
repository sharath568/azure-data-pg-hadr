"""What a drill costs, before you run it.

Reference prices: Azure retail prices API (prices.azure.com), pay-as-you-go,
Central US, USD, checked 2026-10-11. They change; check before you rely on them.
HA doubles compute and storage (the standby is a full second server). A read
replica is a third server.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_AS_OF = "2026-10-11"
PRICES_REGION = "centralus"

# USD per hour of compute
COMPUTE_HOURLY = {
    "Standard_B1ms": 0.0192,
    "Standard_D2ds_v5": 0.201,
    "Standard_D4ds_v5": 0.402,
}

# USD per GB-month of provisioned storage ("Storage Data Stored" meter, Premium SSD)
STORAGE_GB_MONTH = 0.13
HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class Estimate:
    sku: str
    servers: int
    storage_gb: int
    hours: float
    compute_usd: float
    storage_usd: float

    @property
    def total_usd(self) -> float:
        return round(self.compute_usd + self.storage_usd, 2)


def estimate(
    sku: str = "Standard_D2ds_v5",
    *,
    ha: bool = True,
    replicas: int = 0,
    storage_gb: int = 32,
    hours: float = 3.0,
) -> Estimate:
    if sku not in COMPUTE_HOURLY:
        raise ValueError(f"no reference price for {sku}; known: {sorted(COMPUTE_HOURLY)}")
    if hours < 0 or replicas < 0:
        raise ValueError("hours and replicas must not be negative")
    servers = (2 if ha else 1) + replicas
    compute = COMPUTE_HOURLY[sku] * servers * hours
    storage = STORAGE_GB_MONTH * storage_gb * servers * hours / HOURS_PER_MONTH
    return Estimate(sku, servers, storage_gb, hours, round(compute, 2), round(storage, 2))
