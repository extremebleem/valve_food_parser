"""Depot build ids read through steamcmd, and per-subject polling intervals."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.models import utcnow
from src.providers.steam_depot import (
    SteamDepotProvider,
    describe_branch_change,
    parse_branches,
)
from src.subjects import Subject, default_subjects
from src.watch_telegram import WatchNotifier
from src.watcher import CHANGE_KEYS

# trimmed from a real `app_info_print 730` run, 2026-09-17
APP_INFO = '''
Connecting anonymously to Steam Public...OK
"730"
{
        "common" {  "name"  "Counter-Strike 2" }
        "depots"
        {
                "branches"
                {
                        "public"
                        {
                                "buildid"       "25218825"
                                "timeupdated"   "1789310940"
                        }
                        "csgo_legacy"
                        {
                                "buildid"       "12426195"
                                "description"   "Legacy Version of CS:GO"
                                "timeupdated"   "1697161380"
                        }
                        "1.41.7.4"
                        {
                                "buildid"       "24537688"
                                "description"   "1.41.7.4"
                                "timeupdated"   "1754255880"
                        }
                }
        }
}
'''


def cs2():
    return Subject.steam_app(730, "Counter-Strike 2", meta={"watch_depot": True})


class RecordingClient:
    def __init__(self):
        self.messages = []

    def send_message(self, text, silent=None):
        self.messages.append(text)
        return True


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_branches_are_parsed_from_real_output():
    branches = parse_branches(APP_INFO)
    assert set(branches) == {"public", "csgo_legacy", "1.41.7.4"}
    assert branches["public"]["buildid"] == "25218825"
    assert branches["1.41.7.4"]["description"] == "1.41.7.4"


@pytest.mark.parametrize(
    "text",
    ["", "no branches here", '"branches"', '"branches" {', "Connecting anonymously...FAILED"],
)
def test_parsing_survives_output_without_branches(text):
    assert parse_branches(text) == {}


def test_parser_stops_at_the_end_of_the_branches_block():
    """steamcmd prints its own log lines around the VDF; a greedy reader would
    swallow them."""
    noisy = APP_INFO + '\n"config"\n{\n    "installdir" "Counter-Strike Global Offensive"\n}\n'
    assert "installdir" not in str(parse_branches(noisy))


def test_provider_emits_build_id_and_branch_set(settings, monkeypatch):
    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/fake/steamcmd.sh")
    monkeypatch.setattr(provider, "app_info", lambda appid: APP_INFO)
    values = {v.key: v for v in provider.read(cs2())}
    assert values["depot_public_buildid"].value == "25218825"
    assert "25218825" in values["depot_public_buildid"].label
    # timeupdated is rendered so the message says when, not just what
    assert "2026-09-" in values["depot_public_buildid"].label
    assert values["depot_branches"].value == "1.41.7.4|csgo_legacy|public"


def test_branch_set_is_order_independent(settings, monkeypatch):
    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/fake/steamcmd.sh")
    monkeypatch.setattr(provider, "app_info", lambda appid: APP_INFO)
    first = {v.key: v.value for v in provider.read(cs2())}["depot_branches"]

    shuffled = APP_INFO.replace('"public"', '"zzz_public"').replace('"zzz_public"', '"public"')
    monkeypatch.setattr(provider, "app_info", lambda appid: shuffled)
    assert {v.key: v.value for v in provider.read(cs2())}["depot_branches"] == first


def test_no_values_when_output_has_no_branches(settings, monkeypatch):
    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/fake/steamcmd.sh")
    monkeypatch.setattr(provider, "app_info", lambda appid: "nothing useful")
    assert provider.read(cs2()) == []


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #


def test_provider_is_dormant_without_steamcmd(settings, monkeypatch):
    """Exercised through STEAMCMD_PATH rather than by patching the lookup, so
    the test covers the path an operator actually configures."""
    monkeypatch.setenv("STEAMCMD_PATH", "/definitely/not/here/steamcmd.sh")
    provider = SteamDepotProvider(settings)
    assert provider.steamcmd is None
    assert provider.enabled is False
    assert provider.supports(cs2()) is False


def test_an_explicit_steamcmd_path_is_honoured(settings, monkeypatch):
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix="steamcmd.sh") as fake:
        monkeypatch.setenv("STEAMCMD_PATH", fake.name)
        provider = SteamDepotProvider(settings)
        assert provider.steamcmd == fake.name
        assert provider.enabled is True
        assert os.path.exists(provider.steamcmd)


def test_provider_only_claims_flagged_subjects(settings, monkeypatch):
    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/fake/steamcmd.sh")
    assert provider.supports(cs2()) is True
    assert provider.supports(Subject.steam_app(730, "CS2")) is False
    assert provider.supports(Subject.github_repo("ValveSoftware/Proton")) is False


def test_a_steamcmd_failure_is_a_provider_error_not_a_crash(settings, monkeypatch):
    from src.providers.base import ProviderError

    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/definitely/not/here")
    with pytest.raises(ProviderError):
        provider.app_info("730")


# --------------------------------------------------------------------------- #
# branch diff in the message
# --------------------------------------------------------------------------- #


def test_branch_diff_names_what_appeared():
    """A new pinned version branch tends to precede the public push."""
    assert describe_branch_change("public|1.41.7.4", "public|1.41.7.4|1.41.8.2") == ["+ 1.41.8.2"]
    assert describe_branch_change("public|old", "public") == ["− old"]
    assert describe_branch_change("public", "public") == []
    assert describe_branch_change("", "public") == ["+ public"]


def test_depot_keys_are_change_signals_and_rank_first():
    from src.watch_telegram import KEY_TITLES, PRIORITY

    for key in ("depot_public_buildid", "depot_branches"):
        assert key in CHANGE_KEYS
        assert key in KEY_TITLES
    # nothing is earlier than the depot
    assert PRIORITY["depot_branches"] <= min(PRIORITY.values())


def test_branch_change_message_lists_the_new_branch(settings, storage):
    from src.subjects import WatchEvent

    notifier = WatchNotifier(settings, storage, client=RecordingClient())
    event = WatchEvent(
        Subject.steam_app(730, "CS2"), "depot_branches", "public|1.41.7.4", "public|1.41.7.4|1.41.8.2"
    )
    text = "\n".join(notifier.render_event(event))
    assert "+ 1.41.8.2" in text
    assert "1.41.7.4" not in text.split("+ 1.41.8.2")[1]  # unchanged ones are not repeated


# --------------------------------------------------------------------------- #
# per-subject polling intervals
# --------------------------------------------------------------------------- #


def test_a_subject_without_an_interval_is_always_due():
    subject = Subject.steam_app(730, "CS2")
    assert subject.min_interval_minutes == 0
    assert subject.due(utcnow()) is True


def test_an_interval_is_respected():
    now = utcnow()
    subject = Subject.github_repo("ValveSoftware/Proton", min_interval_minutes=60)
    subject.last_read = now - timedelta(minutes=30)
    assert subject.due(now) is False
    subject.last_read = now - timedelta(minutes=61)
    assert subject.due(now) is True


def test_a_never_read_subject_is_due():
    subject = Subject.github_repo("ValveSoftware/Proton", min_interval_minutes=180)
    assert subject.last_read is None
    assert subject.due(utcnow()) is True


def test_cs2_is_the_only_subject_polled_every_run():
    """The cadence is set by the depot build id; everything else has a floor,
    so a ten-minute schedule does not hammer the GitHub API."""
    every_run = [s for s in default_subjects() if s.min_interval_minutes == 0]
    assert [s.external_id for s in every_run] == ["730"]


def test_intervals_round_trip_through_storage(storage):
    subject = Subject.github_repo("ValveSoftware/Proton", min_interval_minutes=60)
    storage.upsert_subjects([subject])
    stored = storage.list_subjects()[0]
    assert stored.min_interval_minutes == 60
    assert stored.last_read is None

    storage.mark_subject_read(subject.id)
    assert storage.list_subjects()[0].last_read is not None


def test_the_watcher_skips_subjects_that_are_not_due(settings, storage):
    from src.watcher import Watcher

    subject = Subject.github_repo("ValveSoftware/Proton", min_interval_minutes=180)
    storage.upsert_subjects([subject])
    storage.mark_subject_read(subject.id)

    watcher = Watcher(settings, storage, [], notifier=None)
    watcher.seed_subjects = lambda: {"inserted": 0, "updated": 0}
    stats = watcher.run()
    assert stats.subjects_skipped >= 1
    assert "subjects_skipped=" in stats.as_logline()
