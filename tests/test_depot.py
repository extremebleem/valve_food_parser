"""Depot build ids read through steamcmd, and per-subject polling intervals."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.models import utcnow
from src.providers.steam_depot import (
    SteamDepotProvider,
    describe_branch_change,
    parse_branches,
    parse_depots,
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


# --------------------------------------------------------------------------- #
# per-depot manifests -- how Steam itself knows what to fetch
# --------------------------------------------------------------------------- #

# trimmed from a real `app_info_print 730` run, 2026-09-17
DEPOTS_INFO = (
    '"730"\n{\n'
    '\t"depots"\n\t{\n'
    '\t\t"732"\n\t\t{\n'
    '\t\t\t"config"\n\t\t\t{\n\t\t\t\t"oslist"\t\t"windows"\n\t\t\t}\n'
    '\t\t\t"manifests"\n\t\t\t{\n'
    '\t\t\t\t"public"\n\t\t\t\t{\n'
    '\t\t\t\t\t"gid"\t\t"2849850780169022159"\n'
    '\t\t\t\t\t"size"\t\t"8"\n'
    '\t\t\t\t\t"download"\t\t"64"\n'
    '\t\t\t\t}\n\t\t\t}\n\t\t}\n'
    '\t\t"2347773"\n\t\t{\n'
    '\t\t\t"config"\n\t\t\t{\n\t\t\t\t"oslist"\t\t"linux"\n\t\t\t}\n'
    '\t\t\t"manifests"\n\t\t\t{\n'
    '\t\t\t\t"public"\n\t\t\t\t{\n'
    '\t\t\t\t\t"gid"\t\t"8639120305802825922"\n'
    '\t\t\t\t\t"size"\t\t"9550335930"\n'
    '\t\t\t\t\t"download"\t\t"4604239936"\n'
    '\t\t\t\t}\n'
    '\t\t\t\t"1.41.7.4"\n\t\t\t\t{\n'
    '\t\t\t\t\t"gid"\t\t"1111111111111111111"\n'
    '\t\t\t\t\t"download"\t\t"4000000000"\n'
    '\t\t\t\t}\n\t\t\t}\n\t\t}\n'
    '\t\t"branches"\n\t\t{\n'
    '\t\t\t"public"\n\t\t\t{\n\t\t\t\t"buildid"\t\t"25218825"\n\t\t\t}\n'
    '\t\t}\n\t}\n}\n'
)


def test_depot_manifests_are_parsed_with_platform_and_size():
    depots = parse_depots(DEPOTS_INFO)
    assert set(depots) == {"732", "2347773"}
    assert depots["2347773"]["os"] == "linux"
    public = depots["2347773"]["manifests"]["public"]
    assert public["gid"] == "8639120305802825922"
    assert public["download"] == "4604239936"
    # non-public branches are captured too
    assert depots["2347773"]["manifests"]["1.41.7.4"]["gid"] == "1111111111111111111"


@pytest.mark.parametrize("text", ["", "nothing", '"depots"', '"depots" {'])
def test_depot_parsing_survives_unusable_output(text):
    assert parse_depots(text) == {}


def test_launcher_shell_depots_are_not_watched(settings, monkeypatch):
    """732 downloads 64 bytes. Listing it would add a line to every
    notification and say nothing."""
    provider = SteamDepotProvider(settings)
    monkeypatch.setattr(provider, "steamcmd", "/fake/steamcmd.sh")
    monkeypatch.setattr(provider, "app_info", lambda appid: DEPOTS_INFO)
    values = {v.key: v for v in provider.read(cs2())}
    assert "732" not in values["depot_manifests"].value
    assert values["depot_manifests"].value.startswith("2347773:linux:8639120305802825922:")


def test_manifest_change_names_only_the_depots_that_moved():
    from src.providers.steam_depot import describe_manifest_change

    old = "2347770:any:AAA:53900780944|2347773:linux:BBB:4604239936"
    new = "2347770:any:AAA:53900780944|2347773:linux:CCC:4711000000"
    lines = describe_manifest_change(old, new)
    assert len(lines) == 1
    assert "2347773" in lines[0] and "linux" in lines[0]
    assert "4.7" in lines[0]
    assert "2347770" not in "".join(lines), "an unchanged depot must not be reprinted"


def test_manifest_change_reports_a_new_and_a_withdrawn_depot():
    from src.providers.steam_depot import describe_manifest_change

    appeared = describe_manifest_change("1:any:AAA:5", "1:any:AAA:5|2:linux:BBB:120000000")
    assert len(appeared) == 1 and "новый" in appeared[0]

    gone = describe_manifest_change("1:any:AAA:5|2:linux:BBB:5", "1:any:AAA:5")
    assert len(gone) == 1 and "больше не публикуется" in gone[0]


def test_manifest_change_is_empty_when_nothing_moved():
    from src.providers.steam_depot import describe_manifest_change

    same = "2347770:any:AAA:5|2347773:linux:BBB:6"
    assert describe_manifest_change(same, same) == []


def test_manifest_change_tolerates_malformed_entries():
    from src.providers.steam_depot import describe_manifest_change

    assert describe_manifest_change("", "") == []
    assert describe_manifest_change("garbage", "garbage") == []
    assert describe_manifest_change("1:any", "1:any:AAA:5") == ["депот 1 (any) — новый"]
    # a stub-sized download prints no size rather than "0.0 GB"
    assert describe_manifest_change("", "1:any:AAA:64") == ["депот 1 (any) — новый"]
    assert "МБ" in describe_manifest_change("", "1:any:AAA:26889664")[0]
    assert "ГБ" in describe_manifest_change("", "1:any:AAA:4604239936")[0]


def test_manifests_are_a_change_signal_ranked_with_the_build_id():
    from src.watch_telegram import KEY_TITLES, PRIORITY
    from src.watcher import CHANGE_KEYS, SET_KEYS

    assert "depot_manifests" in CHANGE_KEYS
    assert "depot_manifests" in SET_KEYS
    assert "depot_manifests" in KEY_TITLES
    assert PRIORITY["depot_manifests"] <= PRIORITY["depot_public_buildid"]
