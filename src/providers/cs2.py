"""CS2-focused watchers.

Counter-Strike 2 exposes more public state than any other Valve title, and most
of it needs no key at all. Each signal here was probed live on 2026-09-17; the
ones that turned out to be dead ends are recorded in ``docs/RESEARCH-VALVE.md``
rather than silently omitted.

``ISteamApps/GetSDRConfig`` (keyless)
    The Steam Datagram Relay configuration CS2 clients use to reach Valve's
    servers. ``revision`` is a unix timestamp of the last change to that
    network, and ``pops`` lists the 48 relay datacenters. Valve touches this
    when it moves capacity around -- which is infrastructure work, and
    infrastructure work tends to precede the thing it is for.

``IGCVersion_<appid>/GetServerVersion`` (keyless)
    ``deploy_version`` and ``active_version`` of a game coordinator. When they
    differ a deploy is **in flight right now**, which is as early as a public
    signal gets. CS2 reports zeros here, so it is only wired up for the titles
    that answer honestly (Dota 2, Deadlock, TF2).

``ICSGOServers_730/GetGameServersStatus`` (free key)
    The richest CS2 signal by a wide margin: authoritative app version,
    matchmaking scheduler state, online/searching player counts, average search
    time and per-datacenter load. The scheduler leaving ``normal`` is how an
    update announces itself before anyone posts about it. Needs a Steam Web API
    key, which is free and instant from https://steamcommunity.com/dev/apikey
    -- without one the endpoint answers 403 and this provider stays dormant.
"""

from __future__ import annotations

import os
from typing import Any, List

from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import utcnow
from ..subjects import Subject, SubjectKind, WatchValue
from .base import ProviderError, WatchProvider

log = get_logger(__name__)

SDR_URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/"
GC_URL = "https://api.steampowered.com/IGCVersion_{appid}/GetServerVersion/v1/"
CS2_STATUS_URL = "https://api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/"
PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"

#: IGCVersion_730 answers with deploy_version=0 and active_version=0, so there
#: is nothing to watch there. Verified 2026-09-17.
GC_UNSUPPORTED_APPS = frozenset({"730"})


class SteamSDRProvider(WatchProvider):
    """Steam Datagram Relay network configuration."""

    name = "steam_sdr"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=1.0)
        self.client.cache_ttl = 0

    def supports(self, subject: Subject) -> bool:
        return subject.kind == SubjectKind.STEAM_APP and bool(subject.meta.get("watch_sdr"))

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                SDR_URL, params={"appid": subject.external_id}, cache_ttl=0
            )
        except HttpError as exc:
            raise ProviderError("GetSDRConfig failed for {}: {}".format(subject.name, exc)) from exc

        if not isinstance(payload, dict) or not payload.get("success"):
            log.info("sdr config unavailable", extra={"subject": subject.name})
            return []

        now = utcnow()
        values: List[WatchValue] = []

        revision = payload.get("revision")
        if revision is not None:
            try:
                stamp = int(revision)
            except (TypeError, ValueError):
                stamp = None
            label = "конфиг сети обновлён"
            if stamp:
                import datetime

                label = "конфиг сети от {}".format(
                    datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    )
                )
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="sdr_revision",
                    value=str(revision),
                    label=label,
                    detail="Steam Datagram Relay",
                    url=subject.url,
                    observed_at=now,
                )
            )

        pops = payload.get("pops")
        if isinstance(pops, dict) and pops:
            names = sorted(pops)
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="sdr_pops",
                    # the sorted list rather than a digest, so the notification
                    # can name the datacentre that appeared or went away
                    value="|".join(names),
                    label="{} релейных дата-центров".format(len(names)),
                    detail=", ".join(names[:16]) + ("…" if len(names) > 16 else ""),
                    url=subject.url,
                    observed_at=now,
                )
            )
        return values


class SteamGCVersionProvider(WatchProvider):
    """Game coordinator deploy/active versions."""

    name = "steam_gc"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=2.0)
        self.client.cache_ttl = 0

    def supports(self, subject: Subject) -> bool:
        return (
            subject.kind == SubjectKind.STEAM_APP
            and subject.external_id.isdigit()
            and subject.external_id not in GC_UNSUPPORTED_APPS
            and bool(subject.meta.get("watch_gc"))
        )

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                GC_URL.format(appid=subject.external_id), cache_ttl=0
            )
        except HttpError as exc:
            if exc.status == 404:
                return []
            raise ProviderError("IGCVersion failed for {}: {}".format(subject.name, exc)) from exc

        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or not result.get("success"):
            return []

        active = _int(result.get("active_version"))
        deploy = _int(result.get("deploy_version"))
        if not active and not deploy:
            return []

        now = utcnow()
        values = [
            WatchValue(
                subject_id=subject.id,
                key="gc_active_version",
                value=str(active),
                label="game coordinator {}".format(active),
                detail="",
                url=subject.url,
                observed_at=now,
            )
        ]
        # deploy_version running ahead of active_version means a rollout is in
        # flight at this moment -- the earliest public signal there is.
        in_flight = bool(deploy and active and deploy != active)
        values.append(
            WatchValue(
                subject_id=subject.id,
                key="gc_deploy_in_flight",
                value="yes" if in_flight else "no",
                label=(
                    "идёт выкатка: deploy {} против active {}".format(deploy, active)
                    if in_flight
                    else "выкатка не идёт"
                ),
                detail="",
                url=subject.url,
                observed_at=now,
            )
        )
        return values


