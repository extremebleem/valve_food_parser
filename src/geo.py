"""Geodesic helpers (no external dependency needed at this scale)."""

from __future__ import annotations

import math
from typing import Tuple

EARTH_RADIUS_M = 6_371_008.8


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Accurate to ~0.3% which is far below
    the precision of the venue coordinates themselves."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bbox_around(lat: float, lon: float, radius_m: float) -> Tuple[float, float, float, float]:
    """(south, west, north, east) bounding box that contains the radius."""
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * max(math.cos(math.radians(lat)), 1e-6)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def format_distance(meters: float) -> str:
    if meters < 1000:
        return "{:.0f} м".format(meters)
    return "{:.1f} км".format(meters / 1000.0)
