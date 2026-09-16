"""Providers must degrade, never crash, on malformed or hostile API responses."""

from __future__ import annotations

import dataclasses

import pytest

from src.models import MetricType, Venue
from src.providers.base import ProviderError
from src.providers.besttime import BestTimeProvider
from src.providers.foursquare import FoursquareProvider
from src.providers.generic_http import (
    GenericHttpProvider,
    GenericProviderSpec,
    ProviderConfigError,
    dig,
    expand_env,
)
from src.providers.google_places import GooglePlacesProvider
from src.providers.osm import OverpassProvider

MALFORMED = [
    None,
    [],
    "",
    "not json at all",
    0,
    {},
    {"elements": None},
    {"elements": "nope"},
    {"places": None},
    {"results": {"unexpected": "shape"}},
]


def bt(settings):
    return BestTimeProvider(
        dataclasses.replace(
            settings,
            providers=dataclasses.replace(settings.providers, besttime_private_key="pri_test"),
        )
    )


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [None, [], "", 0, {"elements": None}, {"elements": "nope"}])
def test_overpass_rejects_unusable_payloads(settings, payload):
    with pytest.raises(ProviderError):
        OverpassProvider(settings).parse(payload, settings.office)


def test_overpass_skips_broken_elements_but_keeps_good_ones(settings):
    payload = {
        "elements": [
            None,
            "string",
            {"type": "node"},                                   # no tags
            {"type": "node", "tags": {"amenity": "restaurant"}},  # no name, no coords
            {"type": "node", "tags": {"name": "No Coords", "amenity": "cafe"}},
            {"type": "node", "lat": "bad", "lon": 0, "tags": {"name": "Bad Coords", "amenity": "cafe"}},
            {
                "type": "node",
                "id": 1,
                "lat": 47.6143,
                "lon": -122.2008,
                "tags": {"name": "Good Cafe", "amenity": "cafe", "takeaway": "yes"},
            },
            {
                "type": "way",
                "id": 2,
                "center": {"lat": 47.6150, "lon": -122.2010},
                "tags": {"name": "Good Pizza", "amenity": "restaurant", "cuisine": "pizza"},
            },
            {
                "type": "node",
                "id": 3,
                "lat": 47.9,
                "lon": -122.9,
                "tags": {"name": "Too Far", "amenity": "cafe"},
            },
            {
                "type": "node",
                "id": 4,
                "lat": 47.6143,
                "lon": -122.2008,
                "tags": {"name": "Closed", "disused:amenity": "restaurant", "amenity": "restaurant"},
            },
        ]
    }
    venues = OverpassProvider(settings).parse(payload, settings.office)
    names = sorted(v.name for v in venues)
    assert names == ["Good Cafe", "Good Pizza"]
    assert [v.category for v in venues if v.name == "Good Pizza"] == ["pizzeria"]
    assert [v.takeaway for v in venues if v.name == "Good Cafe"] == [True]


def test_overpass_category_mapping(settings):
    categorise = OverpassProvider(settings)._category
    assert categorise({"amenity": "restaurant", "cuisine": "pizza"}) == "pizzeria"
    assert categorise({"amenity": "restaurant", "cuisine": "sushi;japanese"}) == "asian"
    assert categorise({"amenity": "fast_food", "cuisine": "burger"}) == "burger"
    assert categorise({"shop": "bakery"}) == "bakery"
    assert categorise({"amenity": "pub"}) == "bar_pub"
    assert categorise({"amenity": "bench"}) is None
    assert categorise({}) is None


def test_overpass_address_assembly(settings):
    build = OverpassProvider(settings)._address
    assert build({"addr:housenumber": "10400", "addr:street": "NE 4th St", "addr:city": "Bellevue"}) == (
        "10400 NE 4th St, Bellevue"
    )
    assert build({"addr:full": "Somewhere"}) == "Somewhere"
    assert build({}) == ""


