"""Produce a paper-ready numeric summary from the pilot benchmark CSVs.

Runs `analyze_results.analyze_results()` to refresh the processed
files, then prints (1) the headline-condition medians + 95% bootstrap
CIs and Mann-Whitney p-values, (2) cache-on vs cache-off comparison,
and (3) a list of every TODO numeric placeholder in main.tex that the
pilot now has a real value for.

Intended to be the first thing the operator runs after the pilot
benchmark completes. No state is modified beyond results/processed/.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from benchmark.analyze_results import (  # noqa: E402
    analyze_results,
    PROCESSED_DIR,
    HEADLINE_CONDITION,
)


def _headline_row(stats: pd.DataFrame, mode: str, cache_on: bool) -> dict:
    """Pick the median latency for one cell of the matrix."""
    row = stats[
        (stats["mode"] == mode)
        & (stats["cache"] == cache_on)
        & (stats["churn"] == HEADLINE_CONDITION["churn"])
        & (stats["delegation_depth"] == HEADLINE_CONDITION["delegation_depth"])
    ]
    if row.empty:
        return {"latency_median": None, "latency_p95": None, "n": 0}
    r = row.iloc[0]
    return {
        "latency_median": float(r["latency_median"]),
        "latency_p95": float(r["latency_p95"]),
        "n": int(r["total_count"]),
    }


def main() -> int:
    print("Running analyze_results.analyze_results() over results/raw/...")
    analyze_results()

    print()
    print("=" * 80)
    print("PILOT NUMERIC SUMMARY")
    print("=" * 80)

    # Load processed stats.
    stats_path = PROCESSED_DIR / "aggregated_statistics.csv"
    stats = pd.read_csv(stats_path)

    # Headline (cache-off, churn=5, depth=1)
    print(f"\nHeadline condition (cache=off, churn={HEADLINE_CONDITION['churn']}, "
          f"depth={HEADLINE_CONDITION['delegation_depth']}):")
    headline = {mode: _headline_row(stats, mode, False) for mode in ("A", "B", "C")}
    for mode, r in headline.items():
        med = r["latency_median"]
        p95 = r["latency_p95"]
        if med is None:
            print(f"  Mode {mode}: NO DATA (cell missing from pilot output)")
            continue
        print(f"  Mode {mode}: median={med:6.2f} ms   P95={p95:6.2f} ms   n={r['n']}")

    # Cache-on comparison at the same headline cell
    print("\nCache-on at same cell:")
    for mode in ("A", "B", "C"):
        r = _headline_row(stats, mode, True)
        med = r["latency_median"]
        if med is None:
            print(f"  Mode {mode} cache-on: NO DATA")
            continue
        cache_off_med = headline[mode]["latency_median"]
        if cache_off_med is not None and cache_off_med > 0:
            pct = (1.0 - med / cache_off_med) * 100
            print(f"  Mode {mode} cache-on: median={med:6.2f} ms   "
                  f"(cache-off was {cache_off_med:6.2f} ms, "
                  f"{pct:+.0f}% reduction)")
        else:
            print(f"  Mode {mode} cache-on: median={med:6.2f} ms")

    # Headline deltas
    if all(headline[m]["latency_median"] is not None for m in ("A", "B", "C")):
        a = headline["A"]["latency_median"]
        b = headline["B"]["latency_median"]
        c = headline["C"]["latency_median"]
        print("\nHeadline deltas:")
        print(f"  B over A: +{b - a:5.2f} ms")
        print(f"  C over A: +{c - a:5.2f} ms")
        print(f"  C over B: +{c - b:5.2f} ms")

    # Significance tests
    sig_path = PROCESSED_DIR / "significance_tests.json"
    if sig_path.exists():
        with open(sig_path) as f:
            sig = json.load(f)
        print("\nPairwise Mann-Whitney U + Cliff's delta on headline:")
        for ci in sig.get("per_mode_bootstrap_ci", {}).items():
            mode, body = ci
            print(f"  Mode {mode}: median={body['median_ms']:.2f} ms  "
                  f"95% CI [{body['ci_lower_ms']:.2f}, {body['ci_upper_ms']:.2f}]  "
                  f"n={body['n']}")
        for p in sig.get("pairwise_tests", []):
            print(f"  {p['mode_a']} vs {p['mode_b']}: "
                  f"p={p['p_value']:.2e}  d={p['cliffs_delta']:+.3f} "
                  f"({p['delta_magnitude']})")

    # Locate TODO markers in main.tex so the operator knows exactly
    # what to update.
    main_tex = ROOT / "paper" / "main.tex"
    print()
    print("=" * 80)
    print("TODO MARKERS REMAINING IN paper/main.tex")
    print("=" * 80)
    todo_re = re.compile(r"%\s*TODO\b.*", re.IGNORECASE)
    if main_tex.exists():
        with open(main_tex) as f:
            for i, line in enumerate(f, 1):
                if todo_re.search(line):
                    print(f"  L{i}: {line.rstrip()}")
    else:
        print(f"  (main.tex not found at {main_tex})")

    print()
    print("Done. Update the abstract and §V/§VII numbers from the headline "
          "block above, then re-build paper/main.tex.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
