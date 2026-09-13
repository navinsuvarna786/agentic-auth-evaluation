"""
Results Analysis and Visualization

Analyzes benchmark results and generates plots for the paper.
"""
import json
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import seaborn as sns
from scipy import stats as sstats

# The benchmark matrix's mode list. Imported here at module scope so the
# plotting helpers below can reference it whether analyze_results is
# invoked as `python -m benchmark.analyze_results` or called as a library
# (e.g. from scripts/pilot_report.py).
from benchmark.benchmark import MODES

# Set style
sns.set_style("whitegrid")
plt.rcParams['figure.figsize'] = (10, 6)
plt.rcParams['font.size'] = 10

# Directories
RESULTS_DIR = Path(__file__).parent.parent / "results"
RAW_DIR = RESULTS_DIR / "raw"
PROCESSED_DIR = RESULTS_DIR / "processed"
FIGURES_DIR = RESULTS_DIR / "figures"


def load_all_results() -> pd.DataFrame:
    """Load all CSV results into a single DataFrame."""
    all_data = []

    for csv_file in RAW_DIR.glob("*.csv"):
        df = pd.read_csv(csv_file)
        all_data.append(df)

    if not all_data:
        raise ValueError("No results files found in {RAW_DIR}")

    combined = pd.concat(all_data, ignore_index=True)
    print(f"Loaded {len(combined)} requests from {len(all_data)} runs")

    return combined


def compute_statistics(df: pd.DataFrame) -> pd.DataFrame:
    """Compute aggregate statistics per condition."""

    # Group by experimental factors
    grouped = df.groupby(['mode', 'cache', 'churn', 'delegation_depth'])

    stats = grouped.agg({
        'latency_ms': ['median', lambda x: np.percentile(x, 95), 'mean', 'std'],
        'success': ['sum', 'count'],
        'exchange_ms': 'median',
        'verification_ms': 'median',
        'dpop_verified': 'sum',
        'audience_ok': 'sum'
    }).reset_index()

    # Flatten column names
    stats.columns = [
        'mode', 'cache', 'churn', 'delegation_depth',
        'latency_median', 'latency_p95', 'latency_mean', 'latency_std',
        'success_count', 'total_count',
        'exchange_ms_median', 'verification_ms_median',
        'dpop_verified_count', 'audience_ok_count'
    ]

    # Compute derived metrics
    stats['throughput'] = stats['success_count'] / 60  # requests per second (60s duration)
    stats['failure_rate'] = (stats['total_count'] - stats['success_count']) / stats['total_count'] * 100
    stats['dpop_rate'] = stats['dpop_verified_count'] / stats['success_count'] * 100
    stats['audience_ok_rate'] = stats['audience_ok_count'] / stats['success_count'] * 100

    return stats


