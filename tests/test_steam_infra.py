"""Steam plumbing signals, and the "servers are straining" correlation."""

from __future__ import annotations

import dataclasses

import pytest

from src.providers.steam_infra import SteamInfraProvider
from src.subjects import Subject, SubjectKind, WatchEvent, default_subjects
from src.watch_telegram import WatchNotifier
from src.watcher import CHANGE_KEYS, DELTA_KEYS, DELTA_RULES, HEALTH_KEYS


def infra():
    return Subject.steam_infra("Инфраструктура Steam")


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text, silent=None):
        self.messages.append(text)
        return True


SERVERS = {
    "response": {
        "servers": [
            {"type": "CDN", "host": "fastly.cdn.steampipe.steamcontent.com", "load": 0},
            {"type": "CDN", "host": "alibaba.cdn.steampipe.steamcontent.com", "load": 0},
            {"type": "SteamCache", "host": "cache1-sto2.steamcontent.com", "load": 77},
            {"type": "SteamCache", "host": "cache2-sto2.steamcontent.com", "load": 80},
        ]
    }
}


def stub(provider, **by_url):
    def fake(url, *a, **k):
        for fragment, payload in by_url.items():
            if fragment in url:
                return payload
        return {}

    provider.client.get_json = fake


def test_load_uses_valves_own_caches_not_the_partner_cdns(settings):
    """Partner CDNs report load 0; including them would halve the number and
    hide a real spike. Verified against the live endpoint 2026-09-17."""
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=SERVERS)
    values = {v.key: v for v in provider.read(infra())}
    assert values["steampipe_load_max"].value == "80"
    assert "пик 80%" in values["steampipe_load_max"].label
    assert "средняя 78%" in values["steampipe_load_max"].label


def test_host_digest_tracks_membership_not_order(settings):
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=SERVERS)
    first = {v.key: v.value for v in provider.read(infra())}["steampipe_hosts"]

    reordered = {"response": {"servers": list(reversed(SERVERS["response"]["servers"]))}}
    stub(provider, GetServersForSteamPipe=reordered)
    assert {v.key: v.value for v in provider.read(infra())}["steampipe_hosts"] == first

    dropped = {"response": {"servers": SERVERS["response"]["servers"][:3]}}
    stub(provider, GetServersForSteamPipe=dropped)
    assert {v.key: v.value for v in provider.read(infra())}["steampipe_hosts"] != first


def test_no_load_value_when_every_server_reports_zero(settings):
    provider = SteamInfraProvider(settings)
    stub(
        provider,
        GetServersForSteamPipe={"response": {"servers": [{"host": "a.b.c", "load": 0}]}},
    )
    keys = {v.key for v in provider.read(infra())}
    assert "steampipe_hosts" in keys
    assert "steampipe_load_max" not in keys


def test_domains_and_client_update_hosts(settings):
    provider = SteamInfraProvider(settings)
    stub(
        provider,
        GetServersForSteamPipe=SERVERS,
        GetSteamPipeDomains={"response": {"domainlist": ["*.steamcontent.com", "cs.steampowered.com"]}},
        GetClientUpdateHosts={"response": {"hosts_kv": '"hosts" { "client-update.akamai.steamstatic.com" { } }'}},
    )
    values = {v.key: v for v in provider.read(infra())}
    assert values["steampipe_domains"].label == "2 доменов раздачи"
    assert "akamai" in values["client_update_hosts"].detail


@pytest.mark.parametrize("bad", [None, {}, {"response": None}, {"response": {"servers": "x"}}])
def test_malformed_server_payloads_are_survivable(settings, bad):
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=bad)
    assert provider.read(infra()) == []


