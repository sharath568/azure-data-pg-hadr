# pghadr HA drill: planned-failover

- run: `ha-20261011T210212Z-95a3ad` (pghadr 0.1.0)
- started: 2026-10-11T21:02:12+00:00
- server: `pghadr-x4baq3gxjvoxg`
- probe: one committed INSERT every 0.20s, 2303 attempts over 502.8s

## Result

| Metric | Value |
|---|---|
| Action took (as reported by the tool) | 382.7 s |
| Longest write gap seen by the client | **46.9 s** |
| Outages (runs of failed writes) | 1 |
| Node switches | 1 |
| Acknowledged writes | 2247 |
| Acknowledged writes missing afterwards | **0 lost** |
| In-doubt writes (sent, then failed) | 1, of which 0 were committed |
| Availability during the run | 97.57 % |
| Write latency p50 / p95 / p99 | 33.9 ms / 40.7 ms / 53.0 ms |

## Timeline

One character per 5 s: `#` all writes ok, `.` all failed, `:` mixed.

```
##############: .......##############################################################################
```

## Events

| t (s) | Event |
|---:|---|
| 60.01 | planned-failover started |
| 73.40 | first failed write (OperationalError: connection failed: connection to server at "52.230.185.172", port 5432 failed: could not receive data from server: Co... (x47); ConnectionTimeout: connection timeout expired (x8); OperationalError: consuming input failed: SSL error: unexpected eof while reading (x1)) |
| 117.61 | now answered by `10.6.0.5` (was `10.6.0.4`) |
| 120.08 | writes back after a 46.9 s gap |
| 442.70 | planned-failover returned |

## Server

| | Before | After |
|---|---|---|
| State | Ready | Ready |
| SKU | Standard_D2ds_v5 | Standard_D2ds_v5 |
| PostgreSQL | 16 | 16 |
| Region | Central US | Central US |
| HA mode | ZoneRedundant | ZoneRedundant |
| HA state | Healthy | Healthy |
| Primary zone | 1 | 2 |
| Standby zone | 2 | 1 |
| Replication role | Primary | Primary |

## How to read this

The write gap is measured from the last acknowledged write before an outage to the first one after it, so it is what an application that writes continuously would see. It overstates the true unavailability by at most one probe interval plus one write latency. A failed write that had already been sent is *in doubt*: the server may have committed it, and either outcome is correct, so it is never counted as lost.