def generate_latency_comparison_plot(stats: pd.DataFrame, output_path: Path):
    """
    Generate Figure 1: Latency comparison by mode and cache.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Filter for a single churn level and delegation depth for clarity
    plot_data = stats[(stats['churn'] == 5) & (stats['delegation_depth'] == 1)]

    # Subplot 1: Median latency
    cache_off = plot_data[plot_data['cache'] == False]
    cache_on = plot_data[plot_data['cache'] == True]

    x = np.arange(len(MODES))
    width = 0.35

    ax1.bar(x - width/2, cache_off['latency_median'], width, label='Cache Off', color='#e74c3c')
    ax1.bar(x + width/2, cache_on['latency_median'], width, label='Cache On', color='#3498db')

    ax1.set_xlabel('Authentication Mode')
    ax1.set_ylabel('Median Latency (ms)')
    ax1.set_title('Median End-to-End Latency')
    ax1.set_xticks(x)
    ax1.set_xticklabels(['Mode A\n(Bearer)', 'Mode B\n(Exchange)', 'Mode C\n(Exchange+DPoP)'])
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)

    # Subplot 2: P95 latency
    ax2.bar(x - width/2, cache_off['latency_p95'], width, label='Cache Off', color='#e74c3c')
    ax2.bar(x + width/2, cache_on['latency_p95'], width, label='Cache On', color='#3498db')

    ax2.set_xlabel('Authentication Mode')
    ax2.set_ylabel('P95 Latency (ms)')
    ax2.set_title('95th Percentile Latency')
    ax2.set_xticks(x)
    ax2.set_xticklabels(['Mode A\n(Bearer)', 'Mode B\n(Exchange)', 'Mode C\n(Exchange+DPoP)'])
    ax2.legend()
    ax2.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"Saved: {output_path}")
    # Also save PNG version
    png_path = output_path.with_suffix('.png')
    plt.savefig(png_path, dpi=300, bbox_inches='tight')
    print(f"Saved: {png_path}")
    plt.close()


def generate_throughput_churn_plot(stats: pd.DataFrame, output_path: Path):
    """
    Generate Figure 2: Dual-panel figure for IEEE paper.
    Panel A: Latency comparison (cache off vs on) showing optimization potential
    Panel B: Security overhead percentage relative to Mode A baseline
    """
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    colors = {'A': '#2ecc71', 'B': '#3498db', 'C': '#e74c3c'}
    mode_labels = {'A': 'Bearer Only', 'B': 'Token Exchange', 'C': 'DPoP'}

    # Panel A: Cache effect on latency (churn=5, depth=1)
    cache_off = stats[(stats['cache'] == False) & (stats['delegation_depth'] == 1) & (stats['churn'] == 5)]
    cache_on = stats[(stats['cache'] == True) & (stats['delegation_depth'] == 1) & (stats['churn'] == 5)]

    x = np.arange(len(MODES))
    width = 0.35

    off_latencies = [cache_off[cache_off['mode'] == m]['latency_median'].values[0] for m in MODES]
    on_latencies = [cache_on[cache_on['mode'] == m]['latency_median'].values[0] for m in MODES]

    bars1 = ax1.bar(x - width/2, off_latencies, width, label='Cache Off',
                    color=[colors[m] for m in MODES], edgecolor='black', linewidth=1)
    bars2 = ax1.bar(x + width/2, on_latencies, width, label='Cache On',
                    color=[colors[m] for m in MODES], edgecolor='black', linewidth=1,
                    alpha=0.5, hatch='//')

    ax1.set_xlabel('Authentication Mode', fontsize=11)
    ax1.set_ylabel('Median Latency (ms)', fontsize=11)
    ax1.set_title('(a) Latency: Cache Off vs Cache On\n(Churn=5, Depth=1)', fontsize=12)
    ax1.set_xticks(x)
    ax1.set_xticklabels([f'Mode {m}\n({mode_labels[m]})' for m in MODES])
    ax1.legend(loc='upper left')
    ax1.grid(axis='y', alpha=0.3)

    # Add value labels
    for bar, val in zip(bars1, off_latencies):
        ax1.annotate(f'{val:.1f}', xy=(bar.get_x() + bar.get_width()/2, val),
                    ha='center', va='bottom', fontsize=9)
    for bar, val in zip(bars2, on_latencies):
        ax1.annotate(f'{val:.1f}', xy=(bar.get_x() + bar.get_width()/2, val),
                    ha='center', va='bottom', fontsize=9)

    # Panel B: Security overhead as percentage of Mode A (cache off, all churns)
    plot_data = stats[(stats['cache'] == False) & (stats['delegation_depth'] == 1)]
    churn_levels = sorted(plot_data['churn'].unique())

    x2 = np.arange(len(churn_levels))
    width2 = 0.3

    # Calculate overhead relative to Mode A
    for i, mode in enumerate(['B', 'C']):
        overheads = []
        for churn in churn_levels:
            mode_a_lat = plot_data[(plot_data['mode'] == 'A') & (plot_data['churn'] == churn)]['latency_median'].values[0]
            mode_lat = plot_data[(plot_data['mode'] == mode) & (plot_data['churn'] == churn)]['latency_median'].values[0]
            overhead = ((mode_lat - mode_a_lat) / mode_a_lat) * 100
            overheads.append(overhead)

        offset = (i - 0.5) * width2
        bars = ax2.bar(x2 + offset, overheads, width2,
                      label=f'Mode {mode} ({mode_labels[mode]})',
                      color=colors[mode], edgecolor='black', linewidth=1)

        for bar, val in zip(bars, overheads):
            ax2.annotate(f'{val:.0f}%', xy=(bar.get_x() + bar.get_width()/2, val),
                        ha='center', va='bottom', fontsize=9)

    ax2.set_xlabel('Agent Churn Rate (agents/second)', fontsize=11)
    ax2.set_ylabel('Latency Overhead vs Mode A (%)', fontsize=11)
    ax2.set_title('(b) Security Overhead by Churn Rate\n(Cache Off, Depth=1)', fontsize=12)
    ax2.set_xticks(x2)
    ax2.set_xticklabels([str(c) for c in churn_levels])
    ax2.legend(loc='upper right')
    ax2.grid(axis='y', alpha=0.3)
    ax2.axhline(y=0, color='black', linestyle='-', linewidth=0.5)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"Saved: {output_path}")
    # Also save PNG version
    png_path = output_path.with_suffix('.png')
    plt.savefig(png_path, dpi=300, bbox_inches='tight')
    print(f"Saved: {png_path}")
    plt.close()
    plt.close()


SCENARIO_TITLES = {
    "S1": "S1: Stolen bearer token replay",
    "S2": "S2: DPoP proof replay (same jti)",
    "S3": "S3: Wrong audience token",
    "S4": "S4: Missing actor chain (D2)",
    "S5": "S5: Expired token reuse",
}


def _summarize_mode_rows(scenario: str, rows: list) -> str:
    """Collapse the per-row entries for one (scenario, mode) cell into a
    paper-friendly short string driven entirely by what actually happened
    on the wire. Falls back to "—" if the scenario produced no row for
    that mode."""
    if not rows:
        return "—"
    if any(r.get("actual_error") == "not_applicable" for r in rows):
        return "N/A"
    # Replay test has two rows per mode (first request + second request);
    # the mitigation is characterized by the second presentation, so
    # prefer that row when summarising S2.
    chosen = None
    for r in rows:
        attack = r.get("attack", "")
        if scenario == "S2" and attack.endswith("second_request"):
            chosen = r
            break
    if chosen is None:
        chosen = rows[0]

    if scenario == "S4" and "actual_actor_present" in chosen:
        return (
            "Actor chain present" if chosen["actual_actor_present"]
            else "Actor chain absent"
        )

    status = chosen.get("actual_status")
    err = chosen.get("actual_error")
    if status == 200:
        return "Succeeds"
    if status == 401 and err == "invalid_dpop":
        return "Fails (DPoP key mismatch, 401)"
    if status == 401 and err == "authentication_failed":
        return "Fails (token rejected, 401)"
    if status == 403 and err == "replay_attack":
        return "Fails (replay cache, 403)"
    if status == 403 and err == "invalid_audience":
        return "Fails (audience, 403)"
    if status == 503 and err == "replay_cache_unavailable":
        return "Fails-closed (cache unavailable, 503)"
    if status in (-1, 0):
        return err or "—"
    return f"status={status}, error={err}"


def generate_attack_mitigation_table(_stats: pd.DataFrame, output_path: Path) -> pd.DataFrame:
    """Build the attack/mitigation table directly from
    `security_verification.json` produced by `verify_security_claims.py`.

    Replaces a hardcoded table that LANMAN R3 flagged as misleading —
    every cell is now derived from an HTTP status returned by the live
    API. If the JSON is missing (e.g. `analyze_results.py` is run before
    the stack is up), a NO DATA placeholder table is written so the
    omission is visible rather than silent.
    """
    sec_path = PROCESSED_DIR / "security_verification.json"
    if not sec_path.exists():
        print(
            f"Warning: {sec_path} not found. "
            "Run `python -m benchmark.verify_security_claims` first; "
            "writing a placeholder table that flags every cell as NO DATA."
        )
        empty = pd.DataFrame({
            "Attack Scenario": list(SCENARIO_TITLES.values()),
            "Mode A (Bearer)": ["NO DATA"] * len(SCENARIO_TITLES),
            "Mode B (Exchange)": ["NO DATA"] * len(SCENARIO_TITLES),
            "Mode C (Exchange+DPoP)": ["NO DATA"] * len(SCENARIO_TITLES),
        })
        empty.to_csv(output_path, index=False)
        return empty

    with open(sec_path) as f:
        sec = json.load(f)

    table_rows = []
    for scenario, title in SCENARIO_TITLES.items():
        rows = sec.get("scenarios", {}).get(scenario, [])
        per_mode = {"A": [], "B": [], "C": []}
        for r in rows:
            mode = r.get("mode")
            if mode in per_mode:
                per_mode[mode].append(r)
        table_rows.append({
            "Attack Scenario": title,
            "Mode A (Bearer)": _summarize_mode_rows(scenario, per_mode["A"]),
            "Mode B (Exchange)": _summarize_mode_rows(scenario, per_mode["B"]),
            "Mode C (Exchange+DPoP)": _summarize_mode_rows(scenario, per_mode["C"]),
        })

    df = pd.DataFrame(table_rows)
    df.to_csv(output_path, index=False)
    print(f"Saved: {output_path}  (sourced from {sec_path.name})")
    return df


HEADLINE_CONDITION = {"cache": False, "churn": 5, "delegation_depth": 1}
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260506


def _filter_headline(df: pd.DataFrame) -> pd.DataFrame:
    """Restrict to the headline condition referenced in the paper's Table I."""
    mask = (
        (df["cache"] == HEADLINE_CONDITION["cache"])
        & (df["churn"] == HEADLINE_CONDITION["churn"])
        & (df["delegation_depth"] == HEADLINE_CONDITION["delegation_depth"])
    )
    return df[mask]


