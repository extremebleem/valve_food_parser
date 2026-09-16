"""Persistence.

Why an external store and not a file committed back to the repository: a GitHub
Actions runner is ephemeral, and committing a binary database on every run would
race between overlapping runs and bloat the history. Two interchangeable
backends sit behind one interface:

* ``postgresql://`` -- Supabase/Neon/anything, durable and queryable from outside CI
* ``sqlite://``     -- used locally, in the tests, and in CI where the file is
  carried between runs inside a workflow artifact

Same SQL, same code path, chosen from ``DATABASE_URL``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .logging_utils import get_logger
from .models import AlertRecord, iso, parse_iso, utcnow

log = get_logger(__name__)


SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS subjects (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL,
    name TEXT NOT NULL, url TEXT DEFAULT '', active INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 100, meta TEXT DEFAULT '{}',
    min_interval_minutes INTEGER DEFAULT 0,
    first_seen TEXT, last_seen TEXT, last_read TEXT
);

-- one row per (subject, watched key): what we saw last time
CREATE TABLE IF NOT EXISTS watch_state (
    subject_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    label TEXT DEFAULT '', detail TEXT DEFAULT '', url TEXT DEFAULT '',
    first_seen TEXT, updated_at TEXT,
    PRIMARY KEY (subject_id, key)
);

-- append-only log of every detected change
CREATE TABLE IF NOT EXISTS watch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL, key TEXT NOT NULL,
    old_value TEXT DEFAULT '', new_value TEXT NOT NULL,
    label TEXT DEFAULT '', detail TEXT DEFAULT '', url TEXT DEFAULT '',
    detected_at TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS watch_events_idx ON watch_events (subject_id, detected_at DESC);

-- history of numeric watch values (commit rates etc), for the baseline engine
CREATE TABLE IF NOT EXISTS subject_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL, key TEXT NOT NULL, ts TEXT NOT NULL,
    value REAL NOT NULL, local_day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subject_obs_idx ON subject_observations (subject_id, key, ts DESC);

-- what we notified about, for de-duplication
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, venue_id TEXT NOT NULL, kind TEXT NOT NULL,
    load_score REAL NOT NULL DEFAULT 0, baseline_score REAL NOT NULL DEFAULT 0,
    deviation_percent REAL NOT NULL DEFAULT 0, metric_type TEXT NOT NULL,
    sent_at TEXT NOT NULL, message_hash TEXT NOT NULL DEFAULT '',
    delivered INTEGER NOT NULL DEFAULT 1, direction TEXT NOT NULL DEFAULT 'high'
);
CREATE INDEX IF NOT EXISTS alerts_venue_idx ON alerts (venue_id, kind, sent_at DESC);

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

    def ts(self, value: Optional[datetime]) -> Any:
        """Backend representation of a timestamp (ISO text vs native)."""
        return iso(value)

    # -- subjects ---------------------------------------------------------- #

    SUBJECT_COLUMNS = (
        "id, kind, external_id, name, url, active, priority, meta, "
        "min_interval_minutes, first_seen, last_seen, last_read"
    )

    #: columns added after the first release; applied idempotently on migrate
    ADDED_COLUMNS = (
        ("subjects", "min_interval_minutes", "INTEGER DEFAULT 0"),
        ("subjects", "last_read", "TEXT"),
    )

    def upsert_subjects(self, subjects: Sequence[Any]) -> Dict[str, int]:
        if not subjects:
            return {"inserted": 0, "updated": 0}
        existing = {row[0] for row in self.fetchall("SELECT id FROM subjects")}
        now = utcnow()
        inserted = updated = 0
        for subject in subjects:
            common = (
                subject.kind,
                subject.external_id,
                subject.name,
                subject.url,
                self.bool_true if subject.active else self.bool_false,
                int(subject.priority),
                json.dumps(subject.meta, ensure_ascii=False),
                int(subject.min_interval_minutes),
            )
            if subject.id in existing:
                self.execute(
                    "UPDATE subjects SET kind=?, external_id=?, name=?, url=?, active=?, "
                    "priority=?, meta=?, min_interval_minutes=?, last_seen=? WHERE id=?",
                    common + (self.ts(now), subject.id),
                )
                updated += 1
            else:
                self.execute(
                    "INSERT INTO subjects ({}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)".format(
                        self.SUBJECT_COLUMNS
                    ),
                    (subject.id,)
                    + common
                    + (self.ts(subject.first_seen or now), self.ts(now), None),
                )
                inserted += 1
        self.commit()
        return {"inserted": inserted, "updated": updated}

    def list_subjects(self, active_only: bool = True) -> List[Any]:
        from .subjects import Subject

        sql = "SELECT {} FROM subjects".format(self.SUBJECT_COLUMNS)
        params: List[Any] = []
        if active_only:
            sql += " WHERE active = ?"
            params.append(self.bool_true)
        sql += " ORDER BY priority ASC, name ASC"
        return [
            Subject(
                id=row[0],
                kind=row[1],
                external_id=row[2],
                name=row[3],
                url=row[4] or "",
                active=bool(row[5]),
                priority=int(row[6] or 100),
                meta=json.loads(row[7] or "{}"),
                min_interval_minutes=int(row[8] or 0),
                first_seen=parse_iso(row[9]),
                last_seen=parse_iso(row[10]),
                last_read=parse_iso(row[11]),
            )
            for row in self.fetchall(sql, params)
        ]

    def mark_subject_read(self, subject_id: str, moment: Optional[datetime] = None) -> None:
        self.execute(
            "UPDATE subjects SET last_read = ? WHERE id = ?",
            (self.ts(moment or utcnow()), subject_id),
        )
        self.commit()

    # -- watch state ------------------------------------------------------- #

    def get_watch_value(self, subject_id: str, key: str) -> Optional[Dict[str, Any]]:
        row = self.fetchone(
            "SELECT subject_id, key, value, label, detail, url, first_seen, updated_at "
            "FROM watch_state WHERE subject_id = ? AND key = ?",
            (subject_id, key),
        )
        if not row:
            return None
        return {
            "subject_id": row[0],
            "key": row[1],
            "value": row[2],
            "label": row[3] or "",
            "detail": row[4] or "",
            "url": row[5] or "",
            "first_seen": parse_iso(row[6]),
            "updated_at": parse_iso(row[7]),
        }

    def set_watch_value(self, value: Any) -> None:
        previous = self.get_watch_value(value.subject_id, value.key)
        self.execute(
            "DELETE FROM watch_state WHERE subject_id = ? AND key = ?",
            (value.subject_id, value.key),
        )
        self.execute(
            "INSERT INTO watch_state (subject_id, key, value, label, detail, url, first_seen, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value.subject_id,
                value.key,
                str(value.value),
                value.label,
                value.detail,
                value.url,
                self.ts((previous or {}).get("first_seen") or value.observed_at or utcnow()),
                self.ts(value.observed_at or utcnow()),
            ),
        )
        self.commit()

    def record_watch_event(self, event: Any, notified: bool = False) -> None:
        self.execute(
            "INSERT INTO watch_events (subject_id, key, old_value, new_value, label, detail, "
            "url, detected_at, notified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.subject.id,
                event.key,
                event.old_value,
                event.new_value,
                event.label,
                event.detail,
                event.url,
                self.ts(event.detected_at or utcnow()),
                self.bool_true if notified else self.bool_false,
            ),
        )
        self.commit()

    def count_watch_events(self) -> int:
        row = self.fetchone("SELECT COUNT(*) FROM watch_events")
        return int(row[0]) if row else 0

    def recent_watch_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.fetchall(
            "SELECT subject_id, key, old_value, new_value, label, detected_at "
            "FROM watch_events ORDER BY detected_at DESC LIMIT {}".format(int(limit))
        )
        return [
            {
                "subject_id": r[0],
                "key": r[1],
                "old_value": r[2],
                "new_value": r[3],
                "label": r[4],
                "detected_at": parse_iso(r[5]),
            }
            for r in rows
        ]

    # -- numeric history --------------------------------------------------- #

    def record_subject_value(
        self, subject_id: str, key: str, value: float, moment: datetime, local_day: str
    ) -> None:
        self.execute(
            "INSERT INTO subject_observations (subject_id, key, ts, value, local_day) "
            "VALUES (?, ?, ?, ?, ?)",
            (subject_id, key, self.ts(moment), float(value), local_day),
        )
        self.commit()

    def daily_series(
        self,
        subject_id: str,
        key: str,
        lookback_days: int = 28,
        exclude_day: Optional[str] = None,
    ) -> List[float]:
        """One value per local day -- that day's peak reading.

        A 24-hour rolling count sampled several times a day would otherwise put
        the same event into the baseline repeatedly and flatten it.
        """
        cutoff = utcnow() - timedelta(days=max(1, lookback_days))
        rows = self.fetchall(
            "SELECT local_day, MAX(value) FROM subject_observations "
            "WHERE subject_id = ? AND key = ? AND ts >= ? GROUP BY local_day ORDER BY local_day",
            (subject_id, key, self.ts(cutoff)),
        )
        return [float(r[1]) for r in rows if exclude_day is None or r[0] != exclude_day]

    def purge_subject_observations(self, keep_days: int) -> int:
        cutoff = utcnow() - timedelta(days=max(1, keep_days))
        cur = self.execute("DELETE FROM subject_observations WHERE ts < ?", (self.ts(cutoff),))
        count = cur.rowcount or 0
        cur.close()
        self.commit()
        return count

    # -- alerts ------------------------------------------------------------ #

    def record_alert(self, alert: AlertRecord) -> None:
        self.execute(
            "INSERT INTO alerts (venue_id, kind, load_score, baseline_score, deviation_percent, "
            "metric_type, sent_at, message_hash, delivered, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                alert.direction,
            ),
        )
        self.commit()

    def last_alert(self, venue_id: str, kind: Optional[str] = None) -> Optional[AlertRecord]:
        sql = (
            "SELECT id, venue_id, kind, load_score, baseline_score, deviation_percent, "
            "metric_type, sent_at, message_hash, delivered, direction FROM alerts WHERE venue_id = ?"
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
            direction=row[10] or "high",
        )

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

    def migrate(self) -> None:
        self.conn.executescript(SQLITE_DDL)
        # bring databases created by an older revision up to date
        for table, column, definition in self.ADDED_COLUMNS:
            existing = {row[1] for row in self.fetchall("PRAGMA table_info({})".format(table))}
            if column not in existing:
                self.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))
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
    """Build the right backend for a ``DATABASE_URL``."""
    url = (database_url or "").strip()
    if not url:
        raise StorageError("DATABASE_URL is empty")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return PostgresStorage(url.replace("postgresql+psycopg://", "postgresql://"))

    if scheme == "sqlite":
        remainder = url[len("sqlite://") :]
        if remainder in {":memory:", "/:memory:"}:
            return SQLiteStorage(":memory:")
        path = remainder.lstrip("/") if not remainder.startswith("//") else remainder[1:]
        return SQLiteStorage(path or "data/watch.db")

    raise StorageError(
        "unsupported DATABASE_URL scheme {!r}; use postgresql:// or sqlite://".format(scheme)
    )
