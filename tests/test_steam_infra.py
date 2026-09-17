"""Steam plumbing signals, and the "servers are straining" correlation."""

from __future__ import annotations


import pytest

from src.providers.steam_infra import SteamInfraProvider
from src.subjects import Subject, SubjectKind, WatchEvent, default_subjects
from src.watch_telegram import WatchNotifier
from src.watcher import (
    CHANGE_KEYS,
    DELTA_KEYS,
    DELTA_RULES,
    LOAD_DELTA_RULE,
    SET_KEYS,
    is_delta_key,
    is_health_key,
)


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
    for key in ("steampipe_domains", "client_update_hosts"):
        assert key in CHANGE_KEYS
        assert key in SET_KEYS
    assert is_delta_key("cs2_search_seconds_avg")
    assert is_delta_key("steampipe_load_fra1")
    assert not (CHANGE_KEYS & DELTA_KEYS)


def test_the_unstable_host_set_is_not_watched():
    """GetServersForSteamPipe ignores cell_id, answers by caller location and
    returns a different set on two back-to-back calls. It produced a false
    alert on its first day, so it is not a signal."""
    assert "steampipe_hosts" not in CHANGE_KEYS
    assert "steampipe_hosts" not in SET_KEYS
    assert not is_delta_key("steampipe_hosts")


def test_thresholds_are_scaled_per_metric():
    """One global threshold cannot serve a million-player counter and a
    forty-second search time at the same time."""
    assert DELTA_RULES["cs2_search_seconds_avg"][1] < DELTA_RULES["players_current"][1]
    assert DELTA_RULES["cs2_search_seconds_avg"][0] > DELTA_RULES["players_current"][0]
    for key in DELTA_RULES:
        assert is_delta_key(key)
    # regional load keys share one rule rather than needing an entry each
    assert LOAD_DELTA_RULE[0] > 0 and LOAD_DELTA_RULE[1] > 0


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
    for key in ("cs2_scheduler", "cs2_search_seconds_avg"):
        assert is_health_key(key)
    assert is_health_key("steampipe_load_iad")
    # a new preview post is news, not strain
    assert not is_health_key("latest_prerelease")


def test_strain_line_appears_only_when_several_signals_move(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    one = [{"subject": cs, "key": "steampipe_load_fra1", "current": 98, "previous": 79, "change": 0.24}]
    two = one + [
        {"subject": cs, "key": "cs2_search_seconds_avg", "current": 96, "previous": 38, "change": 1.5}
    ]
    assert "инфраструктурного напряжения" not in notifier.build([], [], one)
    assert "инфраструктурного напряжения" in notifier.build([], [], two)


def test_strain_counts_change_events_too(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    events = [WatchEvent(cs, "cs2_scheduler", "normal", "delayed")]
    deltas = [{"subject": cs, "key": "steampipe_load_fra1", "current": 98, "previous": 79, "change": 0.24}]
    assert notifier.health_signal_count(events, deltas) == 2
    assert "инфраструктурного напряжения" in notifier.build(events, [], deltas)


def test_strain_does_not_double_count_the_same_key(settings, storage):
    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    cs = Subject.steam_app(730, "Counter-Strike 2")
    deltas = [
        {"subject": cs, "key": "steampipe_load_fra1", "current": 98, "previous": 79, "change": 0.24},
        {"subject": cs, "key": "steampipe_load_fra1", "current": 99, "previous": 80, "change": 0.24},
    ]
    assert notifier.health_signal_count([], deltas) == 1


# --------------------------------------------------------------------------- #
# load, keyed per datacentre
# --------------------------------------------------------------------------- #


SERVERS_FRA = {
    "response": {
        "servers": [
            {"type": "CDN", "host": "fastly.cdn.steampipe.steamcontent.com", "load": 0},
            {"type": "SteamCache", "host": "cache1-fra1.steamcontent.com", "load": 30},
            {"type": "SteamCache", "host": "cache2-fra1.steamcontent.com", "load": 32},
        ]
    }
}
SERVERS_IAD = {
    "response": {
        "servers": [
            {"type": "SteamCache", "host": "cache1-iad.steamcontent.com", "load": 77},
            {"type": "SteamCache", "host": "cache2-iad.steamcontent.com", "load": 80},
        ]
    }
}


def test_load_is_keyed_by_datacentre(settings):
    """A run on a European runner and one on a US runner read different caches;
    a shared key would make that look like a collapse."""
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=SERVERS_FRA)
    fra = {v.key: v for v in provider.read(infra())}
    stub(provider, GetServersForSteamPipe=SERVERS_IAD)
    iad = {v.key: v for v in provider.read(infra())}

    assert "steampipe_load_fra1" in fra
    assert "steampipe_load_iad" in iad
    assert set(fra) & set(iad) == set(), "the two regions must not share a key"
    assert fra["steampipe_load_fra1"].value == "32"
    assert iad["steampipe_load_iad"].value == "80"


def test_partner_cdns_are_excluded_from_the_load(settings):
    """They report 0, and including them would halve the figure."""
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=SERVERS_FRA)
    load = {v.key: v for v in provider.read(infra())}["steampipe_load_fra1"]
    assert "пик 32%" in load.label
    assert "средняя 31%" in load.label


