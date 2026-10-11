# Azure setup for pghadr

`main.bicep` creates the smallest environment that can show a real HA
failover and a real cross-region promote:

| Resource | Size | Why |
|---|---|---|
| Flexible Server (primary) | General Purpose `Standard_D2ds_v5`, 32 GiB, PostgreSQL 16 | HA needs General Purpose or Memory Optimized |
| HA standby | same size, another availability zone | `ZoneRedundant`: commits wait for the standby, so a failover should lose nothing |
| Read replica (optional) | same size, `westus3` by default | asynchronous, so a forced promote can lose the last few writes; that's what the DR drill measures |
| Firewall rule | your public IP only | |

## Region

Zone-redundant HA is not offered in every region, and what a region offers can
depend on your subscription type. Check before you deploy:

```bash
az postgres flexible-server list-skus -l centralus \
  --query "[0].{zoneRedundantHa:zoneRedundantHaSupported, geoBackup:geoBackupSupported}"
```

On the Free Trial subscription used for the published runs (October 2026),
`eastus2` reported `zoneRedundantHaSupported: Disabled` while `centralus` and
`westus3` reported `Enabled`. Creating a read replica in `eastus2` failed too:
through Bicep with an opaque `InternalServerError`, and through
`az postgres flexible-server replica create` with "The location is restricted
from performing this operation". So the primary runs in `centralus` and the
replica in `westus3`. If your subscription has no such restriction, the paired
region (`eastus2` for `centralus`) is the usual choice.

A replica created with `az postgres flexible-server replica create` instead
of this template does not get a firewall rule, so connections to it time out.
Add one: `az postgres flexible-server firewall-rule create -s <replica> -g <rg>
-n client --start-ip-address <ip> --end-ip-address <ip>`. The template adds it
for you.

## Cost

Reference prices, Central US pay-as-you-go, checked 2026-10-11
([Azure retail prices API](https://prices.azure.com/api/retail/prices)):

| Item | Price |
|---|---|
| `Standard_D2ds_v5` compute | $0.201 per hour, per server |
| Storage | $0.13 per GB-month, per server |

So the HA pair is about **$0.40 per hour**, and the replica adds about
**$0.20 per hour**. A full session (deploy, both HA drills, the DR drill,
delete) takes 2 to 3 hours: about **$1.50**. `pghadr cost --replicas 1 --hours 3`
prints the estimate.

The things that cost real money are the ones you forget: a General Purpose HA
pair left running for a week is about $70. `scripts/up.sh` tags the resource
group with an `expires` date for a sweeper, and `scripts/down.sh` deletes the
whole group. Run it the same day.

## Deploy

```bash
./scripts/up.sh --replica     # about 15-25 minutes
set -a; . ./.env.azure; set +a
pghadr status -n "$PGHADR_SERVER" -g "$PGHADR_RG"
```

## Delete

```bash
./scripts/down.sh
```