def bootstrap_median_ci(
    values: np.ndarray,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = 0.95,
    seed: int = BOOTSTRAP_SEED,
) -> Tuple[float, float, float]:
    """Bootstrap percentile CI for the median of `values`.

    Returns (median, lower_bound, upper_bound) at the given confidence level.
    Uses numpy random generator with a fixed seed for reproducibility.
    """
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    medians = np.empty(n_resamples)
    n = len(arr)
    for i in range(n_resamples):
        sample = rng.choice(arr, size=n, replace=True)
        medians[i] = np.median(sample)
    alpha = (1.0 - confidence) / 2.0
    lo = float(np.quantile(medians, alpha))
    hi = float(np.quantile(medians, 1.0 - alpha))
    return float(np.median(arr)), lo, hi


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's delta non-parametric effect size, robust to non-normal data.

    Range: [-1, 1]. |delta| < 0.147 = negligible, < 0.33 = small, < 0.474 =
    medium, else large (Romano et al., 2006).
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    # Pairwise sign comparison via broadcasting; safe for the headline-condition
    # sample sizes we expect (~5 reps × 60 s × 60 req/s = ~18 K each).
    diff = a[:, None] - b[None, :]
    gt = np.sum(diff > 0)
    lt = np.sum(diff < 0)
    return float((gt - lt) / (a.size * b.size))


