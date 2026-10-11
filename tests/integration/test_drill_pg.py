"""Drills against a real Postgres, with a real outage.

The outage: an admin connection (to a different database) blocks new
connections to the test database and terminates the existing ones, waits, then
lets connections back in. To the probe that looks like a failover: its
connection dies mid-flight and reconnects fail for a few seconds.

    PGHADR_TEST_DSN=postgresql://postgres:postgres@localhost:5432/pghadr_test
    PGHADR_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
import time

import psycopg
import pytest
from psycopg import sql

from pghadr import drill, report
from pghadr.probe import Probe

DSN = os.environ.get("PGHADR_TEST_DSN")
ADMIN = os.environ.get("PGHADR_TEST_ADMIN_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (DSN and ADMIN), reason="set PGHADR_TEST_DSN and PGHADR_TEST_ADMIN_DSN"),
]


def _dbname() -> str:
    with psycopg.connect(DSN) as c:
        return c.execute("SELECT current_database()").fetchone()[0]


def outage(seconds: float):
    """Block and kill connections to the test database for ``seconds``."""
    db = _dbname()

    def run():
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(db))
            )
            try:
                c.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (db,),
                )
                time.sleep(seconds)
            finally:
                c.execute(
                    sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(db))
                )

    return run


def test_probe_writes_and_sees_server():
    drill.init_schema(DSN)
    samples = Probe(DSN, drill.new_run_id("it"), interval=0.05).run_for(1.0)
    assert len(samples) >= 15
    assert all(s.ok for s in samples)
    assert samples[0].started  # pg_postmaster_start_time came back


def test_ha_drill_measures_outage_and_loses_nothing(tmp_path):
    result = drill.run_ha_drill(
        DSN,
        action=outage(3.0),
        action_name="block connections for 3 s",
        warmup_s=1.0,
        settle_s=1.0,
        interval=0.1,
        recover_timeout_s=60,
        log=lambda m: None,
    )
    s = result.summary
    assert result.notes == []
    assert len(s.outages) >= 1
    # the gap covers the 3 s block; allow for connect timeouts on top
    assert 2.5 <= s.max_write_gap_s <= 15
    assert result.loss.ok, result.loss.lost
    assert result.loss.acknowledged == s.ok
    # same server, same postmaster: an outage, not a switch
    assert s.switches == []
    md, _ = report.write(result, tmp_path, "it")
    assert "**0 lost**" in md.read_text()


def test_dr_drill_against_same_database_finds_everything():
    """With the 'replica' being the primary itself, nothing can be missing."""
    result = drill.run_dr_drill(
        DSN,
        DSN,
        promote=lambda: time.sleep(0.5),
        action_name="no-op promote",
        warmup_s=1.0,
        interval=0.1,
        replica_interval=0.2,
        writable_timeout_s=30,
        log=lambda m: None,
    )
    assert result.dr.missing == ()
    assert result.dr.rpo_s == 0.0
    assert result.dr.acknowledged_before_cutoff >= 5
    assert result.dr_rto_s is not None
    assert result.dr_rto_s < 5
