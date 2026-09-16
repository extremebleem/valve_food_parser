"""Robust statistics and baseline computation for rate signals."""

from __future__ import annotations

import pytest

from src.anomaly import (
    compute_baseline,
    mean,
    median,
    median_absolute_deviation,
    percentile,
    robust_z,
)
from src.config import AnomalyConfig
from src.models import BaselineStatus


def test_median_odd_and_even():
    assert median([5, 1, 3]) == 3
    assert median([4, 1, 3, 2]) == 2.5


@pytest.mark.parametrize("fn", [median, percentile, median_absolute_deviation, mean])
def test_statistics_reject_empty_input(fn):
    with pytest.raises(ValueError):
        fn([], 50) if fn is percentile else fn([])


@pytest.mark.parametrize("pct,expected", [(0, 1.0), (50, 5.5), (90, 9.1), (100, 10.0)])
def test_percentile_matches_linear_interpolation(pct, expected):
    assert percentile(list(range(1, 11)), pct) == pytest.approx(expected)


def test_percentile_rejects_out_of_range():
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 101)


def test_mad_is_outlier_resistant():
    """One merge day with fifty commits must not move the baseline spread."""
    clean = [3, 4, 3, 5, 4]
    spiked = clean + [50]
    assert median_absolute_deviation(spiked) <= median_absolute_deviation(clean) + 1.0


def test_robust_z_is_zero_when_spread_is_unmeasurable():
    assert robust_z(90.0, 50.0, 0.0) == 0.0


def test_robust_z_scales_with_spread():
    assert robust_z(10, 4, 1.0) > robust_z(10, 4, 4.0)


def test_baseline_ok_with_enough_samples():
    baseline = compute_baseline(
        "s", "commits_24h", 0, 0, [3, 4, 3, 5, 4, 3, 4], AnomalyConfig(min_baseline_samples=7)
    )
    assert baseline.status is BaselineStatus.OK
    assert baseline.sample_count == 7
    assert baseline.median == 4.0
    assert baseline.usable is True


def test_baseline_is_learning_below_min_samples():
    baseline = compute_baseline(
        "s", "commits_24h", 0, 0, [3, 4, 3], AnomalyConfig(min_baseline_samples=7)
    )
    assert baseline.status is BaselineStatus.LEARNING
    assert baseline.usable is False


def test_baseline_with_no_samples_is_no_data():
    baseline = compute_baseline("s", "commits_24h", 0, 0, [])
    assert baseline.status is BaselineStatus.NO_DATA
    assert baseline.sample_count == 0
    assert baseline.median == 0.0


def test_baseline_ignores_none_values():
    baseline = compute_baseline("s", "m", 0, 0, [3, None, 5, None, 4, 3, 4])
    assert baseline.sample_count == 5


def test_baseline_median_is_not_dragged_by_a_single_spike():
    normal = compute_baseline("s", "m", 0, 0, [3, 4, 3, 5, 4])
    spiked = compute_baseline("s", "m", 0, 0, [3, 4, 3, 5, 4, 100])
    assert abs(spiked.median - normal.median) <= 1.0
