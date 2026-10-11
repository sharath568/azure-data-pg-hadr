import json

from click.testing import CliRunner

from conftest import ok
from pghadr import analysis, report
from pghadr.cli import main
from pghadr.drill import ActionRecord, DrillResult, schema_sql
from pghadr.events import read_jsonl


def ha_result(samples, lost=()):
    acked = {s.seq for s in samples if s.ok} - set(lost)
    return DrillResult(
        kind="ha",
        run_id="ha-test",
        started="2026-10-11T20:00:00+00:00",
        params={"interval_s": 0.2},
        action=ActionRecord("forced-failover", 2.1, 40.5),
        samples=samples,
        summary=analysis.summarize(samples, 0.2),
        state_before={"name": "pghadr-x", "zone": "1", "standby_zone": "2", "ha_state": "Healthy"},
        state_after={"name": "pghadr-x", "zone": "2", "standby_zone": "1", "ha_state": "Healthy"},
        loss=analysis.check_loss(samples, acked),
    )


def test_markdown_ha(failover_samples):
    md = report.to_markdown(ha_result(failover_samples))
    assert "# pghadr HA drill: forced-failover" in md
    assert "Longest write gap seen by the client | **9.2 s**" in md
    assert "**0 lost**" in md
    assert "| Primary zone | 1 | 2 |" in md
    assert "now answered by `10.0.0.5` (was `10.0.0.4`)" in md
    assert "```" in md  # timeline


def test_markdown_reports_lost_writes(failover_samples):
    md = report.to_markdown(ha_result(failover_samples, lost=(9,)))
    assert "**1 LOST**" in md
    assert "Sequence numbers: 9" in md


def test_markdown_dr():
    samples = [ok(i, i * 0.2) for i in range(1, 11)]
    r = DrillResult(
        kind="dr",
        run_id="dr-test",
        started="2026-10-11T20:00:00+00:00",
        params={},
        action=ActionRecord("promote replica (forced)", 1.5, 90.0),
        samples=samples,
        summary=analysis.summarize(samples, 0.2),
        dr=analysis.check_dr(samples, 1.5, {1, 2, 3, 4}),
        replica_writable_t=95.25,
    )
    md = report.to_markdown(r)
    assert "Cross-region DR drill" in md
    assert "Replica accepted writes after (RTO) | **93.8 s**" in md
    assert "missing (data lost in a real regional failover) | **3**" in md
    d = report.to_dict(r)
    assert d["dr"]["missing"] == [5, 6, 7] or d["dr"]["missing"] == (5, 6, 7)
    assert d["dr"]["rto_s"] == 93.75


def test_write_files(tmp_path, failover_samples):
    md, js = report.write(ha_result(failover_samples), tmp_path, "run1")
    assert md.read_text().startswith("# pghadr")
    d = json.loads(js.read_text())
    assert d["summary"]["max_write_gap_s"] == 9.2
    assert d["loss"]["ok"] is True
    assert d["summary"]["switches"][0]["after"] == ["10.0.0.5", "s2"]
    assert len(read_jsonl(tmp_path / "run1.samples.jsonl")) == len(failover_samples)


def test_schema_sql_is_found():
    assert "CREATE TABLE IF NOT EXISTS pghadr_heartbeat" in schema_sql()


def test_cli_cost():
    res = CliRunner().invoke(main, ["cost", "--hours", "2"])
    assert res.exit_code == 0, res.output
    assert "2 x Standard_D2ds_v5 for 2 h" in res.output


def test_cli_version_and_help():
    r = CliRunner()
    assert r.invoke(main, ["--version"]).exit_code == 0
    out = r.invoke(main, ["--help"]).output
    for cmd in ("drill", "dr-drill", "probe", "status", "cost", "init"):
        assert cmd in out


def test_cli_drill_needs_server_for_failover():
    res = CliRunner().invoke(
        main, ["drill", "--dsn", "host=x", "--action", "forced-failover", "--warmup", "0"]
    )
    assert res.exit_code != 0
    assert "needs --server and --resource-group" in res.output


def test_cli_drill_command_needs_command():
    res = CliRunner().invoke(main, ["drill", "--dsn", "host=x", "--action", "command"])
    assert res.exit_code != 0
    assert "needs --command" in res.output


def test_markdown_restart_is_not_called_a_new_node():
    samples = [ok(1, 0.0, started="a"), ok(2, 0.2, started="b")]
    md = report.to_markdown(ha_result(samples))
    assert "new postmaster start time (server restarted)" in md
    assert "now answered by" not in md
