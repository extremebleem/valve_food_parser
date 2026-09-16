"""Change detection, the watch providers and the notification path."""

from __future__ import annotations


import pytest

from src.models import utcnow
from src.providers.github import GitHubProvider
from src.providers.steam import SteamNewsProvider, SteamVersionProvider
from src.subjects import Subject, SubjectKind, WatchEvent, WatchValue, default_subjects
from src.watch_telegram import WatchNotifier, event_hash
from src.watcher import CHANGE_KEYS, Watcher


class StubProvider:
    """Replays canned values; never touches the network."""

    name = "stub"

    def __init__(self, values_by_subject, fail_for=(), crash_for=()):
        self.values_by_subject = values_by_subject
        self.fail_for = set(fail_for)
        self.crash_for = set(crash_for)

    def supports(self, subject):
        return True

    def read(self, subject):
        from src.providers.base import ProviderError

        if subject.name in self.crash_for:
            raise RuntimeError("provider bug")
        if subject.name in self.fail_for:
            raise ProviderError("upstream down")
        return list(self.values_by_subject.get(subject.id, []))


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def value(subject, key, val, label="", detail="", url=""):
    return WatchValue(
        subject_id=subject.id, key=key, value=val, label=label, detail=detail,
        url=url, observed_at=utcnow(),
    )


def make_watcher(settings, storage, values, client=None, **kw):
    notifier = WatchNotifier(settings, storage, client=client or RecordingClient())
    return Watcher(settings, storage, [StubProvider(values, **kw)], notifier=notifier)


# --------------------------------------------------------------------------- #
# subjects
# --------------------------------------------------------------------------- #


def test_watch_list_is_not_empty_and_has_no_duplicate_ids():
    subjects = default_subjects()
    assert len(subjects) >= 10
    assert len({s.id for s in subjects}) == len(subjects)


def test_priority_order_puts_cs2_first_then_the_beta_channel():
    """CS2 is the focus; the SteamOS preview feed is the longest-lead signal."""
    order = [s.external_id for s in sorted(default_subjects(), key=lambda s: s.priority)]
    assert order[0] == "730"
    assert order[1] == "1675200"


def test_appid_753_is_not_watched():
    """Its feed returns only syndicated PCGamesN articles, verified 2026-09-17."""
    assert all(
        not (s.kind == SubjectKind.STEAM_FEED and s.external_id == "753")
        for s in default_subjects()
    )


def test_subject_ids_are_stable():
    assert Subject.steam_app(730, "x").id == Subject.steam_app(730, "renamed").id
    assert Subject.github_repo("ValveSoftware/Proton").id != Subject.steam_app(730, "x").id


# --------------------------------------------------------------------------- #
# change detection
# --------------------------------------------------------------------------- #


def test_first_run_records_state_and_sends_nothing(settings, storage):
    """Otherwise the very first run fires one alert per watched key."""
    subject = default_subjects()[0]
    client = RecordingClient()
    watcher = make_watcher(
        settings, storage, {subject.id: [value(subject, "required_version", "100")]}, client
    )
    stats = watcher.run()
    assert stats.changes == 0
    assert stats.changes_first_seen >= 1
    assert stats.alerts_sent == 0
    assert client.messages == []


def test_an_unchanged_value_is_silent(settings, storage):
    subject = default_subjects()[0]
    values = {subject.id: [value(subject, "required_version", "100")]}
    client = RecordingClient()
    make_watcher(settings, storage, values, client).run()
    client.messages.clear()

    stats = make_watcher(settings, storage, values, client).run()
    assert stats.changes == 0
    assert client.messages == []


def test_a_changed_value_alerts(settings, storage):
    subject = default_subjects()[0]
    client = RecordingClient()
    make_watcher(
        settings, storage, {subject.id: [value(subject, "required_version", "100")]}, client
    ).run()
    client.messages.clear()

    stats = make_watcher(
        settings,
        storage,
        {subject.id: [value(subject, "required_version", "101", label="1.2.3")]},
        client,
    ).run()
    assert stats.changes == 1
    assert stats.alerts_sent == 1
    assert len(client.messages) == 1
    assert "100 → 101" in client.messages[0]


def test_the_same_change_is_not_announced_twice(settings, storage):
    """A repeated run must not re-announce a value we already reported."""
    subject = default_subjects()[0]
    client = RecordingClient()
    make_watcher(settings, storage, {subject.id: [value(subject, "required_version", "100")]}, client).run()
    changed = {subject.id: [value(subject, "required_version", "101")]}
    make_watcher(settings, storage, changed, client).run()
    client.messages.clear()

    # storage now holds 101, so nothing changes; but force the event again
    event = WatchEvent(subject, "required_version", "100", "101", detected_at=utcnow())
    notifier = WatchNotifier(settings, storage, client=client)
    assert notifier.notify([event], []) == 0
    assert client.messages == []


