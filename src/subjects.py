"""Things we watch, and the values we watch on them.

A :class:`Subject` is anything with a public state that changes when Valve does
something: a Steam app, a Steam news feed, a GitHub repository.

A :class:`WatchValue` is one observed piece of that state -- the required game
version, the newest release tag, the id of the latest news post. Change
detection is a string comparison against what we saw last time: no baseline, no
warm-up, no statistics. That is deliberate. "Is something coming?" is answered
by a discrete state change, not by a deviation from a median.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import iso, parse_iso, utcnow


class SubjectKind:
    STEAM_APP = "steam_app"        # a game/tool: watch its required version
    STEAM_FEED = "steam_feed"      # a news feed: watch the newest post
    GITHUB_REPO = "github_repo"    # a repository: watch tags and commit rate
    STEAM_INFRA = "steam_infra"    # Steam's plumbing: content delivery, update hosts


#: How much warning a signal typically gives. Used only to sort and label
#: notifications -- it is a documented editorial judgement, not a measurement.
LEAD_TIME = {
    "depot_branches": "часы–дни — ветка появляется раньше публичной выкладки",
    "depot_public_buildid": "минуты — билд выложен, до анонса",
    "steampipe_hosts": "дни",
    "steampipe_domains": "дни",
    "client_update_hosts": "часы–дни",
    "steampipe_load_max": "минуты — идёт массовая загрузка",
    "cs2_search_seconds_avg": "минуты — матчмейкингу тяжело",
    "latest_prerelease": "дни–недели",
    "sdr_pops": "дни–недели",
    "sdr_revision": "часы–дни",
    "gc_deploy_in_flight": "минуты — выкатка идёт прямо сейчас",
    "cs2_scheduler": "минуты — матчмейкинг трогают",
    "cs2_services": "минуты",
    "latest_news": "часы–дни",
    "required_version": "минуты–часы",
    "cs2_app_version": "минуты–часы",
    "gc_active_version": "минуты–часы",
    "latest_release": "дни",
    "players_current": "минуты — серверы перезапускаются",
    "cs2_online_players": "минуты",
    "cs2_online_servers": "минуты",
}


def make_subject_id(kind: str, external_id: str) -> str:
    return "{}:{}".format(kind, external_id)


@dataclass
class Subject:
    id: str
    kind: str
    external_id: str
    name: str
    url: str = ""
    active: bool = True
    priority: int = 100
    #: do not re-read this subject more often than every N minutes. The run
    #: cadence is set by the cheapest, most valuable signal (the depot build
    #: id); everything slower carries its own floor so a 10-minute schedule
    #: does not hammer the GitHub API or burn Actions minutes.
    min_interval_minutes: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_read: Optional[datetime] = None

    def due(self, now: datetime) -> bool:
        """Has this subject's own minimum interval elapsed?"""
        if self.min_interval_minutes <= 0 or self.last_read is None:
            return True
        return (now - self.last_read).total_seconds() / 60.0 >= self.min_interval_minutes

    @classmethod
    def steam_app(cls, appid: int, name: str, **kw: Any) -> "Subject":
        return cls(
            id=make_subject_id(SubjectKind.STEAM_APP, str(appid)),
            kind=SubjectKind.STEAM_APP,
            external_id=str(appid),
            name=name,
            url="https://store.steampowered.com/app/{}/".format(appid),
            **kw,
        )

    @classmethod
    def steam_feed(cls, appid: int, name: str, **kw: Any) -> "Subject":
        return cls(
            id=make_subject_id(SubjectKind.STEAM_FEED, str(appid)),
            kind=SubjectKind.STEAM_FEED,
            external_id=str(appid),
            name=name,
            url="https://store.steampowered.com/news/app/{}".format(appid),
            **kw,
        )

    @classmethod
    def steam_infra(cls, name: str, **kw: Any) -> "Subject":
        return cls(
            id=make_subject_id(SubjectKind.STEAM_INFRA, "steampipe"),
            kind=SubjectKind.STEAM_INFRA,
            external_id="steampipe",
            name=name,
            url="https://store.steampowered.com/",
            **kw,
        )

    @classmethod
    def github_repo(cls, full_name: str, **kw: Any) -> "Subject":
        return cls(
            id=make_subject_id(SubjectKind.GITHUB_REPO, full_name),
            kind=SubjectKind.GITHUB_REPO,
            external_id=full_name,
            name=full_name.split("/")[-1],
            url="https://github.com/{}".format(full_name),
            **kw,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["first_seen"] = iso(self.first_seen)
        data["last_seen"] = iso(self.last_seen)
        data["last_read"] = iso(self.last_read)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Subject":
        return cls(
            id=data["id"],
            kind=data.get("kind", ""),
            external_id=data.get("external_id", ""),
            name=data.get("name", ""),
            url=data.get("url", "") or "",
            active=bool(data.get("active", True)),
            priority=int(data.get("priority", 100) or 100),
            min_interval_minutes=int(data.get("min_interval_minutes", 0) or 0),
            meta=dict(data.get("meta") or {}),
            first_seen=parse_iso(data.get("first_seen")),
            last_seen=parse_iso(data.get("last_seen")),
            last_read=parse_iso(data.get("last_read")),
        )


@dataclass
class WatchValue:
    """One observed piece of public state."""

    subject_id: str
    key: str
    value: str
    label: str = ""
    detail: str = ""
    url: str = ""
    observed_at: Optional[datetime] = None

    def fingerprint(self) -> str:
        return hashlib.sha1(
            "{}|{}|{}".format(self.subject_id, self.key, self.value).encode("utf-8")
        ).hexdigest()[:16]


@dataclass
class WatchEvent:
    """A watched value changed since the previous run."""

    subject: Subject
    key: str
    old_value: str
    new_value: str
    label: str = ""
    detail: str = ""
    url: str = ""
    detected_at: Optional[datetime] = None
    first_ever: bool = False

    @property
    def lead_time(self) -> str:
        return LEAD_TIME.get(self.key, "")


# --------------------------------------------------------------------------- #
# The watch list.
#
# Deliberately explicit rather than "everything Valve owns": 55 public repos and
# hundreds of app ids would drown the signal. These are the surfaces where a
# change actually precedes something a player would notice.
# --------------------------------------------------------------------------- #

DEFAULT_SUBJECTS: List[Subject] = [
    # --- games: required_version bumps the moment a build goes live ---------
    #
    # meta flags pick which extra providers run for a subject. CS2 gets the
    # most because it exposes the most: the relay network config and, with a
    # free Steam Web API key, the matchmaking scheduler. Its game coordinator
    # is deliberately not watched -- IGCVersion_730 answers with zeros.
    Subject.steam_app(
        730,
        "Counter-Strike 2",
        priority=1,
        meta={
            "watch_sdr": True,
            "watch_players": True,
            "watch_cs2_status": True,
            "watch_depot": True,
        },
    ),
    Subject.steam_app(
        570,
        "Dota 2",
        priority=10,
        min_interval_minutes=20,
        meta={"watch_gc": True, "watch_players": True, "watch_depot": True},
    ),
    Subject.steam_app(
        440, "Team Fortress 2", priority=40, min_interval_minutes=60, meta={"watch_gc": True}
    ),
    Subject.steam_app(
        1422450,
        "Deadlock",
        priority=10,
        min_interval_minutes=20,
        meta={"watch_gc": True, "watch_players": True, "watch_depot": True},
    ),
    # --- feeds: beta and preview channels lead stable releases by days ------
    # 1675200 carries SteamOS Previews, SteamOS Betas *and* Steam Beta Client
    # Updates, all as official announcements. It is the single highest-value
    # subject in this list.
    #
    # appid 753 ("Steam") is deliberately absent: its feed returns nothing but
    # syndicated PCGamesN articles, which are somebody else's reporting rather
    # than evidence of anything. Verified 2026-09-17.
    Subject.steam_feed(
        1675200, "SteamOS / Steam Deck (beta & preview)", priority=5, min_interval_minutes=20
    ),
    Subject.steam_feed(730, "Counter-Strike 2 news", priority=15, min_interval_minutes=20),
    Subject.steam_feed(570, "Dota 2 news", priority=15, min_interval_minutes=30),
    Subject.steam_feed(1422450, "Deadlock news", priority=15, min_interval_minutes=30),
    # --- Steam plumbing: an update is felt here before it is announced -----
    Subject.steam_infra("Инфраструктура Steam", priority=8, min_interval_minutes=20),

    # --- repositories: tags and commit bursts precede releases --------------
    Subject.github_repo("ValveSoftware/Proton", priority=30, min_interval_minutes=60),
    Subject.github_repo("ValveSoftware/gamescope", priority=30, min_interval_minutes=60),
    Subject.github_repo("ValveSoftware/SteamOS", priority=30, min_interval_minutes=60),
    Subject.github_repo("ValveSoftware/steam-for-linux", priority=50, min_interval_minutes=180),
    Subject.github_repo("ValveSoftware/Fossilize", priority=60, min_interval_minutes=180),
    Subject.github_repo("ValveSoftware/source-sdk-2013", priority=60, min_interval_minutes=180),
]


def default_subjects() -> List[Subject]:
    now = utcnow()
    out = []
    for subject in DEFAULT_SUBJECTS:
        copy = Subject.from_dict(subject.to_dict())
        copy.first_seen = copy.first_seen or now
        copy.last_seen = now
        out.append(copy)
    return out
