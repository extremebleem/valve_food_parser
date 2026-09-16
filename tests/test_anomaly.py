"""Baseline calculation and anomaly detection."""

from __future__ import annotations

import dataclasses

import pytest

from src.anomaly import (
    AnomalyDetector,
    compute_baseline,
    median,
    median_absolute_deviation,
    percentile,
    robust_z,
)
from src.config import AnomalyConfig
from src.models import BaselineStatus

from .conftest import make_observation


# --------------------------------------------------------------------------- #
# robust statistics
# --------------------------------------------------------------------------- #


def test_median_odd_and_even():
    assert median([5, 1, 3]) == 3
    assert median([4, 1, 3, 2]) == 2.5


def test_median_rejects_empty():
    with pytest.raises(ValueError):
        median([])


@pytest.mark.parametrize(
    "pct,expected",
    [(0, 1.0), (50, 5.5), (90, 9.1), (100, 10.0)],
)
def test_percentile_matches_linear_interpolation(pct, expected):
    assert percentile(list(range(1, 11)), pct) == pytest.approx(expected)


def test_percentile_rejects_out_of_range():
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 101)


def test_mad_is_outlier_resistant():
    clean = [50, 51, 49, 50, 51]
    spiked = clean + [500]
    # one huge outlier barely moves the MAD, unlike a standard deviation
    assert median_absolute_deviation(spiked) <= median_absolute_deviation(clean) + 1.0


def test_robust_z_is_zero_when_spread_is_unmeasurable():
    assert robust_z(90.0, 50.0, 0.0) == 0.0


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #


def test_baseline_ok_with_enough_samples():
    baseline = compute_baseline("v1", "live_busyness_index", 2, 760, [44, 46, 48, 45, 47, 43])
    assert baseline.status is BaselineStatus.OK
    assert baseline.sample_count == 6
    assert baseline.median == pytest.approx(45.5)
    assert baseline.usable is True


def test_baseline_is_learning_below_min_samples():
    baseline = compute_baseline(
        "v1", "live_busyness_index", 2, 760, [44, 46, 48], AnomalyConfig(min_baseline_samples=5)
    )
    assert baseline.status is BaselineStatus.LEARNING
    assert baseline.usable is False


def test_baseline_with_no_samples_is_no_data():
    baseline = compute_baseline("v1", "live_busyness_index", 2, 760, [])
    assert baseline.status is BaselineStatus.NO_DATA
    assert baseline.sample_count == 0
    assert baseline.median == 0.0


def test_baseline_ignores_none_values():
    baseline = compute_baseline("v1", "m", 2, 760, [40, None, 50, None, 60, 45, 55])
    assert baseline.sample_count == 5


def test_baseline_median_is_not_dragged_by_a_single_spike():
    normal = compute_baseline("v1", "m", 2, 760, [40, 41, 42, 43, 44])
    spiked = compute_baseline("v1", "m", 2, 760, [40, 41, 42, 43, 44, 100])
    assert abs(spiked.median - normal.median) <= 1.0


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


def test_clear_anomaly_is_detected(venue):
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [44, 46, 48, 45, 47, 43, 50, 46])
    result = detector.evaluate(venue, make_observation(venue.id, 78.0), baseline)
    assert result.is_anomaly is True
    assert result.deviation_percent == pytest.approx(69.6, abs=0.5)
    assert result.robust_z > 3.0
    assert result.status is BaselineStatus.OK


def test_small_relative_increase_is_not_an_anomaly(venue):
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [44, 46, 48, 45, 47, 43, 50, 46])
    result = detector.evaluate(venue, make_observation(venue.id, 55.0), baseline)
    assert result.is_anomaly is False
    assert "ratio" in result.reason


def test_absolute_delta_gate_blocks_tiny_numbers(venue):
    """+150% from 4 to 10 is noise, not a lunch rush."""
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [4, 4, 5, 4, 3, 4])
    result = detector.evaluate(venue, make_observation(venue.id, 10.0), baseline)
    assert result.deviation_ratio >= 2.0
    assert result.is_anomaly is False
    assert "absolute delta" in result.reason


