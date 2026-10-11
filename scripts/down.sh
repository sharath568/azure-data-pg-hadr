#!/usr/bin/env bash
# Delete the whole pghadr resource group: the HA pair, the replica (or the
# promoted ex-replica) and all their data. There is no undo.
set -euo pipefail
RG="${RG:-rg-mvp-pghadr}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v az >/dev/null || { echo "Azure CLI (az) is required" >&2; exit 1; }
echo "Deleting resource group $RG (runs in the background)..."
az group delete --name "$RG" --yes --no-wait
rm -f "$ROOT/.env.azure"
echo "Requested. Check progress with: az group show --name $RG"