def test_a_broken_secondary_endpoint_does_not_lose_the_primary(settings):
    """GetSteamPipeDomains failing must not cost us the server list."""
    from src.http import HttpError

    provider = SteamInfraProvider(settings)

    def fake(url, *a, **k):
        if "GetServersForSteamPipe" in url:
            return SERVERS
        raise HttpError("boom", 500)

    provider.client.get_json = fake
    keys = {v.key for v in provider.read(infra())}
    assert "steampipe_hosts" in keys
    assert "steampipe_domains" not in keys


def test_infra_subject_is_in_the_watch_list():
    infra_subjects = [s for s in default_subjects() if s.kind == SubjectKind.STEAM_INFRA]
    assert len(infra_subjects) == 1


def test_provider_only_claims_infra_subjects(settings):
    provider = SteamInfraProvider(settings)
    assert provider.supports(infra()) is True
    assert provider.supports(Subject.steam_app(730, "CS2")) is False


# --------------------------------------------------------------------------- #
# thresholds and the strain correlation
# --------------------------------------------------------------------------- #


def test_every_infra_key_is_classified():
    for key in ("steampipe_hosts", "steampipe_domains", "client_update_hosts"):
        assert key in CHANGE_KEYS
    for key in ("steampipe_load_max", "cs2_search_seconds_avg"):
        assert key in DELTA_KEYS
    assert not (CHANGE_KEYS & DELTA_KEYS)


def test_thresholds_are_scaled_per_metric():
    """One global threshold cannot serve a million-player counter and a
    forty-second search time at the same time."""
    assert DELTA_RULES["cs2_search_seconds_avg"][1] < DELTA_RULES["players_current"][1]
    assert DELTA_RULES["cs2_search_seconds_avg"][0] > DELTA_RULES["players_current"][0]
    for key in DELTA_RULES:
        assert key in DELTA_KEYS


def test_search_time_uses_its_own_floor(settings, storage):
    """With the global 500 floor a search time of 40s could never fire."""
    from src.subjects import WatchValue
    from src.models import utcnow
    from src.watcher import Watcher

    subject = Subject.steam_app(730, "CS2")
    storage.upsert_subjects([subject])
    watcher = Watcher(settings, storage, [], notifier=None)
    mk = lambda v: WatchValue(subject.id, "cs2_search_seconds_avg", v, observed_at=utcnow())  # noqa: E731

    assert watcher.check_delta(subject, mk("38")) is None      # first reading
    assert watcher.check_delta(subject, mk("45")) is None      # +18%, under the 40% rule
    hit = watcher.check_delta(subject, mk("96"))
    assert hit is not None and hit["change"] > 1.0


def test_health_keys_are_the_strain_signals():
    for key in ("cs2_scheduler", "cs2_search_seconds_avg", "steampipe_load_max"):
        assert key in HEALTH_KEYS
    # a new preview post is news, not strain
    assert "latest_prerelease" not in HEALTH_KEYS


def test_strain_line_appears_only_when_several_signals_move(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    one = [{"subject": cs, "key": "steampipe_load_max", "current": 98, "previous": 79, "change": 0.24}]
    two = one + [
        {"subject": cs, "key": "cs2_search_seconds_avg", "current": 96, "previous": 38, "change": 1.5}
    ]
    assert "инфраструктурного напряжения" not in notifier.build([], [], one)
    assert "инфраструктурного напряжения" in notifier.build([], [], two)


def test_strain_counts_change_events_too(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    events = [WatchEvent(cs, "cs2_scheduler", "normal", "delayed")]
    deltas = [{"subject": cs, "key": "steampipe_load_max", "current": 98, "previous": 79, "change": 0.24}]
    assert notifier.health_signal_count(events, deltas) == 2
    assert "инфраструктурного напряжения" in notifier.build(events, [], deltas)


def test_strain_does_not_double_count_the_same_key(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    deltas = [
        {"subject": cs, "key": "steampipe_load_max", "current": 98, "previous": 79, "change": 0.24},
        {"subject": cs, "key": "steampipe_load_max", "current": 99, "previous": 80, "change": 0.24},
    ]
    assert notifier.health_signal_count([], deltas) == 1
