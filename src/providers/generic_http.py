"""Configurable JSON HTTP load provider.

Public consumer APIs for delivery/pickup ETAs do not exist -- DoorDash Drive,
Uber Eats and Olo/Toast all gate those behind a merchant or partner agreement
(see ``docs/RESEARCH.md``). Operators who *do* hold such credentials, or who run
their own POS/queue endpoint, can plug them in here declaratively instead of
writing a new provider class.

Everything is driven by ``config/providers.json`` (path from
``GENERIC_PROVIDER_CONFIG``). Secrets are referenced as ``${ENV_VAR}`` and
resolved at request time, so no credential is ever written to the config file.

Example::

    {
      "providers": [
        {
          "name": "pos_pickup_eta",
          "metric_type": "pickup_eta_minutes",
          "method": "GET",
          "url_template": "https://pos.example.com/stores/{source_id}/quote",
          "source_id_key": "pos",
          "require_source_id": true,
          "headers": {"Authorization": "Bearer ${POS_TOKEN}"},
          "value_path": "quote.pickup_eta_minutes"
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import (
    CongestionDomain,
    LoadSignal,
    MetricType,
    SignalQuality,
    Venue,
    utcnow,
)
from .base import LoadProvider, ProviderError

log = get_logger(__name__)

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")
_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")


class ProviderConfigError(ValueError):
    """The declarative provider definition is invalid."""


@dataclass
class GenericProviderSpec:
    name: str
    metric_type: MetricType
    url_template: str
    method: str = "GET"
    domain: CongestionDomain = CongestionDomain.UNKNOWN
    signal_quality: SignalQuality = SignalQuality.MEDIUM
    confidence: float = 0.6
    headers: Dict[str, str] = field(default_factory=dict)
    query: Dict[str, str] = field(default_factory=dict)
    json_body: Optional[Dict[str, Any]] = None
    value_path: str = "value"
    value_scale: float = 1.0
    value_offset: float = 0.0
    value_map: Dict[str, float] = field(default_factory=dict)
    source_id_key: str = ""
    require_source_id: bool = False
    only_categories: List[str] = field(default_factory=list)
    require_delivery: bool = False
    rate_limit_rps: float = 2.0
    cache_ttl_seconds: int = 0
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenericProviderSpec":
        if not isinstance(data, dict):
            raise ProviderConfigError("provider entry must be an object")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ProviderConfigError("provider entry is missing 'name'")
        raw_metric = str(data.get("metric_type") or "").strip()
        try:
            metric = MetricType(raw_metric)
        except ValueError as exc:
            raise ProviderConfigError(
                "provider {!r}: unknown metric_type {!r}; valid values: {}".format(
                    name, raw_metric, ", ".join(m.value for m in MetricType)
                )
            ) from exc
        url = str(data.get("url_template") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ProviderConfigError("provider {!r}: url_template must be absolute".format(name))

        domain_raw = str(data.get("domain") or "").strip()
        quality_raw = str(data.get("signal_quality") or "medium").strip().lower()
        try:
            domain = CongestionDomain(domain_raw) if domain_raw else CongestionDomain.UNKNOWN
        except ValueError as exc:
            raise ProviderConfigError("provider {!r}: unknown domain {!r}".format(name, domain_raw)) from exc
        try:
            quality = SignalQuality(quality_raw)
        except ValueError as exc:
            raise ProviderConfigError(
                "provider {!r}: signal_quality must be low/medium/high".format(name)
            ) from exc

        return cls(
            name=name,
            metric_type=metric,
            url_template=url,
            method=str(data.get("method") or "GET").upper(),
            domain=domain,
            signal_quality=quality,
            confidence=float(data.get("confidence", 0.6)),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            query={str(k): str(v) for k, v in (data.get("query") or {}).items()},
            json_body=data.get("json_body") if isinstance(data.get("json_body"), dict) else None,
            value_path=str(data.get("value_path") or "value"),
            value_scale=float(data.get("value_scale", 1.0)),
            value_offset=float(data.get("value_offset", 0.0)),
            value_map={str(k): float(v) for k, v in (data.get("value_map") or {}).items()},
            source_id_key=str(data.get("source_id_key") or ""),
            require_source_id=bool(data.get("require_source_id", False)),
            only_categories=[str(c) for c in (data.get("only_categories") or [])],
            require_delivery=bool(data.get("require_delivery", False)),
            rate_limit_rps=float(data.get("rate_limit_rps", 2.0)),
            cache_ttl_seconds=int(data.get("cache_ttl_seconds", 0)),
            enabled=bool(data.get("enabled", True)),
        )


def expand_env(text: str) -> str:
    """Replace ``${VAR}`` with the environment value (empty when unset)."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), text)


