"""Environment-driven configuration.

Everything that could reasonably change between deployments lives here, so the
office can be moved, thresholds retuned or providers swapped without touching
source code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:  # optional: only used for local development
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or cannot be parsed."""


def env_str(name: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if required and default is None:
            raise ConfigError(f"required environment variable {name} is not set")
        return default
    return raw.strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def env_list(name: str, default: str = "") -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raw = default
    return [part.strip() for part in raw.split(",") if part.strip()]


# --------------------------------------------------------------------------- #
# config sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OfficeConfig:
    """Anchor point the search radius is measured from."""

    name: str
    address: str
    latitude: float
    longitude: float
    timezone: str
    radius_meters: int

    def validate(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ConfigError(f"OFFICE_LAT out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ConfigError(f"OFFICE_LON out of range: {self.longitude}")
        if not 50 <= self.radius_meters <= 50_000:
            raise ConfigError(f"SEARCH_RADIUS_METERS out of range: {self.radius_meters}")


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 30.0
    user_agent: str = "valve-food-monitor/1.0 (+https://github.com/)"
    cache_ttl_seconds: int = 300


@dataclass(frozen=True)
class DiscoveryConfig:
    overpass_urls: List[str]
    enable_osm: bool = True
    enable_google: bool = False
    enable_foursquare: bool = False
    google_api_key: Optional[str] = None
    foursquare_api_key: Optional[str] = None
    dedupe_distance_meters: float = 75.0
    dedupe_name_ratio: float = 0.82
    # venues not re-seen by discovery for this long are marked inactive
    stale_days: int = 21


@dataclass(frozen=True)
class NormalizationConfig:
    """Envelopes that map a raw metric onto the 0-100 load_score scale.

    ``low`` maps to load_score 0, ``high`` maps to load_score 100, linear in
    between, clamped at both ends. Values were chosen from published
    typical-service-time ranges and are deliberately configurable: the
    *relative* comparison against a venue's own baseline is what drives alerts,
    the absolute mapping only needs to be monotonic and stable.
    """

    delivery_eta_low: float = 15.0
    delivery_eta_high: float = 75.0
    pickup_eta_low: float = 5.0
    pickup_eta_high: float = 45.0
    wait_time_low: float = 0.0
    wait_time_high: float = 60.0
    reservation_low: float = 0.0
    reservation_high: float = 180.0
    queue_low: float = 0.0
    queue_high: float = 25.0
    prep_time_low: float = 5.0
    prep_time_high: float = 45.0


@dataclass(frozen=True)
class AnomalyConfig:
    multiplier: float = 1.5
    min_baseline_samples: int = 5
    lookback_weeks: int = 8
    window_minutes: int = 60
    min_score: float = 55.0
    min_absolute_delta: float = 10.0
    min_robust_z: float = 3.0
    require_robust_z: bool = True
    min_confidence: float = 0.4
    # when there is no usable baseline the venue is reported as learning and
    # never alerts, unless an operator opts into an absolute-score fallback
    fallback_absolute_enabled: bool = False
    fallback_absolute_score: float = 90.0


@dataclass(frozen=True)
class ActiveWindowConfig:
    """Hours of the day, in the *office's local time*, when monitoring runs.

    Outside the window the run exits immediately without touching any API.
    Evaluated against the office timezone rather than UTC, so the window does
    not drift by an hour at each DST transition the way a UTC cron does.

    ``start_hour == end_hour`` means "always on". A window may wrap midnight
    (``start_hour=18, end_hour=2``).
    """

    # 14:00-21:00 office-local. NOTE: this excludes the lunch peak (~11:30-13:30);
    # set ACTIVE_HOURS_START=11 to cover it.
    start_hour: int = 14
    end_hour: int = 21
    weekdays: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    @property
    def always_on(self) -> bool:
        return self.start_hour == self.end_hour and len(self.weekdays) == 7

    def validate(self) -> None:
        for name, value in (("ACTIVE_HOURS_START", self.start_hour), ("ACTIVE_HOURS_END", self.end_hour)):
            if not 0 <= value <= 24:
                raise ConfigError("{} must be between 0 and 24, got {}".format(name, value))
        if not self.weekdays:
            raise ConfigError("ACTIVE_WEEKDAYS cannot be empty")
        if any(not 0 <= d <= 6 for d in self.weekdays):
            raise ConfigError("ACTIVE_WEEKDAYS must contain 0 (Mon) .. 6 (Sun)")

    def contains(self, weekday: int, hour: int) -> bool:
        """Is this local weekday/hour inside the window?"""
        if weekday not in self.weekdays:
            return False
        if self.always_on or self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour  # wraps midnight

    def describe(self) -> str:
        if self.always_on:
            return "24/7"
        days = "".join("MTWTFSS"[d] for d in sorted(self.weekdays))
        return "{:02d}:00-{:02d}:00 local ({})".format(self.start_hour, self.end_hour, days)


@dataclass(frozen=True)
class AlertConfig:
    cooldown_minutes: int = 120
    # anti-flap: even after a venue recovered, do not re-alert sooner than this
    rearm_minutes: int = 30
    escalation_delta: float = 15.0
    send_recovery: bool = True
    recovery_ratio: float = 1.15
    max_alerts_per_run: int = 12
    aggregate: bool = True


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: Optional[str]
    chat_id: Optional[str]
    api_base: str = "https://api.telegram.org"
    parse_mode: str = "HTML"
    disable_notification: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class ProviderConfig:
    load_provider_order: List[str]
    besttime_private_key: Optional[str] = None
    besttime_public_key: Optional[str] = None
    besttime_api_base: str = "https://besttime.app/api/v1"
    besttime_allow_forecast_as_load: bool = False
    besttime_rate_limit_rps: float = 4.0
    generic_config_path: Optional[str] = None
    google_api_key: Optional[str] = None


@dataclass(frozen=True)
class Settings:
    office: OfficeConfig
    http: HttpConfig
    discovery: DiscoveryConfig
    normalization: NormalizationConfig
    anomaly: AnomalyConfig
    alerts: AlertConfig
    active_window: ActiveWindowConfig
    telegram: TelegramConfig
    providers: ProviderConfig
    database_url: str
    dry_run: bool = True
    log_level: str = "INFO"
    log_format: str = "json"
    skip_closed_venues: bool = True
    assume_open_when_unknown: bool = True
    # keep the provider's raw JSON alongside each observation. Useful for
    # debugging and re-normalising history, but it is ~46% of the row size.
    store_raw_value: bool = True
    max_venues_per_run: int = 150
    run_id: str = ""
    extra: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Default anchor: verified 2026-09-16 against OpenStreetMap node 5270634805
# ("Valve Corporation Headquarters", 10400 NE 4th St, Bellevue, WA 98004) and
# the Bellevue Downtown Association business directory. Override via env.
# --------------------------------------------------------------------------- #

DEFAULT_OFFICE_NAME = "Valve Corporation HQ"
DEFAULT_OFFICE_ADDRESS = "10400 NE 4th St, Bellevue, WA 98004, USA"
DEFAULT_OFFICE_LAT = 47.6142467
DEFAULT_OFFICE_LON = -122.2007170
DEFAULT_OFFICE_TZ = "America/Los_Angeles"

DEFAULT_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter,"
    "https://overpass.kumi.systems/api/interpreter,"
    "https://overpass.osm.ch/api/interpreter"
)


def load_settings(env_file: Optional[str] = ".env", *, run_id: str = "") -> Settings:
    """Build :class:`Settings` from the process environment (and optional .env)."""

    if env_file and os.path.exists(env_file):
        load_dotenv(env_file, override=False)

    office = OfficeConfig(
        name=env_str("OFFICE_NAME", DEFAULT_OFFICE_NAME) or DEFAULT_OFFICE_NAME,
        address=env_str("OFFICE_ADDRESS", DEFAULT_OFFICE_ADDRESS) or DEFAULT_OFFICE_ADDRESS,
        latitude=env_float("OFFICE_LAT", DEFAULT_OFFICE_LAT),
        longitude=env_float("OFFICE_LON", DEFAULT_OFFICE_LON),
        timezone=env_str("OFFICE_TIMEZONE", DEFAULT_OFFICE_TZ) or DEFAULT_OFFICE_TZ,
        radius_meters=env_int("SEARCH_RADIUS_METERS", 2000),
    )
    office.validate()

    http = HttpConfig(
        timeout_seconds=env_float("HTTP_TIMEOUT_SECONDS", 20.0),
        connect_timeout_seconds=env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 10.0),
        max_retries=env_int("HTTP_MAX_RETRIES", 3),
        backoff_base_seconds=env_float("HTTP_BACKOFF_BASE_SECONDS", 1.5),
        backoff_max_seconds=env_float("HTTP_BACKOFF_MAX_SECONDS", 30.0),
        user_agent=env_str(
            "HTTP_USER_AGENT",
            "valve-food-monitor/1.0 (+https://github.com/valve-food-monitor)",
        )
        or "valve-food-monitor/1.0",
        cache_ttl_seconds=env_int("HTTP_CACHE_TTL_SECONDS", 300),
    )

    google_key = env_str("GOOGLE_MAPS_API_KEY")
    fsq_key = env_str("FOURSQUARE_API_KEY")

    discovery = DiscoveryConfig(
        overpass_urls=env_list("OVERPASS_URLS", DEFAULT_OVERPASS_URLS),
        enable_osm=env_bool("ENABLE_OSM_DISCOVERY", True),
        enable_google=env_bool("ENABLE_GOOGLE_DISCOVERY", bool(google_key)),
        enable_foursquare=env_bool("ENABLE_FOURSQUARE_DISCOVERY", bool(fsq_key)),
        google_api_key=google_key,
        foursquare_api_key=fsq_key,
        dedupe_distance_meters=env_float("DEDUPE_DISTANCE_METERS", 75.0),
        dedupe_name_ratio=env_float("DEDUPE_NAME_RATIO", 0.82),
        stale_days=env_int("VENUE_STALE_DAYS", 21),
    )

    normalization = NormalizationConfig(
        delivery_eta_low=env_float("NORM_DELIVERY_ETA_LOW_MIN", 15.0),
        delivery_eta_high=env_float("NORM_DELIVERY_ETA_HIGH_MIN", 75.0),
        pickup_eta_low=env_float("NORM_PICKUP_ETA_LOW_MIN", 5.0),
        pickup_eta_high=env_float("NORM_PICKUP_ETA_HIGH_MIN", 45.0),
        wait_time_low=env_float("NORM_WAIT_LOW_MIN", 0.0),
        wait_time_high=env_float("NORM_WAIT_HIGH_MIN", 60.0),
        reservation_low=env_float("NORM_RESERVATION_LOW_MIN", 0.0),
        reservation_high=env_float("NORM_RESERVATION_HIGH_MIN", 180.0),
        queue_low=env_float("NORM_QUEUE_LOW", 0.0),
        queue_high=env_float("NORM_QUEUE_HIGH", 25.0),
        prep_time_low=env_float("NORM_PREP_LOW_MIN", 5.0),
        prep_time_high=env_float("NORM_PREP_HIGH_MIN", 45.0),
    )

    anomaly = AnomalyConfig(
        multiplier=env_float("ANOMALY_MULTIPLIER", 1.5),
        min_baseline_samples=env_int("MIN_BASELINE_SAMPLES", 5),
        lookback_weeks=env_int("BASELINE_LOOKBACK_WEEKS", 8),
        window_minutes=env_int("BASELINE_WINDOW_MINUTES", 60),
        min_score=env_float("ANOMALY_MIN_SCORE", 55.0),
        min_absolute_delta=env_float("ANOMALY_MIN_ABSOLUTE_DELTA", 10.0),
        min_robust_z=env_float("ANOMALY_MIN_ROBUST_Z", 3.0),
        require_robust_z=env_bool("ANOMALY_REQUIRE_ROBUST_Z", True),
        min_confidence=env_float("ANOMALY_MIN_CONFIDENCE", 0.4),
        fallback_absolute_enabled=env_bool("ANOMALY_FALLBACK_ABSOLUTE_ENABLED", False),
        fallback_absolute_score=env_float("ANOMALY_FALLBACK_ABSOLUTE_SCORE", 90.0),
    )

    alerts = AlertConfig(
        cooldown_minutes=env_int("ALERT_COOLDOWN_MINUTES", 120),
        rearm_minutes=env_int("ALERT_REARM_MINUTES", 30),
        escalation_delta=env_float("ALERT_ESCALATION_DELTA", 15.0),
        send_recovery=env_bool("SEND_RECOVERY_ALERTS", True),
        recovery_ratio=env_float("RECOVERY_RATIO", 1.15),
        max_alerts_per_run=env_int("MAX_ALERTS_PER_RUN", 12),
        aggregate=env_bool("AGGREGATE_ALERTS", True),
    )

    active_window = ActiveWindowConfig(
        start_hour=env_int("ACTIVE_HOURS_START", 14),
        end_hour=env_int("ACTIVE_HOURS_END", 21),
        weekdays=tuple(sorted({int(d) for d in env_list("ACTIVE_WEEKDAYS", "0,1,2,3,4,5,6")})),
    )
    active_window.validate()

    telegram = TelegramConfig(
        bot_token=env_str("TELEGRAM_BOT_TOKEN"),
        chat_id=env_str("TELEGRAM_CHAT_ID"),
        api_base=env_str("TELEGRAM_API_BASE", "https://api.telegram.org") or "https://api.telegram.org",
        parse_mode=env_str("TELEGRAM_PARSE_MODE", "HTML") or "HTML",
        disable_notification=env_bool("TELEGRAM_DISABLE_NOTIFICATION", False),
    )

    providers = ProviderConfig(
        load_provider_order=env_list("LOAD_PROVIDERS", "besttime,generic_http"),
        besttime_private_key=env_str("BESTTIME_API_KEY_PRIVATE"),
        besttime_public_key=env_str("BESTTIME_API_KEY_PUBLIC"),
        besttime_api_base=env_str("BESTTIME_API_BASE", "https://besttime.app/api/v1")
        or "https://besttime.app/api/v1",
        besttime_allow_forecast_as_load=env_bool("BESTTIME_ALLOW_FORECAST_AS_LOAD", False),
        besttime_rate_limit_rps=env_float("BESTTIME_RATE_LIMIT_RPS", 4.0),
        generic_config_path=env_str("GENERIC_PROVIDER_CONFIG", "config/providers.json"),
        google_api_key=google_key,
    )

    database_url = env_str("DATABASE_URL", "sqlite:///data/monitor.db") or "sqlite:///data/monitor.db"

    return Settings(
        office=office,
        http=http,
        discovery=discovery,
        normalization=normalization,
        anomaly=anomaly,
        alerts=alerts,
        active_window=active_window,
        telegram=telegram,
        providers=providers,
        database_url=database_url,
        dry_run=env_bool("DRY_RUN", True),
        log_level=(env_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=(env_str("LOG_FORMAT", "json") or "json").lower(),
        skip_closed_venues=env_bool("SKIP_CLOSED_VENUES", True),
        assume_open_when_unknown=env_bool("ASSUME_OPEN_WHEN_UNKNOWN", True),
        store_raw_value=env_bool("STORE_RAW_VALUE", True),
        max_venues_per_run=env_int("MAX_VENUES_PER_RUN", 150),
        run_id=run_id,
    )