# --------------------------------------------------------------------------- #
# BestTime
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", MALFORMED)
def test_besttime_returns_none_for_malformed_payloads(settings, venue, payload):
    import logging

    # a real handler must actually format the record, or the bug stays hidden
    logging.getLogger("src.providers.besttime").setLevel(logging.INFO)
    assert bt(settings).parse_live(payload, venue) is None


def test_besttime_parses_a_live_reading(settings, venue):
    signal = bt(settings).parse_live(
        {
            "status": "OK",
            "analysis": {
                "venue_live_busyness": 87,
                "venue_forecasted_busyness": 54,
                "venue_live_busyness_available": True,
                "venue_live_forecasted_delta": 33,
            },
        },
        venue,
    )
    assert signal.metric_type is MetricType.LIVE_BUSYNESS_INDEX
    assert signal.metric_value == 87.0
    assert signal.confidence >= 0.8


def test_besttime_accepts_flat_payloads(settings, venue):
    signal = bt(settings).parse_live(
        {"venue_live_busyness": 61, "venue_live_busyness_available": True}, venue
    )
    assert signal is not None and signal.metric_value == 61.0


def test_besttime_refuses_a_forecast_as_a_measurement_by_default(settings, venue):
    payload = {
        "status": "OK",
        "analysis": {
            "venue_live_busyness_available": False,
            "venue_forecasted_busyness": 54,
            "venue_forecast_busyness_available": True,
        },
    }
    import logging

    # a real handler must actually format the record, or the bug stays hidden
    logging.getLogger("src.providers.besttime").setLevel(logging.INFO)
    assert bt(settings).parse_live(payload, venue) is None


def test_besttime_forecast_fallback_is_opt_in_and_low_quality(settings, venue):
    tuned = dataclasses.replace(
        settings,
        providers=dataclasses.replace(
            settings.providers,
            besttime_private_key="pri_test",
            besttime_allow_forecast_as_load=True,
        ),
    )
    signal = BestTimeProvider(tuned).parse_live(
        {
            "status": "OK",
            "analysis": {
                "venue_live_busyness_available": False,
                "venue_forecasted_busyness": 54,
                "venue_forecast_busyness_available": True,
            },
        },
        venue,
    )
    assert signal.metric_type is MetricType.FORECAST_BUSYNESS_INDEX
    assert signal.confidence < 0.4


def test_besttime_ignores_non_numeric_values(settings, venue):
    payload = {"analysis": {"venue_live_busyness": "very busy", "venue_live_busyness_available": True}}
    import logging

    # a real handler must actually format the record, or the bug stays hidden
    logging.getLogger("src.providers.besttime").setLevel(logging.INFO)
    assert bt(settings).parse_live(payload, venue) is None


def test_besttime_honours_an_error_status(settings, venue):
    payload = {"status": "ERROR", "analysis": {"venue_live_busyness": 99}}
    import logging

    # a real handler must actually format the record, or the bug stays hidden
    logging.getLogger("src.providers.besttime").setLevel(logging.INFO)
    assert bt(settings).parse_live(payload, venue) is None


def test_besttime_is_disabled_without_a_key(settings):
    assert BestTimeProvider(settings).enabled is False


def test_besttime_supports_only_identifiable_venues(settings):
    provider = bt(settings)
    assert provider.supports(Venue(id="x", name="Nameless", address="")) is False
    assert provider.supports(Venue(id="x", name="Cafe", address="1 Main St")) is True


# --------------------------------------------------------------------------- #
# Google Places
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", MALFORMED)
def test_google_tolerates_malformed_payloads(settings, payload):
    assert GooglePlacesProvider(settings)._places(payload) == []


