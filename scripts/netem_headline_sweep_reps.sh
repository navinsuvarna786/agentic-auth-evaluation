#!/usr/bin/env bash
# Replicated B3b headline-cell netem RTT sweep.
#
# Same measurement as netem_headline_sweep.sh, but runs REPS full sweeps so
# the RTT figure carries an across-replication band instead of resting on a
# single run per point.
#
# Design notes:
#   * Rep is the OUTER loop. Each rep is a complete 0/1/10/50 ms sweep, so
#     drift over the ~45 min session lands across reps rather than being
#     confounded with delay ordering (which is what an inner rep loop would
#     do).
#   * A discarded warm-up run precedes recording. The Keycloak JVM has been
#     idle for weeks between sweeps; without this the first recorded cell
#     absorbs JIT and connection-pool warm-up.
#   * REP_START defaults to 1 so existing rep-0 files are never clobbered.
#
# Wall-clock: REPS × 4 delays × 3 modes × ~72 s (60 s record + 10 s warmup)
#             + ~72 s warm-up  =  ~15 min per rep.

set -uo pipefail

DELAYS="${DELAYS:-0 1 10 50}"
MODES="${MODES:-A B C}"
REPS="${REPS:-2}"
REP_START="${REP_START:-1}"
DEVBOX_HOST="${DEVBOX_HOST:-authserver.example.internal}"
DEVBOX_USER="${DEVBOX_USER:-user}"
DEVBOX_KEY="${DEVBOX_KEY:-$HOME/.ssh/id_devbox}"
PROJECT="${COMPOSE_PROJECT:-cns2026}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RAW_DIR="$REPO_DIR/results/raw"

# benchmark.config reads credentials from the process environment and does not
# itself load .env, so the caller must export it (DEVBOX_RUNBOOK.md step 8).
# Sourcing here rather than relying on the operator's shell keeps an
# unattended run from failing every cell with a missing CLIENT_SECRET.
if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_DIR/.env"
  set +a
else
  echo "FATAL: $REPO_DIR/.env not found; benchmark cells would all fail" >&2
  exit 1
fi
: "${CLIENT_SECRET:?CLIENT_SECRET missing from .env}"

ssh_dev() {
  ssh -i "$DEVBOX_KEY" -o IdentitiesOnly=yes "$DEVBOX_USER@$DEVBOX_HOST" "$@"
}

ensure_sidecars() {
  echo "[$(date -Is)] ensuring sidecar-keycloak and sidecar-redis"
  ssh_dev "
    set -e
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

# The smoke benchmark always writes to the un-suffixed rep-0 path, and this
# script renames that file after each cell. Pre-existing rep-0 files from the
# main suite live at exactly that path, so quarantine them first: otherwise a
# benchmark that fails to produce output would leave a stale file to be
# renamed as if it were fresh, silently poisoning the sweep.
BACKUP_DIR="$RAW_DIR/.presweep_backup"

quarantine_preexisting() {
  mkdir -p "$BACKUP_DIR"
  local moved=0
  for m in A B C; do
    local f="$RAW_DIR/mode-${m}_cache-off_churn-5_depth-1_rep-0.csv"
    [ -f "$f" ] && { mv "$f" "$BACKUP_DIR/"; moved=$((moved + 1)); }
  done
  echo "[$(date -Is)] quarantined $moved pre-existing rep-0 file(s) to $(basename "$BACKUP_DIR")"
}

restore_preexisting() {
  [ -d "$BACKUP_DIR" ] || return 0
  for f in "$BACKUP_DIR"/*.csv; do
    [ -e "$f" ] || continue
    mv -n "$f" "$RAW_DIR/"
  done
  rmdir "$BACKUP_DIR" 2>/dev/null || true
  echo "[$(date -Is)] restored quarantined rep-0 file(s)"
}

# Clear netem and drop the sidecars, so an interrupted run cannot leave a
# delay rule applied to a long-lived stack.
teardown() {
  echo "[$(date -Is)] clearing qdisc and removing sidecars"
  ssh_dev "
    for s in sidecar-keycloak sidecar-redis; do
      docker exec \$s tc qdisc del dev eth0 root 2>/dev/null || true
    done
    docker rm -f sidecar-keycloak sidecar-redis 2>/dev/null || true
  " >/dev/null 2>&1
  restore_preexisting
}
trap teardown EXIT

run_cell() {  # mode, delay, rep ("warmup" discards output)
  local m="$1" d="$2" rep="$3"
  BENCHMARK_SMOKE=1 BENCHMARK_SMOKE_MODE="$m" BENCHMARK_SMOKE_CACHE=false \
    BENCHMARK_SMOKE_CHURN=5 BENCHMARK_SMOKE_DEPTH=1 \
    WORKLOAD_MODE=synthetic \
    ./venv/bin/python -m benchmark.benchmark 2>&1 | tail -2
  local src="$RAW_DIR/mode-${m}_cache-off_churn-5_depth-1_rep-0.csv"
  if [ ! -f "$src" ]; then
    echo "  WARNING: $src missing (smoke benchmark did not run as expected)"
    return 1
  fi
  if [ "$rep" = "warmup" ]; then
    rm -f "$src"
    echo "  -> warm-up cell discarded"
    return 0
  fi
  local dst="$RAW_DIR/mode-${m}_cache-off_churn-5_depth-1_rep-${rep}_netem-${d}ms.csv"
  mv "$src" "$dst"
  local med
  med=$(./venv/bin/python -c "import pandas as pd; print(round(pd.read_csv('$dst').latency_ms.median(),2))")
  echo "  -> $(basename "$dst")  median=${med}ms  n=$(($(wc -l < "$dst") - 1))"
}

cd "$REPO_DIR"
quarantine_preexisting
ensure_sidecars

echo "[$(date -Is)] === warm-up pass (discarded) ==="
apply_qdisc 0
for m in $MODES; do
  echo "[$(date -Is)] warm-up mode=$m"
  run_cell "$m" 0 warmup
done

last_rep=$((REP_START + REPS - 1))
for rep in $(seq "$REP_START" "$last_rep"); do
  echo "[$(date -Is)] ================ REP $rep of $last_rep ================"
  for d in $DELAYS; do
    apply_qdisc "$d"
    for m in $MODES; do
      echo "[$(date -Is)] benchmark rep=$rep netem=${d}ms mode=$m"
      run_cell "$m" "$d" "$rep"
    done
  done
done

echo "[$(date -Is)] sweep complete"
ls -1 "$RAW_DIR"/mode-?_cache-off_churn-5_depth-1_rep-*_netem-*ms.csv | grep -v redis-only | wc -l
