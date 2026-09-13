"""Tests for the arrival-distribution selector in benchmark.launcher.

The launcher supports two inter-arrival distributions, selected via the
ARRIVAL_DIST env var:
  - deterministic (default): fixed 1/rate gap between spawns
  - poisson: exponential gaps with mean 1/rate (Poisson arrival process)

Reproducibility is via ARRIVAL_SEED.
"""
import importlib
import os
import statistics

import pytest


def _reload_with_env(**env):
    """Reload benchmark.launcher with the given env vars active so the
    module-level seed and dist defaults are re-evaluated."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import benchmark.launcher as L
    importlib.reload(L)
    return L


def test_deterministic_is_exactly_one_over_rate():
    L = _reload_with_env(ARRIVAL_DIST="deterministic")
    assert L._next_arrival_delta(64.0) == pytest.approx(1 / 64.0)
    assert L._next_arrival_delta(1.0) == 1.0


def test_default_is_deterministic():
    L = _reload_with_env(ARRIVAL_DIST=None)
    assert L._next_arrival_delta(10.0) == pytest.approx(0.1)


def test_poisson_mean_matches_one_over_rate():
    L = _reload_with_env(ARRIVAL_DIST="poisson", ARRIVAL_SEED="42")
    samples = [L._next_arrival_delta(64.0) for _ in range(5000)]
    mean = statistics.mean(samples)
    assert mean == pytest.approx(1 / 64.0, rel=0.05)


def test_poisson_variance_is_exponential():
    """For an exponential distribution, stdev equals the mean (CoV = 1)."""
    L = _reload_with_env(ARRIVAL_DIST="poisson", ARRIVAL_SEED="42")
    samples = [L._next_arrival_delta(64.0) for _ in range(5000)]
    cov = statistics.stdev(samples) / statistics.mean(samples)
    assert cov == pytest.approx(1.0, abs=0.05)


def test_poisson_is_reproducible_under_seed():
    L1 = _reload_with_env(ARRIVAL_DIST="poisson", ARRIVAL_SEED="123")
    a = [L1._next_arrival_delta(10.0) for _ in range(20)]
    L2 = _reload_with_env(ARRIVAL_DIST="poisson", ARRIVAL_SEED="123")
    b = [L2._next_arrival_delta(10.0) for _ in range(20)]
    assert a == b
