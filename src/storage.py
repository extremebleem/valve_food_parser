"""Persistence.

Why an external SQL database and not a file committed back to the repository:
a GitHub Actions runner is ephemeral, the monitor runs every 30 minutes, and
committing a binary database on every run would produce dozens of commits a day, race
between overlapping runs, and eventually make the repository unusable. A single
``DATABASE_URL`` secret pointed at a free Supabase/Neon Postgres keeps history
outside CI, gives real indexes for the baseline query, and survives the runner.

``sqlite://`` is supported for local development and the test-suite, using the
same SQL and the same code path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .logging_utils import get_logger
from .models import (
    AlertRecord,
    Baseline,
    BaselineStatus,
    Observation,
    Venue,
    iso,
    parse_iso,
    utcnow,
)

log = get_logger(__name__)


SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT DEFAULT '',
    latitude REAL NOT NULL, longitude REAL NOT NULL, distance_meters REAL DEFAULT 0,
    category TEXT DEFAULT '', website TEXT DEFAULT '', phone TEXT DEFAULT '',
    delivery INTEGER DEFAULT 0, takeaway INTEGER DEFAULT 0, sources TEXT DEFAULT '[]',
    cuisine TEXT DEFAULT '', opening_hours TEXT DEFAULT '',
    business_status TEXT DEFAULT 'OPERATIONAL', source_ids TEXT DEFAULT '{}',
    active INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS venues_active_idx ON venues (active, distance_meters);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL,
    metric_type TEXT NOT NULL, metric_value REAL NOT NULL, raw_value TEXT,
    load_score REAL NOT NULL, domain TEXT NOT NULL DEFAULT 'unknown',
    confidence REAL NOT NULL DEFAULT 0.5, signal_quality TEXT NOT NULL DEFAULT 'medium',
    local_weekday INTEGER NOT NULL, local_minutes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_baseline_idx
    ON observations (venue_id, metric_type, local_weekday, local_minutes, ts DESC);
CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations (ts);

CREATE TABLE IF NOT EXISTS baselines (
    venue_id TEXT NOT NULL, metric_type TEXT NOT NULL, weekday INTEGER NOT NULL,
    minutes INTEGER NOT NULL, sample_count INTEGER NOT NULL, median REAL NOT NULL,
    mad REAL NOT NULL, p90 REAL NOT NULL, mean REAL NOT NULL, status TEXT NOT NULL,
    computed_at TEXT, PRIMARY KEY (venue_id, metric_type, weekday, minutes)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, venue_id TEXT NOT NULL, kind TEXT NOT NULL,
    load_score REAL NOT NULL, baseline_score REAL NOT NULL, deviation_percent REAL NOT NULL,
    metric_type TEXT NOT NULL, sent_at TEXT NOT NULL, message_hash TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS alerts_venue_idx ON alerts (venue_id, kind, sent_at DESC);

CREATE TABLE IF NOT EXISTS alert_state (
    venue_id TEXT PRIMARY KEY, metric_type TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0, last_alert_at TEXT, last_score REAL DEFAULT 0,
    last_deviation REAL DEFAULT 0, peak_score REAL DEFAULT 0, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    kind TEXT NOT NULL, stats TEXT NOT NULL DEFAULT '{}'
);
"""


class StorageError(RuntimeError):
    pass


