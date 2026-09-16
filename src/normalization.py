"""Normalisation layer: heterogeneous provider metrics -> ``load_score`` 0-100.

Design rules:

* Every mapping is monotonic and *stable over time*. Absolute calibration is
  intentionally coarse -- anomaly detection compares a venue against its own
  history, so only monotonicity and stability actually matter.
* No pseudo-precision. A metric that only supports a relative reading keeps a
  lower ``confidence`` and ``signal_quality`` and is reported as such.
* Scores from different :class:`CongestionDomain` values are never merged; a
  delivery ETA and a live footfall index answer different questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import NormalizationConfig
from .models import (
    CongestionDomain,
    LoadBand,
    LoadSignal,
    MetricType,
    SignalQuality,
    band_for_score,
)


class NormalizationError(ValueError):
    """The signal cannot be expressed on the 0-100 scale."""


@dataclass(frozen=True)
class NormalizedLoad:
    load_score: float
    band: LoadBand
    confidence: float
    signal_quality: SignalQuality
    domain: CongestionDomain
    metric_type: str
    metric_value: float
    explanation: str = ""


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def linear_map(value: float, low: float, high: float, *, invert: bool = False) -> float:
    """Map ``value`` from the ``[low, high]`` envelope onto 0-100, clamped.

    ``invert=True`` means *smaller is busier* (not used today but kept because
    some providers report "minutes until a free table" inverted).
    """
    if high == low:
        raise NormalizationError("degenerate envelope: low == high == {}".format(low))
    if high < low:
        low, high = high, low
        invert = not invert
    ratio = (value - low) / (high - low)
    score = (1.0 - ratio) if invert else ratio
    return clamp(score * 100.0)


# metric -> (domain, default confidence, default quality)
_METRIC_META = {
    MetricType.LIVE_BUSYNESS_INDEX: (CongestionDomain.PHYSICAL_OCCUPANCY, 0.85, SignalQuality.HIGH),
    MetricType.FORECAST_BUSYNESS_INDEX: (CongestionDomain.PHYSICAL_OCCUPANCY, 0.35, SignalQuality.LOW),
    MetricType.LIVE_VS_FORECAST_DELTA: (CongestionDomain.PHYSICAL_OCCUPANCY, 0.6, SignalQuality.MEDIUM),
    MetricType.DELIVERY_ETA_MINUTES: (CongestionDomain.DELIVERY_CONGESTION, 0.6, SignalQuality.MEDIUM),
    MetricType.PICKUP_ETA_MINUTES: (CongestionDomain.PICKUP_CONGESTION, 0.7, SignalQuality.MEDIUM),
    MetricType.PREP_TIME_MINUTES: (CongestionDomain.PICKUP_CONGESTION, 0.75, SignalQuality.MEDIUM),
    MetricType.WAIT_TIME_MINUTES: (CongestionDomain.PHYSICAL_OCCUPANCY, 0.8, SignalQuality.HIGH),
    MetricType.QUEUE_LENGTH: (CongestionDomain.PHYSICAL_OCCUPANCY, 0.8, SignalQuality.HIGH),
    MetricType.NEXT_RESERVATION_MINUTES: (
        CongestionDomain.RESERVATION_CONGESTION,
        0.55,
        SignalQuality.MEDIUM,
    ),
    MetricType.LOAD_SCORE_DIRECT: (CongestionDomain.UNKNOWN, 0.5, SignalQuality.MEDIUM),
}


class Normalizer:
    def __init__(self, config: Optional[NormalizationConfig] = None) -> None:
        self.config = config or NormalizationConfig()

    def normalize(self, signal: LoadSignal) -> NormalizedLoad:
        metric = signal.metric_type
        if not isinstance(metric, MetricType):
            try:
                metric = MetricType(str(metric))
            except ValueError as exc:
                raise NormalizationError("unknown metric_type {!r}".format(signal.metric_type)) from exc

        value = signal.metric_value
        if value is None:
            raise NormalizationError("metric_value is None for {}".format(metric.value))
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise NormalizationError("metric_value {!r} is not numeric".format(value)) from exc
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise NormalizationError("metric_value is not finite for {}".format(metric.value))

        cfg = self.config
        if metric in (MetricType.LIVE_BUSYNESS_INDEX, MetricType.FORECAST_BUSYNESS_INDEX):
            # Providers may report >100 when a venue beats its weekly peak.
            score = clamp(value)
            explanation = "busyness index {:.0f}% of weekly peak".format(value)
        elif metric is MetricType.LOAD_SCORE_DIRECT:
            score = clamp(value)
            explanation = "provider-supplied load score"
        elif metric is MetricType.LIVE_VS_FORECAST_DELTA:
            # -100..+100 delta around the forecast -> centred on 50
            score = clamp(50.0 + value / 2.0)
            explanation = "{:+.0f}pp vs forecast".format(value)
        elif metric is MetricType.DELIVERY_ETA_MINUTES:
            score = linear_map(value, cfg.delivery_eta_low, cfg.delivery_eta_high)
            explanation = "delivery ETA {:.0f} min".format(value)
        elif metric is MetricType.PICKUP_ETA_MINUTES:
            score = linear_map(value, cfg.pickup_eta_low, cfg.pickup_eta_high)
            explanation = "pickup ETA {:.0f} min".format(value)
        elif metric is MetricType.PREP_TIME_MINUTES:
            score = linear_map(value, cfg.prep_time_low, cfg.prep_time_high)
            explanation = "prep time {:.0f} min".format(value)
        elif metric is MetricType.WAIT_TIME_MINUTES:
            score = linear_map(value, cfg.wait_time_low, cfg.wait_time_high)
            explanation = "wait {:.0f} min".format(value)
        elif metric is MetricType.QUEUE_LENGTH:
            score = linear_map(value, cfg.queue_low, cfg.queue_high)
            explanation = "{:.0f} people queueing".format(value)
        elif metric is MetricType.NEXT_RESERVATION_MINUTES:
            score = linear_map(value, cfg.reservation_low, cfg.reservation_high)
            explanation = "next table in {:.0f} min".format(value)
        else:  # pragma: no cover - guarded by the MetricType conversion above
            raise NormalizationError("no mapping for {}".format(metric.value))

        default_domain, default_conf, default_quality = _METRIC_META[metric]
        domain = signal.domain if signal.domain is not CongestionDomain.UNKNOWN else default_domain
        confidence = signal.confidence if signal.confidence is not None else default_conf
        confidence = clamp(float(confidence), 0.0, 1.0)
        quality = signal.signal_quality or default_quality

        return NormalizedLoad(
            load_score=round(score, 1),
            band=band_for_score(score),
            confidence=round(confidence, 3),
            signal_quality=quality,
            domain=domain,
            metric_type=metric.value,
            metric_value=value,
            explanation=explanation,
        )

    def envelope(self, metric_type: str) -> Optional[tuple]:
        """The ``(low, high)`` envelope for a linearly mapped metric, else ``None``."""
        cfg = self.config
        return {
            MetricType.DELIVERY_ETA_MINUTES.value: (cfg.delivery_eta_low, cfg.delivery_eta_high),
            MetricType.PICKUP_ETA_MINUTES.value: (cfg.pickup_eta_low, cfg.pickup_eta_high),
            MetricType.PREP_TIME_MINUTES.value: (cfg.prep_time_low, cfg.prep_time_high),
            MetricType.WAIT_TIME_MINUTES.value: (cfg.wait_time_low, cfg.wait_time_high),
            MetricType.QUEUE_LENGTH.value: (cfg.queue_low, cfg.queue_high),
            MetricType.NEXT_RESERVATION_MINUTES.value: (cfg.reservation_low, cfg.reservation_high),
        }.get(metric_type)

    def invert(self, metric_type: str, load_score: float) -> Optional[float]:
        """Approximate raw-metric value for a ``load_score``.

        Used only to render "usually ~32 min" next to "now 55 min" in a
        notification. Returns ``None`` for metrics whose mapping is not
        invertible, and the caller must present the result as approximate --
        the 0-100 scale is clamped, so the inverse is lossy at the ends.
        """
        if metric_type in (
            MetricType.LIVE_BUSYNESS_INDEX.value,
            MetricType.FORECAST_BUSYNESS_INDEX.value,
            MetricType.LOAD_SCORE_DIRECT.value,
        ):
            return clamp(load_score)
        envelope = self.envelope(metric_type)
        if envelope is None:
            return None
        low, high = envelope
        if high == low:
            return None
        return low + (clamp(load_score) / 100.0) * (high - low)

    def describe_metric(self, metric_type: str, value: float) -> str:
        """Human-readable rendering used in Telegram messages."""
        mapping = {
            MetricType.DELIVERY_ETA_MINUTES.value: "🚚 Delivery ETA: {:.0f} мин",
            MetricType.PICKUP_ETA_MINUTES.value: "🥡 Pickup ETA: {:.0f} мин",
            MetricType.PREP_TIME_MINUTES.value: "👨‍🍳 Время приготовления: {:.0f} мин",
            MetricType.WAIT_TIME_MINUTES.value: "⏳ Ожидание: {:.0f} мин",
            MetricType.QUEUE_LENGTH.value: "🧍 Очередь: {:.0f} чел.",
            MetricType.NEXT_RESERVATION_MINUTES.value: "🪑 Ближайший столик через {:.0f} мин",
            MetricType.LIVE_BUSYNESS_INDEX.value: "📶 Live busyness: {:.0f}% от недельного пика",
            MetricType.FORECAST_BUSYNESS_INDEX.value: "📶 Прогноз busyness: {:.0f}%",
            MetricType.LIVE_VS_FORECAST_DELTA.value: "📶 Отклонение от прогноза: {:+.0f} п.п.",
            MetricType.LOAD_SCORE_DIRECT.value: "📊 Load score: {:.0f}",
        }
        template = mapping.get(metric_type)
        if not template:
            return "{}: {:.1f}".format(metric_type, value)
        return template.format(value)


BAND_LABELS = {
    LoadBand.LOW: "низкая",
    LoadBand.NORMAL: "обычная",
    LoadBand.BUSY: "высокая",
    LoadBand.EXTREMELY_BUSY: "очень высокая",
}

DOMAIN_LABELS = {
    CongestionDomain.PHYSICAL_OCCUPANCY: "заполненность зала (proxy)",
    CongestionDomain.DELIVERY_CONGESTION: "загрузка доставки",
    CongestionDomain.PICKUP_CONGESTION: "загрузка самовывоза",
    CongestionDomain.RESERVATION_CONGESTION: "загрузка бронирования",
    CongestionDomain.UNKNOWN: "неизвестно",
}
