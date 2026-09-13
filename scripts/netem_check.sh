#!/usr/bin/env bash
# Pre-benchmark safety: confirm no leftover netem/qdisc on Redis or Keycloak.
# B3-stress lesson: yesterday's RTT sweep left `netem delay 50ms` on both
# sidecars. Anything benchmarked between then and the cleanup was off by ~50 ms
# per Redis/Keycloak round trip and should be excluded from the paper.
#
# Run this BEFORE any stress benchmark. Captures before/after qdisc state into
# the result directory for traceability.
#
# Usage:  ./scripts/netem_check.sh [OUT_DIR]

set -uo pipefail
OUT_DIR="${1:-results/stress_external/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_DIR"

declare -a SIDECARS=(sidecar-redis sidecar-keycloak)

{
  echo "=== qdisc state before cleanup ($(date -Is)) ==="
  for s in "${SIDECARS[@]}"; do
    echo "--- $s ---"
    docker exec "$s" tc qdisc show dev eth0 2>&1
  done
} | tee "$OUT_DIR/qdisc_before.txt"

DIRTY=0
for s in "${SIDECARS[@]}"; do
  if docker exec "$s" tc qdisc show dev eth0 2>/dev/null | grep -q netem; then
    echo "[netem_check] $s has leftover netem qdisc — clearing"
    docker exec "$s" tc qdisc del dev eth0 root 2>&1 | tee -a "$OUT_DIR/qdisc_cleanup.log"
    DIRTY=1
  fi
done

{
  echo "=== qdisc state after cleanup ($(date -Is)) ==="
  for s in "${SIDECARS[@]}"; do
    echo "--- $s ---"
    docker exec "$s" tc qdisc show dev eth0 2>&1
  done
} | tee "$OUT_DIR/qdisc_after.txt"

if [ "$DIRTY" -eq 1 ]; then
  echo "[netem_check] WARNING: leftover netem detected and cleared. Re-run benchmarks." | tee -a "$OUT_DIR/qdisc_cleanup.log"
  exit 2
fi
echo "[netem_check] clean — safe to benchmark"
