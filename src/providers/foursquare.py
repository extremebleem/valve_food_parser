"""Foursquare Places API discovery (optional, key-gated).

Useful as a third opinion: Foursquare's POI graph often carries venues that are
missing or stale in OSM, plus a normalised category taxonomy. It provides no
busyness signal for arbitrary venues, so it is discovery-only.

The parser accepts both the current ``places-api.foursquare.com`` response shape
(``results[].latitude``) and the legacy v3 shape (``results[].geocodes.main``),
because the v3 endpoints are only retired in mid-2026.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import OfficeConfig
from ..geo import haversine_meters
from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import Venue, make_venue_id
from .base import DiscoveryProvider, ProviderError, ProviderUnavailable

log = get_logger(__name__)

SEARCH_URL = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"
PAGE_LIMIT = 50
MAX_PAGES = 10

# Foursquare category ids under "Dining and Drinking" (13000) plus bakeries
CATEGORY_IDS = "13000,13002,13003,13034,13035,13040,13065,13145,13199,13236,13263,13297,13338,17142"

CATEGORY_KEYWORDS = (
    ("pizza", "pizzeria"),
    ("burger", "burger"),
    ("bakery", "bakery"),
    ("bagel", "bakery"),
    ("donut", "bakery"),
    ("coffee", "coffee"),
    ("tea", "coffee"),
    ("cafe", "cafe"),
    ("café", "cafe"),
    ("bar", "bar_pub"),
    ("pub", "bar_pub"),
    ("brewery", "bar_pub"),
    ("fast food", "fast_food"),
    ("food court", "food_court"),
    ("ice cream", "dessert"),
    ("dessert", "dessert"),
    ("deli", "deli"),
    ("sushi", "asian"),
    ("ramen", "asian"),
    ("chinese", "asian"),
    ("japanese", "asian"),
    ("korean", "asian"),
    ("thai", "asian"),
    ("vietnamese", "asian"),
    ("asian", "asian"),
    ("indian", "asian"),
    ("noodle", "asian"),
)


class FoursquareProvider(DiscoveryProvider):
    name = "foursquare"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.api_key = settings.discovery.foursquare_api_key
        self.client = client_from_settings(settings, rate_limit_rps=5.0)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.discovery.enable_foursquare and self.api_key)

    def discover(self, office: OfficeConfig) -> List[Venue]:
        if not self.api_key:
            raise ProviderUnavailable("FOURSQUARE_API_KEY is not set")

        venues: Dict[str, Venue] = {}
        cursor: Optional[str] = None
        for page in range(MAX_PAGES):
            params: Dict[str, Any] = {
                "ll": "{:.6f},{:.6f}".format(office.latitude, office.longitude),
                "radius": int(min(office.radius_meters, 100_000)),
                "fsq_category_ids": CATEGORY_IDS,
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                payload = self.client.get_json(
                    SEARCH_URL,
                    params=params,
                    headers={
                        "Authorization": "Bearer {}".format(self.api_key),
                        "X-Places-Api-Version": API_VERSION,
                    },
                    cache_ttl=0,
                )
            except HttpError as exc:
                if page == 0:
                    raise ProviderError("Foursquare search failed: {}".format(exc)) from exc
                log.warning("foursquare page failed", extra={"page": page, "error": str(exc)})
                break

            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or not results:
                break
            for item in results:
                venue = self._to_venue(item, office)
                if venue is not None:
                    venues[venue.id] = venue

            cursor = self._next_cursor(payload)
            if not cursor:
                break
        return list(venues.values())

    @staticmethod
    def _next_cursor(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        context = payload.get("context")
        if isinstance(context, dict):
            next_cursor = context.get("next_cursor") or context.get("cursor")
            if isinstance(next_cursor, str) and next_cursor:
                return next_cursor
        link = payload.get("next")
        if isinstance(link, str) and "cursor=" in link:
            return link.split("cursor=", 1)[1].split("&", 1)[0]
        return None

    def _to_venue(self, item: Any, office: OfficeConfig) -> Optional[Venue]:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        if not name:
            return None

        lat = item.get("latitude")
        lon = item.get("longitude")
        if lat is None or lon is None:  # legacy v3 shape
            main = ((item.get("geocodes") or {}).get("main") or {})
            lat, lon = main.get("latitude"), main.get("longitude")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return None

        distance = haversine_meters(office.latitude, office.longitude, lat, lon)
        if distance > office.radius_meters:
            return None

        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        categories = item.get("categories") if isinstance(item.get("categories"), list) else []
        category_names = [
            str(c.get("name", "")) for c in categories if isinstance(c, dict) and c.get("name")
        ]

        fsq_id = str(item.get("fsq_place_id") or item.get("fsq_id") or "")
        return Venue(
            id=make_venue_id(name, lat, lon),
            name=name,
            address=str(location.get("formatted_address") or location.get("address") or ""),
            latitude=round(lat, 7),
            longitude=round(lon, 7),
            distance_meters=round(distance, 1),
            category=self._category(category_names),
            website=str(item.get("website") or ""),
            phone=str(item.get("tel") or ""),
            delivery=False,
            takeaway=False,
            sources=["foursquare"],
            cuisine=";".join(category_names)[:120],
            opening_hours="",
            business_status="OPERATIONAL",
            source_ids={"foursquare": fsq_id} if fsq_id else {},
        )

    @staticmethod
    def _category(category_names: List[str]) -> str:
        blob = " ".join(category_names).lower()
        for keyword, category in CATEGORY_KEYWORDS:
            if keyword in blob:
                return category
        return "restaurant"