def test_min_score_floor_blocks_busy_for_itself_but_quiet_venues(venue):
    detector = AnomalyDetector(AnomalyConfig(min_absolute_delta=5.0, require_robust_z=False))
    baseline = compute_baseline("v1", "m", 2, 760, [20, 21, 19, 20, 22, 20])
    result = detector.evaluate(venue, make_observation(venue.id, 40.0), baseline)
    assert result.deviation_ratio >= 1.5
    assert result.is_anomaly is False
    assert "floor" in result.reason


def test_low_confidence_signal_cannot_alert(venue):
    detector = AnomalyDetector(AnomalyConfig(min_confidence=0.5))
    baseline = compute_baseline("v1", "m", 2, 760, [44, 46, 48, 45, 47, 43, 50, 46])
    observation = make_observation(venue.id, 90.0, confidence=0.3)
    result = detector.evaluate(venue, observation, baseline)
    assert result.is_anomaly is False
    assert "confidence" in result.reason


def test_learning_baseline_never_alerts(venue):
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [44, 46, 45])
    result = detector.evaluate(venue, make_observation(venue.id, 95.0), baseline)
    assert result.is_anomaly is False
    assert result.status is BaselineStatus.LEARNING
    assert "learning baseline" in result.reason


def test_missing_baseline_never_alerts_by_default(venue):
    detector = AnomalyDetector(AnomalyConfig())
    result = detector.evaluate(venue, make_observation(venue.id, 99.0), None)
    assert result.is_anomaly is False
    assert result.status is BaselineStatus.NO_DATA


def test_absolute_fallback_can_be_opted_into(venue):
    config = AnomalyConfig(fallback_absolute_enabled=True, fallback_absolute_score=90.0)
    detector = AnomalyDetector(config)
    assert detector.evaluate(venue, make_observation(venue.id, 95.0), None).is_anomaly is True
    assert detector.evaluate(venue, make_observation(venue.id, 80.0), None).is_anomaly is False


def test_zero_baseline_does_not_divide_by_zero(venue):
    detector = AnomalyDetector(AnomalyConfig(require_robust_z=False))
    baseline = compute_baseline("v1", "m", 2, 760, [0, 0, 0, 0, 0, 0])
    result = detector.evaluate(venue, make_observation(venue.id, 70.0), baseline)
    assert result.deviation_ratio == 99.0
    assert result.deviation_percent == 999.0
    assert result.is_anomaly is True


def test_flat_history_skips_the_z_gate(venue):
    """MAD == 0 would make every z-score infinite; the gate must be skipped."""
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [40, 40, 40, 40, 40, 40])
    result = detector.evaluate(venue, make_observation(venue.id, 70.0), baseline)
    assert baseline.mad == 0.0
    assert result.is_anomaly is True


def test_multiplier_is_configurable(venue):
    baseline = compute_baseline("v1", "m", 2, 760, [50, 50, 51, 49, 50, 50])
    observation = make_observation(venue.id, 62.0)
    strict = AnomalyDetector(AnomalyConfig(multiplier=1.5, require_robust_z=False))
    loose = AnomalyDetector(AnomalyConfig(multiplier=1.2, require_robust_z=False))
    assert strict.evaluate(venue, observation, baseline).is_anomaly is False
    assert loose.evaluate(venue, observation, baseline).is_anomaly is True


def test_recovery_detection(venue):
    detector = AnomalyDetector(AnomalyConfig())
    baseline = compute_baseline("v1", "m", 2, 760, [50, 51, 49, 50, 52, 50])
    assert detector.is_recovered(make_observation(venue.id, 55.0), baseline, 1.15) is True
    assert detector.is_recovered(make_observation(venue.id, 80.0), baseline, 1.15) is False
    assert detector.is_recovered(make_observation(venue.id, 55.0), None, 1.15) is False


def test_config_replacement_does_not_leak_between_detectors(venue):
    base = AnomalyConfig()
    tuned = dataclasses.replace(base, multiplier=3.0)
    assert base.multiplier == 1.5
    assert tuned.multiplier == 3.0
