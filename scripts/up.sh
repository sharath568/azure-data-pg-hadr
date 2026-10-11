#!/usr/bin/env bash
# Deploy the pghadr drill environment to Azure and write .env.azure.
#
# Usage:
#   ./scripts/up.sh             # zone-redundant HA primary
#   ./scripts/up.sh --replica   # same, plus a read replica in REPLICA_LOCATION
#                               # (safe to run again on an existing deployment)
#
# Optional environment variables:
#   RG                resource group name             (default: rg-mvp-pghadr)
#   LOCATION          region for the primary          (default: centralus)
#   REPLICA_LOCATION  region for the read replica     (default: westus3)
#   CLIENT_IP         IP allowed through the firewall (default: auto-detect)
#   SKU               compute size                    (default: Standard_D2ds_v5)
#   HA_MODE           ZoneRedundant | SameZone        (default: ZoneRedundant)
#   EXPIRES_DAYS      days until the lab sweeper may delete the group (default: 1)
#
# This bills by the hour: about $0.40/h for the HA pair, plus about $0.20/h for
# the replica (pghadr cost). Delete everything the same day: ./scripts/down.sh
set -euo pipefail

RG="${RG:-rg-mvp-pghadr}"
LOCATION="${LOCATION:-centralus}"
REPLICA_LOCATION="${REPLICA_LOCATION:-westus3}"
SKU="${SKU:-Standard_D2ds_v5}"
HA_MODE="${HA_MODE:-ZoneRedundant}"
EXPIRES_DAYS="${EXPIRES_DAYS:-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ENV="$ROOT/.env.azure"
REPLICA=false
[[ "${1:-}" == "--replica" ]] && REPLICA=true

command -v az >/dev/null || { echo "Azure CLI (az) is required: https://aka.ms/azcli" >&2; exit 1; }

if [[ -z "${CLIENT_IP:-}" ]]; then
  CLIENT_IP="$(curl -fsS https://api.ipify.org || true)"
  [[ -n "$CLIENT_IP" ]] || { echo "could not detect your IP; set CLIENT_IP" >&2; exit 1; }
fi

# Re-use the password from an earlier run, so --replica doesn't reset the admin.
if [[ -z "${ADMIN_PASSWORD:-}" && -f "$OUT_ENV" ]]; then
  ADMIN_PASSWORD="$(sed -n 's/^PGPASSWORD=//p' "$OUT_ENV")"
fi
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)Aa1!}"

if date -v+1d >/dev/null 2>&1; then
  EXPIRES="$(date -v+"${EXPIRES_DAYS}"d +%F)"
else
  EXPIRES="$(date -d "+${EXPIRES_DAYS} days" +%F)"
fi

echo "Resource group : $RG ($LOCATION), expires $EXPIRES"
echo "Primary        : $SKU, PostgreSQL 16, 32 GiB, HA $HA_MODE"
echo "Read replica   : $([[ $REPLICA == true ]] && echo "$REPLICA_LOCATION" || echo none)"
echo "Client IP      : $CLIENT_IP"

# Save the password before deploying: if the deployment fails half-way (the
# server exists, the replica doesn't), you still need it.
umask 077
{
  echo "# Written by scripts/up.sh. Contains a secret; do not commit."
  echo "PGPASSWORD=$ADMIN_PASSWORD"
  echo "PGHADR_RG=$RG"
} > "$OUT_ENV"

az group create --name "$RG" --location "$LOCATION" \
  --tags project=mvp-lab app=pghadr expires="$EXPIRES" --output none

OUTPUTS="$(az deployment group create \
  --resource-group "$RG" \
  --name pghadr \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters \
      clientIp="$CLIENT_IP" \
      adminPassword="$ADMIN_PASSWORD" \
      skuName="$SKU" \
      haMode="$HA_MODE" \
      deployReplica="$REPLICA" \
      replicaLocation="$REPLICA_LOCATION" \
      tags="{\"project\":\"mvp-lab\",\"app\":\"pghadr\",\"expires\":\"$EXPIRES\"}" \
  --query properties.outputs --output json)"

out() { echo "$OUTPUTS" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }

SERVER="$(out serverName)"
FQDN="$(out fqdn)"
DB="$(out databaseName)"
ADMIN="$(out adminLogin)"

{
  echo "# Written by scripts/up.sh. Contains a secret; do not commit."
  echo "# Load with:  set -a; . ./.env.azure; set +a"
  echo "PGPASSWORD=$ADMIN_PASSWORD"
  echo "PGHADR_DSN=\"host=$FQDN port=5432 dbname=$DB user=$ADMIN sslmode=require\""
  echo "PGHADR_SERVER=$SERVER"
  echo "PGHADR_RG=$RG"
  if [[ "$REPLICA" == true ]]; then
    echo "PGHADR_REPLICA=$(out replicaName)"
    echo "PGHADR_REPLICA_DSN=\"host=$(out replicaFqdn) port=5432 dbname=$DB user=$ADMIN sslmode=require\""
  fi
} > "$OUT_ENV"
chmod 600 "$OUT_ENV"

cat <<MSG

Done. Connection details are in .env.azure (mode 600, git-ignored).

Next:
  set -a; . ./.env.azure; set +a
  pghadr status -n "\$PGHADR_SERVER" -g "\$PGHADR_RG"
  pghadr drill --action planned-failover -n "\$PGHADR_SERVER" -g "\$PGHADR_RG" --out-dir docs/runs
  pghadr drill --action forced-failover  -n "\$PGHADR_SERVER" -g "\$PGHADR_RG" --out-dir docs/runs

When you are finished:  ./scripts/down.sh
MSG
