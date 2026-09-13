"""
Benchmark Runner

Executes the full experiment matrix:
- 3 authentication modes (A, B, C)
- 2 cache modes (off, on)
- 3 churn levels (1, 5, 20 agents/second)
- 2 delegation depths (1, 2)
- N replications each (configurable; see REPLICATIONS)

Run order is randomized via a seeded shuffle to break correlation between
condition and time-of-execution; the seed is recorded in benchmark_summary
for reproducibility.
"""
import asyncio
import csv
import os
import random
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict

from benchmark.launcher import run_launcher_test


# Experiment matrix
MODES = ['A', 'B', 'C']
CACHE_MODES = [False, True]
CHURN_RATES = [1, 5, 20]
DELEGATION_DEPTHS = [1, 2]
# Default replication counts for CNS 2026. The headline condition gets twice
# the samples so the bootstrap CIs reported in Table I are tighter than the
# rest of the matrix — that's the condition reviewers will scrutinize and
# the only one that feeds analyze_results.compute_significance_tests.
# Override via env vars to validate stability or run cheaper smoke tests.
REPLICATIONS = int(os.getenv("BENCHMARK_REPLICATIONS", "5"))
HEADLINE_REPLICATIONS = int(os.getenv("BENCHMARK_HEADLINE_REPLICATIONS", "10"))

# Must match analyze_results.HEADLINE_CONDITION exactly. Kept in sync via
# a runtime assertion in run_full_benchmark() rather than a cross-module
# import (the benchmark runs without the scipy stack imported).
HEADLINE_CONDITION = {"cache": False, "churn": 5, "delegation_depth": 1}


def _replications_for(cache: bool, churn: int, depth: int) -> int:
    """Return how many replications a single experiment configuration
    should receive: HEADLINE_REPLICATIONS for the cache-off/churn-5/depth-1
    cell that the paper's Table I reports, REPLICATIONS otherwise."""
    if (
        cache == HEADLINE_CONDITION["cache"]
        and churn == HEADLINE_CONDITION["churn"]
        and depth == HEADLINE_CONDITION["delegation_depth"]
    ):
        return HEADLINE_REPLICATIONS
    return REPLICATIONS


# Reproducible randomization. Override via env var to validate seed-independence.
RUN_ORDER_SEED = int(os.getenv("BENCHMARK_SEED", "20260506"))

# Test parameters
DURATION = int(os.getenv("BENCHMARK_DURATION", "60"))  # seconds per run
WARMUP = int(os.getenv("BENCHMARK_WARMUP", "10"))  # seconds discarded at start of run

# Smoke mode collapses the 195-run matrix to a single (mode, cache, churn,
# depth, rep) configuration so we can validate that Keycloak, Redis, and
# the API are correctly wired end-to-end before launching the ~3.8-hour
# full pilot. Pair with BENCHMARK_DURATION=10, BENCHMARK_WARMUP=2 for a
# ~15 s canary on devbox. Variables (with defaults) — every one overridable:
#   BENCHMARK_SMOKE=1               (any non-empty value enables smoke mode)
#   BENCHMARK_SMOKE_MODE=C
#   BENCHMARK_SMOKE_CACHE=false      ("true"/"false"; case-insensitive)
#   BENCHMARK_SMOKE_CHURN=5
#   BENCHMARK_SMOKE_DEPTH=1
SMOKE_MODE = bool(os.getenv("BENCHMARK_SMOKE"))


def _smoke_experiment() -> Dict:
    """Resolve the smoke-mode configuration from env vars; called only when
    SMOKE_MODE is true."""
    cache_raw = os.getenv("BENCHMARK_SMOKE_CACHE", "false").strip().lower()
    return {
        "mode": os.getenv("BENCHMARK_SMOKE_MODE", "C").upper(),
        "cache": cache_raw in ("1", "true", "yes", "on"),
        "churn": int(os.getenv("BENCHMARK_SMOKE_CHURN", "5")),
        "depth": int(os.getenv("BENCHMARK_SMOKE_DEPTH", "1")),
        "replication": 0,
    }

# Output directories
# B3-stress: BENCHMARK_OUTPUT_DIR overrides the default so parallel launcher
# workers can isolate their CSVs. Run-id collisions otherwise overwrite when
# multiple workers in the same cell use the same churn rate.
RESULTS_DIR = Path(os.getenv("BENCHMARK_OUTPUT_DIR", str(Path(__file__).parent.parent / "results")))
RAW_DIR = RESULTS_DIR / "raw"
PROCESSED_DIR = RESULTS_DIR / "processed"
FIGURES_DIR = RESULTS_DIR / "figures"


