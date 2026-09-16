"""Domain models shared by every layer.

Plain dataclasses + ``to_dict``/``from_dict`` so the same objects round-trip
through SQLite, Postgres and JSON dumps without an ORM.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parser (accepts ``Z``, naive values and datetimes)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MetricType(str, Enum):
    """What a provider actually measured. Never mix these in one baseline."""

    LIVE_BUSYNESS_INDEX = "live_busyness_index"
    FORECAST_BUSYNESS_INDEX = "forecast_busyness_index"
    LIVE_VS_FORECAST_DELTA = "live_vs_forecast_delta"
    DELIVERY_ETA_MINUTES = "delivery_eta_minutes"
    PICKUP_ETA_MINUTES = "pickup_eta_minutes"
    PREP_TIME_MINUTES = "prep_time_minutes"
    WAIT_TIME_MINUTES = "wait_time_minutes"
    QUEUE_LENGTH = "queue_length"
    NEXT_RESERVATION_MINUTES = "next_reservation_minutes"
    LOAD_SCORE_DIRECT = "load_score_direct"


class CongestionDomain(str, Enum):
    """Which kind of congestion the metric describes.

    Deliberately explicit: a 55-minute delivery ETA says something about the
    courier network and the kitchen queue, *not* about how many people are
    sitting in the dining room.
    """

    PHYSICAL_OCCUPANCY = "physical_occupancy"
    DELIVERY_CONGESTION = "delivery_congestion"
    PICKUP_CONGESTION = "pickup_congestion"
    RESERVATION_CONGESTION = "reservation_congestion"
    UNKNOWN = "unknown"


class SignalQuality(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LoadBand(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    BUSY = "busy"
    EXTREMELY_BUSY = "extremely_busy"


class BaselineStatus(str, Enum):
    OK = "ok"
    LEARNING = "learning_baseline"
    NO_DATA = "no_data"


class AlertKind(str, Enum):
    ANOMALY = "anomaly"
    RECOVERY = "recovery"


class Direction(str, Enum):
    """Which way an observation deviates from its own baseline.

    ``HIGH`` is the classic "unusually busy". ``LOW`` matters because the
    hypothesis this project was built around -- office workers staying in and
    ordering delivery instead of walking to a restaurant -- predicts *fewer*
    people in the nearby venues, not more.
    """

    NONE = "none"
    HIGH = "high"
    LOW = "low"


def band_for_score(score: float) -> LoadBand:
    if score <= 30:
        return LoadBand.LOW
    if score <= 60:
        return LoadBand.NORMAL
    if score <= 80:
        return LoadBand.BUSY
    return LoadBand.EXTREMELY_BUSY


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NAME_NOISE = re.compile(
    r"\b(the|a|an|restaurant|cafe|caffe|coffee|bar|grill|kitchen|bistro|"
    r"pizzeria|pizza|bakery|deli|llc|inc|co|company|bellevue|downtown)\b"
)


def normalize_name(name: str) -> str:
    """Aggressive name normalisation used for cross-source de-duplication."""
    lowered = (name or "").lower().strip()
    # strip accents so "Fogo de Chao" and "Fogo de Chão" collapse to one venue
    lowered = "".join(
        ch for ch in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(ch)
    )
    lowered = lowered.replace("&", " and ")
    lowered = _NON_ALNUM.sub(" ", lowered)
    lowered = _NAME_NOISE.sub(" ", lowered)
    return " ".join(lowered.split())


def make_venue_id(name: str, latitude: float, longitude: float) -> str:
    """Stable, source-independent surrogate key.

    Coordinates are rounded to ~11 m so the same venue keeps its id when a
    source nudges the position slightly between refreshes.
    """
    key = "{}|{:.4f}|{:.4f}".format(normalize_name(name) or "unnamed", latitude, longitude)
    return "v_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class Venue:
    id: str
    name: str
    address: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    distance_meters: float = 0.0
    category: str = ""
    website: str = ""
    phone: str = ""
    delivery: bool = False
    takeaway: bool = False
    sources: List[str] = field(default_factory=list)
    cuisine: str = ""
    opening_hours: str = ""
    business_status: str = "OPERATIONAL"
    source_ids: Dict[str, str] = field(default_factory=dict)
    active: bool = True
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["first_seen"] = iso(self.first_seen)
        data["last_seen"] = iso(self.last_seen)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Venue":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            address=data.get("address", "") or "",
            latitude=float(data.get("latitude") or 0.0),
            longitude=float(data.get("longitude") or 0.0),
            distance_meters=float(data.get("distance_meters") or 0.0),
            category=data.get("category", "") or "",
            website=data.get("website", "") or "",
            phone=data.get("phone", "") or "",
            delivery=bool(data.get("delivery")),
            takeaway=bool(data.get("takeaway")),
            sources=list(data.get("sources") or []),
            cuisine=data.get("cuisine", "") or "",
            opening_hours=data.get("opening_hours", "") or "",
            business_status=data.get("business_status", "OPERATIONAL") or "OPERATIONAL",
            source_ids=dict(data.get("source_ids") or {}),
            active=bool(data.get("active", True)),
            first_seen=parse_iso(data.get("first_seen")),
            last_seen=parse_iso(data.get("last_seen")),
        )


@dataclass
class LoadSignal:
    """Raw provider output, before normalisation."""

    venue_id: str
    source: str
    metric_type: MetricType
    metric_value: float
    raw_value: Any = None
    domain: CongestionDomain = CongestionDomain.UNKNOWN
    # None => let the normaliser apply the per-metric default
    confidence: Optional[float] = None
    signal_quality: Optional[SignalQuality] = None
    observed_at: Optional[datetime] = None
    note: str = ""


@dataclass
class Observation:
    """A normalised, storable measurement."""

    venue_id: str
    timestamp: datetime
    source: str
    metric_type: str
    metric_value: float
    raw_value: Any = None
    load_score: float = 0.0
    domain: str = CongestionDomain.UNKNOWN.value
    confidence: float = 0.5
    signal_quality: str = SignalQuality.MEDIUM.value
    local_weekday: int = 0
    local_minutes: int = 0
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue_id": self.venue_id,
            "timestamp": iso(self.timestamp),
            "source": self.source,
            "metric_type": self.metric_type,
            "metric_value": self.metric_value,
            "raw_value": self.raw_value,
            "load_score": self.load_score,
            "domain": self.domain,
            "confidence": self.confidence,
            "signal_quality": self.signal_quality,
            "local_weekday": self.local_weekday,
            "local_minutes": self.local_minutes,
        }

    @property
    def band(self) -> LoadBand:
        return band_for_score(self.load_score)


@dataclass
class Baseline:
    """Robust summary of a venue's historical load for one time slot."""

    venue_id: str
    metric_type: str
    weekday: int
    minutes: int
    sample_count: int
    median: float
    mad: float
    p90: float
    mean: float
    status: BaselineStatus = BaselineStatus.OK
    computed_at: Optional[datetime] = None

    @property
    def usable(self) -> bool:
        return self.status is BaselineStatus.OK