def test_no_load_value_when_every_node_reports_zero(settings):
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe={"response": {"servers": [{"host": "a-fra1.x", "load": 0}]}})
    assert [v for v in provider.read(infra()) if v.key.startswith("steampipe_load_")] == []


def test_region_falls_back_when_host_names_carry_none(settings):
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe={"response": {"servers": [{"host": "weird", "load": 40}]}})
    keys = {v.key for v in provider.read(infra())}
    assert "steampipe_load_unknown" in keys


@pytest.mark.parametrize("bad", [None, {}, {"response": None}, {"response": {"servers": "x"}}])
def test_malformed_server_payloads_are_survivable(settings, bad):
    provider = SteamInfraProvider(settings)
    stub(provider, GetServersForSteamPipe=bad)
    assert [v for v in provider.read(infra()) if v.key.startswith("steampipe_load_")] == []


# --------------------------------------------------------------------------- #
# sets are stored readable, so a change can be described
# --------------------------------------------------------------------------- #


def test_domains_are_stored_as_a_sorted_list(settings):
    provider = SteamInfraProvider(settings)
    stub(
        provider,
        GetServersForSteamPipe=SERVERS_FRA,
        GetSteamPipeDomains={"response": {"domainlist": ["b.example", "a.example"]}},
    )
    domains = {v.key: v for v in provider.read(infra())}["steampipe_domains"]
    assert domains.value == "a.example|b.example"


def test_a_set_change_names_what_moved(settings, storage):
    from src.watch_telegram import describe_set_change

    assert describe_set_change("a|b", "a|b|c") == ["+ c"]
    assert describe_set_change("a|b", "a") == ["− b"]
    assert describe_set_change("a|b", "a|c") == ["+ c", "− b"]
    assert describe_set_change("a", "a") == []
    assert describe_set_change("", "a") == ["+ a"]


def test_the_digest_to_list_upgrade_is_not_reported_as_a_change(settings, storage):
    """These keys used to hold a digest. Re-recording the readable form is not
    news, and reporting it would have fired one alert per key on upgrade."""
    from src.models import utcnow
    from src.subjects import WatchValue
    from src.watcher import Watcher

    subject = infra()
    storage.upsert_subjects([subject])
    storage.set_watch_value(
        WatchValue(subject.id, "steampipe_domains", "7792f31c9bc9705c", observed_at=utcnow())
    )
    watcher = Watcher(settings, storage, [], notifier=None)
    event = watcher.detect_change(
        subject, WatchValue(subject.id, "steampipe_domains", "a.example|b.example", observed_at=utcnow())
    )
    assert event is None
    # and the readable value is now what is stored
    assert storage.get_watch_value(subject.id, "steampipe_domains")["value"] == "a.example|b.example"
