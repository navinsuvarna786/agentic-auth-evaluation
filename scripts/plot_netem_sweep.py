"""Plot the netem RTT sweep at the headline cell.

Two-panel figure:
  (a) absolute median latency vs netem RTT, log-log
  (b) per-mode overhead over Mode A vs netem RTT

Reads `results/raw/mode-{A,B,C}_cache-off_churn-5_depth-1_rep-{i}_netem-{0,1,10,50}ms.csv`
across all available replications and writes
`results/figures/netem_rtt_sweep.{pdf,png}` plus
`results/processed/netem_sweep_reps.json`.

Aggregation: each run contributes one summary statistic (its median request
latency); points are the median of those per-run medians across replications
and bands are their min--max range. The band therefore expresses
across-replication variability, not the within-run request spread an earlier
version of this script plotted -- with a single run per cell that band was
indistinguishable from raw observation scatter.

Panel (b) differences replications pairwise: for each rep i and delay d it
computes median_X(i,d) - median_A(i,d) and then summarises across i. Taking
the difference before aggregating keeps a rep's shared ambient conditions
(host load, network state) from leaking into the overhead estimate, which
differencing the aggregates would not.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "results" / "raw"
FIGURES = ROOT / "results" / "figures"
PROCESSED = ROOT / "results" / "processed"

RE = re.compile(
    r"mode-([ABC])_cache-off_churn-5_depth-1_rep-(\d+)_netem-(\d+)ms\.csv$"
)

COLORS = {"A": "#2ecc71", "B": "#3498db", "C": "#e74c3c"}
LABELS = {"A": "Mode A", "B": "Mode B", "C": "Mode C"}
MARKERS = {"A": "o", "B": "s", "C": "^"}
LINESTYLES = {"A": "-", "B": "--", "C": ":"}  # solid / dashed / dotted, as the paper caption states


def load_runs() -> pd.DataFrame:
    """One row per (mode, rep, delay): that run's median request latency."""
    rows: list[dict] = []
    for f in sorted(RAW.glob("mode-?_cache-off_churn-5_depth-1_rep-*_netem-*ms.csv")):
        # Skip the redis-only sweep so the figure reflects the both-endpoint
        # configuration documented in §IV.Prototype.
        if "redis-only" in f.name:
            continue
        m = RE.match(f.name)
        if not m:
            continue
        mode, rep, delay = m.group(1), int(m.group(2)), int(m.group(3))
        df = pd.read_csv(f)
        rows.append({
            "mode": mode,
            "rep": rep,
            "netem_ms": delay,
            "median_ms": float(df["latency_ms"].median()),
            "p95_ms": float(df["latency_ms"].quantile(0.95)),
            "requests": int(len(df)),
        })
    return pd.DataFrame(rows)


def summarise(runs: pd.DataFrame) -> pd.DataFrame:
    """Median / min / max of the per-run medians, across replications.

    p95 is carried alongside because the prose quotes a tail figure. It is
    summarised the same way (median of the per-run p95s): the tail is far less
    reproducible than the median, so a single run's p95 is not a safe number
    to quote.
    """
    return (
        runs.groupby(["mode", "netem_ms"])
        .agg(median=("median_ms", "median"),
             lo=("median_ms", "min"),
             hi=("median_ms", "max"),
             p95=("p95_ms", "median"),
             p95_lo=("p95_ms", "min"),
             p95_hi=("p95_ms", "max"),
             n=("median_ms", "count"))
        .reset_index()
        .sort_values(["mode", "netem_ms"])
    )


