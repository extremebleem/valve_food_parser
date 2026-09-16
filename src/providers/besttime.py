"""BestTime.app live foot-traffic provider -- the primary load signal.

Why this one: it is the only source found during research that (a) exposes a
*live* busyness reading for arbitrary third-party venues, (b) does so through a
documented, paid, terms-of-service-compliant API rather than scraping, and
(c) covers ordinary restaurants and cafes rather than only partner merchants.

What it actually measures: an anonymised mobile-signal derived index, expressed
as a percentage of that venue's own weekly peak. It is a **proxy for physical
occupancy**, not a headcount, and the venue record says so
(:class:`CongestionDomain.PHYSICAL_OCCUPANCY`).

Credit model (from the vendor's published pricing):
  * ``POST /forecasts``      2 credits -- once per venue, ever (resolves venue_id)
  * ``POST /forecasts/live`` 1 credit  -- once per venue per monitoring run

``BESTTIME_MAX_NEW_FORECASTS_PER_RUN`` bounds the onboarding cost so a fresh
deployment ramps up over several runs instead of spending hundreds of credits in
one go.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from ..http import HttpError, RateLimitError, client_from_settings
from ..logging_utils import get_logger
from ..models import (
    CongestionDomain,
    LoadSignal,
    MetricType,
    SignalQuality,
    Venue,
    utcnow,
)
from .base import LoadProvider, ProviderError, ProviderUnavailable

log = get_logger(__name__)


class BestTimeProvider(LoadProvider):
    name = "besttime"
    priority = 10

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        cfg = settings.providers
        self.api_base = cfg.besttime_api_base.rstrip("/")
        self.private_key = cfg.besttime_private_key
        self.allow_forecast = cfg.besttime_allow_forecast_as_load
        self.client = client_from_settings(settings, rate_limit_rps=cfg.besttime_rate_limit_rps)
        self.client.cache_ttl = 0  # live data must never be served from cache
        self.max_new_forecasts = int(os.environ.get("BESTTIME_MAX_NEW_FORECASTS_PER_RUN", "10"))
        self._new_forecasts_used = 0
        self._resolved: Dict[str, str] = {}
        self.credits_spent = 0

    @property
    def enabled(self) -> bool:
        return bool(self.private_key)

    def supports(self, venue: Venue) -> bool:
        # needs either a known provider id, or enough identity to look one up
        return bool(venue.source_ids.get("besttime") or (venue.name and venue.address))

    # -- public API -------------------------------------------------------- #

    def get_current_load(self, venue: Venue) -> Optional[LoadSignal]:
        if not self.private_key:
            raise ProviderUnavailable("BESTTIME_API_KEY_PRIVATE is not set")

        params: Dict[str, Any] = {"api_key_private": self.private_key}
        provider_id = venue.source_ids.get("besttime") or self._resolved.get(venue.id)
        if provider_id:
            params["venue_id"] = provider_id
        elif venue.name and venue.address:
            params["venue_name"] = venue.name
            params["venue_address"] = venue.address
        else:
            return None

        try:
            payload = self.client.post_json(
                "{}/forecasts/live".format(self.api_base), params=params, cache_ttl=0
            )
            self.credits_spent += 1
        except RateLimitError as exc:
            raise ProviderError("BestTime rate limit: {}".format(exc)) from exc
        except HttpError as exc:
            if exc.status in (404, 422):
                # venue unknown to the provider: not an error, just no data
                log.debug("besttime has no venue", extra={"venue": venue.name})
                return None
            raise ProviderError("BestTime live call failed: {}".format(exc)) from exc

        return self.parse_live(payload, venue)

    def prefetch(self, venues: Sequence[Venue]) -> None:
        """Resolve provider-side ids for venues we have never seen, bounded by
        ``BESTTIME_MAX_NEW_FORECASTS_PER_RUN``."""
        if not self.private_key:
            return
        for venue in venues:
            if self._new_forecasts_used >= self.max_new_forecasts:
                return
            if venue.source_ids.get("besttime") or self._resolved.get(venue.id):
                continue
            if not (venue.name and venue.address):
                continue
            provider_id = self._create_forecast(venue)
            if provider_id:
                self._resolved[venue.id] = provider_id

    def resolved_ids(self) -> Dict[str, str]:
        """venue_id -> BestTime venue id discovered during this run."""
        return dict(self._resolved)

    def stats(self) -> Dict[str, Any]:
        return {
            "credits_spent": self.credits_spent,
            "new_forecasts": self._new_forecasts_used,
            "requests": self.client.request_count,
        }

    # -- internals --------------------------------------------------------- #

    def _create_forecast(self, venue: Venue) -> Optional[str]:
        params = {
            "api_key_private": self.private_key,
            "venue_name": venue.name,
            "venue_address": venue.address,
        }
        try:
            payload = self.client.post_json(
                "{}/forecasts".format(self.api_base), params=params, cache_ttl=0
            )
        except HttpError as exc:
            log.info(
                "besttime forecast unavailable",
                extra={"venue": venue.name, "status": exc.status, "error": str(exc)[:160]},
            )
            self._new_forecasts_used += 1
            self.credits_spent += 1
            return None

        self._new_forecasts_used += 1
        self.credits_spent += 2
        info = payload.get("venue_info") if isinstance(payload, dict) else None
        if isinstance(info, dict) and info.get("venue_id"):
            log.info(
                "besttime venue resolved",
                extra={"venue": venue.name, "besttime_id": info["venue_id"]},
            )
            return str(info["venue_id"])
        return None

    def parse_live(self, payload: Any, venue: Venue) -> Optional[LoadSignal]:
        """Turn a ``/forecasts/live`` response into a :class:`LoadSignal`.

        Tolerates the fields being at the top level or nested under
        ``analysis``/``venue_info``, and never raises on a malformed payload.
        """
        if not isinstance(payload, dict):
            log.warning("besttime returned non-object payload", extra={"venue": venue.name})
            return None

        status = str(payload.get("status") or "OK").upper()
        if status not in {"OK", "SUCCESS", ""}:
            log.info(
                "besttime status not OK",
                extra={"venue": venue.name, "status": status, "msg": str(payload.get("message"))[:160]},
            )
            return None

        analysis = payload.get("analysis")
        scopes: List[Dict[str, Any]] = [payload]
        if isinstance(analysis, dict):
            scopes.insert(0, analysis)

        live = _first_number(scopes, "venue_live_busyness")
        forecast = _first_number(scopes, "venue_forecasted_busyness")
        delta = _first_number(scopes, "venue_live_forecasted_delta")
        live_available = _first_bool(scopes, "venue_live_busyness_available")
        forecast_available = _first_bool(scopes, "venue_forecast_busyness_available")

        raw = {
            "venue_live_busyness": live,
            "venue_forecasted_busyness": forecast,
            "venue_live_forecasted_delta": delta,
            "venue_live_busyness_available": live_available,
            "venue_forecast_busyness_available": forecast_available,
        }

        if live is not None and live_available is not False:
            return LoadSignal(
                venue_id=venue.id,
                source=self.name,
                metric_type=MetricType.LIVE_BUSYNESS_INDEX,
                metric_value=float(live),
                raw_value=raw,
                domain=CongestionDomain.PHYSICAL_OCCUPANCY,
                confidence=0.85,
                signal_quality=SignalQuality.HIGH,
                observed_at=utcnow(),
                note="live foot-traffic index (% of venue weekly peak)",
            )

        if self.allow_forecast and forecast is not None and forecast_available is not False:
            # A forecast is not a measurement. It is only ever used when the
            # operator explicitly opts in, and it is flagged low quality so the
            # confidence gate in AnomalyConfig keeps it out of alerts by default.
            return LoadSignal(
                venue_id=venue.id,
                source=self.name,
                metric_type=MetricType.FORECAST_BUSYNESS_INDEX,
                metric_value=float(forecast),
                raw_value=raw,
                domain=CongestionDomain.PHYSICAL_OCCUPANCY,
                confidence=0.3,
                signal_quality=SignalQuality.LOW,
                observed_at=utcnow(),
                note="forecast only -- live signal unavailable for this venue",
            )

        return None


def _first_number(scopes: Sequence[Dict[str, Any]], key: str) -> Optional[float]:
    for scope in scopes:
        if not isinstance(scope, dict) or key not in scope:
            continue
        value = scope[key]
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number:  # NaN
            continue
        return number
    return None


def _first_bool(scopes: Sequence[Dict[str, Any]], key: str) -> Optional[bool]:
    for scope in scopes:
        if isinstance(scope, dict) and key in scope and scope[key] is not None:
            return bool(scope[key])
    return None