def test_every_change_key_is_known_to_the_notifier():
    from src.watch_telegram import KEY_TITLES, PRIORITY

    for key in CHANGE_KEYS:
        assert key in KEY_TITLES
        assert key in PRIORITY


def test_a_crashing_provider_does_not_abort_the_run(settings, storage):
    subjects = default_subjects()
    watcher = make_watcher(settings, storage, {}, crash_for={subjects[0].name})
    stats = watcher.run()
    assert stats.subjects_failed == 1
    assert stats.subjects_read == len(subjects) - 1


def test_a_failing_provider_is_counted_not_fatal(settings, storage):
    subjects = default_subjects()
    stats = make_watcher(settings, storage, {}, fail_for={subjects[1].name}).run()
    assert stats.subjects_failed == 1


# --------------------------------------------------------------------------- #
# rate anomalies
# --------------------------------------------------------------------------- #


def test_a_commit_burst_is_flagged_only_against_enough_history(settings, storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    now = utcnow()

    # a quiet history of 3-4 commits a day
    for day in range(1, 15):
        storage.record_subject_value(
            subject.id, "commits_24h", 3 + (day % 2), now, "2026-09-{:02d}".format(day)
        )

    quiet = watcher.check_rate(subject, value(subject, "commits_24h", "5"), now)
    assert quiet is None

    burst = watcher.check_rate(subject, value(subject, "commits_24h", "31"), now)
    assert burst is not None
    assert burst["current"] == 31.0
    assert burst["median"] <= 5


def test_a_rate_without_history_never_alerts(settings, storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    assert watcher.check_rate(subject, value(subject, "commits_24h", "99"), utcnow()) is None


def test_daily_series_takes_one_value_per_day(storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    now = utcnow()
    for v in (2, 7, 5):
        storage.record_subject_value(subject.id, "commits_24h", v, now, "2026-09-10")
    storage.record_subject_value(subject.id, "commits_24h", 4, now, "2026-09-11")
    assert sorted(storage.daily_series(subject.id, "commits_24h")) == [4.0, 7.0]
    assert storage.daily_series(subject.id, "commits_24h", exclude_day="2026-09-11") == [7.0]


def test_a_non_numeric_rate_is_ignored(settings, storage):
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    assert watcher.check_rate(subject, value(subject, "commits_24h", "n/a"), utcnow()) is None


# --------------------------------------------------------------------------- #
# provider parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [None, [], "", 0, {}, {"response": None}, {"response": {}}])
def test_steam_version_tolerates_malformed_payloads(settings, bad, monkeypatch):
    provider = SteamVersionProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: bad)
    assert provider.read(Subject.steam_app(730, "CS2")) == []


def test_steam_version_parses_a_real_payload(settings, monkeypatch):
    payload = {
        "response": {
            "success": True,
            "up_to_date": False,
            "required_version": 14181,
            "message": "Server version required: 1.41.8.1",
        }
    }
    provider = SteamVersionProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    values = provider.read(Subject.steam_app(730, "CS2"))
    assert len(values) == 1
    assert values[0].value == "14181"
    assert "1.41.8.1" in values[0].label
    assert values[0].detail == ""  # must not repeat the label


def test_steam_version_ignores_an_unsuccessful_lookup(settings, monkeypatch):
    provider = SteamVersionProvider(settings)
    monkeypatch.setattr(
        provider.client, "get_json", lambda *a, **k: {"response": {"success": False}}
    )
    assert provider.read(Subject.steam_app(1422450, "Deadlock")) == []


def test_steam_news_keeps_only_official_posts(settings, monkeypatch):
    """appid 753 returns nothing but syndicated PCGamesN articles."""
    payload = {
        "appnews": {
            "newsitems": [
                {"gid": "1", "title": "Steam rumour", "feedname": "PCGamesN", "date": 200},
                {
                    "gid": "2",
                    "title": "SteamOS 3.9.1 Preview",
                    "feedname": "steam_community_announcements",
                    "date": 100,
                },
            ]
        }
    }
    provider = SteamNewsProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    values = provider.read(Subject.steam_feed(1675200, "SteamOS"))
    assert {v.value for v in values} == {"2"}
    assert any(v.key == "latest_prerelease" for v in values)


def test_steam_news_separates_prerelease_from_newest(settings, monkeypatch):
    payload = {
        "appnews": {
            "newsitems": [
                {
                    "gid": "new",
                    "title": "Counter-Strike 2 Update",
                    "feedname": "steam_community_announcements",
                    "date": 300,
                },
                {
                    "gid": "beta",
                    "title": "Steam Beta Client Update",
                    "feedname": "steam_community_announcements",
                    "date": 200,
                },
            ]
        }
    }
    provider = SteamNewsProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    values = {v.key: v.value for v in provider.read(Subject.steam_feed(730, "CS2"))}
    assert values["latest_news"] == "new"
    assert values["latest_prerelease"] == "beta"


@pytest.mark.parametrize(
    "bad", [None, [], "", {}, {"appnews": None}, {"appnews": {"newsitems": "x"}}, {"appnews": {}}]
)
def test_steam_news_tolerates_malformed_payloads(settings, bad, monkeypatch):
    provider = SteamNewsProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: bad)
    assert provider.read(Subject.steam_feed(730, "CS2")) == []


def test_github_does_not_watch_tags(settings):
    """/tags has no documented ordering and returned a stale tag for Proton,
    which would produce false change alerts."""
    from src.providers.github import GITHUB_CHANGE_KEYS

    assert "latest_tag" not in GITHUB_CHANGE_KEYS
    assert "latest_release" in GITHUB_CHANGE_KEYS


def test_github_supports_only_repo_subjects(settings):
    provider = GitHubProvider(settings)
    assert provider.supports(Subject.github_repo("ValveSoftware/Proton")) is True
    assert provider.supports(Subject.steam_app(730, "CS2")) is False


def test_event_hash_separates_subjects_keys_and_values():
    a = Subject.steam_app(730, "CS2")
    b = Subject.steam_app(570, "Dota")
    mk = lambda s, k, v: WatchEvent(s, k, "old", v)  # noqa: E731
    assert event_hash(mk(a, "required_version", "1")) != event_hash(mk(a, "required_version", "2"))
    assert event_hash(mk(a, "required_version", "1")) != event_hash(mk(b, "required_version", "1"))
    assert event_hash(mk(a, "required_version", "1")) != event_hash(mk(a, "latest_news", "1"))


# --------------------------------------------------------------------------- #
# message
# --------------------------------------------------------------------------- #


def test_message_leads_with_the_prerelease_signal(settings, storage):
    subject_beta = Subject.steam_feed(1675200, "SteamOS")
    subject_cs = Subject.steam_app(730, "Counter-Strike 2")
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    text = notifier.build(
        [
            WatchEvent(subject_cs, "required_version", "1", "2", label="1.41.9.0"),
            WatchEvent(subject_beta, "latest_prerelease", "a", "b", label="SteamOS 3.9.2 Preview"),
        ],
        [],
    )
    assert text.index("SteamOS 3.9.2 Preview") < text.index("Counter-Strike 2")
    assert "дни–недели" in text
    assert "не инсайд" in text


def test_message_escapes_html(settings, storage):
    subject = Subject.steam_app(730, "<b>CS2</b>")
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    text = notifier.build([WatchEvent(subject, "latest_news", "a", "b", label="<script>")], [])
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# --------------------------------------------------------------------------- #
# sharp moves on constantly-changing counters
# --------------------------------------------------------------------------- #


def test_a_sharp_drop_is_flagged(settings, storage):
    """A player-count collapse is what a server restart looks like."""
    subject = Subject.steam_app(730, "CS2", meta={"watch_players": True})
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})

    assert watcher.check_delta(subject, value(subject, "players_current", "1450000")) is None
    hit = watcher.check_delta(subject, value(subject, "players_current", "1186514"))
    assert hit is not None
    assert hit["change"] < -0.15
    assert hit["previous"] == 1450000.0


