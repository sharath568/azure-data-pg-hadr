# pghadr: measure your Azure PostgreSQL failover, don't assume it

[![CI](https://github.com/sharath568/azure-data-pg-hadr/actions/workflows/ci.yml/badge.svg)](https://github.com/sharath568/azure-data-pg-hadr/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Every HA design review I sat in as an Oracle DBA had the same two numbers on
a slide: RPO and RTO. Almost nobody had measured them. The figures came from
the vendor's documentation, and the documentation describes what the
database does, not what your application sees.

`pghadr` measures what the application sees. It runs a small write probe
against Azure Database for PostgreSQL Flexible Server, triggers a real
failover (or a cross-region replica promote), and reports:

- **the write gap**: how long writes actually stopped, from the client's side
- **acknowledged writes lost**: every committed write is checked afterwards
- **in-doubt writes**: sent, then failed; committed or not, both are correct
- **the zone and node** before and after, from Azure and from the server itself
- for DR: **RPO** (writes the primary acknowledged that never reached the
  replica) and **RTO** (time until the promoted replica accepted a write)

Everything runs offline against Docker too, so you can try it for free.

## Results from a real run

Azure Database for PostgreSQL Flexible Server, General Purpose
`Standard_D2ds_v5`, PostgreSQL 16, zone-redundant HA, Central US, probed from
a laptop over the internet, one committed INSERT every 200 ms. 11 October 2026.
One run of each; treat them as observations, not averages.

| Drill | Client write gap | `az` call took | Acknowledged writes | Lost | Zone |
|---|---:|---:|---:|---:|---|
| [Planned failover](docs/runs/2026-10-11-ha-planned.md) | **46.9 s** | 382.7 s | 2,247 | **0** | 1 → 2 |
| [Forced failover](docs/runs/2026-10-11-ha-forced.md) | **24.1 s** | 363.6 s | 2,279 | **0** | 2 → 1 |

What stood out:

1. **The control plane is not the outage.** `az postgres flexible-server
   restart --failover` took about six minutes to return both times, but writes
   were back after 47 s and 24 s. Azure keeps rebuilding the old primary as
   the new standby long after clients are served. If your runbook waits for
   the command to finish before declaring recovery, it overstates RTO by
   minutes.
2. **Planned was slower than forced** in this run. A planned failover is the
   gentle one (it lets the standby catch up and shuts the primary down
   cleanly), so I expected the opposite. One run each is not enough to call
   it a rule, but it is enough to stop assuming "planned means shorter".
3. **Zero acknowledged writes lost, both times.** Zone-redundant HA commits
   synchronously to the standby, and the drill confirms it: every write that
   got a COMMIT back was in the table afterwards. The one in-doubt write per
   run (the connection dropped after the INSERT was sent) was not committed,
   which is also correct.
4. **What the application saw was mostly reconnect failures**: `could not
   receive data from server`, a few `connection timeout expired`, and one
   `SSL error: unexpected eof` when the old primary went away mid-statement.
   An application needs retry logic for all three.
5. **Latency stayed flat.** Write p50 was 28 to 34 ms (most of that is the
   laptop-to-Central-US round trip) before and after the failover.

The raw files are in [`docs/runs/`](docs/runs/): a Markdown report, a JSON
summary and every single probe attempt as JSON lines.

### Cross-region DR

A read replica in West US 3, force-promoted while the probe kept writing to
the Central US primary at 5 writes per second
([report](docs/runs/2026-10-11-dr-forced-promote.md)):

| Metric | Value |
|---|---:|
| Promoted replica accepted its first write (checked every 1 s) | **16.3 s** after the promote was requested |
| `az ... replica promote` returned | 66.8 s after the request |
| Primary writes acknowledged before the request | 599 |
| ...missing on the promoted replica | **0** |
| Replication kept running after the request | 7.0 s |
| Acknowledged writes missing before replication stopped | **0** |

Two things here are easy to get wrong:

1. **A requested promote doesn't cut replication at once.** The primary
   kept shipping WAL for another 7 seconds, and 36 writes acknowledged
   *after* the request still reached the replica. So "nothing was missing at
   the cutoff" flatters the result. The honest measure is the last column:
   when replication finally stopped, the replica held every earlier write
   with no holes. At this write rate it was less than one probe interval
   (200 ms) behind. That lag, not the cutoff, is what losing the primary
   region would cost you, and it grows with write load. Measure it under
   yours.
2. **Asynchronous means you choose.** The HA standby can't lose an
   acknowledged write; a cross-region replica can. This run lost nothing
   because the load was light, not because the design guarantees it.

"Writable" didn't mean "stable" straight away. The replica probe (one write
per second, in
[`replica-samples.jsonl`](docs/runs/2026-10-11-dr-forced-promote.replica-samples.jsonl))
shows the promoted server accepting writes from about 16 s, then dropping the
connection with `terminating connection due to administrator command` at
about 25 s and refusing connections until about 32 s. Its
`pg_postmaster_start_time()` changed from 21:28:17 to 21:34:04 UTC, so it
restarted once after it first became writable. Keep retrying for the first
minute after a promote.

The replica was writable before the control-plane call finished, just like
the HA failovers. An application also has to switch its connection string to
the replica, because a standalone promote gives it a new name. Azure's
virtual endpoints can hide that switch; this drill measures the raw promote.

## How it works

```mermaid
flowchart LR
    probe["pghadr probe<br/>INSERT every 200 ms<br/>keepalives, tcp_user_timeout,<br/>statement_timeout, reconnect"]
    subgraph az["Azure, Central US"]
        dns["server FQDN"]
        p[("primary<br/>zone 1")]
        s[("standby<br/>zone 2")]
        p -- "synchronous" --> s
    end
    r[("read replica<br/>West US 3")]
    probe --> dns --> p
    p -. "asynchronous" .-> r
    drill["pghadr drill / dr-drill"] -- "az ... restart --failover<br/>az ... replica promote" --> az
    drill --> probe
    drill -- "after: which acknowledged<br/>seq numbers survived?" --> p
```

1. The probe writes `(run_id, seq)` rows to `pghadr_heartbeat` on a fixed
   schedule. Each attempt is recorded: when it started, how long it took,
   whether it was acknowledged, and which node answered (`inet_server_addr()`
   and `pg_postmaster_start_time()`).
2. After a warm-up, the drill triggers the action through the Azure CLI and
   keeps probing until writes have been flowing again for a while.
3. It reads back every `seq` for the run and compares it with the set of
   acknowledged writes. Anything acknowledged and missing is lost.

The probe is configured like a careful application, not like a benchmark:
one reused connection, TCP keepalives and `tcp_user_timeout` so a dead
socket fails in seconds, a `statement_timeout`, and a fresh DNS lookup on
every reconnect, which is how a client follows the server name to the new
primary. The definitions of write gap, switch, lost and in-doubt are at the
top of [`src/pghadr/analysis.py`](src/pghadr/analysis.py).

## Try it locally (free)

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -e .
export PGHADR_DSN=postgresql://postgres:postgres@localhost:5432/pghadr_test
pghadr drill --action command --command "docker restart pghadr-pg" --warmup 10 --settle 10
```

A container restart is not a failover, but it exercises the same path: the
connection dies mid-statement, reconnects fail, then writes resume. The report
shows a short gap, a "server restarted" event and zero lost writes.

## Run it on Azure

```bash
pghadr cost --replicas 1 --hours 3     # about $1.86 at Central US list prices
./scripts/up.sh --replica              # HA pair + read replica, 15-25 min
./scripts/drills.sh --dr               # planned, forced, then the DR promote
./scripts/down.sh                      # delete everything, same day
```

`up.sh` writes the connection details to `.env.azure` (git-ignored, mode
600). The password is passed through `PGPASSWORD`, never on the command line.
Region and cost notes, including a Free Trial region restriction I ran into,
are in [`infra/README.md`](infra/README.md).

## Commands

| Command | What it does |
|---|---|
| `pghadr init` | Create the heartbeat table |
| `pghadr probe --seconds 60` | Run the probe on its own and print a summary |
| `pghadr status -n <server> -g <rg>` | HA mode, state and zones, via `az` |
| `pghadr drill --action planned-failover\|forced-failover\|restart\|command\|none` | HA drill |
| `pghadr dr-drill --replica <name> -g <rg>` | Force-promote a read replica, measure RPO and RTO |
| `pghadr cost` | Estimate a session's cost from reference prices |

`drill` and `dr-drill` exit non-zero if an acknowledged write was lost or
writes never recovered, so they can gate a pipeline.

## Limitations

- **One client, one region.** The probe runs where you run it. An application
  in the same Azure region would see lower latency and possibly a different
  gap, because DNS caching and connection pools behave differently there.
- **Resolution is the probe interval.** The write gap is accurate to about one
  interval (200 ms) plus one write latency.
- **One run is one run.** Failover times vary with load, region and Azure's
  own maintenance. Run the drill several times before you put a number in a
  design document.
- **The DR drill ends replication.** A promoted replica is a standalone server;
  recreate the replica afterwards if you need one.

## License

MIT
