#!/usr/bin/env bash
# B3b: drive a netem RTT sweep across the cns2026 stack.
#
# For each value of NETEM_DELAY_MS in the sweep, this script:
#   1. Applies `tc qdisc ... netem delay Xms` inside the Keycloak and
#      Redis container network namespaces (via the netem sidecars in
#      docker-compose-netem.yml).
#   2. Runs the benchmark for whatever cell of the matrix the caller
#      configured via BENCHMARK_SMOKE_* / BENCHMARK_REPLICATIONS env
#      vars.
#   3. Renames the resulting raw CSVs with a _netem-Xms suffix so the
#      sweep's data doesn't shadow itself across delays.
#
# Designed to run on the JUMPHOST. The benchmark driver and API run
# here; Keycloak/Redis run on the devbox via the SSH local-forward.
#
# Usage:
#   ./scripts/netem_sweep.sh "0 1 10 50"
#
# Prerequisites:
#   - cns2026 stack is up on devbox (see DEVBOX_RUNBOOK.md)
#   - SSH tunnel to devbox is open
#   - API is running on 127.0.0.1:8001 with .env sourced
#   - jumphost .env loaded (or sourced before invoking)

set -euo pipefail

DELAYS=${1:-"0 1 10 50"}
DEVBOX_HOST="${DEVBOX_HOST:-authserver.example.internal}"
DEVBOX_USER="${DEVBOX_USER:-user}"
DEVBOX_KEY="${DEVBOX_KEY:-$HOME/.ssh/id_devbox}"
DEVBOX_DIR="${DEVBOX_DIR:-/home/user/cns2026}"
PROJECT="${COMPOSE_PROJECT:-cns2026}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RAW_DIR="$REPO_DIR/results/raw"

ssh_dev() {
  ssh -i "$DEVBOX_KEY" -o IdentitiesOnly=yes "$DEVBOX_USER@$DEVBOX_HOST" "$@"
}

apply_netem() {
  local delay_ms="$1"
  echo "[$(date -Is)] applying netem delay=${delay_ms}ms"
  ssh_dev "cd $DEVBOX_DIR && NETEM_DELAY_MS=$delay_ms docker compose -p $PROJECT \
    -f docker-compose-devbox.yml -f docker-compose-netem.yml --env-file .env \
    up -d --force-recreate keycloak-netem redis-netem" >/dev/null
  # Let the sidecars apply the qdisc and stabilise.
  sleep 5
  echo "[$(date -Is)] netem now reports:"
  ssh_dev "docker logs --tail 3 ${PROJECT}-keycloak-netem-1 2>&1 | head -2"
}

clear_netem() {
  echo "[$(date -Is)] clearing netem sidecars"
  ssh_dev "cd $DEVBOX_DIR && docker compose -p $PROJECT \
    -f docker-compose-devbox.yml -f docker-compose-netem.yml --env-file .env \
    rm -sf keycloak-netem redis-netem" >/dev/null
}

trap clear_netem EXIT

cd "$REPO_DIR"
for d in $DELAYS; do
  apply_netem "$d"

  # Capture a list of files before the run so we can rename only the
  # newly-produced ones.
  before=$(mktemp)
  ls -1 "$RAW_DIR"/*.csv 2>/dev/null | sort > "$before"

  echo "[$(date -Is)] running benchmark for delay=${d}ms"
  ./venv/bin/python -m benchmark.benchmark

  # Tag every newly-produced CSV with the delay value so subsequent
  # sweep values don't overwrite this one.
  after=$(mktemp)
  ls -1 "$RAW_DIR"/*.csv 2>/dev/null | sort > "$after"
  comm -13 "$before" "$after" | while read -r f; do
    case "$f" in
      *_netem-*ms.csv) ;;  # already tagged; skip
      *)
        new="${f%.csv}_netem-${d}ms.csv"
        mv -v "$f" "$new"
        ;;
    esac
  done
  rm -f "$before" "$after"
done

echo "[$(date -Is)] netem sweep complete: delays={$DELAYS}"
