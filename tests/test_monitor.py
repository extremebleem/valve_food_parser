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


def build(settings, storage, providers, client=None, *, gate_time=False):
    """Construct a Monitor for tests.

    ``gate_time=False`` (the default) disables the active-hours window, so a
    run-flow test never depends on what time of day the suite happens to run.
    The window tests opt back in by passing an explicit ActiveWindowConfig.
    """
    if not gate_time:
        settings = dataclasses.replace(
            settings,
            active_window=dataclasses.replace(settings.active_window, start_hour=0, end_hour=0),
        )
    return Monitor(
        settings,
        storage,
        providers=providers,
        notifier=Notifier(settings, storage, client=client or RecordingClient()),
    )


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


# --------------------------------------------------------------------------- #
# active window (office-local hours)
# --------------------------------------------------------------------------- #


def window(settings, start, end, weekdays=(0, 1, 2, 3, 4, 5, 6)):
    from src.config import ActiveWindowConfig

    return dataclasses.replace(
        settings, active_window=ActiveWindowConfig(start, end, tuple(weekdays))
    )


@pytest.mark.parametrize(
    "utc_hour,expected",
    [
        (13, False),  # 06:00 PDT
        (18, False),  # 11:00 PDT -- lunch, deliberately outside a 14-21 window
        (20, False),  # 13:00 PDT
        (21, True),   # 14:00 PDT
        (0, True),    # 17:00 PDT
        (3, True),    # 20:00 PDT
        (4, False),   # 21:00 PDT -- end is exclusive
    ],
)
def test_active_window_uses_office_local_time(settings, storage, utc_hour, expected):
    tuned = window(settings, 14, 21)
    moment = datetime(2026, 7, 15, utc_hour, 30, tzinfo=timezone.utc)
    monitor = build(tuned, storage, [StubProvider(tuned, {})], gate_time=True)
    assert monitor.is_active_now(moment) is expected


def test_active_window_survives_dst(settings, storage):
    """The same local hour maps to different UTC hours in summer and winter."""
    tuned = window(settings, 14, 21)
    monitor = build(tuned, storage, [StubProvider(tuned, {})], gate_time=True)
    summer = datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc)  # 14:30 PDT
    winter = datetime(2026, 1, 14, 22, 30, tzinfo=timezone.utc)  # 14:30 PST
    assert monitor.is_active_now(summer) is True
    assert monitor.is_active_now(winter) is True
    # and one hour earlier is outside the window in each regime
    assert monitor.is_active_now(datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)) is False
    assert monitor.is_active_now(datetime(2026, 1, 14, 21, 30, tzinfo=timezone.utc)) is False


def test_window_can_wrap_midnight(settings, storage):
    tuned = window(settings, 18, 2)
    monitor = build(tuned, storage, [StubProvider(tuned, {})], gate_time=True)
    assert monitor.is_active_now(datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)) is True   # 21:00
    assert monitor.is_active_now(datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)) is True   # 01:00
    assert monitor.is_active_now(datetime(2026, 7, 16, 17, 0, tzinfo=timezone.utc)) is False  # 10:00


def test_window_can_restrict_weekdays(settings, storage):
    tuned = window(settings, 14, 21, weekdays=(0, 1, 2, 3, 4))
    monitor = build(tuned, storage, [StubProvider(tuned, {})], gate_time=True)
    assert monitor.is_active_now(datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)) is True   # Wed
    assert monitor.is_active_now(datetime(2026, 7, 18, 22, 0, tzinfo=timezone.utc)) is False  # Sat


def test_equal_start_and_end_means_always_on(settings, storage):
    tuned = window(settings, 0, 0)
    monitor = build(tuned, storage, [StubProvider(tuned, {})], gate_time=True)
    for utc_hour in range(0, 24, 3):
        assert monitor.is_active_now(datetime(2026, 7, 15, utc_hour, tzinfo=timezone.utc)) is True


def test_run_outside_the_window_touches_no_provider(settings, storage, monkeypatch):
    """The whole point: zero API calls, zero cost, outside the window."""
    storage.upsert_venues([make_venue("Cafe")])
    tuned = window(settings, 14, 21)
    provider = StubProvider(tuned, {"Cafe": 95})
    client = RecordingClient()
    monitor = build(tuned, storage, [provider], client, gate_time=True)

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "utcnow", lambda: datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))
    stats = monitor.run(concurrency=1)

    assert "outside active window" in stats.skipped_reason
    assert provider.calls == []
    assert client.messages == []
    assert stats.venues_checked == 0
    assert storage.count_observations() == 0


def test_run_inside_the_window_proceeds(settings, storage, monkeypatch):
    storage.upsert_venues([make_venue("Cafe")])
    tuned = window(settings, 14, 21)
    provider = StubProvider(tuned, {"Cafe": 70})
    monitor = build(tuned, storage, [provider], gate_time=True)

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "utcnow", lambda: datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc))
    stats = monitor.run(concurrency=1)

    assert stats.skipped_reason == ""
    assert stats.venues_checked == 1


def test_default_window_is_14_to_21_office_local(settings, storage):
    """The shipped default matches .env.example and the workflow, so a local run
    and a CI run behave identically."""
    assert (settings.active_window.start_hour, settings.active_window.end_hour) == (14, 21)
    monitor = build(settings, storage, [StubProvider(settings, {})], gate_time=True)
    assert monitor.is_active_now(datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)) is False  # 06:00
    assert monitor.is_active_now(datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)) is True   # 15:00


def test_outside_the_window_a_misconfigured_run_still_exits_cleanly(settings, storage, monkeypatch):
    """~3 of the 24 daily cron ticks land outside the window. They must never
    turn the workflow red, whatever else is missing."""
    tuned = window(settings, 14, 21)
    monitor = build(tuned, storage, [], gate_time=True)  # no providers, no venues

    import src.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "utcnow", lambda: datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))
    stats = monitor.run(concurrency=1)
    assert "outside active window" in stats.skipped_reason

    # inside the window the same misconfiguration is still a systemic failure
    monkeypatch.setattr(monitor_module, "utcnow", lambda: datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc))
    with pytest.raises(MonitorError):
        monitor.run(concurrency=1)


def test_venues_without_a_reading_are_counted_separately_from_failures(settings, storage):
    """Live-signal coverage is the number that decides whether the approach is
    viable, so "no data" must not hide inside "checked" or "failed"."""
    storage.upsert_venues(
        [make_venue("Has Data"), make_venue("No Data", distance=200), make_venue("Broken", distance=300)]
    )
    provider = StubProvider(settings, {"Has Data": 70}, fail_for=["Broken"])
    stats = build(settings, storage, [provider]).run(concurrency=1)
    assert stats.venues_checked == 1
    assert stats.venues_no_data == 1
    assert stats.venues_failed == 1
    assert "venues_no_data=1" in stats.as_logline()
