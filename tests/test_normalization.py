"""Normalisation of heterogeneous provider metrics onto the 0-100 scale."""

from __future__ import annotations

import pytest

from src.config import NormalizationConfig
from src.models import CongestionDomain, LoadBand, LoadSignal, MetricType, SignalQuality, band_for_score
from src.normalization import NormalizationError, Normalizer, clamp, linear_map


def signal(metric, value, **kwargs):
    return LoadSignal(venue_id="v1", source="test", metric_type=metric, metric_value=value, **kwargs)


@pytest.mark.parametrize(
    "score,band",
    [
        (0, LoadBand.LOW),
        (30, LoadBand.LOW),
        (31, LoadBand.NORMAL),
        (60, LoadBand.NORMAL),
        (61, LoadBand.BUSY),
        (80, LoadBand.BUSY),
        (81, LoadBand.EXTREMELY_BUSY),
        (100, LoadBand.EXTREMELY_BUSY),
    ],
)
def test_band_boundaries_match_the_specification(score, band):
    assert band_for_score(score) is band


def test_clamp_and_linear_map():
    assert clamp(150) == 100
    assert clamp(-5) == 0
    assert linear_map(15, 15, 75) == 0.0
    assert linear_map(75, 15, 75) == 100.0
    assert linear_map(45, 15, 75) == pytest.approx(50.0)
    assert linear_map(500, 15, 75) == 100.0  # clamped, never > 100
    assert linear_map(0, 15, 75) == 0.0


def test_linear_map_rejects_degenerate_envelope():
    with pytest.raises(NormalizationError):
        linear_map(10, 30, 30)


def test_live_busyness_passes_through_and_clamps():
    normalizer = Normalizer()
    assert normalizer.normalize(signal(MetricType.LIVE_BUSYNESS_INDEX, 87)).load_score == 87.0
    # BestTime reports >100 when a venue beats its own weekly peak
    assert normalizer.normalize(signal(MetricType.LIVE_BUSYNESS_INDEX, 143)).load_score == 100.0


def test_delivery_eta_matches_the_worked_example():
    """55 min delivery ETA -> busy; the inverse of 28.3 is ~32 min."""
    normalizer = Normalizer()
    result = normalizer.normalize(signal(MetricType.DELIVERY_ETA_MINUTES, 55))
    assert result.load_score == pytest.approx(66.7, abs=0.1)
    assert result.band is LoadBand.BUSY
    assert result.domain is CongestionDomain.DELIVERY_CONGESTION
    assert normalizer.invert(MetricType.DELIVERY_ETA_MINUTES.value, 28.3) == pytest.approx(32.0, abs=0.1)


def test_pickup_and_reservation_metrics_keep_their_own_domains():
    normalizer = Normalizer()
    pickup = normalizer.normalize(signal(MetricType.PICKUP_ETA_MINUTES, 35))
    reservation = normalizer.normalize(signal(MetricType.NEXT_RESERVATION_MINUTES, 90))
    assert pickup.load_score == pytest.approx(75.0)
    assert pickup.domain is CongestionDomain.PICKUP_CONGESTION
    assert reservation.load_score == pytest.approx(50.0)
    assert reservation.domain is CongestionDomain.RESERVATION_CONGESTION


def test_delta_metric_is_centred_on_fifty():
    normalizer = Normalizer()
    assert normalizer.normalize(signal(MetricType.LIVE_VS_FORECAST_DELTA, 0)).load_score == 50.0
    assert normalizer.normalize(signal(MetricType.LIVE_VS_FORECAST_DELTA, 40)).load_score == 70.0
    assert normalizer.normalize(signal(MetricType.LIVE_VS_FORECAST_DELTA, -100)).load_score == 0.0


def test_forecast_is_flagged_as_low_quality():
    """A forecast is not a measurement and must not look like one."""
    result = Normalizer().normalize(signal(MetricType.FORECAST_BUSYNESS_INDEX, 54))
    assert result.signal_quality is SignalQuality.LOW
    assert result.confidence < 0.4


def test_live_signal_is_flagged_as_high_quality():
    result = Normalizer().normalize(signal(MetricType.LIVE_BUSYNESS_INDEX, 54))
    assert result.signal_quality is SignalQuality.HIGH
    assert result.confidence >= 0.8


def test_provider_can_override_confidence_and_quality():
    result = Normalizer().normalize(
        signal(
            MetricType.DELIVERY_ETA_MINUTES,
            55,
            confidence=0.95,
            signal_quality=SignalQuality.HIGH,
        )
    )
    assert result.confidence == 0.95
    assert result.signal_quality is SignalQuality.HIGH


def test_confidence_is_clamped_to_unit_interval():
    assert Normalizer().normalize(signal(MetricType.QUEUE_LENGTH, 5, confidence=7.0)).confidence == 1.0
    assert Normalizer().normalize(signal(MetricType.QUEUE_LENGTH, 5, confidence=-2)).confidence == 0.0


def test_envelopes_are_configurable():
    tight = Normalizer(NormalizationConfig(delivery_eta_low=20.0, delivery_eta_high=40.0))
    assert tight.normalize(signal(MetricType.DELIVERY_ETA_MINUTES, 30)).load_score == pytest.approx(50.0)
    assert tight.normalize(signal(MetricType.DELIVERY_ETA_MINUTES, 55)).load_score == 100.0


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), [1, 2]])
def test_malformed_metric_values_are_rejected(bad):
    with pytest.raises(NormalizationError):
        Normalizer().normalize(signal(MetricType.DELIVERY_ETA_MINUTES, bad))


def test_unknown_metric_type_is_rejected():
    with pytest.raises(NormalizationError):
        Normalizer().normalize(signal("teleportation_latency", 5))


def test_numeric_string_values_are_accepted():
    assert Normalizer().normalize(signal(MetricType.WAIT_TIME_MINUTES, "30")).load_score == 50.0


def test_invert_returns_none_for_non_invertible_metrics():
    normalizer = Normalizer()
    assert normalizer.invert(MetricType.LIVE_VS_FORECAST_DELTA.value, 70) is None
    assert normalizer.invert("nonsense", 70) is None
    assert normalizer.invert(MetricType.LIVE_BUSYNESS_INDEX.value, 87) == 87


def test_describe_metric_renders_every_known_metric():
    normalizer = Normalizer()
    for metric in MetricType:
        text = normalizer.describe_metric(metric.value, 42)
        assert text and "42" in text
    assert "unknown_metric" in normalizer.describe_metric("unknown_metric", 1.0)