class CS2ServerStatusProvider(WatchProvider):
    """``ICSGOServers_730/GetGameServersStatus`` -- needs a free Steam Web API key."""

    name = "cs2_status"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
        self.client = client_from_settings(settings, rate_limit_rps=1.0)
        self.client.cache_ttl = 0

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def supports(self, subject: Subject) -> bool:
        return bool(self.key) and subject.external_id == "730" and subject.kind == SubjectKind.STEAM_APP

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(CS2_STATUS_URL, params={"key": self.key}, cache_ttl=0)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise ProviderError(
                    "CS2 status rejected the key (HTTP {}). Get a free one at "
                    "https://steamcommunity.com/dev/apikey".format(exc.status)
                ) from exc
            raise ProviderError("CS2 status failed: {}".format(exc)) from exc

        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            log.info("cs2 status: unusable payload", extra={"subject": subject.name})
            return []

        now = utcnow()
        values: List[WatchValue] = []

        app = result.get("app") if isinstance(result.get("app"), dict) else {}
        version = _int(app.get("version"))
        if version:
            values.append(
                WatchValue(
                    subject_id=subject.id, key="cs2_app_version", value=str(version),
                    label="версия CS2 {}".format(version), url=subject.url, observed_at=now,
                )
            )

        matchmaking = result.get("matchmaking") if isinstance(result.get("matchmaking"), dict) else {}
        scheduler = str(matchmaking.get("scheduler") or "").strip()
        if scheduler:
            values.append(
                WatchValue(
                    subject_id=subject.id, key="cs2_scheduler", value=scheduler,
                    label="матчмейкинг: {}".format(scheduler), url=subject.url, observed_at=now,
                )
            )

        services = result.get("services") if isinstance(result.get("services"), dict) else {}
        if services:
            # Report the state, do not grade it. Valve returns IEconItems=offline
            # and Leaderboards=idle as steady values, so calling everything that
            # is not "normal" a problem would make the label permanently wrong.
            # The change itself is the signal; the message diffs old against new.
            state = ",".join("{}={}".format(k, services[k]) for k in sorted(services))
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="cs2_services",
                    value=state,
                    label="; ".join("{}: {}".format(k, services[k]) for k in sorted(services)),
                    url=subject.url,
                    observed_at=now,
                )
            )

        # rates -- these feed the baseline engine, not change detection
        for field, key, label in (
            ("online_players", "cs2_online_players", "игроков онлайн"),
            ("searching_players", "cs2_searching_players", "в поиске игры"),
            ("search_seconds_avg", "cs2_search_seconds_avg", "среднее время поиска, с"),
            ("online_servers", "cs2_online_servers", "серверов онлайн"),
        ):
            number = _int(matchmaking.get(field))
            if number is not None:
                values.append(
                    WatchValue(
                        subject_id=subject.id, key=key, value=str(number),
                        label="{}: {}".format(label, number), url=subject.url, observed_at=now,
                    )
                )
        return values


class SteamPlayerCountProvider(WatchProvider):
    """Concurrent players -- a rate. A sharp collapse is servers going down."""

    name = "steam_players"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=2.0)
        self.client.cache_ttl = 0

    def supports(self, subject: Subject) -> bool:
        return (
            subject.kind == SubjectKind.STEAM_APP
            and subject.external_id.isdigit()
            and bool(subject.meta.get("watch_players"))
        )

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                PLAYERS_URL, params={"appid": subject.external_id}, cache_ttl=0
            )
        except HttpError as exc:
            # Not every app publishes a concurrent-player counter -- Deadlock
            # answers 404. That is "no data", not a failure.
            if exc.status == 404:
                log.debug("no player counter for this app", extra={"subject": subject.name})
                return []
            raise ProviderError("player count failed for {}: {}".format(subject.name, exc)) from exc

        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, dict) or response.get("result") != 1:
            return []
        count = _int(response.get("player_count"))
        if count is None:
            return []
        return [
            WatchValue(
                subject_id=subject.id, key="players_current", value=str(count),
                label="{:,} игроков".format(count).replace(",", " "),
                url=subject.url, observed_at=utcnow(),
            )
        ]


def _int(value: Any):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
