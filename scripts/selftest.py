"""End-to-end dry-run of the pipeline against a **synthetic** signal source.

This is a verification harness, not a data source. It seeds a throwaway database
with a deterministic, obviously-fake history so that baseline computation,
anomaly detection, cooldown/de-duplication and message rendering can be
exercised without any paid API key and without inventing "real" busyness numbers
anywhere in the production code path.

    python -m scripts.selftest
    python -m scripts.selftest --from-db sqlite:///data/monitor.db
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from datetime import timedelta
from typing import List, Optional

from src.config import load_settings
from src.logging_utils import setup_logging
from src.models import (
    CongestionDomain,
    LoadSignal,
    MetricType,
    Observation,
    SignalQuality,
    Venue,
    make_venue_id,
    utcnow,
)
from src.anomaly import district_index
from src.monitor import Monitor, local_slot, office_timezone
from src.providers.base import LoadProvider
from src.storage import create_storage

# (name, category, distance_m, historical mean load, current load)
SCENARIOS = [
    # the hypothesis case: venues in Valve's own building go quiet while the
    # rest of the district carries on as usual
    ("Lincoln Square South Food Hall", "food_court", 17.0, 62.0, 24.0),
    ("Mix Sushi Bar", "asian", 14.0, 58.0, 26.0),
    ("Din Tai Fung", "asian", 650.0, 46.0, 78.0),
    ("Blue Bottle Coffee", "coffee", 240.0, 61.0, 64.0),
    ("Monsoon East", "asian", 1180.0, 38.0, 41.0),
    ("Just Opened Ramen", "asian", 430.0, 0.0, 83.0),   # no history at all
    ("Newly Tracked Pho", "asian", 820.0, 44.0, 81.0),  # only 3 samples -> learning_baseline
]

# venues that should only get a partial history, to exercise the learning path
PARTIAL_HISTORY = {"Newly Tracked Pho": 3}


class ReplayProvider(LoadProvider):
    """SYNTHETIC provider used only by this self-test."""

    name = "selftest_replay"
    priority = 1

    def __init__(self, settings, current_by_venue) -> None:
        super().__init__(settings)
        self.current_by_venue = current_by_venue

    def get_current_load(self, venue: Venue) -> Optional[LoadSignal]:
        value = self.current_by_venue.get(venue.id)
        if value is None:
            return None
        return LoadSignal(
            venue_id=venue.id,
            source=self.name,
            metric_type=MetricType.LIVE_BUSYNESS_INDEX,
            metric_value=float(value),
            raw_value={"synthetic": True, "venue_live_busyness": value},
            domain=CongestionDomain.PHYSICAL_OCCUPANCY,
            confidence=0.85,
            signal_quality=SignalQuality.HIGH,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run the whole pipeline")
    parser.add_argument("--db", default="sqlite:///data/selftest.db")
    parser.add_argument("--weeks", type=int, default=8)
    args = parser.parse_args(argv)

    path = args.db.replace("sqlite:///", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)

    os.environ.setdefault("DRY_RUN", "true")
    settings = load_settings(None)
    settings = dataclasses.replace(settings, database_url=args.db, dry_run=True, log_format="text")
    setup_logging("WARNING", "text")
    tz = office_timezone(settings)

    now = utcnow()
    venues: List[Venue] = []
    current = {}
    history: List[Observation] = []

    for index, (name, category, distance, hist_mean, current_value) in enumerate(SCENARIOS):
        lat = settings.office.latitude + 0.001 * (index + 1)
        lon = settings.office.longitude + 0.001 * (index + 1)
        venue = Venue(
            id=make_venue_id(name, lat, lon),
            name=name,
            address="Bellevue, WA",
            latitude=lat,
            longitude=lon,
            distance_meters=distance,
            category=category,
            opening_hours="24/7",
            sources=["selftest"],
            takeaway=True,
        )
        venues.append(venue)
        current[venue.id] = current_value
        if hist_mean <= 0:
            continue
        samples_wanted = PARTIAL_HISTORY.get(name)
        emitted = 0
        # deterministic wobble so the MAD is non-zero but small
        for week in range(1, args.weeks + 1):
            for offset in (-35, -12, 0, 17, 41):
                if samples_wanted is not None and emitted >= samples_wanted:
                    break
                emitted += 1
                moment = now - timedelta(weeks=week, minutes=offset)
                weekday, minutes = local_slot(moment, tz)
                value = hist_mean + ((week * 7 + offset) % 9) - 4
                history.append(
                    Observation(
                        venue_id=venue.id,
                        timestamp=moment,
                        source="selftest_replay",
                        metric_type=MetricType.LIVE_BUSYNESS_INDEX.value,
                        metric_value=value,
                        load_score=round(max(0.0, value), 1),
                        domain=CongestionDomain.PHYSICAL_OCCUPANCY.value,
                        confidence=0.85,
                        signal_quality=SignalQuality.HIGH.value,
                        local_weekday=weekday,
                        local_minutes=minutes,
                    )
                )

    with create_storage(settings.database_url) as storage:
        storage.upsert_venues(venues)
        storage.insert_observations(history)

        print("=" * 78)
        print("SELF-TEST (synthetic signal source -- no real busyness data involved)")
        print("office : {}  ({:.5f}, {:.5f})".format(
            settings.office.name, settings.office.latitude, settings.office.longitude))
        print("history: {} observations over {} weeks for {} venues".format(
            len(history), args.weeks, sum(1 for s in SCENARIOS if s[3] > 0)))
        print("=" * 78)

        monitor = Monitor(
            settings, storage, providers=[ReplayProvider(settings, current)]
        )
        venues_now, _ = monitor.select_venues(now)
        # same two-pass shape as Monitor.run(): baselines first, then the
        # district index, then judge each venue against both
        observations, baselines, ratios = [], {}, []
        for venue in venues_now:
            signal = monitor.collect_signal(venue)[0]
            if signal is None:
                continue
            observation = monitor.to_observation(venue, signal, now)
            if observation is None:
                continue
            storage.insert_observations([observation])
            observations.append((venue, observation))
            baseline = monitor.baseline_for(observation)
            baselines[observation.venue_id] = baseline
            if baseline.usable and baseline.median > 0:
                ratios.append(observation.load_score / baseline.median)

        district = district_index(ratios, min_venues=3)
        print("district index: {} (1.00 = район ведёт себя нормально; "
              "по {} заведениям с baseline)".format(district, len(ratios)))

        rows = [
            monitor.evaluate(venue, observation, baselines[observation.venue_id], district)
            for venue, observation in observations
        ]

        print()
        for result in sorted(rows, key=lambda r: -abs(r.relative_percent)):
            print(result.venue.name)
            print("  Current:   {:.0f}".format(result.current_score))
            print("  Baseline:  {}".format(
                "{:.0f}  (n={}, MAD={:.1f})".format(
                    result.baseline.median, result.baseline.sample_count, result.baseline.mad)
                if result.baseline and result.baseline.sample_count else "none"))
            print("  Deviation: {:+.0f}%   vs district: {:+.0f}%   robust z: {:.1f}".format(
                result.deviation_percent, result.relative_percent, result.robust_z))
            print("  ANOMALY = {:<5}  direction = {:<5}  [{}]".format(
                str(result.is_anomaly).upper(), result.direction.value, result.reason))
            print()

        print("=" * 78)
        print("TELEGRAM OUTPUT (DRY_RUN)")
        print("=" * 78)
        outcome = monitor.notifier.process(rows)
        print("sent={alerts_sent} suppressed={alerts_suppressed} recoveries={recoveries_sent}".format(**outcome))

        print()
        print("=" * 78)
        print("SECOND RUN, same conditions -> cooldown must suppress everything")
        print("=" * 78)
        outcome2 = monitor.notifier.process(rows)
        print("sent={alerts_sent} suppressed={alerts_suppressed}".format(**outcome2))
        for reason in outcome2["suppressed_reasons"]:
            print("  - {}".format(reason))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
