# Outlier Exclusion Policy (Pre-registered)

**Purpose.** Define, *before* benchmark re-collection for IEEE CNS 2026, the
exact criteria under which a benchmark run will be excluded from analysis.
Pre-registration prevents reviewer concerns that we cherry-picked which runs
to discard after seeing the data.

**Pre-registered:** 2026-05-06 (criteria); 2026-05-12 (replication counts)
**Applies to:** every result CSV under `results/raw/` produced by
`benchmark/benchmark.py`. Existing pre-LANMAN data files are kept on disk
as historical record but are NOT used for any paper figure or table.

**Replication budget (B4d).** Every non-headline cell of the matrix
`(mode, cache, churn, depth)` is replicated **5 times**. The headline
condition (`cache=False, churn=5, depth=1`) is replicated **10 times per
mode** so the bootstrap CIs reported in Table I are tighter than the
rest of the matrix. Total: 33 cells × 5 + 3 cells × 10 = **195 runs**
under nominal conditions. Replacement runs (per the C1–C4 procedure
below) raise the count but do not change which cells contribute to
analysis — only the replacement counts.

---

## Granularity: per-run, never per-request

Tail latency is the *signal*, not the noise — a single 30-second request in
Mode C at high concurrency is exactly the kind of result the paper is
about. Therefore individual `latency_ms` values are **never** discarded.

Exclusion happens at the level of an entire **run** (one
`(mode, cache, churn, depth, replication)` cell of the matrix). When a run
is excluded, every request in its CSV is excluded together; the run is
re-executed (with the same seed advanced by one to break correlation).

## Exclusion criteria (any one triggers exclusion)

A run is excluded from analysis if **any** of the following is true:

### C1. Wall-clock duration exceeds 1.5× target

Each run is configured for `WARMUP + DURATION = 10 + 60 = 70` seconds.
If a run takes longer than `1.5 × 70 = 105` seconds to complete, the
underlying infrastructure was misbehaving (long GC, container restart,
network stall, replay-cache timeout cascade) and the latency distribution
is contaminated. Threshold value: `run.duration_seconds > 105`.

### C2. Failure rate exceeds 3σ above the per-condition mean

For each condition `(mode, cache, churn, depth)`, compute the mean and
standard deviation of failure rate across replications. A specific rep
is excluded if its failure rate is more than three standard deviations
above the condition's mean *and* differs by more than 1 percentage point
in absolute terms (to prevent over-exclusion on conditions where every
rep has zero failures).

### C3. Replay-cache unavailable mid-run

If the API logs `redis_client is None` or `Replay cache check failed`
during the measurement window, the run is excluded because the test was
not actually evaluating Mode C's intended security posture. (See the
B6 decision on Redis fail-closed vs. fail-open in STATE.md.)

### C4. Container health check failure

If `docker ps` shows any of `auth-keycloak`, `auth-postgres`, `auth-redis`,
or `auth-api` as `unhealthy` or `restarting` during the run, the run is
excluded.

## Replacement procedure

When a run is excluded:

1. Append a row to `results/processed/excluded_runs.csv` with the run id,
   trigger (C1–C4), and the measured value of the trigger metric.
2. Re-execute the run with `BENCHMARK_SEED = original_seed + 1`. (Both
   seeds are recorded; only the *replacement* run feeds the analysis.)
3. If the replacement also triggers an exclusion, escalate: investigate
   the root cause (load on the host, gateway throttle, etc.) and resolve
   before retrying.

## Reporting in the paper

The CNS paper will:

- Report the total number of runs initially executed.
- Report the number excluded under each criterion.
- Reproduce the exclusion table verbatim from
  `results/processed/excluded_runs.csv`.
- State the policy was pre-registered on 2026-05-06 and cite this file.

## Application to pre-LANMAN data (informational)

The Mode A cache-on churn-20 rep-0 anomaly identified in the pre-LANMAN
review (`comment.txt:107-110`: 602 s wall-clock duration, 6.33% failure
rate) would have triggered both C1 (602 > 105) and C2 (6.33 ≫ neighbors'
0%) under this policy. The pre-LANMAN paper retained it; the CNS paper
will not, regardless of whether the new benchmark reproduces the
anomaly.
