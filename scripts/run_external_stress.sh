#!/usr/bin/env bash
# B3-stress: parallel-launcher sweep on devbox loopback.
#
# Why parallel launcher and not wrk/hey: each Mode-C request requires a fresh
# DPoP proof (signed JWT with unique jti, time-bounded). Off-the-shelf load
# generators cannot mint per-request crypto. The fastest correct path is to
# horizontally scale the existing Python launcher (each worker process uses
# agent_client.py to mint valid proofs), aggregating CSVs after the fact. This
# matches the prompt's named fallback. Mode A and B paths are also exercised
# end-to-end through the same client, keeping the comparison fair.
#
# Sweep: aggregate concurrency {32, 128, 512} RPS x modes {A, B, C} x 2 reps,
# 30s record per cell. Each launcher worker is capped at 64 RPS (well below
# its observed ~94 RPS single-process saturation), so:
#   32  -> 1 worker  x 32 RPS
#   128 -> 2 workers x 64 RPS
#   512 -> 8 workers x 64 RPS
#
# Total cells = 3 modes x 3 levels x 2 reps = 18 cells x ~35s = ~10 min benchmark
# wall clock plus per-cell overhead.
#
# Pre-condition: API listening on $API_URL (loopback recommended). The .env in
# this repo provides the loopback config when run on devbox. Always run
# scripts/netem_check.sh first.

set -uo pipefail

LEVELS="${LEVELS:-32 128 512}"
MODES="${MODES:-A B C}"
REPS="${REPS:-2}"
DURATION="${BENCHMARK_DURATION:-30}"
WARMUP="${BENCHMARK_WARMUP:-5}"
WORKER_CAP_RPS="${WORKER_CAP_RPS:-64}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-$REPO_DIR/results/stress_external/$TS}"
mkdir -p "$OUT_DIR"

cd "$REPO_DIR"
set -a; . ./.env; set +a

# Safety: refuse to run if netem present.
if ! ./scripts/netem_check.sh "$OUT_DIR" >/dev/null 2>&1; then
  echo "ERROR: netem leftover or check failed. Re-run scripts/netem_check.sh and inspect."
  ./scripts/netem_check.sh "$OUT_DIR"
  exit 1
fi

# Capture system context once
{
  echo "host=$(hostname)  user=$(id -un)  date=$(date -Is)"
  echo "API_URL=$API_URL  KEYCLOAK_URL=$KEYCLOAK_URL  REDIS_URL=$REDIS_URL"
  echo "load: $(uptime)"
  echo "free: $(free -m | head -2 | tail -1)"
  echo "git: $(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || echo 'no-git')"
} > "$OUT_DIR/context.txt"

