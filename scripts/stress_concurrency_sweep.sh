#!/usr/bin/env bash
# B3-stress: push churn beyond pilot's max (20/s) at the headline cell,
# Mode C only, looking for the empirical operational-regime boundary
# the paper claims exists.
#
# Hypothesis: at sufficiently high churn the DPoP verify path saturates
# (Redis SETNX queue + API CPU) and either (a) latency p99 grows
# super-linearly or (b) freshness window violations surface as 401s.
#
# If we observe 0 failures and linear latency at the highest churn we
# can sustain over the SSH tunnel, that itself is a useful headroom
# claim for §V.RQ1 (and a softer regime characterization).
#
# Outputs CSVs tagged `_stress-<churn>rps`. Mode C only, depth 1,
# cache off, 60 s record + 10 s warmup.

set -uo pipefail

CHURNS="${CHURNS:-20 50 100 200}"
MODE="${STRESS_MODE:-C}"
DURATION="${BENCHMARK_DURATION:-60}"
WARMUP="${BENCHMARK_WARMUP:-10}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RAW_DIR="$REPO_DIR/results/raw"

cd "$REPO_DIR"
set -a; . ./.env; set +a
for c in $CHURNS; do
  echo "[$(date -Is)] stress mode=$MODE churn=${c}/s duration=${DURATION}s"
  BENCHMARK_SMOKE=1 BENCHMARK_SMOKE_MODE="$MODE" BENCHMARK_SMOKE_CACHE=false \
    BENCHMARK_SMOKE_CHURN="$c" BENCHMARK_SMOKE_DEPTH=1 \
    BENCHMARK_DURATION="$DURATION" BENCHMARK_WARMUP="$WARMUP" \
    WORKLOAD_MODE=synthetic \
    ./venv/bin/python -m benchmark.benchmark 2>&1 | tail -3

  src="$RAW_DIR/mode-${MODE}_cache-off_churn-${c}_depth-1_rep-0.csv"
  dst="$RAW_DIR/mode-${MODE}_cache-off_churn-${c}_depth-1_rep-0_stress-${c}rps.csv"
  if [ -f "$src" ]; then
    mv "$src" "$dst"
    ./venv/bin/python -c "
import pandas as pd
df = pd.read_csv('$dst')
n = len(df)
ok = (df.success == True).sum() if 'success' in df.columns else n
fail = n - ok
med = round(df.latency_ms.median(), 2)
p95 = round(df.latency_ms.quantile(0.95), 2)
p99 = round(df.latency_ms.quantile(0.99), 2)
print(f'  -> n={n}  ok={ok}  fail={fail}  median={med}ms  p95={p95}ms  p99={p99}ms')
"
  else
    echo "  WARNING: $src missing"
  fi
done

echo "[$(date -Is)] stress sweep complete"