class BaseStorage:
    """Shared SQL logic. Subclasses only supply a connection and a paramstyle."""

    placeholder = "?"
    bool_true: Any = 1
    bool_false: Any = 0

    def __init__(self) -> None:
        self._conn: Any = None

    # -- lifecycle --------------------------------------------------------- #

    def connect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "BaseStorage":
        self.connect()
        self.migrate()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> Any:
        if self._conn is None:
            self.connect()
        return self._conn

    def migrate(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- low level --------------------------------------------------------- #

    def q(self, sql: str) -> str:
        """Translate ``?`` placeholders to the backend's paramstyle."""
        if self.placeholder == "?":
            return sql
        return sql.replace("?", self.placeholder)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self.conn.cursor()
        cur.execute(self.q(sql), tuple(params))
        return cur

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(self.q(sql), [tuple(r) for r in rows])
        cur.close()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [tuple(row) for row in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Tuple[Any, ...]]:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return tuple(row) if row is not None else None

    def commit(self) -> None:
        self.conn.commit()

    # -- timestamps -------------------------------------------------------- #

    def ts(self, value: Optional[datetime]) -> Any:
        """Backend representation of a timestamp (ISO text vs native)."""
        return iso(value)

    # -- venues ------------------------------------------------------------ #

    VENUE_COLUMNS = (
        "id, name, address, latitude, longitude, distance_meters, category, website, phone, "
        "delivery, takeaway, sources, cuisine, opening_hours, business_status, source_ids, "
        "active, first_seen, last_seen"
    )

    def upsert_venues(self, venues: Iterable[Venue]) -> Dict[str, int]:
        venues = list(venues)
        if not venues:
            return {"inserted": 0, "updated": 0}
        existing = {row[0] for row in self.fetchall("SELECT id FROM venues")}
        now = utcnow()
        inserted = updated = 0
        for venue in venues:
            venue.last_seen = venue.last_seen or now
            payload = (
                venue.name,
                venue.address,
                venue.latitude,
                venue.longitude,
                venue.distance_meters,
                venue.category,
                venue.website,
                venue.phone,
                self.bool_true if venue.delivery else self.bool_false,
                self.bool_true if venue.takeaway else self.bool_false,
                json.dumps(sorted(set(venue.sources)), ensure_ascii=False),
                venue.cuisine,
                venue.opening_hours,
                venue.business_status,
                json.dumps(venue.source_ids, ensure_ascii=False),
                self.bool_true if venue.active else self.bool_false,
                self.ts(venue.last_seen),
                venue.id,
            )
            if venue.id in existing:
                self.execute(
                    "UPDATE venues SET name=?, address=?, latitude=?, longitude=?, "
                    "distance_meters=?, category=?, website=?, phone=?, delivery=?, takeaway=?, "
                    "sources=?, cuisine=?, opening_hours=?, business_status=?, source_ids=?, "
                    "active=?, last_seen=? WHERE id=?",
                    payload,
                )
                updated += 1
            else:
                self.execute(
                    "INSERT INTO venues ({}) VALUES ({})".format(
                        self.VENUE_COLUMNS, ", ".join(["?"] * 19)
                    ),
                    (
                        venue.id,
                        venue.name,
                        venue.address,
                        venue.latitude,
                        venue.longitude,
                        venue.distance_meters,
                        venue.category,
                        venue.website,
                        venue.phone,
                        self.bool_true if venue.delivery else self.bool_false,
                        self.bool_true if venue.takeaway else self.bool_false,
                        json.dumps(sorted(set(venue.sources)), ensure_ascii=False),
                        venue.cuisine,
                        venue.opening_hours,
                        venue.business_status,
                        json.dumps(venue.source_ids, ensure_ascii=False),
                        self.bool_true if venue.active else self.bool_false,
                        self.ts(venue.first_seen or now),
                        self.ts(venue.last_seen),
                    ),
                )
                inserted += 1
        self.commit()
        return {"inserted": inserted, "updated": updated}

    def list_venues(self, active_only: bool = True, limit: Optional[int] = None) -> List[Venue]:
        sql = "SELECT {} FROM venues".format(self.VENUE_COLUMNS)
        params: List[Any] = []
        if active_only:
            sql += " WHERE active = ?"
            params.append(self.bool_true)
        sql += " ORDER BY distance_meters ASC"
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return [self._row_to_venue(row) for row in self.fetchall(sql, params)]

    def get_venue(self, venue_id: str) -> Optional[Venue]:
        row = self.fetchone(
            "SELECT {} FROM venues WHERE id = ?".format(self.VENUE_COLUMNS), (venue_id,)
        )
        return self._row_to_venue(row) if row else None

    def update_venue_source_ids(self, venue_id: str, mapping: Dict[str, str]) -> None:
        """Persist provider-side ids discovered at runtime (e.g. BestTime venue_id)
        so the expensive lookup happens once per venue, ever."""
        if not mapping:
            return
        row = self.fetchone("SELECT source_ids, sources FROM venues WHERE id = ?", (venue_id,))
        if not row:
            return
        current = json.loads(row[0] or "{}")
        sources = set(json.loads(row[1] or "[]"))
        current.update({k: v for k, v in mapping.items() if v})
        sources.update(mapping.keys())
        self.execute(
            "UPDATE venues SET source_ids = ?, sources = ? WHERE id = ?",
            (
                json.dumps(current, ensure_ascii=False),
                json.dumps(sorted(sources), ensure_ascii=False),
                venue_id,
            ),
        )
        self.commit()

    def deactivate_stale_venues(self, stale_days: int) -> int:
        cutoff = utcnow() - timedelta(days=max(1, stale_days))
        cur = self.execute(
            "UPDATE venues SET active = ? WHERE active = ? AND last_seen < ?",
            (self.bool_false, self.bool_true, self.ts(cutoff)),
        )
        count = cur.rowcount or 0
        cur.close()
        self.commit()
        return count

    @staticmethod
    def _row_to_venue(row: Sequence[Any]) -> Venue:
        return Venue(
            id=row[0],
            name=row[1],
            address=row[2] or "",
            latitude=float(row[3]),
            longitude=float(row[4]),
            distance_meters=float(row[5] or 0),
            category=row[6] or "",
            website=row[7] or "",
            phone=row[8] or "",
            delivery=bool(row[9]),
            takeaway=bool(row[10]),
            sources=json.loads(row[11] or "[]"),
            cuisine=row[12] or "",
            opening_hours=row[13] or "",
            business_status=row[14] or "OPERATIONAL",
            source_ids=json.loads(row[15] or "{}"),
            active=bool(row[16]),
            first_seen=parse_iso(row[17]),
            last_seen=parse_iso(row[18]),
        )

    # -- observations ------------------------------------------------------ #

    def insert_observations(self, observations: Sequence[Observation]) -> int:
        if not observations:
            return 0
        rows = [
            (
                obs.venue_id,
                self.ts(obs.timestamp),
                obs.source,
                obs.metric_type,
                float(obs.metric_value),
                json.dumps(obs.raw_value, ensure_ascii=False, default=str)
                if obs.raw_value is not None
                else None,
                float(obs.load_score),
                obs.domain,
                float(obs.confidence),
                obs.signal_quality,
                int(obs.local_weekday),
                int(obs.local_minutes),
            )
            for obs in observations
        ]
        self.executemany(
            "INSERT INTO observations (venue_id, ts, source, metric_type, metric_value, "
            "raw_value, load_score, domain, confidence, signal_quality, local_weekday, "
            "local_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.commit()
        return len(rows)

    @staticmethod
    def _slot_windows(weekday: int, minutes: int, window: int) -> List[Tuple[int, int, int]]:
        """(weekday, min_minutes, max_minutes) triples covering a +/- window that
        may wrap across midnight and therefore across the weekday boundary."""
        low, high = minutes - window, minutes + window
        windows: List[Tuple[int, int, int]] = [(weekday, max(0, low), min(1439, high))]
        if low < 0:
            windows.append(((weekday - 1) % 7, 1440 + low, 1439))
        if high > 1439:
            windows.append(((weekday + 1) % 7, 0, high - 1440))
        return windows

    def fetch_baseline_samples(
        self,
        venue_id: str,
        metric_type: str,
        weekday: int,
        minutes: int,
        window_minutes: int,
        lookback_weeks: int,
        *,
        exclude_after: Optional[datetime] = None,
    ) -> List[float]:
        """Historical ``load_score`` values comparable to *now* for this venue.

        Same venue, same metric, same local weekday, local time within
        +/- ``window_minutes``, not older than ``lookback_weeks``. The current
        observation is excluded via ``exclude_after`` so a venue is never
        compared against itself.
        """
        cutoff = utcnow() - timedelta(weeks=max(1, lookback_weeks))
        windows = self._slot_windows(weekday, minutes, window_minutes)
        clauses = []
        params: List[Any] = [venue_id, metric_type, self.ts(cutoff)]
        for wd, lo, hi in windows:
            clauses.append("(local_weekday = ? AND local_minutes BETWEEN ? AND ?)")
            params.extend([wd, lo, hi])
        sql = (
            "SELECT load_score FROM observations WHERE venue_id = ? AND metric_type = ? "
            "AND ts >= ? AND ({})".format(" OR ".join(clauses))
        )
        if exclude_after is not None:
            sql += " AND ts < ?"
            params.append(self.ts(exclude_after))
        sql += " ORDER BY ts DESC LIMIT 2000"
        return [float(row[0]) for row in self.fetchall(sql, params)]

    def purge_observations(self, keep_days: int) -> int:
        cutoff = utcnow() - timedelta(days=max(1, keep_days))
        cur = self.execute("DELETE FROM observations WHERE ts < ?", (self.ts(cutoff),))
        count = cur.rowcount or 0
        cur.close()
        self.commit()
        return count

    def count_observations(self) -> int:
        row = self.fetchone("SELECT COUNT(*) FROM observations")
        return int(row[0]) if row else 0

    # -- baselines --------------------------------------------------------- #

    def upsert_baseline(self, baseline: Baseline) -> None:
        self.execute(
            "DELETE FROM baselines WHERE venue_id=? AND metric_type=? AND weekday=? AND minutes=?",
            (baseline.venue_id, baseline.metric_type, baseline.weekday, baseline.minutes),
        )
        self.execute(
            "INSERT INTO baselines (venue_id, metric_type, weekday, minutes, sample_count, "
            "median, mad, p90, mean, status, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                baseline.venue_id,
                baseline.metric_type,
                baseline.weekday,
                baseline.minutes,
                baseline.sample_count,
                baseline.median,
                baseline.mad,
                baseline.p90,
                baseline.mean,
                baseline.status.value,
                self.ts(baseline.computed_at or utcnow()),
            ),
        )
        self.commit()

    def get_baseline(
        self, venue_id: str, metric_type: str, weekday: int, minutes: int
    ) -> Optional[Baseline]:
        row = self.fetchone(
            "SELECT venue_id, metric_type, weekday, minutes, sample_count, median, mad, p90, "
            "mean, status, computed_at FROM baselines WHERE venue_id=? AND metric_type=? "
            "AND weekday=? AND minutes=?",
            (venue_id, metric_type, weekday, minutes),
        )
        if not row:
            return None
        return Baseline(
            venue_id=row[0],
            metric_type=row[1],
            weekday=int(row[2]),
            minutes=int(row[3]),
            sample_count=int(row[4]),
            median=float(row[5]),
            mad=float(row[6]),
            p90=float(row[7]),
            mean=float(row[8]),
            status=BaselineStatus(row[9]),
            computed_at=parse_iso(row[10]),
        )

    # -- alerts ------------------------------------------------------------ #

    def record_alert(self, alert: AlertRecord) -> None:
        self.execute(
            "INSERT INTO alerts (venue_id, kind, load_score, baseline_score, deviation_percent, "
            "metric_type, sent_at, message_hash, delivered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alert.venue_id,
                alert.kind,
                float(alert.load_score),
                float(alert.baseline_score),
                float(alert.deviation_percent),
                alert.metric_type,
                self.ts(alert.sent_at or utcnow()),
                alert.message_hash,
                self.bool_true if alert.delivered else self.bool_false,
            ),
        )
        self.commit()

    def last_alert(self, venue_id: str, kind: Optional[str] = None) -> Optional[AlertRecord]:
        sql = (
            "SELECT id, venue_id, kind, load_score, baseline_score, deviation_percent, "
            "metric_type, sent_at, message_hash, delivered FROM alerts WHERE venue_id = ?"
        )
        params: List[Any] = [venue_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY sent_at DESC LIMIT 1"
        row = self.fetchone(sql, params)
        if not row:
            return None
        return AlertRecord(
            id=int(row[0]),
            venue_id=row[1],
            kind=row[2],
            load_score=float(row[3]),
            baseline_score=float(row[4]),
            deviation_percent=float(row[5]),
            metric_type=row[6],
            sent_at=parse_iso(row[7]),
            message_hash=row[8] or "",
            delivered=bool(row[9]),
        )

    def alert_state(self, venue_id: str) -> Dict[str, Any]:
        row = self.fetchone(
            "SELECT venue_id, metric_type, active, last_alert_at, last_score, last_deviation, "
            "peak_score, updated_at FROM alert_state WHERE venue_id = ?",
            (venue_id,),
        )
        if not row:
            return {
                "venue_id": venue_id,
                "metric_type": "",
                "active": False,
                "last_alert_at": None,
                "last_score": 0.0,
                "last_deviation": 0.0,
                "peak_score": 0.0,
                "updated_at": None,
            }
        return {
            "venue_id": row[0],
            "metric_type": row[1] or "",
            "active": bool(row[2]),
            "last_alert_at": parse_iso(row[3]),
            "last_score": float(row[4] or 0.0),
            "last_deviation": float(row[5] or 0.0),
            "peak_score": float(row[6] or 0.0),
            "updated_at": parse_iso(row[7]),
        }

    def set_alert_state(
        self,
        venue_id: str,
        *,
        metric_type: str = "",
        active: bool = False,
        last_alert_at: Optional[datetime] = None,
        last_score: float = 0.0,
        last_deviation: float = 0.0,
        peak_score: float = 0.0,
    ) -> None:
        self.execute("DELETE FROM alert_state WHERE venue_id = ?", (venue_id,))
        self.execute(
            "INSERT INTO alert_state (venue_id, metric_type, active, last_alert_at, last_score, "
            "last_deviation, peak_score, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                venue_id,
                metric_type,
                self.bool_true if active else self.bool_false,
                self.ts(last_alert_at),
                float(last_score),
                float(last_deviation),
                float(peak_score),
                self.ts(utcnow()),
            ),
        )
        self.commit()

    def count_alerts_since(self, since: datetime) -> int:
        row = self.fetchone("SELECT COUNT(*) FROM alerts WHERE sent_at >= ?", (self.ts(since),))
        return int(row[0]) if row else 0

    # -- runs -------------------------------------------------------------- #

    def record_run(self, kind: str, started_at: datetime, stats: Dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO runs (started_at, finished_at, kind, stats) VALUES (?, ?, ?, ?)",
            (
                self.ts(started_at),
                self.ts(utcnow()),
                kind,
                json.dumps(stats, ensure_ascii=False, default=str),
            ),
        )
        self.commit()


