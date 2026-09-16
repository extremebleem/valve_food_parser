"""Persistence: subjects, watch state, numeric history and alert de-duplication."""

from __future__ import annotations

import pytest

from src.models import AlertRecord, utcnow
from src.storage import PostgresStorage, SQLiteStorage, StorageError, create_storage
from src.subjects import Subject, WatchValue, default_subjects


def value(subject, key, val, **kw):
    return WatchValue(subject_id=subject.id, key=key, value=val, observed_at=utcnow(), **kw)


def test_create_storage_selects_the_backend():
    assert isinstance(create_storage("sqlite:///data/x.db"), SQLiteStorage)
    assert isinstance(create_storage("sqlite://:memory:"), SQLiteStorage)
    assert isinstance(create_storage("postgresql://u:p@host/db"), PostgresStorage)
    assert isinstance(create_storage("postgres://u:p@host/db"), PostgresStorage)
    with pytest.raises(StorageError):
        create_storage("mysql://host/db")
    with pytest.raises(StorageError):
        create_storage("")


def test_subject_upsert_is_idempotent(storage):
    subjects = default_subjects()
    assert storage.upsert_subjects(subjects)["inserted"] == len(subjects)
    assert storage.upsert_subjects(subjects) == {"inserted": 0, "updated": len(subjects)}
    assert len(storage.list_subjects()) == len(subjects)


def test_subjects_round_trip_and_sort_by_priority(storage):
    storage.upsert_subjects(
        [
            Subject.steam_app(730, "CS2", priority=50),
            Subject.steam_feed(1675200, "SteamOS", priority=5),
        ]
    )
    listed = storage.list_subjects()
    assert [s.priority for s in listed] == [5, 50]
    assert listed[0].external_id == "1675200"
    assert listed[0].url.startswith("https://")


def test_inactive_subjects_are_filtered(storage):
    storage.upsert_subjects([Subject.steam_app(730, "CS2", active=False)])
    assert storage.list_subjects(active_only=True) == []
    assert len(storage.list_subjects(active_only=False)) == 1


def test_watch_value_round_trip_and_first_seen_is_preserved(storage):
    subject = Subject.steam_app(730, "CS2")
    storage.upsert_subjects([subject])
    assert storage.get_watch_value(subject.id, "required_version") is None

    storage.set_watch_value(value(subject, "required_version", "14181", label="1.41.8.1"))
    first = storage.get_watch_value(subject.id, "required_version")
    assert first["value"] == "14181" and first["label"] == "1.41.8.1"

    storage.set_watch_value(value(subject, "required_version", "14205"))
    second = storage.get_watch_value(subject.id, "required_version")
    assert second["value"] == "14205"
    # first_seen must survive the update: it is when we started watching
    assert second["first_seen"] == first["first_seen"]


def test_watch_events_are_appended(storage):
    from src.subjects import WatchEvent

    subject = Subject.steam_app(730, "CS2")
    storage.upsert_subjects([subject])
    assert storage.count_watch_events() == 0
    for new in ("14190", "14205"):
        storage.record_watch_event(WatchEvent(subject, "required_version", "14181", new))
    assert storage.count_watch_events() == 2
    recent = storage.recent_watch_events()
    assert {r["new_value"] for r in recent} == {"14190", "14205"}


def test_daily_series_keeps_one_value_per_day(storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    now = utcnow()
    for v in (2, 7, 5):
        storage.record_subject_value(subject.id, "commits_24h", v, now, "2026-09-10")
    storage.record_subject_value(subject.id, "commits_24h", 4, now, "2026-09-11")
    assert sorted(storage.daily_series(subject.id, "commits_24h")) == [4.0, 7.0]


def test_daily_series_can_exclude_today(storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    now = utcnow()
    storage.record_subject_value(subject.id, "commits_24h", 3, now, "2026-09-10")
    storage.record_subject_value(subject.id, "commits_24h", 40, now, "2026-09-11")
    assert storage.daily_series(subject.id, "commits_24h", exclude_day="2026-09-11") == [3.0]


def test_numeric_history_can_be_purged(storage):
    from datetime import timedelta

    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    storage.record_subject_value(subject.id, "c", 1, utcnow() - timedelta(days=400), "2025-01-01")
    storage.record_subject_value(subject.id, "c", 2, utcnow(), "2026-09-17")
    assert storage.purge_subject_observations(120) == 1
    assert len(storage.daily_series(subject.id, "c", lookback_days=400)) == 1


def test_alert_history_supports_deduplication(storage):
    subject = Subject.steam_app(730, "CS2")
    storage.upsert_subjects([subject])
    assert storage.last_alert(subject.id) is None
    storage.record_alert(
        AlertRecord(subject.id, "watch", 0, 0, 0, "required_version", utcnow(), "hash-1")
    )
    last = storage.last_alert(subject.id, "watch")
    assert last.message_hash == "hash-1"
    assert storage.last_alert(subject.id, "other") is None


def test_inserting_nothing_is_a_no_op(storage):
    assert storage.upsert_subjects([]) == {"inserted": 0, "updated": 0}
