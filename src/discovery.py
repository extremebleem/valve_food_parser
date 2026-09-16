"""Venue discovery: fan out to every enabled source, then merge.

The venue list is rebuilt from the sources on every discovery run (daily) and
upserted by a stable surrogate id, so it is never hand-maintained. Venues that
stop being reported for ``VENUE_STALE_DAYS`` are deactivated rather than
deleted, which keeps their observation history intact for baselines.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Settings
from .geo import haversine_meters
from .logging_utils import get_logger
from .models import Venue, normalize_name, utcnow
from .providers.base import DiscoveryProvider, ProviderError
from .providers.registry import build_discovery_providers
from .storage import BaseStorage

log = get_logger(__name__)

# Source preference when two records disagree about a field.
SOURCE_RANK = {"google_places": 3, "foursquare": 2, "osm": 1}

# Identical names closer than this are the same venue mapped twice; further
# apart they are two branches of a chain and must stay separate.
SAME_NAME_MERGE_METERS = 150.0

CATEGORY_RANK = {
    "pizzeria": 9,
    "burger": 9,
    "asian": 9,
    "food_court": 8,
    "bar_pub": 7,
    "bakery": 7,
    "coffee": 7,
    "deli": 7,
    "dessert": 6,
    "fast_food": 5,
    "cafe": 4,
    "restaurant": 3,
    "food_shop": 1,
}


def name_similarity(left: str, right: str) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # containment catches "Din Tai Fung" vs "Din Tai Fung Bellevue"
    if a in b or b in a:
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


class VenueMerger:
    """Cross-source de-duplication."""

    def __init__(self, distance_meters: float = 75.0, name_ratio: float = 0.82) -> None:
        self.distance_meters = distance_meters
        self.name_ratio = name_ratio

    def are_duplicates(self, left: Venue, right: Venue) -> bool:
        shared_source_id = any(
            key in right.source_ids and right.source_ids[key] == value
            for key, value in left.source_ids.items()
            if value
        )
        if shared_source_id:
            return True

        distance = haversine_meters(left.latitude, left.longitude, right.latitude, right.longitude)
        similarity = name_similarity(left.name, right.name)

        if similarity >= 0.999:
            return distance <= SAME_NAME_MERGE_METERS
        if distance <= self.distance_meters and similarity >= self.name_ratio:
            return True
        # same building, one source spelled the name differently
        if distance <= 25.0 and similarity >= 0.6:
            return True
        return False

    def merge(self, venues: Iterable[Venue]) -> List[Venue]:
        items = [v for v in venues if v is not None]
        if not items:
            return []

        clusters = self._cluster(items)
        return [self.combine(group) for group in clusters]

    def _cluster(self, items: Sequence[Venue]) -> List[List[Venue]]:
        """Union-find over a coarse spatial grid, so only nearby pairs are compared."""
        parent = list(range(len(items)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        # ~0.003 deg ~= 330 m cells; compare each cell against its 8 neighbours
        cell = 0.003
        grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for index, venue in enumerate(items):
            grid[(int(venue.latitude / cell), int(venue.longitude / cell))].append(index)

        for (gx, gy), indexes in grid.items():
            neighbours: List[int] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbours.extend(grid.get((gx + dx, gy + dy), ()))
            for i in indexes:
                for j in neighbours:
                    if j <= i:
                        continue
                    if self.are_duplicates(items[i], items[j]):
                        union(i, j)

        groups: Dict[int, List[Venue]] = defaultdict(list)
        for index, venue in enumerate(items):
            groups[find(index)].append(venue)
        return list(groups.values())

    @staticmethod
    def combine(group: Sequence[Venue]) -> Venue:
        """Fold a duplicate cluster into one record, field by field."""
        ranked = sorted(
            group,
            key=lambda v: (
                -max((SOURCE_RANK.get(s, 0) for s in v.sources), default=0),
                -len(v.name),
            ),
        )
        best = ranked[0]
        merged = Venue(
            id=min(v.id for v in group),  # deterministic, independent of input order
            name=best.name,
            address=best.address,
            latitude=best.latitude,
            longitude=best.longitude,
            distance_meters=min(v.distance_meters for v in group),
            category=best.category,
            website=best.website,
            phone=best.phone,
            delivery=any(v.delivery for v in group),
            takeaway=any(v.takeaway for v in group),
            sources=sorted({s for v in group for s in v.sources}),
            cuisine=best.cuisine,
            opening_hours=best.opening_hours,
            business_status=best.business_status,
            source_ids={},
            active=True,
        )
        for venue in ranked:
            for field_name in ("address", "website", "phone", "cuisine", "opening_hours"):
                if not getattr(merged, field_name) and getattr(venue, field_name):
                    setattr(merged, field_name, getattr(venue, field_name))
            merged.source_ids.update({k: v for k, v in venue.source_ids.items() if v})
        # prefer the most specific category any source assigned
        merged.category = max(
            (v.category for v in group if v.category),
            key=lambda c: CATEGORY_RANK.get(c, 0),
            default=merged.category,
        )
        if any(v.business_status == "CLOSED_TEMPORARILY" for v in group):
            merged.business_status = "CLOSED_TEMPORARILY"
        return merged


class DiscoveryRunner:
    def __init__(
        self,
        settings: Settings,
        storage: BaseStorage,
        providers: Optional[Sequence[DiscoveryProvider]] = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.providers = list(providers) if providers is not None else build_discovery_providers(settings)
        self.merger = VenueMerger(
            settings.discovery.dedupe_distance_meters, settings.discovery.dedupe_name_ratio
        )

    def run(self, *, store: bool = True) -> Dict[str, Any]:
        started = utcnow()
        raw: List[Venue] = []
        per_source: Dict[str, int] = {}
        failures: Dict[str, str] = {}

        for provider in self.providers:
            try:
                found = provider.discover(self.settings.office)
            except ProviderError as exc:
                failures[provider.name] = str(exc)[:300]
                log.error("discovery provider failed", extra={"provider": provider.name, "error": str(exc)})
                continue
            except Exception as exc:  # defensive: a provider bug must not kill discovery
                failures[provider.name] = "unexpected: {}".format(exc)[:300]
                log.exception("discovery provider crashed", extra={"provider": provider.name})
                continue
            per_source[provider.name] = len(found)
            raw.extend(found)
            log.info("discovery source done", extra={"provider": provider.name, "venues": len(found)})

        if not raw and failures:
            raise ProviderError("every discovery source failed: {}".format(failures))

        merged = self.merger.merge(raw)
        merged = [v for v in merged if v.business_status != "CLOSED_PERMANENTLY"]
        for venue in merged:
            venue.distance_meters = round(
                haversine_meters(
                    self.settings.office.latitude,
                    self.settings.office.longitude,
                    venue.latitude,
                    venue.longitude,
                ),
                1,
            )
        merged = [v for v in merged if v.distance_meters <= self.settings.office.radius_meters]
        merged.sort(key=lambda v: v.distance_meters)

        if store:
            write_stats = self.storage.upsert_venues(merged)
            deactivated = self.storage.deactivate_stale_venues(self.settings.discovery.stale_days)
        else:
            write_stats = {"inserted": 0, "updated": 0}
            deactivated = 0

        stats = {
            "raw_candidates": len(raw),
            "per_source": per_source,
            "merged": len(merged),
            "duplicates_removed": len(raw) - len(merged),
            "inserted": write_stats["inserted"],
            "updated": write_stats["updated"],
            "deactivated_stale": deactivated,
            "failures": failures,
            "categories": self._category_counts(merged),
            "radius_meters": self.settings.office.radius_meters,
        }
        if store:
            self.storage.record_run("discovery", started, stats)
        log.info("discovery complete", extra=stats)
        return stats

    @staticmethod
    def _category_counts(venues: Sequence[Venue]) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for venue in venues:
            counts[venue.category or "unknown"] += 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