def test_a_small_move_is_not_flagged(settings, storage):
    subject = Subject.steam_app(730, "CS2", meta={"watch_players": True})
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    watcher.check_delta(subject, value(subject, "players_current", "1000000"))
    assert watcher.check_delta(subject, value(subject, "players_current", "1050000")) is None


def test_a_sharp_rise_is_flagged_too(settings, storage):
    subject = Subject.steam_app(730, "CS2", meta={"watch_players": True})
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    watcher.check_delta(subject, value(subject, "players_current", "1000000"))
    hit = watcher.check_delta(subject, value(subject, "players_current", "1400000"))
    assert hit is not None and hit["change"] > 0.15


def test_tiny_counters_are_ignored(settings, storage):
    """Relative moves on small numbers are noise: 3 -> 6 is +100% and means
    nothing."""
    subject = Subject.github_repo("ValveSoftware/gamescope")
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    watcher.check_delta(subject, value(subject, "cs2_online_servers", "3"))
    assert watcher.check_delta(subject, value(subject, "cs2_online_servers", "6")) is None


def test_delta_needs_a_previous_reading(settings, storage):
    subject = Subject.steam_app(730, "CS2", meta={"watch_players": True})
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    assert watcher.check_delta(subject, value(subject, "players_current", "999999")) is None


def test_a_non_numeric_delta_is_ignored(settings, storage):
    subject = Subject.steam_app(730, "CS2", meta={"watch_players": True})
    storage.upsert_subjects([subject])
    watcher = make_watcher(settings, storage, {})
    assert watcher.check_delta(subject, value(subject, "players_current", "n/a")) is None
