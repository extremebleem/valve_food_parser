"""CS2-focused providers and the signals they produce."""

from __future__ import annotations

import pytest

from src.providers.cs2 import (
    CS2ServerStatusProvider,
    SteamGCVersionProvider,
    SteamPlayerCountProvider,
    SteamSDRProvider,
)
from src.subjects import Subject, WatchEvent, default_subjects
from src.watch_telegram import WatchNotifier
from src.watcher import CHANGE_KEYS, DELTA_KEYS


def cs2():
    return Subject.steam_app(
        730, "Counter-Strike 2", meta={"watch_sdr": True, "watch_players": True}
    )


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text, silent=None):
        self.messages.append(text)
        return True


# --------------------------------------------------------------------------- #
# Steam Datagram Relay
# --------------------------------------------------------------------------- #


SDR_PAYLOAD = {
    "success": True,
    "revision": 1787769460,
    "pops": {"ams": {}, "sea": {}, "eat": {}, "fra": {}},
    "certs": [1, 2],
}


def test_sdr_reports_revision_and_datacentre_set(settings, monkeypatch):
    provider = SteamSDRProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: SDR_PAYLOAD)
    values = {v.key: v for v in provider.read(cs2())}
    assert values["sdr_revision"].value == "1787769460"
    # the revision is a unix timestamp of the last network change
    assert "2026-08-26" in values["sdr_revision"].label
    assert values["sdr_pops"].label == "4 релейных дата-центров"


def test_sdr_pops_digest_ignores_ordering_but_not_membership(settings, monkeypatch):
    provider = SteamSDRProvider(settings)
    reordered = dict(SDR_PAYLOAD, pops={"sea": {}, "fra": {}, "ams": {}, "eat": {}})
    added = dict(SDR_PAYLOAD, pops=dict(SDR_PAYLOAD["pops"], tyo={}))

    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: SDR_PAYLOAD)
    first = {v.key: v.value for v in provider.read(cs2())}
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: reordered)
    same = {v.key: v.value for v in provider.read(cs2())}
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: added)
    different = {v.key: v.value for v in provider.read(cs2())}

    assert first["sdr_pops"] == same["sdr_pops"]
    assert first["sdr_pops"] != different["sdr_pops"]


@pytest.mark.parametrize("bad", [None, [], "", {}, {"success": False}, {"success": True}])
def test_sdr_tolerates_malformed_payloads(settings, bad, monkeypatch):
    provider = SteamSDRProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: bad)
    assert provider.read(cs2()) == []


def test_sdr_only_runs_for_flagged_subjects(settings):
    provider = SteamSDRProvider(settings)
    assert provider.supports(cs2()) is True
    assert provider.supports(Subject.steam_app(570, "Dota 2")) is False


# --------------------------------------------------------------------------- #
# game coordinator versions
# --------------------------------------------------------------------------- #


def gc_subject():
    return Subject.steam_app(1422450, "Deadlock", meta={"watch_gc": True})


def test_gc_reports_a_rollout_in_flight(settings, monkeypatch):
    provider = SteamGCVersionProvider(settings)
    payload = {"result": {"success": True, "deploy_version": 6700, "active_version": 6689}}
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    values = {v.key: v for v in provider.read(gc_subject())}
    assert values["gc_deploy_in_flight"].value == "yes"
    assert "6700" in values["gc_deploy_in_flight"].label
    assert values["gc_active_version"].value == "6689"


def test_gc_reports_no_rollout_when_versions_match(settings, monkeypatch):
    provider = SteamGCVersionProvider(settings)
    payload = {"result": {"success": True, "deploy_version": 6689, "active_version": 6689}}
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    values = {v.key: v.value for v in provider.read(gc_subject())}
    assert values["gc_deploy_in_flight"] == "no"


def test_gc_is_not_watched_for_cs2(settings):
    """IGCVersion_730 answers with zeros, verified 2026-09-17."""
    provider = SteamGCVersionProvider(settings)
    assert provider.supports(Subject.steam_app(730, "CS2", meta={"watch_gc": True})) is False


def test_gc_ignores_an_all_zero_response(settings, monkeypatch):
    provider = SteamGCVersionProvider(settings)
    payload = {"result": {"success": True, "deploy_version": 0, "active_version": 0}}
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: payload)
    assert provider.read(gc_subject()) == []