def paired_overhead(runs: pd.DataFrame) -> pd.DataFrame:
    """Per-rep overhead over Mode A at the same RTT, summarised across reps."""
    wide = runs.pivot_table(
        index=["rep", "netem_ms"], columns="mode", values="median_ms"
    )
    out: list[dict] = []
    for mode in ("B", "C"):
        if mode not in wide.columns or "A" not in wide.columns:
            continue
        diff = (wide[mode] - wide["A"]).dropna().reset_index(name="overhead_ms")
        agg = (
            diff.groupby("netem_ms")["overhead_ms"]
            .agg(median="median", lo="min", hi="max", n="count")
            .reset_index()
        )
        agg["mode"] = mode
        out.append(agg)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main() -> int:
    runs = load_runs()
    if runs.empty:
        print("No netem CSVs found.", file=sys.stderr)
        return 1

    data = summarise(runs)
    over = paired_overhead(runs)

    print("=== per-run medians ===")
    print(runs.sort_values(["mode", "netem_ms", "rep"]).to_string(index=False))
    print("\n=== across-replication summary (panel a) ===")
    print(data.to_string(index=False))
    if not over.empty:
        print("\n=== paired overhead over Mode A (panel b) ===")
        print(over.sort_values(["mode", "netem_ms"]).to_string(index=False))

    reps_per_cell = sorted(data["n"].unique().tolist())
    print(f"\nreplications per cell: {reps_per_cell}")

    # IEEE single column is 3.5 in; draw at final size so fonts print at
    # 6-7 pt instead of being scaled down ~3x from a wide canvas.
    sns.set_style("whitegrid")
    plt.rcParams.update({"font.size": 6.5, "axes.labelsize": 7, "axes.titlesize": 7,
                         "xtick.labelsize": 6.5, "ytick.labelsize": 6.5,
                         "legend.fontsize": 6, "lines.linewidth": 1.2,
                         "lines.markersize": 3.5, "axes.linewidth": 0.6,
                         "grid.linewidth": 0.4})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(3.5, 1.8))

    # Panel (a): absolute median latency vs netem, min-max band across reps
    for mode in ("A", "B", "C"):
        sub = data[data["mode"] == mode]
        if sub.empty:
            continue
        ax1.plot(sub["netem_ms"], sub["median"],
                 marker=MARKERS[mode], linestyle=LINESTYLES[mode],
                 color=COLORS[mode], label=LABELS[mode])
        ax1.fill_between(sub["netem_ms"], sub["lo"], sub["hi"],
                         alpha=0.15, color=COLORS[mode])
    ax1.set_xlabel("Injected RTT (ms)")
    ax1.set_ylabel("Median request latency (ms)")
    ax1.set_title("(a) Median latency (min–max band)")
    ax1.set_xscale("symlog", linthresh=1)
    ax1.set_yscale("log")
    ax1.set_xticks([0, 1, 10, 50])
    ax1.set_xticklabels(["0", "1", "10", "50"])
    ax1.legend(loc="upper left", frameon=True, handlelength=2.2, borderpad=0.3)
    ax1.grid(True, which="both", alpha=0.3)

    # Panel (b): paired overhead over Mode A
    for mode in ("B", "C"):
        sub = over[over["mode"] == mode] if not over.empty else pd.DataFrame()
        if sub.empty:
            continue
        sub = sub.sort_values("netem_ms")
        ax2.plot(sub["netem_ms"], sub["median"],
                 marker=MARKERS[mode], linestyle=LINESTYLES[mode],
                 color=COLORS[mode],
                 label=f"{LABELS[mode]} − A")
        ax2.fill_between(sub["netem_ms"], sub["lo"], sub["hi"],
                         alpha=0.15, color=COLORS[mode])
    ax2.set_xlabel("Injected RTT (ms)")
    ax2.set_ylabel("Overhead over Mode A (ms)")
    ax2.set_title("(b) Paired overhead")
    ax2.set_xscale("symlog", linthresh=1)
    ax2.set_xticks([0, 1, 10, 50])
    ax2.set_xticklabels(["0", "1", "10", "50"])
    ax2.legend(loc="upper left", frameon=True, handlelength=2.2, borderpad=0.3)
    ax2.grid(True, which="both", alpha=0.3)
    ax2.axhline(0, color="black", linewidth=0.4, alpha=0.5)

    plt.tight_layout(pad=0.3, w_pad=1.6)
    FIGURES.mkdir(parents=True, exist_ok=True)
    pdf = FIGURES / "netem_rtt_sweep.pdf"
    png = FIGURES / "netem_rtt_sweep.png"
    plt.savefig(pdf, dpi=300, bbox_inches="tight")
    plt.savefig(png, dpi=300, bbox_inches="tight")
    print(f"Saved {pdf}")
    print(f"Saved {png}")

    PROCESSED.mkdir(parents=True, exist_ok=True)
    out_json = PROCESSED / "netem_sweep_reps.json"
    out_json.write_text(json.dumps({
        "description": (
            "Headline-cell netem RTT sweep. Points are the median of per-run "
            "median latencies across replications; bands are their min--max "
            "range. Overhead is differenced per replication before aggregation."
        ),
        "replications_per_cell": reps_per_cell,
        "per_run_medians": runs.sort_values(["mode", "netem_ms", "rep"]).to_dict("records"),
        "panel_a": data.to_dict("records"),
        "panel_b_paired_overhead": over.sort_values(["mode", "netem_ms"]).to_dict("records") if not over.empty else [],
    }, indent=2))
    print(f"Saved {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