def dig(payload: Any, path: str) -> Any:
    """Read a dotted path with optional list indexes: ``a.b[0].c``."""
    current = payload
    for part in [p for p in path.split(".") if p]:
        match = _INDEX.match(part)
        index = None
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        if index is not None:
            if not isinstance(current, (list, tuple)) or index >= len(current):
                return None
            current = current[index]
    return current


class GenericHttpProvider(LoadProvider):
    """One instance per entry in ``config/providers.json``."""

    priority = 50

    def __init__(self, settings: Any, spec: GenericProviderSpec) -> None:
        super().__init__(settings)
        self.spec = spec
        self.name = spec.name
        self.client = client_from_settings(settings, rate_limit_rps=spec.rate_limit_rps)
        self.client.cache_ttl = spec.cache_ttl_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.spec.enabled)

    def supports(self, venue: Venue) -> bool:
        spec = self.spec
        if spec.require_source_id and not venue.source_ids.get(spec.source_id_key or spec.name):
            return False
        if spec.only_categories and venue.category not in spec.only_categories:
            return False
        if spec.require_delivery and not venue.delivery:
            return False
        return True

    def get_current_load(self, venue: Venue) -> Optional[LoadSignal]:
        spec = self.spec
        context = {
            "venue_id": venue.id,
            "name": venue.name,
            "address": venue.address,
            "latitude": "{:.7f}".format(venue.latitude),
            "longitude": "{:.7f}".format(venue.longitude),
            "category": venue.category,
            "source_id": venue.source_ids.get(spec.source_id_key or spec.name, ""),
        }

        try:
            url = expand_env(spec.url_template).format(**context)
            headers = {k: expand_env(v).format(**context) for k, v in spec.headers.items()}
            query = {k: expand_env(v).format(**context) for k, v in spec.query.items()}
            body = (
                json.loads(expand_env(json.dumps(spec.json_body)).format(**context))
                if spec.json_body
                else None
            )
        except KeyError as exc:
            raise ProviderError(
                "provider {}: unknown placeholder {} in template".format(spec.name, exc)
            ) from exc

        try:
            payload = self.client.request_json(
                spec.method,
                url,
                params=query or None,
                json_body=body,
                headers=headers or None,
                cache_ttl=spec.cache_ttl_seconds,
            )
        except HttpError as exc:
            if exc.status in (404, 204):
                return None
            raise ProviderError("provider {} failed: {}".format(spec.name, exc)) from exc

        value = self.extract_value(payload)
        if value is None:
            return None

        return LoadSignal(
            venue_id=venue.id,
            source=spec.name,
            metric_type=spec.metric_type,
            metric_value=value,
            raw_value=payload if isinstance(payload, (dict, list)) else str(payload)[:500],
            domain=spec.domain,
            confidence=spec.confidence,
            signal_quality=spec.signal_quality,
            observed_at=utcnow(),
        )

    def extract_value(self, payload: Any) -> Optional[float]:
        raw = dig(payload, self.spec.value_path)
        if raw is None:
            return None
        if isinstance(raw, str):
            key = raw.strip()
            if key in self.spec.value_map:
                raw = self.spec.value_map[key]
            else:
                digits = re.findall(r"-?\d+(?:\.\d+)?", key)
                if not digits:
                    return None
                raw = digits[0]
        if isinstance(raw, bool):
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number * self.spec.value_scale + self.spec.value_offset


def load_generic_providers(settings: Any) -> List[GenericHttpProvider]:
    """Build every provider declared in the config file.

    A missing file is normal (the feature is opt-in). A malformed file is logged
    and skipped rather than crashing the run -- the primary provider must keep
    working.
    """
    path = settings.providers.generic_config_path
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("cannot read generic provider config", extra={"path": path, "error": str(exc)})
        return []

    entries = data.get("providers") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        log.error("generic provider config has no 'providers' list", extra={"path": path})
        return []

    providers: List[GenericHttpProvider] = []
    for entry in entries:
        try:
            spec = GenericProviderSpec.from_dict(entry)
        except ProviderConfigError as exc:
            log.error("invalid generic provider entry", extra={"error": str(exc)})
            continue
        if spec.enabled:
            providers.append(GenericHttpProvider(settings, spec))
    log.info("generic providers loaded", extra={"count": len(providers), "path": path})
    return providers