@pytest.mark.parametrize("bad", [None, [], "", {}, {"result": None}, {"result": {"success": False}}])
def test_gc_tolerates_malformed_payloads(settings, bad, monkeypatch):
    provider = SteamGCVersionProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: bad)
    assert provider.read(gc_subject()) == []


# --------------------------------------------------------------------------- #
# CS2 server status (needs a free key)
# --------------------------------------------------------------------------- #


STATUS_PAYLOAD = {
    "result": {
        "app": {"version": 14181},
        "services": {"SessionsLogon": "normal", "IEconItems": "normal"},
        "matchmaking": {
            "scheduler": "normal",
            "online_players": 900000,
            "searching_players": 12000,
            "search_seconds_avg": 40,
            "online_servers": 5000,
        },
    }
}


def test_cs2_status_is_dormant_without_a_key(settings, monkeypatch):
    monkeypatch.delenv("STEAM_WEB_API_KEY", raising=False)
    provider = CS2ServerStatusProvider(settings)
    assert provider.enabled is False
    assert provider.supports(cs2()) is False


def test_cs2_status_extracts_every_signal(settings, monkeypatch):
    monkeypatch.setenv("STEAM_WEB_API_KEY", "abc")
    provider = CS2ServerStatusProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: STATUS_PAYLOAD)
    values = {v.key: v for v in provider.read(cs2())}
    assert values["cs2_app_version"].value == "14181"
    assert values["cs2_scheduler"].value == "normal"
    assert values["cs2_online_players"].value == "900000"
    assert values["cs2_search_seconds_avg"].value == "40"
    assert "SessionsLogon: normal" in values["cs2_services"].label


def test_cs2_status_reports_service_state_without_grading_it(settings, monkeypatch):
    """Valve returns IEconItems=offline and Leaderboards=idle as steady values.
    Calling everything that is not "normal" a problem made the label
    permanently wrong -- observed on the first live run."""
    monkeypatch.setenv("STEAM_WEB_API_KEY", "abc")
    provider = CS2ServerStatusProvider(settings)
    live = {
        "result": dict(
            STATUS_PAYLOAD["result"],
            services={
                "IEconItems": "offline",
                "Leaderboards": "idle",
                "SessionsLogon": "normal",
                "SteamCommunity": "normal",
            },
        )
    }
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: live)
    services = {v.key: v for v in provider.read(cs2())}["cs2_services"]
    assert "проблем" not in services.label.lower()
    assert "IEconItems: offline" in services.label
    # the stored value must be complete and stable in ordering
    assert services.value.startswith("IEconItems=offline,Leaderboards=idle,")


def test_cs2_status_explains_a_rejected_key(settings, monkeypatch):
    from src.http import HttpError
    from src.providers.base import ProviderError

    monkeypatch.setenv("STEAM_WEB_API_KEY", "bad")
    provider = CS2ServerStatusProvider(settings)

    def boom(*a, **k):
        raise HttpError("forbidden", 403)

    monkeypatch.setattr(provider.client, "get_json", boom)
    with pytest.raises(ProviderError) as exc:
        provider.read(cs2())
    assert "steamcommunity.com/dev/apikey" in str(exc.value)


# --------------------------------------------------------------------------- #
# player counts
# --------------------------------------------------------------------------- #


def test_player_count_is_parsed(settings, monkeypatch):
    provider = SteamPlayerCountProvider(settings)
    monkeypatch.setattr(
        provider.client, "get_json", lambda *a, **k: {"response": {"player_count": 1186514, "result": 1}}
    )
    values = provider.read(cs2())
    assert values[0].key == "players_current"
    assert values[0].value == "1186514"


@pytest.mark.parametrize(
    "bad", [None, {}, {"response": {}}, {"response": {"result": 0, "player_count": 5}}]
)
def test_player_count_tolerates_malformed_payloads(settings, bad, monkeypatch):
    provider = SteamPlayerCountProvider(settings)
    monkeypatch.setattr(provider.client, "get_json", lambda *a, **k: bad)
    assert provider.read(cs2()) == []


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #


def test_cs2_is_the_top_priority_subject():
    assert sorted(default_subjects(), key=lambda s: s.priority)[0].external_id == "730"


