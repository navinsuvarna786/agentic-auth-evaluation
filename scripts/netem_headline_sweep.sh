#!/usr/bin/env bash
# B3b headline-cell netem RTT sweep, using docker-run-managed sidecars.
#
# Why not docker compose: compose's `up --force-recreate <sidecar>`
# propagates the recreate through `depends_on: keycloak`, killing
# Keycloak between every delay value. The docker-run path creates two
# persistent netshoot sidecars that share the keycloak and redis
# network namespaces (`--network container:cns2026-<svc>-1`)
# and applies `tc qdisc` rules via `docker exec` -- the rule changes
# touch only the sidecar namespace, never the target container.
#
# For each delay value in DELAYS the script:
#   1. tc qdisc replace dev eth0 root netem delay <d>ms  (or del at 0)
#   2. runs benchmark.benchmark in SMOKE_MODE once per mode {A, B, C}
#      at the headline cell (cache=off, churn=5, depth=1)
#   3. renames each produced CSV with a _netem-<d>ms suffix
#
# Total wall-clock at default settings: 4 delays × 3 modes × ~72 s
# (60 s record + 10 s warmup + ~2 s overhead) = ~14 min.

set -uo pipefail

DELAYS="${DELAYS:-0 1 10 50}"
MODES="${MODES:-A B C}"
DEVBOX_HOST="${DEVBOX_HOST:-authserver.example.internal}"
DEVBOX_USER="${DEVBOX_USER:-user}"
DEVBOX_KEY="${DEVBOX_KEY:-$HOME/.ssh/id_devbox}"
PROJECT="${COMPOSE_PROJECT:-cns2026}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RAW_DIR="$REPO_DIR/results/raw"

ssh_dev() {
  ssh -i "$DEVBOX_KEY" -o IdentitiesOnly=yes "$DEVBOX_USER@$DEVBOX_HOST" "$@"
}

ensure_sidecars() {
  echo "[$(date -Is)] ensuring sidecar-keycloak and sidecar-redis"
  ssh_dev "
    set -e
    # Remove any stale sidecars (e.g. orphaned from a previous keycloak
    # recreate). Then create fresh ones in the current container netns.
    docker rm -f sidecar-keycloak sidecar-redis 2>/dev/null || true
    docker run -d --name sidecar-keycloak \
      --network container:${PROJECT}-keycloak-1 \
      --cap-add NET_ADMIN \
      nicolaka/netshoot:v0.13 sleep infinity
    docker run -d --name sidecar-redis \
      --network container:${PROJECT}-redis-1 \
      --cap-add NET_ADMIN \
      nicolaka/netshoot:v0.13 sleep infinity
    sleep 2
    docker exec sidecar-keycloak ip -br link show eth0 || { echo BUG: sidecar-keycloak lost eth0; exit 1; }
    docker exec sidecar-redis    ip -br link show eth0 || { echo BUG: sidecar-redis lost eth0;    exit 1; }
  "
}

apply_qdisc() {
  local delay_ms="$1"
  echo "[$(date -Is)] apply qdisc delay=${delay_ms}ms (both endpoints)"
  ssh_dev "
    for s in sidecar-keycloak sidecar-redis; do
      docker exec \$s sh -c '
        tc qdisc del dev eth0 root 2>/dev/null || true
        [ '${delay_ms}' -gt 0 ] && tc qdisc add dev eth0 root netem delay '${delay_ms}'ms
        tc qdisc show dev eth0
      '
    done
  " 2>&1 | sed 's/^/  /'
}

teardown_sidecars() {
  echo "[$(date -Is)] removing sidecars"
  ssh_dev "docker rm -f sidecar-keycloak sidecar-redis 2>/dev/null || true" >/dev/null 2>&1
}
trap teardown_sidecars EXIT

ensure_sidecars

cd "$REPO_DIR"
for d in $DELAYS; do
  apply_qdisc "$d"
  for m in $MODES; do
    echo "[$(date -Is)] benchmark netem=${d}ms mode=$m"
    BENCHMARK_SMOKE=1 BENCHMARK_SMOKE_MODE="$m" BENCHMARK_SMOKE_CACHE=false \
      BENCHMARK_SMOKE_CHURN=5 BENCHMARK_SMOKE_DEPTH=1 \
      WORKLOAD_MODE=synthetic \
      ./venv/bin/python -m benchmark.benchmark 2>&1 | tail -2
    src="$RAW_DIR/mode-${m}_cache-off_churn-5_depth-1_rep-0.csv"
    dst="$RAW_DIR/mode-${m}_cache-off_churn-5_depth-1_rep-0_netem-${d}ms.csv"
    if [ -f "$src" ]; then
      mv "$src" "$dst"
      med=$(./venv/bin/python -c "import pandas as pd; print(round(pd.read_csv('$dst').latency_ms.median(),2))")
      echo "  -> $(basename $dst)  median=${med}ms  n=$(($(wc -l < $dst) - 1))"
    else
      echo "  WARNING: $src missing (smoke benchmark did not run as expected)"
    fi
  done
done

echo "[$(date -Is)] sweep complete; results/raw/*_netem-*ms.csv contains $((4 * 3)) files"
