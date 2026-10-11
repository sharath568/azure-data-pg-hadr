#!/usr/bin/env bash
# Run the published drill sequence against the environment from scripts/up.sh:
#   1. planned HA failover
#   2. forced HA failover (fails back to the original zone)
#   3. cross-region DR: forced promote of the read replica (only with --replica;
#      it ends replication for good, so it runs last)
#
# Reports land in docs/runs/. Usage:  ./scripts/drills.sh [--dr]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$ROOT/docs/runs}"
DAY="$(date -u +%F)"
[[ -f "$ROOT/.env.azure" ]] || { echo "run ./scripts/up.sh first" >&2; exit 1; }
set -a; . "$ROOT/.env.azure"; set +a

wait_healthy() {
  # After a failover Azure rebuilds the old primary as the new standby; the next
  # drill should start from a healthy pair.
  for _ in $(seq 1 120); do
    st="$(az postgres flexible-server show -n "$PGHADR_SERVER" -g "$PGHADR_RG" \
          --query highAvailability.state -o tsv 2>/dev/null || true)"
    [[ "$st" == "Healthy" ]] && return 0
    echo "  HA state: ${st:-unknown}, waiting..." >&2
    sleep 15
  done
  echo "HA pair did not become Healthy" >&2
  return 1
}

wait_healthy
pghadr drill --action planned-failover -n "$PGHADR_SERVER" -g "$PGHADR_RG" \
  --warmup 60 --settle 60 --out-dir "$OUT" --name "$DAY-ha-planned"

wait_healthy
pghadr drill --action forced-failover -n "$PGHADR_SERVER" -g "$PGHADR_RG" \
  --warmup 60 --settle 60 --out-dir "$OUT" --name "$DAY-ha-forced"

if [[ "${1:-}" == "--dr" ]]; then
  : "${PGHADR_REPLICA:?no replica in .env.azure; deploy with ./scripts/up.sh --replica}"
  pghadr dr-drill --replica "$PGHADR_REPLICA" -g "$PGHADR_RG" \
    --warmup 120 --out-dir "$OUT" --name "$DAY-dr-forced-promote"
fi
echo "Reports are in $OUT. Delete the environment: ./scripts/down.sh"