def test_google_place_parsing_and_filtering(settings):
    provider = GooglePlacesProvider(settings)
    good = {
        "id": "ChIJabc",
        "displayName": {"text": "Din Tai Fung"},
        "formattedAddress": "700 Bellevue Way NE",
        "location": {"latitude": 47.6165, "longitude": -122.2010},
        "types": ["chinese_restaurant", "restaurant"],
        "primaryType": "chinese_restaurant",
        "takeout": True,
        "businessStatus": "OPERATIONAL",
    }
    venue = provider._place_to_venue(good, settings.office)
    assert venue.category == "asian"
    assert venue.takeaway is True
    assert venue.source_ids["google_places"] == "ChIJabc"

    for bad in (
        {},
        {"displayName": {"text": ""}, "location": {"latitude": 1, "longitude": 1}},
        {"displayName": {"text": "X"}},
        {"displayName": {"text": "X"}, "location": {"latitude": "a", "longitude": "b"}},
        dict(good, businessStatus="CLOSED_PERMANENTLY"),
        dict(good, location={"latitude": 40.0, "longitude": -100.0}),  # far away
    ):
        assert provider._place_to_venue(bad, settings.office) is None


def test_google_opening_hours_conversion_round_trips_into_the_parser(settings):
    from datetime import datetime

    from src import opening_hours as oh

    spec = GooglePlacesProvider.opening_hours_to_osm(
        {
            "periods": [
                {"open": {"day": 1, "hour": 11, "minute": 0}, "close": {"day": 1, "hour": 21, "minute": 30}},
                {"open": {"day": 2, "hour": 11, "minute": 0}, "close": {"day": 2, "hour": 21, "minute": 30}},
            ]
        }
    )
    assert spec.startswith("Mo 11:00-21:30")
    assert oh.is_open(spec, datetime(2026, 9, 15, 12, 0)) is True   # Tuesday
    assert oh.is_open(spec, datetime(2026, 9, 15, 23, 0)) is False


@pytest.mark.parametrize("bad", [None, {}, {"periods": "nope"}, {"periods": [None, {"open": {}}]}])
def test_google_opening_hours_handles_junk(bad):
    assert GooglePlacesProvider.opening_hours_to_osm(bad) == ""


def test_google_tiling_covers_the_radius(settings):
    provider = GooglePlacesProvider(settings)
    tiles = list(provider.tile_circle(settings.office.latitude, settings.office.longitude, 2000))
    assert len(tiles) > 10
    assert all(abs(lat - settings.office.latitude) < 0.05 for lat, _, _ in tiles)


def test_google_is_disabled_without_a_key(settings):
    assert GooglePlacesProvider(settings).enabled is False


# --------------------------------------------------------------------------- #
# Foursquare
# --------------------------------------------------------------------------- #


def test_foursquare_accepts_both_response_shapes(settings):
    provider = FoursquareProvider(settings)
    new_shape = {
        "fsq_place_id": "abc",
        "name": "Sushi Kashiba",
        "latitude": 47.6150,
        "longitude": -122.2005,
        "location": {"formatted_address": "10 Main St"},
        "categories": [{"name": "Sushi Restaurant"}],
    }
    legacy = {
        "fsq_id": "def",
        "name": "Old Shape Cafe",
        "geocodes": {"main": {"latitude": 47.6150, "longitude": -122.2005}},
        "categories": [{"name": "Coffee Shop"}],
    }
    assert provider._to_venue(new_shape, settings.office).category == "asian"
    assert provider._to_venue(legacy, settings.office).category == "coffee"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "string",
        {},
        {"name": ""},
        {"name": "No coords"},
        {"name": "Bad coords", "latitude": "x", "longitude": "y"},
        {"name": "Far", "latitude": 40.0, "longitude": -100.0},
    ],
)
def test_foursquare_rejects_unusable_items(settings, bad):
    assert FoursquareProvider(settings)._to_venue(bad, settings.office) is None


def test_foursquare_cursor_extraction(settings):
    provider = FoursquareProvider(settings)
    assert provider._next_cursor({"context": {"next_cursor": "abc"}}) == "abc"
    assert provider._next_cursor({"next": "https://x?cursor=zzz&limit=50"}) == "zzz"
    assert provider._next_cursor({}) is None
    assert provider._next_cursor("garbage") is None


# --------------------------------------------------------------------------- #
# generic declarative provider
# --------------------------------------------------------------------------- #


