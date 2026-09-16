"""OpenStreetMap / Overpass API discovery.

Primary discovery source: free, key-less, worldwide, and its licence (ODbL)
explicitly permits programmatic use with attribution. Coverage in downtown
Bellevue is good because the area is heavily mapped, and OSM carries exactly the
attributes the venue record needs: ``cuisine``, ``delivery``, ``takeaway``,
``opening_hours``, ``website``, ``phone``.

Overpass is a shared community resource: the query is issued once per day (not
per monitoring run), a descriptive User-Agent is sent, and mirrors are tried in
order when one is busy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import OfficeConfig
from ..geo import haversine_meters
from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import Venue, make_venue_id
from .base import DiscoveryProvider, ProviderError

log = get_logger(__name__)

# amenity / shop values that serve prepared food or drink to take away or eat in
AMENITY_VALUES = (
    "restaurant|fast_food|cafe|bar|pub|biergarten|food_court|ice_cream|internet_cafe"
)
SHOP_VALUES = "bakery|pastry|confectionery|deli|coffee|tea|chocolate|food|frozen_food"

AMENITY_CATEGORY = {
    "restaurant": "restaurant",
    "fast_food": "fast_food",
    "cafe": "cafe",
    "bar": "bar_pub",
    "pub": "bar_pub",
    "biergarten": "bar_pub",
    "food_court": "food_court",
    "ice_cream": "dessert",
    "internet_cafe": "cafe",
}
SHOP_CATEGORY = {
    "bakery": "bakery",
    "pastry": "bakery",
    "confectionery": "dessert",
    "chocolate": "dessert",
    "deli": "deli",
    "coffee": "coffee",
    "tea": "coffee",
    "food": "food_shop",
    "frozen_food": "food_shop",
}

ASIAN_CUISINES = {
    "asian", "chinese", "japanese", "sushi", "ramen", "thai", "vietnamese", "korean",
    "indian", "dumpling", "noodle", "pho", "poke", "bubble_tea", "taiwanese", "malaysian",
    "indonesian", "filipino", "nepalese", "szechuan", "cantonese", "dim_sum",
}

# shop=food / frozen_food are grocery-ish; keep them only when they also carry a
# prepared-food signal, otherwise they pollute the list with corner stores
WEAK_SHOP_CATEGORIES = {"food_shop"}


class OverpassProvider(DiscoveryProvider):
    name = "osm"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.urls: List[str] = list(settings.discovery.overpass_urls) or [
            "https://overpass-api.de/api/interpreter"
        ]
        # Overpass answers slowly under load; give it a much longer read timeout
        # than the default provider budget.
        self.client = client_from_settings(settings)
        self.client.timeout = max(settings.http.timeout_seconds, 180.0)
        self.client.cache_ttl = 0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.discovery.enable_osm)

    def build_query(self, office: OfficeConfig) -> str:
        around = "around:{},{:.7f},{:.7f}".format(
            int(office.radius_meters), office.latitude, office.longitude
        )
        return (
            "[out:json][timeout:180];\n"
            "(\n"
            '  nwr["amenity"~"^({amenity})$"]({around});\n'
            '  nwr["shop"~"^({shop})$"]({around});\n'
            '  nwr["cuisine"]["amenity"]({around});\n'
            ");\n"
            "out tags center;"
        ).format(amenity=AMENITY_VALUES, shop=SHOP_VALUES, around=around)

    def discover(self, office: OfficeConfig) -> List[Venue]:
        query = self.build_query(office)
        last_error: Optional[Exception] = None

        for url in self.urls:
            try:
                log.info("overpass query", extra={"endpoint": url, "radius_m": office.radius_meters})
                payload = self.client.post_json(
                    url,
                    data={"data": query},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    cache_ttl=0,
                )
                venues = self.parse(payload, office)
                log.info("overpass ok", extra={"endpoint": url, "venues": len(venues)})
                return venues
            except HttpError as exc:
                last_error = exc
                log.warning("overpass mirror failed", extra={"endpoint": url, "error": str(exc)})
            except (TypeError, ValueError, KeyError) as exc:
                last_error = exc
                log.warning("overpass returned unusable payload", extra={"endpoint": url, "error": str(exc)})

        raise ProviderError("all Overpass mirrors failed: {}".format(last_error))

    # -- parsing ----------------------------------------------------------- #

    def parse(self, payload: Any, office: OfficeConfig) -> List[Venue]:
        if not isinstance(payload, dict):
            raise ProviderError("Overpass payload is not an object")
        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise ProviderError("Overpass payload has no 'elements' list")

        venues: List[Venue] = []
        for element in elements:
            try:
                venue = self._element_to_venue(element, office)
            except Exception as exc:  # one broken element must not kill discovery
                log.debug("skipping malformed OSM element", extra={"error": str(exc)})
                continue
            if venue is not None:
                venues.append(venue)
        return venues

    def _element_to_venue(self, element: Any, office: OfficeConfig) -> Optional[Venue]:
        if not isinstance(element, dict):
            return None
        tags = element.get("tags")
        if not isinstance(tags, dict):
            return None

        name = (tags.get("name") or tags.get("brand") or "").strip()
        if not name:
            return None  # a venue we cannot name cannot be matched by any load provider
        if any(key.startswith(("disused:", "was:", "removed:")) for key in tags):
            return None
        if tags.get("amenity") == "vending_machine":
            return None

        lat, lon = self._coordinates(element)
        if lat is None or lon is None:
            return None

        distance = haversine_meters(office.latitude, office.longitude, lat, lon)
        if distance > office.radius_meters * 1.05:  # Overpass can round the radius
            return None

        category = self._category(tags)
        if category is None:
            return None
        if category in WEAK_SHOP_CATEGORIES and not (
            tags.get("cuisine") or tags.get("takeaway") or tags.get("delivery")
        ):
            return None

        osm_id = "{}/{}".format(element.get("type", "node"), element.get("id", ""))
        venue = Venue(
            id=make_venue_id(name, lat, lon),
            name=name,
            address=self._address(tags),
            latitude=round(lat, 7),
            longitude=round(lon, 7),
            distance_meters=round(distance, 1),
            category=category,
            website=(tags.get("website") or tags.get("contact:website") or "").strip(),
            phone=(tags.get("phone") or tags.get("contact:phone") or "").strip(),
            delivery=tags.get("delivery") in ("yes", "only"),
            takeaway=tags.get("takeaway") in ("yes", "only"),
            sources=["osm"],
            cuisine=(tags.get("cuisine") or "").strip(),
            opening_hours=(tags.get("opening_hours") or "").strip(),
            business_status="OPERATIONAL",
            source_ids={"osm": osm_id},
        )
        return venue

    @staticmethod
    def _coordinates(element: Dict[str, Any]):
        if "lat" in element and "lon" in element:
            return float(element["lat"]), float(element["lon"])
        center = element.get("center")
        if isinstance(center, dict) and "lat" in center and "lon" in center:
            return float(center["lat"]), float(center["lon"])
        return None, None

    @staticmethod
    def _address(tags: Dict[str, Any]) -> str:
        parts = []
        number = tags.get("addr:housenumber")
        street = tags.get("addr:street")
        if number and street:
            parts.append("{} {}".format(number, street))
        elif street:
            parts.append(street)
        for key in ("addr:unit", "addr:city", "addr:state", "addr:postcode"):
            value = tags.get(key)
            if value:
                parts.append(str(value))
        if not parts and tags.get("addr:full"):
            return str(tags["addr:full"])
        return ", ".join(parts)

    @classmethod
    def _category(cls, tags: Dict[str, Any]) -> Optional[str]:
        amenity = tags.get("amenity")
        shop = tags.get("shop")
        base = AMENITY_CATEGORY.get(amenity) if amenity else None
        if base is None and shop:
            base = SHOP_CATEGORY.get(shop)
        if base is None:
            return None

        cuisines = {c.strip().lower() for c in (tags.get("cuisine") or "").split(";") if c.strip()}
        # refine into the buckets the brief asks for
        if "pizza" in cuisines:
            return "pizzeria"
        if cuisines & {"burger", "american;burger"}:
            return "burger"
        if cuisines & ASIAN_CUISINES:
            return "asian"
        if "coffee_shop" in cuisines and base == "cafe":
            return "coffee"
        return base


def attribution() -> str:
    return "© OpenStreetMap contributors (ODbL)"
