"""Environment-driven configuration.

Everything that could reasonably change between deployments lives here, so
thresholds, the watch cadence and the notification target can be retuned
without touching source code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:  # optional: only used for local development
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or cannot be parsed."""


def env_str(name: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if required and default is None:
            raise ConfigError("required environment variable {} is not set".format(name))
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
    raise ConfigError("{}={!r} is not a boolean".format(name, raw))


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError as exc:
        raise ConfigError("{}={!r} is not an integer".format(name, raw)) from exc


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError("{}={!r} is not a number".format(name, raw)) from exc


def env_list(name: str, default: str = "") -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raw = default
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 30.0
    user_agent: str = "valve-watch/1.0"
    cache_ttl_seconds: int = 0


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: Optional[str]
    chat_id: Optional[str]
    api_base: str = "https://api.telegram.org"
    parse_mode: str = "HTML"
    disable_notification: bool = False
    #: send a quiet status message on runs with nothing to report
    heartbeat: bool = True
    #: 0 = every run; otherwise the minimum gap between status messages
    heartbeat_min_interval_minutes: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class AnomalyConfig:
    """Thresholds for the rate signals (commit counts and similar).

    Discrete state changes do not use any of this -- a version bump is an event,
    not a deviation.
    """

    multiplier: float = 2.5
    min_baseline_samples: int = 7
    lookback_weeks: int = 4
    #: absolute floor so a jump from 1 to 3 commits is not "a burst"
    min_absolute_delta: float = 3.0
    #: for constantly-moving counters: how big a relative move against the
    #: previous reading counts as sharp. 0.15 = 15%.
    delta_alert_fraction: float = 0.15
    #: ignore relative moves on tiny numbers
    delta_min_absolute: float = 500.0


@dataclass(frozen=True)
class WatchConfig:
    #: keep the numeric history bounded
    retention_days: int = 120
    #: skip subjects whose priority is above this (higher number = less important)
    max_priority: int = 1000


@dataclass(frozen=True)
class Settings:
    http: HttpConfig
    telegram: TelegramConfig
    anomaly: AnomalyConfig
    watch: WatchConfig
    database_url: str
    #: used to bucket daily rate history; defaults to Valve's local time
    timezone: str = "America/Los_Angeles"
    dry_run: bool = True
    log_level: str = "INFO"
    log_format: str = "json"
    extra: Dict[str, str] = field(default_factory=dict)


def load_settings(env_file: Optional[str] = ".env") -> Settings:
    """Build :class:`Settings` from the process environment (and optional .env)."""

    if env_file and os.path.exists(env_file):
        load_dotenv(env_file, override=False)

    http = HttpConfig(
        timeout_seconds=env_float("HTTP_TIMEOUT_SECONDS", 20.0),
        connect_timeout_seconds=env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 10.0),
        max_retries=env_int("HTTP_MAX_RETRIES", 3),
        backoff_base_seconds=env_float("HTTP_BACKOFF_BASE_SECONDS", 1.5),
        backoff_max_seconds=env_float("HTTP_BACKOFF_MAX_SECONDS", 30.0),
        user_agent=env_str("HTTP_USER_AGENT", "valve-watch/1.0 (+https://github.com/)")
        or "valve-watch/1.0",
        cache_ttl_seconds=env_int("HTTP_CACHE_TTL_SECONDS", 0),
    )

    telegram = TelegramConfig(
        bot_token=env_str("TELEGRAM_BOT_TOKEN"),
        chat_id=env_str("TELEGRAM_CHAT_ID"),
        api_base=env_str("TELEGRAM_API_BASE", "https://api.telegram.org")
        or "https://api.telegram.org",
        parse_mode=env_str("TELEGRAM_PARSE_MODE", "HTML") or "HTML",
        disable_notification=env_bool("TELEGRAM_DISABLE_NOTIFICATION", False),
        heartbeat=env_bool("TELEGRAM_HEARTBEAT", True),
        heartbeat_min_interval_minutes=env_int("TELEGRAM_HEARTBEAT_MIN_INTERVAL_MINUTES", 0),
    )

    anomaly = AnomalyConfig(
        multiplier=env_float("RATE_MULTIPLIER", 2.5),
        min_baseline_samples=env_int("MIN_BASELINE_SAMPLES", 7),
        lookback_weeks=env_int("BASELINE_LOOKBACK_WEEKS", 4),
        min_absolute_delta=env_float("RATE_MIN_ABSOLUTE_DELTA", 3.0),
        delta_alert_fraction=env_float("DELTA_ALERT_FRACTION", 0.15),
        delta_min_absolute=env_float("DELTA_MIN_ABSOLUTE", 500.0),
    )

    watch = WatchConfig(
        retention_days=env_int("OBSERVATION_RETENTION_DAYS", 120),
        max_priority=env_int("WATCH_MAX_PRIORITY", 1000),
    )

    database_url = env_str("DATABASE_URL", "sqlite:///data/watch.db") or "sqlite:///data/watch.db"

    return Settings(
        http=http,
        telegram=telegram,
        anomaly=anomaly,
        watch=watch,
        database_url=database_url,
        timezone=env_str("TIMEZONE", "America/Los_Angeles") or "America/Los_Angeles",
        dry_run=env_bool("DRY_RUN", True),
        log_level=(env_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=(env_str("LOG_FORMAT", "json") or "json").lower(),
    )