def test_every_cs2_signal_is_classified():
    """A key that is neither a change nor a delta silently falls through to the
    rate engine, which is wrong for most of these."""
    for key in (
        "sdr_revision",
        "sdr_pops",
        "gc_active_version",
        "gc_deploy_in_flight",
        "cs2_app_version",
        "cs2_scheduler",
        "cs2_services",
    ):
        assert key in CHANGE_KEYS
    for key in ("players_current", "cs2_online_players", "cs2_online_servers"):
        assert key in DELTA_KEYS
    assert not (CHANGE_KEYS & DELTA_KEYS)


@pytest.mark.parametrize(
    "key,old,new,expected",
    [
        ("gc_deploy_in_flight", "no", "yes", "идёт прямо сейчас"),
        ("gc_deploy_in_flight", "yes", "no", "завершилась"),
        ("cs2_scheduler", "normal", "delayed", "delayed"),
        ("cs2_scheduler", "delayed", "normal", "вернулся в норму"),
        # the services title stays neutral: which service moved is in the body,
        # because "not normal" is a steady state for some of them
        ("cs2_services", "a=normal", "a=delayed", "сменили состояние"),
        ("cs2_services", "a=delayed", "a=normal", "сменили состояние"),
    ],
)
def test_title_depends_on_the_new_value(settings, storage, key, old, new, expected):
    """Titling a yes->no rollout transition as "a rollout is in flight" was a
    real bug: the direction of the change is the news."""
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    event = WatchEvent(Subject.steam_app(730, "CS2"), key, old, new)
    assert expected in notifier.title_for(event)


def test_a_finished_rollout_does_not_advertise_lead_time(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    done = WatchEvent(Subject.steam_app(1422450, "Deadlock"), "gc_deploy_in_flight", "yes", "no")
    started = WatchEvent(Subject.steam_app(1422450, "Deadlock"), "gc_deploy_in_flight", "no", "yes")
    assert "типичная фора" not in "\n".join(notifier.render_event(done))
    assert "типичная фора" in "\n".join(notifier.render_event(started))


def test_a_missing_player_counter_is_not_a_failure(settings, monkeypatch):
    """Deadlock answers 404 for GetNumberOfCurrentPlayers -- observed live."""
    from src.http import HttpError

    provider = SteamPlayerCountProvider(settings)

    def not_found(*a, **k):
        raise HttpError("missing", 404)

    monkeypatch.setattr(provider.client, "get_json", not_found)
    assert provider.read(Subject.steam_app(1422450, "Deadlock", meta={"watch_players": True})) == []


def test_a_real_player_counter_failure_still_raises(settings, monkeypatch):
    from src.http import HttpError
    from src.providers.base import ProviderError

    provider = SteamPlayerCountProvider(settings)

    def boom(*a, **k):
        raise HttpError("server error", 500)

    monkeypatch.setattr(provider.client, "get_json", boom)
    with pytest.raises(ProviderError):
        provider.read(cs2())


def test_service_change_lists_only_what_moved(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    old = "IEconItems=offline,Leaderboards=idle,SessionsLogon=normal"
    new = "IEconItems=offline,Leaderboards=idle,SessionsLogon=delayed"
    assert notifier.service_changes(old, new) == ["SessionsLogon: normal → delayed"]


def test_service_change_handles_added_and_removed_services(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    changes = notifier.service_changes("A=normal", "B=normal")
    assert changes == ["A: normal → —", "B: — → normal"]


@pytest.mark.parametrize("old,new", [("", ""), ("garbage", "garbage"), ("A=normal", "A=normal")])
def test_service_change_is_empty_when_nothing_moved(settings, storage, old, new):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    assert notifier.service_changes(old, new) == []


def test_service_event_message_shows_the_diff_not_the_whole_state(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    event = WatchEvent(
        Subject.steam_app(730, "CS2"),
        "cs2_services",
        "IEconItems=offline,SessionsLogon=normal",
        "IEconItems=offline,SessionsLogon=delayed",
        label="IEconItems: offline; SessionsLogon: delayed",
    )
    text = "\n".join(notifier.render_event(event))
    assert "SessionsLogon: normal → delayed" in text
    # the unchanged service must not be repeated as if it were news
    assert "IEconItems: offline; SessionsLogon" not in text