@dataclass
class AnomalyResult:
    venue: Venue
    observation: Observation
    baseline: Optional[Baseline]
    is_anomaly: bool
    deviation_ratio: float = 1.0
    deviation_percent: float = 0.0
    robust_z: float = 0.0
    status: BaselineStatus = BaselineStatus.NO_DATA
    reason: str = ""
    direction: Direction = Direction.NONE
    #: district-wide ratio this venue was compared against (1.0 = district normal)
    district_index: float = 1.0
    #: deviation_ratio after dividing out the district trend
    relative_ratio: float = 1.0

    @property
    def relative_percent(self) -> float:
        return (self.relative_ratio - 1.0) * 100.0

    @property
    def current_score(self) -> float:
        return self.observation.load_score

    @property
    def baseline_score(self) -> float:
        return self.baseline.median if self.baseline else 0.0


@dataclass
class AlertRecord:
    venue_id: str
    kind: str
    load_score: float
    baseline_score: float
    deviation_percent: float
    metric_type: str
    sent_at: Optional[datetime] = None
    message_hash: str = ""
    delivered: bool = True
    direction: str = "high"
    id: Optional[int] = None


@dataclass
class RunStats:
    venues_total: int = 0
    venues_open: int = 0
    venues_checked: int = 0
    venues_failed: int = 0
    venues_learning: int = 0
    observations_written: int = 0
    anomalies: int = 0
    anomalies_low: int = 0
    recoveries: int = 0
    district_index: float = 1.0
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    provider_errors: Dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    skipped_reason: str = ""

    def as_logline(self) -> str:
        return (
            "venues_total={} venues_open={} venues_checked={} venues_failed={} "
            "venues_learning={} observations={} anomalies_high={} anomalies_low={} "
            "recoveries={} district_index={:.2f} "
            "alerts_sent={} alerts_suppressed={} duration={:.1f}s".format(
                self.venues_total,
                self.venues_open,
                self.venues_checked,
                self.venues_failed,
                self.venues_learning,
                self.observations_written,
                self.anomalies,
                self.anomalies_low,
                self.recoveries,
                self.district_index,
                self.alerts_sent,
                self.alerts_suppressed,
                self.duration_seconds,
            )
        )