def compute_significance_tests(df: pd.DataFrame, output_path: Path) -> Dict:
    """Pairwise Mann-Whitney U + Cliff's delta + bootstrap-CI of medians.

    Restricted to the headline condition (cache=off, churn=5, depth=1) so the
    test corresponds to the comparison reported in the paper's main latency
    table; running pairwise across the full matrix would inflate type-I
    error and isn't what reviewers asked for.
    """
    headline = _filter_headline(df)
    modes = sorted(headline["mode"].unique())

    # Per-mode bootstrap CIs of the median latency.
    cis = {}
    for mode in modes:
        latencies = headline[headline["mode"] == mode]["latency_ms"].to_numpy()
        if len(latencies) == 0:
            continue
        median, lo, hi = bootstrap_median_ci(latencies)
        cis[mode] = {
            "n": int(len(latencies)),
            "median_ms": median,
            "ci_lower_ms": lo,
            "ci_upper_ms": hi,
            "ci_confidence": 0.95,
            "ci_method": "percentile bootstrap",
            "n_resamples": BOOTSTRAP_RESAMPLES,
        }

    # Pairwise Mann-Whitney U + Cliff's delta.
    pairs = []
    for i, m1 in enumerate(modes):
        for m2 in modes[i + 1:]:
            a = headline[headline["mode"] == m1]["latency_ms"].to_numpy()
            b = headline[headline["mode"] == m2]["latency_ms"].to_numpy()
            if len(a) == 0 or len(b) == 0:
                continue
            u, p = sstats.mannwhitneyu(a, b, alternative="two-sided")
            delta = cliffs_delta(a, b)
            magnitude = (
                "negligible" if abs(delta) < 0.147
                else "small" if abs(delta) < 0.33
                else "medium" if abs(delta) < 0.474
                else "large"
            )
            pairs.append({
                "mode_a": m1,
                "mode_b": m2,
                "n_a": int(len(a)),
                "n_b": int(len(b)),
                "u_statistic": float(u),
                "p_value": float(p),
                "cliffs_delta": delta,
                "delta_magnitude": magnitude,
            })

    payload = {
        "condition": HEADLINE_CONDITION,
        "per_mode_bootstrap_ci": cis,
        "pairwise_tests": pairs,
        "notes": (
            "Mann-Whitney U is two-sided; Cliff's delta thresholds per "
            "Romano et al. (2006). Bootstrap is percentile method, "
            f"{BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}."
        ),
    }
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Saved: {output_path}")
    return payload


