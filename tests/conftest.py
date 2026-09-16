"""Shared fixtures. Everything runs against an in-memory SQLite database."""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings  # noqa: E402
from src.models import (  # noqa: E402
    CongestionDomain,
    MetricType,
    Observation,
    SignalQuality,
    Venue,
    make_venue_id,
    utcnow,
)
from src.storage import create_storage  # noqa: E402


@pytest.fixture
def settings():
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "BESTTIME_API_KEY_PRIVATE",
        "GOOGLE_MAPS_API_KEY",
        "FOURSQUARE_API_KEY",
        "DATABASE_URL",
    ):
        os.environ.pop(key, None)
    base = load_settings(None)
    return dataclasses.replace(base, dry_run=True, database_url="sqlite://:memory:")


@pytest.fixture
def storage():
    store = create_storage("sqlite://:memory:")
    store.connect()
    store.migrate()
    yield store
    store.close()


@pytest.fixture
def venue():
    return Venue(
        id=make_venue_id("Din Tai Fung", 47.6165, -122.2010),
        name="Din Tai Fung",
        address="700 Bellevue Way NE, Bellevue, WA",
        latitude=47.6165,
        longitude=-122.2010,
        distance_meters=650.0,
        category="asian",
        opening_hours="24/7",
        sources=["osm"],
    )


def make_observation(venue_id, load_score, *, minutes_ago=0, weekday=2, minutes=760, **kwargs):
    return Observation(
        venue_id=venue_id,
        timestamp=utcnow() - timedelta(minutes=minutes_ago),
        source=kwargs.pop("source", "besttime"),
        metric_type=kwargs.pop("metric_type", MetricType.LIVE_BUSYNESS_INDEX.value),
        metric_value=kwargs.pop("metric_value", load_score),
        load_score=load_score,
        domain=kwargs.pop("domain", CongestionDomain.PHYSICAL_OCCUPANCY.value),
        confidence=kwargs.pop("confidence", 0.85),
        signal_quality=kwargs.pop("signal_quality", SignalQuality.HIGH.value),
        local_weekday=weekday,
        local_minutes=minutes,
        **kwargs,
    )
