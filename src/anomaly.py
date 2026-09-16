"""Baseline computation for rate signals.

Pure functions over lists of numbers -- no I/O -- so the whole decision surface
is unit-testable. The storage layer is responsible only for *selecting* the
comparable historical samples: one value per local day, for a given subject and
metric, within the lookback horizon.

Why robust statistics: a commit-rate distribution is skewed and full of
outliers -- a single merge day can carry fifty commits. A mean and a standard
deviation would be dragged around by exactly the bursts we want to detect, so
the baseline is a median and the spread is a MAD-derived robust z-score.

Discrete state changes (a version bump, a new preview build) do not come
through here at all. A change is an event, not a deviation.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

from .config import AnomalyConfig
from .models import Baseline, BaselineStatus, utcnow

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
    subject_id: str,
    metric_type: str,
    weekday: int,
    minutes: int,
    samples: Iterable[float],
    config: Optional[AnomalyConfig] = None,
) -> Baseline:
    """Summarise historical samples into a :class:`Baseline`.

    ``status`` is ``learning_baseline`` below ``MIN_BASELINE_SAMPLES`` -- such a
    baseline never produces an alert, which is what stops a fresh deployment
    from firing on its own first readings.
    """
    config = config or AnomalyConfig()
    values: List[float] = [float(v) for v in samples if v is not None]

    if not values:
        return Baseline(
            venue_id=subject_id,
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
        BaselineStatus.OK if len(values) >= config.min_baseline_samples else BaselineStatus.LEARNING
    )
    return Baseline(
        venue_id=subject_id,
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
