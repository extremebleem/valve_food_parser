"""The monitoring run end to end, with fake providers."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from src.models import (
    CongestionDomain,
    LoadSignal,
    MetricType,
    SignalQuality,
    Venue,
    make_venue_id,
    utcnow,
)
from src.monitor import Monitor, MonitorError, local_slot, office_timezone
from src.providers.base import LoadProvider, ProviderError
from src.telegram import Notifier

from .conftest import make_observation


class StubProvider(LoadProvider):
    def __init__(self, settings, values, name="stub", fail_for=(), crash_for=()):
        super().__init__(settings)
        self.name = name
        self.values = values
        self.fail_for = set(fail_for)
        self.crash_for = set(crash_for)
        self.calls = []

    def get_current_load(self, venue):
        self.calls.append(venue.id)
        if venue.name in self.crash_for:
            raise RuntimeError("provider bug")
        if venue.name in self.fail_for:
            raise ProviderError("upstream down")
        value = self.values.get(venue.name)
        if value is None:
            return None
        return LoadSignal(
            venue_id=venue.id,
            source=self.name,
            metric_type=MetricType.LIVE_BUSYNESS_INDEX,
            metric_value=float(value),
            domain=CongestionDomain.PHYSICAL_OCCUPANCY,
            confidence=0.85,
            signal_quality=SignalQuality.HIGH,
        )


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def make_venue(name, *, hours="24/7", distance=100.0, status="OPERATIONAL"):
    lat = 47.6142 + distance / 1e6
    return Venue(
        id=make_venue_id(name, lat, -122.2007),
        name=name,
        address="Bellevue, WA",
        latitude=lat,
        longitude=-122.2007,
        distance_meters=distance,
        category="restaurant",
        opening_hours=hours,
        business_status=status,
        sources=["osm"],
    )


def build(settings, storage, providers, client=None):
    return Monitor(settings, storage, providers=providers, notifier=Notifier(settings, storage, client=client or RecordingClient()))


# --------------------------------------------------------------------------- #
# venue selection
# --------------------------------------------------------------------------- #


def test_closed_venues_are_not_polled(settings, storage):
    storage.upsert_venues(
        [
            make_venue("Always Open", hours="24/7"),
            make_venue("Closed Now", hours="Mo-Fr 03:00-03:30"),
            make_venue("Permanently Closed", status="CLOSED_PERMANENTLY"),
        ]
    )
    monitor = build(settings, storage, [StubProvider(settings, {})])
    moment = datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc)  # 12:00 in Bellevue
    selected, counters = monitor.select_venues(moment)
    names = {v.name for v in selected}
    assert names == {"Always Open"}
    assert counters["closed"] == 2


def test_unknown_hours_are_polled_when_configured(settings, storage):
    storage.upsert_venues([make_venue("Unknown Hours", hours="")])
    monitor = build(settings, storage, [StubProvider(settings, {})])
    selected, counters = monitor.select_venues(utcnow())
    assert len(selected) == 1
    assert counters["unknown_hours"] == 1

    strict = dataclasses.replace(settings, assume_open_when_unknown=False)
    assert build(strict, storage, [StubProvider(strict, {})]).select_venues(utcnow())[0] == []


def test_venue_budget_keeps_the_nearest(settings, storage):
    storage.upsert_venues([make_venue("Near", distance=50), make_venue("Far", distance=1900)])
    tuned = dataclasses.replace(settings, max_venues_per_run=1)
    selected, _ = build(tuned, storage, [StubProvider(tuned, {})]).select_venues(utcnow())
    assert [v.name for v in selected] == ["Near"]


# --------------------------------------------------------------------------- #
# graceful degradation
# --------------------------------------------------------------------------- #


def test_one_broken_provider_falls_through_to_the_next(settings, storage):
    storage.upsert_venues([make_venue("Cafe One")])
    broken = StubProvider(settings, {"Cafe One": 90}, name="broken", fail_for=["Cafe One"])
    backup = StubProvider(settings, {"Cafe One": 70}, name="backup")
    monitor = build(settings, storage, [broken, backup])
    stats = monitor.run(concurrency=1)
    assert stats.venues_checked == 1
    assert stats.venues_failed == 0
    assert backup.calls == [make_venue("Cafe One").id]


def test_a_crashing_provider_does_not_kill_the_run(settings, storage):
    storage.upsert_venues([make_venue("A"), make_venue("B", distance=200)])
    provider = StubProvider(settings, {"A": 70, "B": 65}, crash_for=["A"])
    stats = build(settings, storage, [provider]).run(concurrency=1)
    assert stats.venues_failed == 1
    assert stats.venues_checked == 1


def test_a_venue_with_no_data_is_skipped_not_failed(settings, storage):
    storage.upsert_venues([make_venue("Has Data"), make_venue("No Data", distance=200)])
    stats = build(settings, storage, [StubProvider(settings, {"Has Data": 70})]).run(concurrency=1)
    assert stats.venues_checked == 1
    assert stats.venues_failed == 0


def test_no_providers_is_a_systemic_failure(settings, storage):
    storage.upsert_venues([make_venue("A")])
    with pytest.raises(MonitorError):
        build(settings, storage, []).run()


def test_an_empty_venue_table_is_a_systemic_failure(settings, storage):
    with pytest.raises(MonitorError):
        build(settings, storage, [StubProvider(settings, {})]).run()


# --------------------------------------------------------------------------- #
# full flow
# --------------------------------------------------------------------------- #


def test_full_flow_detects_an_anomaly_and_sends_one_alert(settings, storage):
    venue = make_venue("Din Tai Fung", distance=650)
    storage.upsert_venues([venue])
    tz = office_timezone(settings)
    now = utcnow()
    weekday, minutes = local_slot(now, tz)
    storage.insert_observations(
        [
            make_observation(
                venue.id, 45 + (week % 3), minutes_ago=60 * 24 * 7 * week,
                weekday=weekday, minutes=minutes,
            )
            for week in range(1, 9)
        ]
    )
    client = RecordingClient()
    stats = build(settings, storage, [StubProvider(settings, {"Din Tai Fung": 82})], client).run(
        concurrency=1
    )
    assert stats.anomalies == 1
    assert stats.alerts_sent == 1
    assert stats.observations_written == 1
    assert "Din Tai Fung" in client.messages[0]
    assert storage.get_baseline(venue.id, "live_busyness_index", weekday, minutes) is not None


def test_a_normal_reading_produces_an_observation_but_no_alert(settings, storage):
    venue = make_venue("Steady Cafe")
    storage.upsert_venues([venue])
    tz = office_timezone(settings)
    weekday, minutes = local_slot(utcnow(), tz)
    storage.insert_observations(
        [
            make_observation(venue.id, 50, minutes_ago=60 * 24 * 7 * w, weekday=weekday, minutes=minutes)
            for w in range(1, 9)
        ]
    )
    client = RecordingClient()
    stats = build(settings, storage, [StubProvider(settings, {"Steady Cafe": 52})], client).run(
        concurrency=1
    )
    assert stats.anomalies == 0
    assert stats.alerts_sent == 0
    assert stats.observations_written == 1
    assert client.messages == []


def test_a_brand_new_venue_never_alerts(settings, storage):
    storage.upsert_venues([make_venue("Brand New")])
    client = RecordingClient()
    stats = build(settings, storage, [StubProvider(settings, {"Brand New": 99})], client).run(
        concurrency=1
    )
    assert stats.anomalies == 0
    assert stats.venues_learning == 1
    assert client.messages == []


def test_observations_are_bucketed_in_local_time(settings):
    tz = office_timezone(settings)
    summer = datetime(2026, 7, 15, 19, 40, tzinfo=timezone.utc)  # 12:40 PDT
    winter = datetime(2026, 1, 14, 20, 40, tzinfo=timezone.utc)  # 12:40 PST
    assert local_slot(summer, tz) == (2, 12 * 60 + 40)
    assert local_slot(winter, tz) == (2, 12 * 60 + 40)


def test_timezone_resolution_uses_the_office_setting(settings):
    tz = office_timezone(settings)
    assert "Los_Angeles" in str(tz) or str(tz) == "UTC"
    broken = dataclasses.replace(
        settings, office=dataclasses.replace(settings.office, timezone="Not/AZone")
    )
    assert str(office_timezone(broken)) == "UTC"


def test_raw_value_can_be_dropped_to_save_storage(settings, storage):
    """STORE_RAW_VALUE=false halves the observation row size."""
    storage.upsert_venues([make_venue("Cafe")])
    lean = dataclasses.replace(settings, store_raw_value=False)
    provider = StubProvider(lean, {"Cafe": 70})
    monitor = build(lean, storage, [provider])
    venue = monitor.select_venues(utcnow())[0][0]
    signal = provider.get_current_load(venue)
    signal.raw_value = {"venue_live_busyness": 70}
    assert monitor.to_observation(venue, signal, utcnow()).raw_value is None

    fat = build(settings, storage, [provider])
    assert fat.to_observation(venue, signal, utcnow()).raw_value == {"venue_live_busyness": 70}
