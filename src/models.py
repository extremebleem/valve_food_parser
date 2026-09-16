"""Domain models shared by every layer.

Plain dataclasses so the same objects round-trip through SQLite, Postgres and
JSON dumps without an ORM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


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


class BaselineStatus(str, Enum):
    OK = "ok"
    LEARNING = "learning_baseline"
    NO_DATA = "no_data"


@dataclass
class Baseline:
    """Robust summary of a subject's historical rate for one metric."""

    venue_id: str          # subject id; the column name predates the pivot
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
class AlertRecord:
    """One notification we sent, kept for de-duplication."""

    venue_id: str          # subject id
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
    """Counters for a run summary."""

    duration_seconds: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)
