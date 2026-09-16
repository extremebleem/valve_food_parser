"""Provider wiring.

Keeps the "which providers exist" decision in one place so ``monitor.py`` and
``discovery.py`` only deal with the abstractions.
"""

from __future__ import annotations

from typing import List

from ..config import Settings
from ..logging_utils import get_logger
from .base import DiscoveryProvider, LoadProvider
from .besttime import BestTimeProvider
from .foursquare import FoursquareProvider
from .generic_http import load_generic_providers
from .google_places import GooglePlacesProvider
from .osm import OverpassProvider

log = get_logger(__name__)


def build_discovery_providers(settings: Settings) -> List[DiscoveryProvider]:
    candidates: List[DiscoveryProvider] = [
        OverpassProvider(settings),
        GooglePlacesProvider(settings),
        FoursquareProvider(settings),
    ]
    enabled = [p for p in candidates if p.enabled]
    log.info(
        "discovery providers",
        extra={
            "enabled": [p.name for p in enabled],
            "disabled": [p.name for p in candidates if not p.enabled],
        },
    )
    return enabled


def build_load_providers(settings: Settings) -> List[LoadProvider]:
    """Load providers in the order given by ``LOAD_PROVIDERS``.

    Unknown names are ignored with a warning; unconfigured providers (missing
    API key) are filtered out so the run degrades instead of failing.
    """
    available = {"besttime": BestTimeProvider(settings)}
    for provider in load_generic_providers(settings):
        available[provider.name] = provider

    ordered: List[LoadProvider] = []
    for name in settings.providers.load_provider_order:
        if name in available:
            ordered.append(available[name])
        elif name == "generic_http":
            # shorthand: every provider that came from the JSON config file
            ordered.extend(p for p in available.values() if p.name != "besttime")
        else:
            log.warning("unknown load provider requested", extra={"provider": name})

    seen = set()
    unique: List[LoadProvider] = []
    for provider in ordered:
        if provider.name in seen:
            continue
        seen.add(provider.name)
        unique.append(provider)

    enabled = [p for p in unique if p.enabled]
    log.info(
        "load providers",
        extra={
            "enabled": [p.name for p in enabled],
            "disabled": [p.name for p in unique if not p.enabled],
        },
    )
    return enabled