def setup_directories():
    """Create output directories if they don't exist."""
    for directory in [RAW_DIR, PROCESSED_DIR, FIGURES_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def generate_run_id(
    mode: str,
    cache: bool,
    churn: int,
    depth: int,
    replication: int
) -> str:
    """Generate a unique run ID."""
    cache_str = "cache-on" if cache else "cache-off"
    return f"mode-{mode}_{cache_str}_churn-{churn}_depth-{depth}_rep-{replication}"


async def run_single_experiment(
    mode: str,
    cache_enabled: bool,
    churn_rate: int,
    delegation_depth: int,
    replication: int
) -> Dict:
    """
    Run a single experiment configuration.

    Returns:
        Dictionary with run metadata and results file path
    """
    run_id = generate_run_id(mode, cache_enabled, churn_rate, delegation_depth, replication)

    print(f"\n{'='*80}")
    print(f"Starting run: {run_id}")
    print(f"  Mode: {mode}")
    print(f"  Cache: {'enabled' if cache_enabled else 'disabled'}")
    print(f"  Churn: {churn_rate} agents/sec")
    print(f"  Delegation depth: {delegation_depth}")
    print(f"  Replication: {replication + 1}/{REPLICATIONS}")
    print(f"{'='*80}\n")

    # Run the test
    start_time = time.time()
    try:
        results = await run_launcher_test(
            mode=mode,
            cache_enabled=cache_enabled,
            churn_rate=churn_rate,
            delegation_depth=delegation_depth,
            duration=DURATION,
            warmup=WARMUP
        )

        elapsed = time.time() - start_time

        # Write results to CSV
        csv_path = RAW_DIR / f"{run_id}.csv"
        write_results_csv(csv_path, run_id, mode, cache_enabled, churn_rate, delegation_depth, results)

        print(f"\n✓ Run complete: {run_id}")
        print(f"  Duration: {elapsed:.1f}s")
        print(f"  Requests: {len(results)}")
        print(f"  Output: {csv_path}")

        return {
            "run_id": run_id,
            "mode": mode,
            "cache": cache_enabled,
            "churn": churn_rate,
            "depth": delegation_depth,
            "replication": replication,
            "num_requests": len(results),
            "duration": elapsed,
            "csv_path": str(csv_path),
            "success": True
        }

    except Exception as e:
        print(f"\n✗ Run failed: {run_id}")
        print(f"  Error: {str(e)}")

        return {
            "run_id": run_id,
            "mode": mode,
            "cache": cache_enabled,
            "churn": churn_rate,
            "depth": delegation_depth,
            "replication": replication,
            "error": str(e),
            "success": False
        }


def write_results_csv(
    csv_path: Path,
    run_id: str,
    mode: str,
    cache: bool,
    churn: int,
    depth: int,
    results: List[dict]
):
    """Write results to CSV file."""
    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)

        # Header. The trailing workload_/llm_ columns are populated only
        # when WORKLOAD_MODE != synthetic; for synthetic runs they are
        # written as empty strings so the CSV schema stays uniform and
        # back-compat with analyze_results.py is preserved.
        writer.writerow([
            'timestamp',
            'run_id',
            'mode',
            'cache',
            'churn',
            'delegation_depth',
            'agent_id',
            'request_num',
            'spawn_time',
            'success',
            'status_code',
            'latency_ms',
            'exchange_ms',
            'verification_ms',
            'error',
            'dpop_verified',
            'audience_ok',
            'workload_mode',
            'llm_status',
            'llm_ms',
            'llm_prompt_tokens',
            'llm_completion_tokens',
            'llm_total_tokens',
            'llm_error',
        ])

        # Data rows
        for r in results:
            result = r['result']
            # `result` may be either the legacy RequestResult or the new
            # WorkloadResult that wraps it. Both expose the attributes
            # written below; the workload-specific fields are read
            # defensively so a future direct RequestResult caller still
            # works.
            llm = getattr(result, "llm", None)
            workload_mode = getattr(result, "workload_mode", "")
            writer.writerow([
                datetime.now().isoformat(),
                run_id,
                mode,
                cache,
                churn,
                depth,
                r['agent_id'],
                r['request_num'],
                r['spawn_time'],
                result.success,
                result.status_code,
                round(result.latency_ms, 2),
                round(result.exchange_ms, 2) if result.exchange_ms else '',
                round(result.verification_ms, 2) if result.verification_ms else '',
                result.error or '',
                result.dpop_verified,
                result.audience_ok,
                workload_mode,
                llm.status_code if llm else '',
                round(llm.latency_ms, 2) if llm else '',
                llm.prompt_tokens if llm else '',
                llm.completion_tokens if llm else '',
                llm.total_tokens if llm else '',
                (llm.error or '') if llm else '',
            ])


