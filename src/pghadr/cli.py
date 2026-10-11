"""pghadr command line."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import click

from pghadr import __version__, analysis, cost, drill, report
from pghadr.azure import Az, AzError
from pghadr.events import write_jsonl
from pghadr.probe import Probe

ACTIONS = ("planned-failover", "forced-failover", "restart", "command", "none")


def _log(msg: str) -> None:
    click.echo(msg, err=True)


def _dsn_option(envvar: str = "PGHADR_DSN", name: str = "--dsn"):
    return click.option(
        name,
        envvar=envvar,
        required=True,
        show_envvar=True,
        help="libpq connection string. Put the password in PGPASSWORD, not here.",
    )


@click.group()
@click.version_option(__version__, prog_name="pghadr")
def main() -> None:
    """HA failover and cross-region DR drills for Azure Database for PostgreSQL."""


@main.command()
@_dsn_option()
def init(dsn: str) -> None:
    """Create the heartbeat table (safe to re-run)."""
    drill.init_schema(dsn)
    click.echo("pghadr_heartbeat is ready")


@main.command()
@_dsn_option()
@click.option("--seconds", type=float, default=30.0, show_default=True)
@click.option("--interval", type=float, default=0.2, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), help="Write every sample as JSON lines.")
def probe(dsn: str, seconds: float, interval: float, out: Path | None) -> None:
    """Run the write probe on its own and print a summary."""
    drill.init_schema(dsn)
    run_id = drill.new_run_id("probe")
    samples = Probe(dsn, run_id, interval=interval).run_for(seconds)
    if out:
        write_jsonl(samples, out)
    s = analysis.summarize(samples, interval)
    lat = s.latency_ms
    click.echo(
        f"{s.attempts} attempts, {s.ok} ok, {s.failed} failed "
        f"({s.availability_pct:g}% ok); latency p50 {lat['p50']} ms, p99 {lat['p99']} ms; "
        f"outages {len(s.outages)}, longest write gap {s.max_write_gap_s or 0:g}s"
    )


@main.command()
@click.option("--server", "-n", required=True, help="Flexible Server name.")
@click.option("--resource-group", "-g", required=True)
def status(server: str, resource_group: str) -> None:
    """Show the server's HA state and zones (via az)."""
    st = Az().server(server, resource_group)
    for k, v in st.to_dict().items():
        click.echo(f"{k:17} {v}")


def _action(name: str, server: str | None, group: str | None, command: str | None):
    if name == "none":
        return None
    if name == "command":
        if not command:
            raise click.UsageError("--action command needs --command")
        argv = shlex.split(command)
        return lambda: subprocess.run(argv, check=True, capture_output=True)
    if not server or not group:
        raise click.UsageError(f"--action {name} needs --server and --resource-group")
    az = Az()
    if name == "restart":
        return lambda: az.restart(server, group)
    mode = name.split("-")[0]  # planned / forced
    return lambda: az.restart(server, group, failover=mode)


def _state_fn(server: str | None, group: str | None):
    if not server or not group:
        return None
    az = Az()

    def state():
        try:
            return az.server(server, group).to_dict()
        except AzError as exc:
            _log(f"  could not read server state: {exc}")
            return None

    return state


def _finish(result: drill.DrillResult, out_dir: Path, name: str | None) -> None:
    md, js = report.write(result, out_dir, name)
    click.echo(report.to_markdown(result))
    _log(f"wrote {md} and {js}")
    bad = (result.loss is not None and not result.loss.ok) or bool(result.notes)
    if bad:
        sys.exit(1)


@main.command("drill")
@_dsn_option()
@click.option("--action", type=click.Choice(ACTIONS), required=True)
@click.option("--server", "-n", help="Flexible Server name (for failover/restart and state).")
@click.option("--resource-group", "-g")
@click.option(
    "--command", "command", help="For --action command: what to run, e.g. a docker restart."
)
@click.option(
    "--warmup", type=float, default=30.0, show_default=True, help="Seconds before the action."
)
@click.option(
    "--settle", type=float, default=30.0, show_default=True, help="Seconds after recovery."
)
@click.option("--interval", type=float, default=0.2, show_default=True)
@click.option("--timeout", type=float, default=600.0, show_default=True, help="Give up after this.")
@click.option("--out-dir", type=click.Path(path_type=Path), default=Path("runs"), show_default=True)
@click.option("--name", help="File name stem for the report (default: the run id).")
def drill_cmd(
    dsn, action, server, resource_group, command, warmup, settle, interval, timeout, out_dir, name
) -> None:
    """HA drill: probe, trigger a failover/restart/command, measure, verify no acknowledged loss."""
    result = drill.run_ha_drill(
        dsn,
        action=_action(action, server, resource_group, command),
        action_name=action if action != "command" else f"command: {command}",
        warmup_s=warmup,
        settle_s=settle,
        interval=interval,
        recover_timeout_s=timeout,
        state=_state_fn(server, resource_group),
        log=_log,
    )
    _finish(result, out_dir, name)


@main.command("dr-drill")
@_dsn_option()
@_dsn_option("PGHADR_REPLICA_DSN", "--replica-dsn")
@click.option("--replica", required=True, help="Read replica server name (it gets promoted).")
@click.option("--resource-group", "-g", required=True)
@click.option(
    "--promote-option",
    type=click.Choice(["forced", "planned"]),
    default="forced",
    show_default=True,
)
@click.option("--warmup", type=float, default=60.0, show_default=True)
@click.option("--interval", type=float, default=0.2, show_default=True)
@click.option("--timeout", type=float, default=900.0, show_default=True)
@click.option("--out-dir", type=click.Path(path_type=Path), default=Path("runs"), show_default=True)
@click.option("--name")
def dr_drill_cmd(
    dsn,
    replica_dsn,
    replica,
    resource_group,
    promote_option,
    warmup,
    interval,
    timeout,
    out_dir,
    name,
) -> None:
    """DR drill: write to the primary, promote the cross-region replica, measure RPO and RTO.

    The replica stops replicating for good. Recreate it afterwards if you need it.
    """
    az = Az()
    result = drill.run_dr_drill(
        dsn,
        replica_dsn,
        promote=lambda: az.promote_replica(replica, resource_group, promote_option),
        action_name=f"promote replica ({promote_option})",
        warmup_s=warmup,
        interval=interval,
        writable_timeout_s=timeout,
        state=_state_fn(replica, resource_group),
        log=_log,
    )
    _finish(result, out_dir, name)


@main.command("cost")
@click.option("--sku", default="Standard_D2ds_v5", show_default=True)
@click.option("--ha/--no-ha", default=True, show_default=True)
@click.option("--replicas", type=int, default=0, show_default=True)
@click.option("--storage-gb", type=int, default=32, show_default=True)
@click.option("--hours", type=float, default=3.0, show_default=True)
def cost_cmd(sku, ha, replicas, storage_gb, hours) -> None:
    """Estimate what a drill session costs (reference prices, see pghadr/cost.py)."""
    e = cost.estimate(sku, ha=ha, replicas=replicas, storage_gb=storage_gb, hours=hours)
    click.echo(
        f"{e.servers} x {sku} for {hours:g} h: compute ${e.compute_usd:.2f} + "
        f"storage ${e.storage_usd:.2f} = ${e.total_usd:.2f} "
        f"({cost.PRICES_REGION} pay-as-you-go, prices as of {cost.PRICES_AS_OF})"
    )
