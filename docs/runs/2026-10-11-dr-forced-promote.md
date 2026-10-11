# pghadr Cross-region DR drill: promote replica (forced)

- run: `dr-20261011T213130Z-0e1a8a` (pghadr 0.1.0)
- started: 2026-10-11T21:31:30+00:00
- server: `pghadr-x4baq3gxjvoxg-drw`
- probe: one committed INSERT every 0.20s, 932 attempts over 186.6s

## Result

| Metric | Value |
|---|---|
| Action took (as reported by the tool) | 66.8 s |
| Replica accepted writes after (RTO) | **16.3 s** |
| Primary writes acknowledged before the promote | 599 |
| ...found on the promoted replica | 599 |
| ...missing (data lost in a real regional failover) | **0** |
| RPO at the cutoff (age of the oldest missing write) | **0.000 s** |
| Replication kept running after the promote was requested | 7.0 s |
| Acknowledged writes missing before replication stopped (holes) | **0** |
| Availability during the run | 100.00 % |
| Write latency p50 / p95 / p99 | 25.3 ms / 35.4 ms / 40.3 ms |

## Timeline

One character per 2 s: `#` all writes ok, `.` all failed, `:` mixed.

```
##############################################################################################
```

## Events

| t (s) | Event |
|---:|---|
| 120.01 | promote replica (forced) started |
| 127.03 | last primary write that reached the replica |
| 136.34 | promoted replica accepted its first write |
| 186.79 | promote replica (forced) returned |

## Server

| | Before | After |
|---|---|---|
| State | Ready | Ready |
| SKU | Standard_D2ds_v5 | Standard_D2ds_v5 |
| PostgreSQL | 16 | 16 |
| Region | West US 3 | West US 3 |
| HA mode | Disabled | Disabled |
| HA state | NotEnabled | NotEnabled |
| Primary zone | 2 | 2 |
| Replication role | GeoAsyncReplica | Primary |

## How to read this

The write gap is measured from the last acknowledged write before an outage to the first one after it, so it is what an application that writes continuously would see. It overstates the true unavailability by at most one probe interval plus one write latency. A failed write that had already been sent is *in doubt*: the server may have committed it, and either outcome is correct, so it is never counted as lost.