async def run_full_benchmark():
    """Run the complete benchmark suite."""
    setup_directories()

    # Generate all experiment configurations in canonical order, then shuffle.
    # Headline condition (cache-off / churn-5 / depth-1) gets HEADLINE_REPLICATIONS
    # replications across all three modes; everything else gets REPLICATIONS.
    headline_count = 0
    nonheadline_count = 0
    if SMOKE_MODE:
        experiments = [_smoke_experiment()]
    else:
        experiments = []
        for mode in MODES:
            for cache in CACHE_MODES:
                for churn in CHURN_RATES:
                    for depth in DELEGATION_DEPTHS:
                        reps = _replications_for(cache, churn, depth)
                        for rep in range(reps):
                            experiments.append({
                                'mode': mode,
                                'cache': cache,
                                'churn': churn,
                                'depth': depth,
                                'replication': rep
                            })
                        if reps == HEADLINE_REPLICATIONS:
                            headline_count += reps
                        else:
                            nonheadline_count += reps

    rng = random.Random(RUN_ORDER_SEED)
    rng.shuffle(experiments)

    total_experiments = len(experiments)
    print(f"\n{'='*80}")
    print(f"BENCHMARK SUITE{' (SMOKE MODE)' if SMOKE_MODE else ''}")
    print(f"{'='*80}")
    print(f"Total experiments: {total_experiments}")
    if SMOKE_MODE:
        e = experiments[0]
        print(f"  Smoke config: mode={e['mode']}, cache={e['cache']}, "
              f"churn={e['churn']}, depth={e['depth']}")
    else:
        print(f"  Headline   (cache-off, churn=5, depth=1): "
              f"{headline_count} runs ({HEADLINE_REPLICATIONS}/mode)")
        print(f"  Non-headline:                              "
              f"{nonheadline_count} runs ({REPLICATIONS}/cell)")
    print(f"Run order: randomized (seed={RUN_ORDER_SEED})")
    print(f"Per-run duration: warmup={WARMUP}s + record={DURATION}s")
    print(f"Estimated total: {total_experiments * (DURATION + WARMUP + 10) / 3600:.2f} hours")
    print(f"Output directory: {RESULTS_DIR}")
    print(f"{'='*80}\n")

    # Run all experiments
    all_results = []
    start_time = time.time()

    for i, exp in enumerate(experiments, 1):
        print(f"\nProgress: {i}/{total_experiments} ({i/total_experiments*100:.1f}%)")

        result = await run_single_experiment(
            mode=exp['mode'],
            cache_enabled=exp['cache'],
            churn_rate=exp['churn'],
            delegation_depth=exp['depth'],
            replication=exp['replication']
        )

        all_results.append(result)

        # Small delay between runs
        await asyncio.sleep(2)

    total_duration = time.time() - start_time

    # Tag every row with the run-order seed so analysis can detect cross-seed
    # comparisons and reproduce ordering exactly.
    for r in all_results:
        r['seed'] = RUN_ORDER_SEED

    # Write summary
    summary_path = PROCESSED_DIR / "benchmark_summary.csv"
    with open(summary_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'run_id', 'mode', 'cache', 'churn', 'depth', 'replication',
            'num_requests', 'duration', 'success', 'csv_path', 'error', 'seed'
        ])
        writer.writeheader()
        writer.writerows(all_results)

    # Print final summary
    successful = sum(1 for r in all_results if r['success'])
    failed = total_experiments - successful

    print(f"\n{'='*80}")
    print(f"BENCHMARK COMPLETE")
    print(f"{'='*80}")
    print(f"Total duration: {total_duration / 3600:.2f} hours")
    print(f"Successful runs: {successful}/{total_experiments}")
    print(f"Failed runs: {failed}/{total_experiments}")
    print(f"Summary: {summary_path}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    asyncio.run(run_full_benchmark())
