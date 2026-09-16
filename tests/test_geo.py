"""Distance maths."""

from __future__ import annotations

import pytest

from src.geo import bbox_around, format_distance, haversine_meters

VALVE = (47.6142467, -122.2007170)


def test_distance_to_self_is_zero():
    assert haversine_meters(VALVE[0], VALVE[1], VALVE[0], VALVE[1]) == pytest.approx(0.0, abs=1e-6)


def test_known_short_distance():
    # ~0.009 deg of latitude is almost exactly 1 km
    assert haversine_meters(47.6142, -122.2007, 47.6232, -122.2007) == pytest.approx(1000, rel=0.01)


def test_distance_is_symmetric():
    a = haversine_meters(47.61, -122.20, 47.62, -122.21)
    b = haversine_meters(47.62, -122.21, 47.61, -122.20)
    assert a == pytest.approx(b)


def test_bbox_contains_the_radius():
    south, west, north, east = bbox_around(VALVE[0], VALVE[1], 2000)
    assert south < VALVE[0] < north
    assert west < VALVE[1] < east
    assert haversine_meters(VALVE[0], VALVE[1], north, VALVE[1]) >= 2000


@pytest.mark.parametrize(
    "meters,expected", [(0, "0 м"), (650.4, "650 м"), (999, "999 м"), (1000, "1.0 км"), (2450, "2.5 км"), (2440, "2.4 км")]
)
def test_distance_formatting(meters, expected):
    assert format_distance(meters) == expected
