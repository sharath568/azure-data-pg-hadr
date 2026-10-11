import json

import pytest

from pghadr import cost
from pghadr.azure import Az, ServerState

SHOW = {
    "name": "pghadr-x",
    "state": "Ready",
    "location": "Central US",
    "version": "16",
    "sku": {"name": "Standard_D2ds_v5", "tier": "GeneralPurpose"},
    "availabilityZone": "1",
    "highAvailability": {
        "mode": "ZoneRedundant",
        "state": "Healthy",
        "standbyAvailabilityZone": "2",
    },
    "fullyQualifiedDomainName": "pghadr-x.postgres.database.azure.com",
    "replicationRole": "Primary",
}


class Recorder:
    def __init__(self, out: str = "") -> None:
        self.calls: list[list[str]] = []
        self.out = out

    def __call__(self, args):
        self.calls.append(list(args))
        return self.out


def test_server_state_parses_show():
    rec = Recorder(json.dumps(SHOW))
    st = Az(rec).server("pghadr-x", "rg")
    assert rec.calls[0][:3] == ["postgres", "flexible-server", "show"]
    assert rec.calls[0][-2:] == ["-o", "json"]
    assert st.zone == "1"
    assert st.standby_zone == "2"
    assert st.ha_mode == "ZoneRedundant"
    assert st.ha_state == "Healthy"
    assert st.sku == "Standard_D2ds_v5"
    assert st.to_dict()["fqdn"].endswith(".azure.com")


def test_server_state_tolerates_missing_fields():
    st = ServerState.from_show({"name": "n"})
    assert st.ha_mode is None
    assert st.zone is None


@pytest.mark.parametrize(("mode", "flag"), [("planned", "Planned"), ("forced", "Forced")])
def test_failover_args(mode, flag):
    rec = Recorder()
    Az(rec).restart("s", "g", failover=mode)
    assert rec.calls == [
        ["postgres", "flexible-server", "restart", "-n", "s", "-g", "g", "--failover", flag]
    ]


def test_plain_restart_and_bad_mode():
    rec = Recorder()
    Az(rec).restart("s", "g")
    assert "--failover" not in rec.calls[0]
    with pytest.raises(ValueError):
        Az(rec).restart("s", "g", failover="sideways")


def test_promote_args():
    rec = Recorder()
    Az(rec).promote_replica("r", "g")
    args = rec.calls[0]
    assert args[:4] == ["postgres", "flexible-server", "replica", "promote"]
    assert args[args.index("--promote-mode") + 1] == "standalone"
    assert args[args.index("--promote-option") + 1] == "forced"
    assert "--yes" in args
    with pytest.raises(ValueError):
        Az(rec).promote_replica("r", "g", option="soon")


def test_cost_ha_doubles_servers():
    e = cost.estimate("Standard_D2ds_v5", ha=True, hours=1)
    assert e.servers == 2
    assert e.compute_usd == pytest.approx(0.40, abs=0.01)
    single = cost.estimate("Standard_D2ds_v5", ha=False, hours=1)
    assert single.servers == 1


def test_cost_replica_and_storage():
    e = cost.estimate("Standard_D2ds_v5", ha=True, replicas=1, storage_gb=32, hours=730)
    assert e.servers == 3
    assert e.storage_usd == pytest.approx(0.13 * 32 * 3, abs=0.01)
    assert e.total_usd == pytest.approx(e.compute_usd + e.storage_usd, abs=0.01)


def test_cost_rejects_unknown_sku_and_negative():
    with pytest.raises(ValueError):
        cost.estimate("Standard_E64s_v9")
    with pytest.raises(ValueError):
        cost.estimate(hours=-1)