run_one_cell() {
  local mode="$1"
  local agg_rps="$2"
  local rep="$3"
  local cell_dir="$OUT_DIR/mode-${mode}_agg-${agg_rps}_rep-${rep}"
  mkdir -p "$cell_dir"

  # Decide worker count and per-worker churn.
  local n_workers per_worker
  if [ "$agg_rps" -le "$WORKER_CAP_RPS" ]; then
    n_workers=1; per_worker="$agg_rps"
  else
    n_workers=$(( (agg_rps + WORKER_CAP_RPS - 1) / WORKER_CAP_RPS ))
    per_worker=$(( agg_rps / n_workers ))
  fi

  echo "[$(date -Is)] cell mode=$mode agg=${agg_rps}rps rep=$rep workers=$n_workers per_worker=${per_worker}rps"

  # Snapshot resource state before the cell
  uptime > "$cell_dir/uptime_before.txt"
  ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes -o ConnectTimeout=3 \
      localhost docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
      cns2026-keycloak-1 cns2026-redis-1 \
      > "$cell_dir/dockerstats_before.txt" 2>/dev/null || \
    docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
      cns2026-keycloak-1 cns2026-redis-1 \
      > "$cell_dir/dockerstats_before.txt" 2>/dev/null || true

  # Launch n_workers in parallel. Each writes its own CSV via the
  # benchmark.benchmark smoke path, then we collect them.
  local pids=()
  for w in $(seq 1 "$n_workers"); do
    (
      worker_dir="$cell_dir/worker-${w}"
      mkdir -p "$worker_dir/results/raw" "$worker_dir/results/processed" "$worker_dir/results/figures"
      BENCHMARK_OUTPUT_DIR="$worker_dir/results" \
        BENCHMARK_SMOKE=1 BENCHMARK_SMOKE_MODE="$mode" BENCHMARK_SMOKE_CACHE=false \
        BENCHMARK_SMOKE_CHURN="$per_worker" BENCHMARK_SMOKE_DEPTH=1 \
        BENCHMARK_DURATION="$DURATION" BENCHMARK_WARMUP="$WARMUP" \
        WORKLOAD_MODE=synthetic \
        PYTHONPATH="$REPO_DIR" \
        "$REPO_DIR/venv/bin/python" -m benchmark.benchmark \
        > "$worker_dir/run.log" 2>&1 || echo "worker $w failed" >> "$worker_dir/run.log"
    ) &
    pids+=("$!")
  done

  # Wait for all workers in this cell
  for p in "${pids[@]}"; do wait "$p"; done

  uptime > "$cell_dir/uptime_after.txt"
  docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
    cns2026-keycloak-1 cns2026-redis-1 \
    > "$cell_dir/dockerstats_after.txt" 2>/dev/null || true

  # Concatenate worker CSVs
  local first=1
  local merged="$cell_dir/merged.csv"
  : > "$merged"
  for w in $(seq 1 "$n_workers"); do
    csv="$cell_dir/worker-${w}/results/raw/mode-${mode}_cache-off_churn-${per_worker}_depth-1_rep-0.csv"
    if [ -f "$csv" ]; then
      if [ "$first" -eq 1 ]; then
        cp "$csv" "$merged"; first=0
      else
        tail -n +2 "$csv" >> "$merged"
      fi
    else
      echo "WARNING: worker $w produced no CSV ($csv)" | tee -a "$cell_dir/run.log"
    fi
  done

  if [ -s "$merged" ]; then
    "$REPO_DIR/venv/bin/python" - "$merged" "$mode" "$agg_rps" "$rep" "$DURATION" "$OUT_DIR/cell_summary.csv" <<'PY'
import sys, csv, pandas as pd
path, mode, agg_rps, rep, dur, out = sys.argv[1:7]
agg_rps, rep, dur = int(agg_rps), int(rep), int(dur)
df = pd.read_csv(path)
n = len(df)
ok = int((df.success == True).sum()) if 'success' in df.columns else n
fail = n - ok
sc = ','.join(f"{int(k)}:{int(v)}" for k,v in df.status_code.value_counts().items()) if 'status_code' in df.columns else ''
errs = ','.join(f"{k}:{int(v)}" for k,v in df.error.fillna('-').value_counts().items() if k != '-') if 'error' in df.columns else ''
row = [mode, agg_rps, rep, n, ok, fail,
       round(n/dur, 2),
       round(df.latency_ms.median(), 2),
       round(df.latency_ms.quantile(0.50), 2),
       round(df.latency_ms.quantile(0.95), 2),
       round(df.latency_ms.quantile(0.99), 2),
       round(df.latency_ms.max(), 2),
       round(df.verification_ms.median(), 2) if 'verification_ms' in df.columns else '',
       round(df.exchange_ms.median(), 2) if 'exchange_ms' in df.columns else '',
       sc, errs]
with open(out, 'a', newline='') as f:
    csv.writer(f).writerow(row)
PY
  fi
}

# Header for cell_summary.csv
echo "mode,agg_rps,rep,n,ok,fail,actual_rps,med_ms,p50_ms,p95_ms,p99_ms,max_ms,verify_med_ms,exch_med_ms,status_codes,errors" \
  > "$OUT_DIR/cell_summary.csv"

for mode in $MODES; do
  for agg in $LEVELS; do
    for rep in $(seq 1 "$REPS"); do
      run_one_cell "$mode" "$agg" "$rep"
    done
  done
done

echo "[$(date -Is)] sweep complete. Results: $OUT_DIR"
echo "  - cell_summary.csv          per-cell summary (mode/agg_rps/rep)"
echo "  - mode-*/                   per-cell raw worker CSVs"
echo "  - qdisc_before.txt / after  netem audit"
echo "  - context.txt               host/git context"
