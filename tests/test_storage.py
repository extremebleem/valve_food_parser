"""Persistence: upserts, the baseline query and its midnight wrap-around."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.models import AlertRecord, Baseline, BaselineStatus, Venue, make_venue_id, utcnow
from src.storage import SQLiteStorage, PostgresStorage, StorageError, create_storage

from .conftest import make_observation


def test_create_storage_selects_the_backend():
    assert isinstance(create_storage("sqlite:///data/x.db"), SQLiteStorage)
    assert isinstance(create_storage("sqlite://:memory:"), SQLiteStorage)
    assert isinstance(create_storage("postgresql://u:p@host/db"), PostgresStorage)
    assert isinstance(create_storage("postgres://u:p@host/db"), PostgresStorage)
    with pytest.raises(StorageError):
        create_storage("mysql://host/db")
    with pytest.raises(StorageError):
        create_storage("")


def test_venue_upsert_is_idempotent(storage, venue):
    assert storage.upsert_venues([venue]) == {"inserted": 1, "updated": 0}
    venue.name = "Din Tai Fung (Bellevue)"
    assert storage.upsert_venues([venue]) == {"inserted": 0, "updated": 1}
    stored = storage.get_venue(venue.id)
    assert stored.name == "Din Tai Fung (Bellevue)"
    assert stored.sources == ["osm"]
    assert len(storage.list_venues()) == 1


def test_venues_round_trip_all_fields(storage):
    original = Venue(
        id=make_venue_id("Test", 47.6, -122.2),
        name="Test",
        address="1 Main",
        latitude=47.6,
        longitude=-122.2,
        distance_meters=123.4,
        category="pizzeria",
        website="https://x.example",
        phone="+1 555",
        delivery=True,
        takeaway=True,
        sources=["osm", "google_places"],
        cuisine="pizza",
        opening_hours="Mo-Fr 08:00-17:00",
        source_ids={"osm": "node/1"},
    )
    storage.upsert_venues([original])
    stored = storage.get_venue(original.id)
    assert stored.delivery is True and stored.takeaway is True
    assert stored.sources == ["google_places", "osm"]
    assert stored.source_ids == {"osm": "node/1"}
    assert stored.distance_meters == 123.4


def test_stale_venues_are_deactivated_not_deleted(storage, venue):
    storage.upsert_venues([venue])
    storage.execute(
        "UPDATE venues SET last_seen = ? WHERE id = ?",
        (storage.ts(utcnow() - timedelta(days=60)), venue.id),
    )
    storage.commit()
    assert storage.deactivate_stale_venues(21) == 1
    assert storage.list_venues(active_only=True) == []
    assert len(storage.list_venues(active_only=False)) == 1


def test_source_ids_are_merged_not_replaced(storage, venue):
    storage.upsert_venues([venue])
    storage.update_venue_source_ids(venue.id, {"besttime": "ven_123"})
    storage.update_venue_source_ids(venue.id, {"pos": "abc"})
    stored = storage.get_venue(venue.id)
    assert stored.source_ids == {"osm": "", "besttime": "ven_123", "pos": "abc"} or (
        stored.source_ids["besttime"] == "ven_123" and stored.source_ids["pos"] == "abc"
    )
    assert "besttime" in stored.sources


def test_baseline_query_filters_by_venue_metric_weekday_and_slot(storage, venue):
    storage.upsert_venues([venue])
    rows = [
        make_observation(venue.id, 40, minutes_ago=60 * 24 * 7, weekday=2, minutes=740),
        make_observation(venue.id, 42, minutes_ago=60 * 24 * 14, weekday=2, minutes=790),
        make_observation(venue.id, 99, minutes_ago=60 * 24 * 7, weekday=3, minutes=760),   # other day
        make_observation(venue.id, 98, minutes_ago=60 * 24 * 7, weekday=2, minutes=1000),  # other slot
        make_observation(
            venue.id, 97, minutes_ago=60 * 24 * 7, weekday=2, minutes=760,
            metric_type="delivery_eta_minutes",
        ),  # other metric
    ]
    storage.insert_observations(rows)
    samples = storage.fetch_baseline_samples(
        venue.id, "live_busyness_index", 2, 760, window_minutes=60, lookback_weeks=8
    )
    assert sorted(samples) == [40.0, 42.0]


def test_baseline_query_respects_the_lookback_horizon(storage, venue):
    storage.upsert_venues([venue])
    storage.insert_observations(
        [
            make_observation(venue.id, 40, minutes_ago=60 * 24 * 7),
            make_observation(venue.id, 41, minutes_ago=60 * 24 * 365),  # a year ago
        ]
    )
    samples = storage.fetch_baseline_samples(
        venue.id, "live_busyness_index", 2, 760, window_minutes=60, lookback_weeks=8
    )
    assert samples == [40.0]


def test_baseline_window_wraps_across_midnight(storage):
    """A 00:20 slot must pull 23:40 samples from the *previous* weekday."""
    assert storage._slot_windows(0, 20, 60) == [(0, 0, 80), (6, 1400, 1439)]
    assert storage._slot_windows(6, 1430, 60) == [(6, 1370, 1439), (0, 0, 50)]
    assert storage._slot_windows(3, 720, 60) == [(3, 660, 780)]


def test_current_observation_is_excluded_from_its_own_baseline(storage, venue):
    storage.upsert_venues([venue])
    now = utcnow()
    current = make_observation(venue.id, 95)
    current.timestamp = now
    storage.insert_observations([make_observation(venue.id, 40, minutes_ago=60 * 24 * 7), current])
    samples = storage.fetch_baseline_samples(
        venue.id, "live_busyness_index", 2, 760, 60, 8, exclude_after=now
    )
    assert 95.0 not in samples


def test_baseline_upsert_replaces(storage, venue):
    storage.upsert_venues([venue])
    for median in (40.0, 55.0):
        storage.upsert_baseline(
            Baseline(venue.id, "m", 2, 760, 10, median, 2.0, 60.0, median, BaselineStatus.OK)
        )
    stored = storage.get_baseline(venue.id, "m", 2, 760)
    assert stored.median == 55.0
    assert storage.get_baseline(venue.id, "m", 3, 760) is None


def test_alert_history_and_state(storage, venue):
    storage.upsert_venues([venue])
    assert storage.last_alert(venue.id) is None
    assert storage.alert_state(venue.id)["active"] is False

    storage.record_alert(
        AlertRecord(venue.id, "anomaly", 87, 54, 61.1, "live_busyness_index", utcnow(), "h1")
    )
    last = storage.last_alert(venue.id, "anomaly")
    assert last.load_score == 87 and last.message_hash == "h1"

    storage.set_alert_state(venue.id, active=True, last_alert_at=utcnow(), last_score=87, peak_score=91)
    state = storage.alert_state(venue.id)
    assert state["active"] is True and state["peak_score"] == 91


def test_observations_can_be_purged(storage, venue):
    storage.upsert_venues([venue])
    storage.insert_observations(
        [
            make_observation(venue.id, 40, minutes_ago=60 * 24 * 400),
            make_observation(venue.id, 41, minutes_ago=10),
        ]
    )
    assert storage.purge_observations(90) == 1
    assert storage.count_observations() == 1


def test_inserting_nothing_is_a_no_op(storage):
    assert storage.insert_observations([]) == 0
    assert storage.upsert_venues([]) == {"inserted": 0, "updated": 0}
