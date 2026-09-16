"""Google Places API (New) discovery + opening-hours enrichment.

What this API *does* give us: authoritative venue identity, address,
``businessStatus``, structured opening hours, and delivery/takeout flags. That
materially improves both the venue list and the "is it open right now" gate.

What it does *not* give us: Popular Times or live busyness. Google renders those
in Maps and Search only; they are not fields of the Places resource and there is
no supported endpoint for them. This provider therefore never produces a load
signal -- see ``docs/RESEARCH.md``.

Nearby Search (New) caps a response at 20 places and has no pagination, so the
search circle is tiled into overlapping sub-circles. Discovery runs once a day,
which keeps the cost bounded and predictable.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import OfficeConfig
from ..geo import haversine_meters
from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import Venue, make_venue_id
from .base import DiscoveryProvider, ProviderError, ProviderUnavailable

log = get_logger(__name__)

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
MAX_RESULTS_PER_CALL = 20

FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.businessStatus",
        "places.regularOpeningHours",
        "places.delivery",
        "places.takeout",
        "places.dineIn",
    )
)

INCLUDED_TYPES = [
    "restaurant",
    "cafe",
    "coffee_shop",
    "fast_food_restaurant",
    "bakery",
    "bar",
    "pub",
    "meal_takeaway",
    "meal_delivery",
    "pizza_restaurant",
    "hamburger_restaurant",
    "sandwich_shop",
    "ice_cream_shop",
    "dessert_shop",
    "breakfast_restaurant",
    "brunch_restaurant",
    "asian_restaurant",
    "chinese_restaurant",
    "japanese_restaurant",
    "korean_restaurant",
    "thai_restaurant",
    "vietnamese_restaurant",
    "sushi_restaurant",
    "ramen_restaurant",
    "indian_restaurant",
    "mexican_restaurant",
    "italian_restaurant",
    "steak_house",
    "seafood_restaurant",
    "vegetarian_restaurant",
    "juice_shop",
    "donut_shop",
    "bagel_shop",
    "deli",
    "food_court",
]

TYPE_CATEGORY = {
    "pizza_restaurant": "pizzeria",
    "hamburger_restaurant": "burger",
    "asian_restaurant": "asian",
    "chinese_restaurant": "asian",
    "japanese_restaurant": "asian",
    "korean_restaurant": "asian",
    "thai_restaurant": "asian",
    "vietnamese_restaurant": "asian",
    "sushi_restaurant": "asian",
    "ramen_restaurant": "asian",
    "indian_restaurant": "asian",
    "cafe": "cafe",
    "coffee_shop": "coffee",
    "bakery": "bakery",
    "bar": "bar_pub",
    "pub": "bar_pub",
    "fast_food_restaurant": "fast_food",
    "meal_takeaway": "fast_food",
    "meal_delivery": "fast_food",
    "ice_cream_shop": "dessert",
    "dessert_shop": "dessert",
    "donut_shop": "bakery",
    "bagel_shop": "bakery",
    "deli": "deli",
    "food_court": "food_court",
    "restaurant": "restaurant",
}

_DAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]  # Google: 0 = Sunday


class GooglePlacesProvider(DiscoveryProvider):
    name = "google_places"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.api_key = settings.discovery.google_api_key
        self.client = client_from_settings(settings, rate_limit_rps=5.0)
        self.tile_radius_m = 400.0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.discovery.enable_google and self.api_key)

    def discover(self, office: OfficeConfig) -> List[Venue]:
        if not self.api_key:
            raise ProviderUnavailable("GOOGLE_MAPS_API_KEY is not set")

        by_id: Dict[str, Venue] = {}
        tiles = list(self.tile_circle(office.latitude, office.longitude, office.radius_meters))
        log.info("google nearby search", extra={"tiles": len(tiles)})

        failures = 0
        for lat, lon, radius in tiles:
            try:
                payload = self._search_nearby(lat, lon, radius)
            except HttpError as exc:
                failures += 1
                log.warning("google tile failed", extra={"error": str(exc), "status": exc.status})
                if failures > max(3, len(tiles) // 4):
                    raise ProviderError("too many Google Places failures: {}".format(exc)) from exc
                continue
            for place in self._places(payload):
                venue = self._place_to_venue(place, office)
                if venue is not None:
                    by_id[venue.id] = venue
        return list(by_id.values())

    # -- HTTP -------------------------------------------------------------- #

    def _search_nearby(self, lat: float, lon: float, radius: float) -> Any:
        body = {
            "includedTypes": INCLUDED_TYPES,
            "maxResultCount": MAX_RESULTS_PER_CALL,
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": float(radius)}
            },
        }
        return self.client.post_json(
            NEARBY_URL,
            json_body=body,
            headers={
                "X-Goog-Api-Key": self.api_key or "",
                "X-Goog-FieldMask": FIELD_MASK,
                "Content-Type": "application/json",
            },
            cache_ttl=0,
        )

    @staticmethod
    def _places(payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        places = payload.get("places")
        return [p for p in places if isinstance(p, dict)] if isinstance(places, list) else []

    # -- geometry ---------------------------------------------------------- #

    def tile_circle(
        self, lat: float, lon: float, radius_m: float
    ) -> Iterable[Tuple[float, float, float]]:
        """Cover a circle with overlapping sub-circles.

        Nearby Search returns at most 20 places per call, so a dense downtown
        needs tiling to approach full coverage. Rings are spaced at
        ``tile_radius * sqrt(3)`` which gives a hexagon-like packing with modest
        overlap.
        """
        step = self.tile_radius_m * math.sqrt(3.0)
        yield lat, lon, self.tile_radius_m
        ring = 1
        while (ring - 0.5) * step < radius_m:
            points = max(6, int(round(2 * math.pi * ring)))
            for index in range(points):
                angle = 2 * math.pi * index / points
                dx = math.cos(angle) * step * ring
                dy = math.sin(angle) * step * ring
                dlat = math.degrees(dy / 6_371_008.8)
                dlon = math.degrees(dx / (6_371_008.8 * math.cos(math.radians(lat))))
                tile_lat, tile_lon = lat + dlat, lon + dlon
                if haversine_meters(lat, lon, tile_lat, tile_lon) <= radius_m + self.tile_radius_m:
                    yield tile_lat, tile_lon, self.tile_radius_m
            ring += 1

    # -- parsing ----------------------------------------------------------- #

    def _place_to_venue(self, place: Dict[str, Any], office: OfficeConfig) -> Optional[Venue]:
        try:
            name = ((place.get("displayName") or {}).get("text") or "").strip()
            location = place.get("location") or {}
            lat = location.get("latitude")
            lon = location.get("longitude")
            if not name or lat is None or lon is None:
                return None
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError, AttributeError):
            return None

        status = str(place.get("businessStatus") or "OPERATIONAL")
        if status in {"CLOSED_PERMANENTLY"}:
            return None

        distance = haversine_meters(office.latitude, office.longitude, lat, lon)
        if distance > office.radius_meters:
            return None

        types = [t for t in (place.get("types") or []) if isinstance(t, str)]
        primary = place.get("primaryType")
        category = "restaurant"
        for candidate in ([primary] if isinstance(primary, str) else []) + types:
            if candidate in TYPE_CATEGORY:
                category = TYPE_CATEGORY[candidate]
                break

        return Venue(
            id=make_venue_id(name, lat, lon),
            name=name,
            address=str(place.get("formattedAddress") or ""),
            latitude=round(lat, 7),
            longitude=round(lon, 7),
            distance_meters=round(distance, 1),
            category=category,
            website=str(place.get("websiteUri") or ""),
            phone=str(place.get("nationalPhoneNumber") or ""),
            delivery=bool(place.get("delivery")),
            takeaway=bool(place.get("takeout")),
            sources=["google_places"],
            cuisine=";".join(t for t in types if t.endswith("_restaurant"))[:120],
            opening_hours=self.opening_hours_to_osm(place.get("regularOpeningHours")),
            business_status=status,
            source_ids={"google_places": str(place.get("id") or "")},
        )

    @staticmethod
    def opening_hours_to_osm(hours: Any) -> str:
        """Convert Google ``regularOpeningHours.periods`` to an OSM-style string
        so a single parser handles every source."""
        if not isinstance(hours, dict):
            return ""
        periods = hours.get("periods")
        if not isinstance(periods, list):
            return ""
        by_day: Dict[int, List[str]] = {}
        for period in periods:
            if not isinstance(period, dict):
                continue
            open_ = period.get("open") or {}
            close = period.get("close") or {}
            day = open_.get("day")
            if day is None:
                continue
            try:
                day = int(day)
                start = "{:02d}:{:02d}".format(int(open_.get("hour", 0)), int(open_.get("minute", 0)))
                if not close:  # open 24h on that day
                    end = "24:00"
                else:
                    end = "{:02d}:{:02d}".format(
                        int(close.get("hour", 0)), int(close.get("minute", 0))
                    )
            except (TypeError, ValueError):
                continue
            by_day.setdefault(day % 7, []).append("{}-{}".format(start, end))
        if not by_day:
            return ""
        return "; ".join(
            "{} {}".format(_DAYS[day], ",".join(sorted(ranges)))
            for day, ranges in sorted(by_day.items())
        )


def attribution() -> str:
    return "Powered by Google"