def spec(**overrides):
    base = {
        "name": "p",
        "metric_type": "pickup_eta_minutes",
        "url_template": "https://example.com/{source_id}",
        "value_path": "quote.eta",
    }
    base.update(overrides)
    return GenericProviderSpec.from_dict(base)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        "string",
        {"name": "p"},
        {"name": "p", "metric_type": "nope", "url_template": "https://x"},
        {"name": "p", "metric_type": "queue_length", "url_template": "ftp://x"},
        {"name": "p", "metric_type": "queue_length", "url_template": "https://x", "domain": "??"},
        {"name": "p", "metric_type": "queue_length", "url_template": "https://x", "signal_quality": "??"},
    ],
)
def test_generic_spec_validation(bad):
    with pytest.raises(ProviderConfigError):
        GenericProviderSpec.from_dict(bad)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"quote": {"eta": 32}}, 32.0),
        ({"quote": {"eta": "32"}}, 32.0),
        ({"quote": {"eta": "about 32 min"}}, 32.0),
        ({"quote": {"eta": None}}, None),
        ({"quote": {"eta": True}}, None),
        ({"quote": {"eta": "soon"}}, None),
        ({"quote": {}}, None),
        ({}, None),
        (None, None),
        ("garbage", None),
        ([1, 2, 3], None),
    ],
)
def test_generic_value_extraction(settings, payload, expected):
    provider = GenericHttpProvider(settings, spec())
    assert provider.extract_value(payload) == expected


def test_generic_value_map_and_scaling(settings):
    provider = GenericHttpProvider(
        settings, spec(value_map={"busy": 40.0}, value_scale=60.0, value_offset=0.0, value_path="v")
    )
    assert provider.extract_value({"v": "busy"}) == 2400.0
    assert provider.extract_value({"v": 0.5}) == 30.0


def test_dotted_path_with_indexes():
    assert dig({"a": {"b": [{"c": 42}]}}, "a.b[0].c") == 42
    assert dig({"a": {"b": []}}, "a.b[0].c") is None
    assert dig({"a": 1}, "a.b") is None
    assert dig(None, "a") is None
    assert dig([{"x": 1}], "[0].x") == 1


def test_env_interpolation_is_lazy(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "s3cret")
    assert expand_env("Bearer ${SOME_TOKEN}") == "Bearer s3cret"
    assert expand_env("Bearer ${MISSING_TOKEN}") == "Bearer "


def test_generic_supports_filters(settings):
    require_id = GenericHttpProvider(settings, spec(require_source_id=True, source_id_key="pos"))
    assert require_id.supports(Venue(id="v", name="X")) is False
    assert require_id.supports(Venue(id="v", name="X", source_ids={"pos": "1"})) is True

    only_pizza = GenericHttpProvider(settings, spec(only_categories=["pizzeria"]))
    assert only_pizza.supports(Venue(id="v", name="X", category="cafe")) is False
    assert only_pizza.supports(Venue(id="v", name="X", category="pizzeria")) is True

    delivery_only = GenericHttpProvider(settings, spec(require_delivery=True))
    assert delivery_only.supports(Venue(id="v", name="X", delivery=False)) is False
    assert delivery_only.supports(Venue(id="v", name="X", delivery=True)) is True


def test_missing_generic_config_is_not_an_error(settings):
    from src.providers.generic_http import load_generic_providers

    tuned = dataclasses.replace(
        settings,
        providers=dataclasses.replace(settings.providers, generic_config_path="/nonexistent/x.json"),
    )
    assert load_generic_providers(tuned) == []


def test_besttime_logs_a_not_ok_status_without_crashing(settings, venue):
    """Regression: `extra={"msg": ...}` collides with LogRecord.msg and raised
    KeyError, which turned a routine "venue is closed" reply into a provider
    failure. Found on a live run, not in the fixtures."""
    payload = {
        "status": "Error",
        "message": "No live data available.",
        "analysis": {"venue_live_busyness_available": False, "venue_forecasted_busyness": 0},
    }
    import logging

    # a real handler must actually format the record, or the bug stays hidden
    logging.getLogger("src.providers.besttime").setLevel(logging.INFO)
    assert bt(settings).parse_live(payload, venue) is None