def generate_summary_statistics(df: pd.DataFrame, output_path: Path):
    """Generate per-mode summary computed from raw request data.

    Computes median, P95, mean, std directly across all requests within a mode
    group. This avoids the mean-of-medians fallacy where one would average
    already-aggregated per-condition medians.
    """
    rows = []
    for mode in sorted(df['mode'].unique()):
        mode_df = df[df['mode'] == mode]
        n_total = len(mode_df)
        n_success = int(mode_df['success'].sum())
        latencies = mode_df['latency_ms']
        rows.append({
            'mode': mode,
            'n_requests': n_total,
            'latency_median_ms': float(latencies.median()),
            'latency_p95_ms': float(np.percentile(latencies, 95)),
            'latency_mean_ms': float(latencies.mean()),
            'latency_std_ms': float(latencies.std()),
            'failure_rate_pct': (1.0 - n_success / n_total) * 100.0 if n_total else 0.0,
        })
    summary = pd.DataFrame(rows).set_index('mode').round(2)
    summary.to_csv(output_path)
    print(f"Saved: {output_path}")

    return summary


def analyze_results():
    """Main analysis function."""
    print("="*80)
    print("RESULTS ANALYSIS")
    print("="*80)

    # Load data
    print("\nLoading results...")
    df = load_all_results()

    # Compute statistics
    print("Computing statistics...")
    stats = compute_statistics(df)

    # Save processed statistics
    stats_path = PROCESSED_DIR / "aggregated_statistics.csv"
    stats.to_csv(stats_path, index=False)
    print(f"Saved aggregated statistics: {stats_path}")

    # Generate plots
    print("\nGenerating plots...")

    fig1_path = FIGURES_DIR / "latency_comparison.pdf"
    generate_latency_comparison_plot(stats, fig1_path)

    fig2_path = FIGURES_DIR / "throughput_churn.pdf"
    generate_throughput_churn_plot(stats, fig2_path)

    # Generate attack table
    print("\nGenerating attack/mitigation table...")
    table_path = PROCESSED_DIR / "attack_mitigation_table.csv"
    attack_table = generate_attack_mitigation_table(stats, table_path)
    print("\nAttack/Mitigation Table:")
    print(attack_table.to_string(index=False))

    # Generate summary
    print("\nGenerating summary statistics...")
    summary_path = PROCESSED_DIR / "summary_statistics.csv"
    summary = generate_summary_statistics(df, summary_path)
    print("\nSummary Statistics by Mode (computed from raw requests):")
    print(summary)

    # Significance tests + bootstrap CIs (headline condition only)
    print("\nComputing significance tests + bootstrap CIs...")
    sig_path = PROCESSED_DIR / "significance_tests.json"
    sig = compute_significance_tests(df, sig_path)
    print(f"Per-mode bootstrap-CI medians (headline condition):")
    for mode, ci in sig["per_mode_bootstrap_ci"].items():
        print(f"  Mode {mode}: median={ci['median_ms']:.2f}ms "
              f"95% CI [{ci['ci_lower_ms']:.2f}, {ci['ci_upper_ms']:.2f}] "
              f"(n={ci['n']})")
    print("Pairwise Mann-Whitney U:")
    for pair in sig["pairwise_tests"]:
        print(f"  {pair['mode_a']} vs {pair['mode_b']}: "
              f"p={pair['p_value']:.3e}, "
              f"Cliff's d={pair['cliffs_delta']:.3f} ({pair['delta_magnitude']})")

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)
    print(f"Figures: {FIGURES_DIR}")
    print(f"Processed data: {PROCESSED_DIR}")
    print("="*80 + "\n")


if __name__ == "__main__":
    analyze_results()