class SQLiteStorage(BaseStorage):
    placeholder = "?"

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def connect(self) -> None:
        if self.path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def migrate(self) -> None:
        self.conn.executescript(SQLITE_DDL)
        self.conn.commit()


class PostgresStorage(BaseStorage):
    placeholder = "%s"
    bool_true = True
    bool_false = False

    def __init__(self, dsn: str) -> None:
        super().__init__()
        self.dsn = dsn

    def connect(self) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise StorageError(
                "DATABASE_URL points at PostgreSQL but psycopg is not installed "
                "(pip install 'psycopg[binary]')"
            ) from exc
        self._conn = psycopg.connect(self.dsn, connect_timeout=15, autocommit=False)

    def ts(self, value: Optional[datetime]) -> Any:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def migrate(self) -> None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_path = os.path.join(here, "migrations", "001_init.sql")
        with open(sql_path, "r", encoding="utf-8") as fh:
            ddl = fh.read()
        cur = self.conn.cursor()
        cur.execute(ddl)
        cur.close()
        self.conn.commit()


def create_storage(database_url: str) -> BaseStorage:
    """Build the right backend for a ``DATABASE_URL``.

    Accepted: ``postgres://``, ``postgresql://`` (Supabase/Neon/RDS/...),
    ``sqlite:///relative/path.db``, ``sqlite:////absolute/path.db``,
    ``sqlite://:memory:``.
    """
    url = (database_url or "").strip()
    if not url:
        raise StorageError("DATABASE_URL is empty")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        dsn = url.replace("postgresql+psycopg://", "postgresql://")
        return PostgresStorage(dsn)

    if scheme == "sqlite":
        remainder = url[len("sqlite://") :]
        if remainder in {":memory:", "/:memory:"}:
            return SQLiteStorage(":memory:")
        path = remainder.lstrip("/") if not remainder.startswith("//") else remainder[1:]
        return SQLiteStorage(path or "data/monitor.db")

    raise StorageError(
        "unsupported DATABASE_URL scheme {!r}; use postgresql:// or sqlite://".format(scheme)
    )
