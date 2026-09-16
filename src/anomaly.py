"""Baseline computation and anomaly detection.

Pure functions over lists of numbers -- no I/O -- so the whole decision surface
is unit-testable. The storage layer is responsible only for *selecting* the
comparable historical samples (same venue, same metric, same weekday, +/- window
minutes, within the lookback horizon).

Why robust statistics: a venue's load distribution is skewed and contains
outliers (a one-off catering order, a provider glitch). The mean and standard
deviation would be dragged around by exactly the events we want to detect, so
the baseline is a median and the spread is a MAD-derived robust z-score.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

from .config import AnomalyConfig
from .models import (
    AnomalyResult,
    Baseline,
    BaselineStatus,
    Direction,
    Observation,
    Venue,
    utcnow,
)

# 1/Phi^-1(0.75): makes MAD a consistent estimator of sigma for normal data
MAD_TO_SIGMA = 1.4826


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (same convention as ``numpy.percentile``)."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0.0 <= pct <= 100.0:
        raise ValueError("percentile out of range: {}".format(pct))
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def median_absolute_deviation(values: Sequence[float], center: Optional[float] = None) -> float:
    if not values:
        raise ValueError("MAD of empty sequence")
    mid = median(values) if center is None else center
    return median([abs(value - mid) for value in values])


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / float(len(values))


def robust_z(current: float, center: float, mad: float) -> float:
    """Robust z-score. Returns 0.0 when the spread is unmeasurable."""
    sigma = mad * MAD_TO_SIGMA
    if sigma <= 1e-9:
        return 0.0
    return (current - center) / sigma


def compute_baseline(
    venue_id: str,
    metric_type: str,
    weekday: int,
    minutes: int,
    samples: Iterable[float],
    config: Optional[AnomalyConfig] = None,
) -> Baseline:
    """Summarise historical samples into a :class:`Baseline`.

    ``status`` is ``learning_baseline`` when there are fewer than
    ``MIN_BASELINE_SAMPLES`` observations -- such a baseline never alerts.
    """
    config = config or AnomalyConfig()
    values: List[float] = [float(v) for v in samples if v is not None]

    if not values:
        return Baseline(
            venue_id=venue_id,
            metric_type=metric_type,
            weekday=weekday,
            minutes=minutes,
            sample_count=0,
            median=0.0,
            mad=0.0,
            p90=0.0,
            mean=0.0,
            status=BaselineStatus.NO_DATA,
            computed_at=utcnow(),
        )

    med = median(values)
    status = (
        BaselineStatus.OK
        if len(values) >= config.min_baseline_samples
        else BaselineStatus.LEARNING
    )
    return Baseline(
        venue_id=venue_id,
        metric_type=metric_type,
        weekday=weekday,
        minutes=minutes,
        sample_count=len(values),
        median=round(med, 2),
        mad=round(median_absolute_deviation(values, med), 2),
        p90=round(percentile(values, 90.0), 2),
        mean=round(mean(values), 2),
        status=status,
        computed_at=utcnow(),
    )


def district_index(
    ratios: Sequence[float], min_venues: int = 10
) -> Optional[float]:
    """How the whole search radius is behaving right now, as one number.

    Each element is a venue's ``current / baseline`` ratio for this run. The
    median of those is the district trend: 1.0 means "the area is behaving
    normally", 0.7 means "everything is a third quieter than usual".

    Dividing a venue's own ratio by this cancels the confounders that move every
    venue at once -- rain, a public holiday, a city-wide event, a panel-data
    artefact -- which would otherwise fire an alert on all 150 venues
    simultaneously and mean nothing.

    Returns ``None`` when too few venues have a usable baseline to trust it, in
    which case the caller falls back to the raw ratio.
    """
    values = [float(r) for r in ratios if r is not None and r > 0]
    if len(values) < max(1, min_venues):
        return None
    return round(median(values), 4)


class AnomalyDetector:
    """Decides whether an observation deviates from that venue's own baseline.

    Deviation is detected in **both** directions.

    ``HIGH`` -- unusually busy. All of these must hold:

    1. the baseline is usable (``>= MIN_BASELINE_SAMPLES`` samples);
    2. ``relative_ratio >= ANOMALY_MULTIPLIER``;
    3. ``current - median >= ANOMALY_MIN_ABSOLUTE_DELTA``
       (kills "+80%" jumps from 5 to 9 on the 0-100 scale);
    4. ``current >= ANOMALY_MIN_SCORE`` (busy-for-itself but objectively quiet
       is not interesting);
    5. robust z clears ``ANOMALY_MIN_ROBUST_Z`` when the spread is measurable;
    6. the signal is confident enough (``ANOMALY_MIN_CONFIDENCE``).

    ``LOW`` -- unusually quiet, which is the signal an office crunch produces
    nearby (people order in instead of walking over):

    1. ``relative_ratio <= DROP_MULTIPLIER``;
    2. ``median - current >= DROP_MIN_ABSOLUTE_DELTA``;
    3. ``median >= DROP_MIN_BASELINE`` -- a venue that is normally quiet cannot
       drop meaningfully;
    4. robust z clears ``-DROP_MIN_ROBUST_Z``;
    5. same confidence gate.

    ``relative_ratio`` is ``current / median`` divided by the district index, so
    a district-wide move does not count as a per-venue anomaly.
    """

    def __init__(self, config: Optional[AnomalyConfig] = None) -> None:
        self.config = config or AnomalyConfig()

    def evaluate(
        self,
        venue: Venue,
        observation: Observation,
        baseline: Optional[Baseline],
        district: Optional[float] = None,
    ) -> AnomalyResult:
        cfg = self.config
        current = float(observation.load_score)
        index = self._district_factor(district)

        if baseline is None or baseline.status is BaselineStatus.NO_DATA:
            fallback = self._absolute_fallback(observation)
            return AnomalyResult(
                venue=venue,
                observation=observation,
                baseline=baseline,
                is_anomaly=fallback,
                deviation_ratio=1.0,
                deviation_percent=0.0,
                robust_z=0.0,
                status=BaselineStatus.NO_DATA,
                reason="no historical samples for this slot",
                direction=Direction.HIGH if fallback else Direction.NONE,
                district_index=index,
                relative_ratio=1.0,
            )

        ratio = self._ratio(current, baseline.median)
        relative = ratio / index if index > 0 else ratio

        if baseline.status is BaselineStatus.LEARNING:
            fallback = self._absolute_fallback(observation)
            return AnomalyResult(
                venue=venue,
                observation=observation,
                baseline=baseline,
                is_anomaly=fallback,
                deviation_ratio=round(ratio, 3),
                deviation_percent=self._percent(current, baseline.median),
                robust_z=robust_z(current, baseline.median, baseline.mad),
                status=BaselineStatus.LEARNING,
                reason="learning baseline ({}/{} samples)".format(
                    baseline.sample_count, cfg.min_baseline_samples
                ),
                direction=Direction.HIGH if fallback else Direction.NONE,
                district_index=index,
                relative_ratio=round(relative, 3),
            )

        z_score = robust_z(current, baseline.median, baseline.mad)
        confidence = float(observation.confidence or 0.0)
        spread_measurable = cfg.require_robust_z and baseline.mad > 0

        high_failures = self._high_failures(
            current, baseline, relative, z_score, confidence, spread_measurable
        )
        low_failures = (
            self._low_failures(current, baseline, relative, z_score, confidence, spread_measurable)
            if cfg.detect_drops
            else ["drop detection disabled"]
        )

        if not high_failures:
            direction, reason = Direction.HIGH, "unusually busy"
        elif not low_failures:
            direction, reason = Direction.LOW, "unusually quiet"
        else:
            direction = Direction.NONE
            # report the side the venue was actually leaning towards
            reason = "; ".join(low_failures if relative < 1.0 else high_failures)

        return AnomalyResult(
            venue=venue,
            observation=observation,
            baseline=baseline,
            is_anomaly=direction is not Direction.NONE,
            deviation_ratio=round(ratio, 3),
            deviation_percent=round(self._percent(current, baseline.median), 1),
            robust_z=round(z_score, 2),
            status=BaselineStatus.OK,
            reason=reason,
            direction=direction,
            district_index=index,
            relative_ratio=round(relative, 3),
        )

    def _district_factor(self, district: Optional[float]) -> float:
        if not self.config.use_district_index:
            return 1.0
        if district is None or district <= 0:
            return 1.0
        return float(district)

    def _high_failures(self, current, baseline, relative, z_score, confidence, spread_measurable):
        cfg = self.config
        delta = current - baseline.median
        checks = [
            (relative >= cfg.multiplier, "ratio {:.2f} < {:.2f}".format(relative, cfg.multiplier)),
            (
                delta >= cfg.min_absolute_delta,
                "absolute delta {:.1f} < {:.1f}".format(delta, cfg.min_absolute_delta),
            ),
            (current >= cfg.min_score, "score {:.1f} < floor {:.1f}".format(current, cfg.min_score)),
            (
                confidence >= cfg.min_confidence,
                "confidence {:.2f} < {:.2f}".format(confidence, cfg.min_confidence),
            ),
        ]
        if spread_measurable:
            checks.append(
                (z_score >= cfg.min_robust_z, "robust z {:.2f} < {:.2f}".format(z_score, cfg.min_robust_z))
            )
        return [why for ok, why in checks if not ok]

    def _low_failures(self, current, baseline, relative, z_score, confidence, spread_measurable):
        cfg = self.config
        drop = baseline.median - current
        checks = [
            (
                relative <= cfg.drop_multiplier,
                "ratio {:.2f} > {:.2f}".format(relative, cfg.drop_multiplier),
            ),
            (
                drop >= cfg.drop_min_absolute_delta,
                "drop {:.1f} < {:.1f}".format(drop, cfg.drop_min_absolute_delta),
            ),
            (
                baseline.median >= cfg.drop_min_baseline,
                "baseline {:.1f} < floor {:.1f}".format(baseline.median, cfg.drop_min_baseline),
            ),
            (
                confidence >= cfg.min_confidence,
                "confidence {:.2f} < {:.2f}".format(confidence, cfg.min_confidence),
            ),
        ]
        if spread_measurable:
            checks.append(
                (
                    z_score <= -cfg.drop_min_robust_z,
                    "robust z {:.2f} > -{:.2f}".format(z_score, cfg.drop_min_robust_z),
                )
            )
        return [why for ok, why in checks if not ok]

    def is_recovered(
        self,
        observation: Observation,
        baseline: Optional[Baseline],
        ratio: float,
        direction: Direction = Direction.HIGH,
    ) -> bool:
        """Has the venue come back to (near) its normal level?

        Recovery is direction-aware: a venue that was unusually *busy* recovers
        by coming down, one that was unusually *quiet* recovers by coming up.
        """
        if baseline is None or not baseline.usable:
            return False
        if baseline.median <= 0:
            return observation.load_score < self.config.min_score
        if direction is Direction.LOW:
            return observation.load_score >= baseline.median / max(ratio, 1e-9)
        return observation.load_score <= baseline.median * ratio

    # -- helpers ---------------------------------------------------------- #

    def _absolute_fallback(self, observation: Observation) -> bool:
        """Cautious escape hatch while a baseline is still being learned.

        Disabled by default: a brand new deployment must not fire alerts it
        cannot justify.
        """
        cfg = self.config
        if not cfg.fallback_absolute_enabled:
            return False
        return (
            observation.load_score >= cfg.fallback_absolute_score
            and float(observation.confidence or 0.0) >= max(cfg.min_confidence, 0.7)
        )

    @staticmethod
    def _ratio(current: float, baseline_value: float) -> float:
        if baseline_value <= 0:
            # Everything is "infinitely" above zero; cap so downstream maths and
            # message formatting stay sane.
            return 99.0 if current > 0 else 1.0
        return current / baseline_value

    @staticmethod
    def _percent(current: float, baseline_value: float) -> float:
        if baseline_value <= 0:
            return 999.0 if current > 0 else 0.0
        return (current / baseline_value - 1.0) * 100.0
