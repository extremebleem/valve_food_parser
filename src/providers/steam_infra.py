"""Steam infrastructure watchers.

The premise: when Valve rolls an update out, the plumbing feels it before
anyone posts about it. Everyone downloads at once, so the content caches load
up; matchmaking strains, so search times climb. None of that is an
announcement, but all of it is public and keyless.

``IContentServerDirectoryService/GetServersForSteamPipe``
    The content-delivery servers for a cell. Two different signals live here:
    the *set of hosts* (Valve adding or dropping a CDN partner is
    infrastructure news, and rare), and the *load* on Valve's own SteamCache
    nodes. Verified 2026-09-17: partner CDNs report ``load: 0`` while
    ``cache1-sto2`` … ``cache6-sto2`` reported 77-80, so the number is real.

``ISteamDirectory/GetSteamPipeDomains``
    The domains content is served from. Changes rarely; a new one appearing is
    worth knowing about.

``IContentServerDirectoryService/GetClientUpdateHosts``
    Where the Steam client fetches its own updates. A ~1 KB KV blob, watched as
    a digest: a change means the client-update path was touched.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, List

from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import utcnow
from ..subjects import Subject, SubjectKind, WatchValue
from .base import ProviderError, WatchProvider

log = get_logger(__name__)

STEAMPIPE_SERVERS_URL = (
    "https://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/"
)
STEAMPIPE_DOMAINS_URL = "https://api.steampowered.com/ISteamDirectory/GetSteamPipeDomains/v1/"
CLIENT_UPDATE_HOSTS_URL = (
    "https://api.steampowered.com/IContentServerDirectoryService/GetClientUpdateHosts/v1/"
)


def _digest(parts: List[str]) -> str:
    return hashlib.sha1(",".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


class SteamInfraProvider(WatchProvider):
    name = "steam_infra"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=2.0)
        self.client.cache_ttl = 0
        # different cells return different server sets, so one is pinned for
        # comparability rather than sampled at random
        self.cell_id = int(os.environ.get("STEAMPIPE_CELL_ID", "0"))

    def supports(self, subject: Subject) -> bool:
        return subject.kind == SubjectKind.STEAM_INFRA

    def read(self, subject: Subject) -> List[WatchValue]:
        now = utcnow()
        values: List[WatchValue] = []
        values.extend(self._steampipe_servers(subject, now))
        values.extend(self._steampipe_domains(subject, now))
        values.extend(self._client_update_hosts(subject, now))
        return values

    # -- content delivery servers ------------------------------------------ #

    def _steampipe_servers(self, subject: Subject, now) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                STEAMPIPE_SERVERS_URL, params={"cell_id": self.cell_id}, cache_ttl=0
            )
        except HttpError as exc:
            raise ProviderError("GetServersForSteamPipe failed: {}".format(exc)) from exc

        response = payload.get("response") if isinstance(payload, dict) else None
        servers = response.get("servers") if isinstance(response, dict) else None
        if not isinstance(servers, list) or not servers:
            return []

        hosts = [str(s.get("host") or "") for s in servers if isinstance(s, dict) and s.get("host")]
        # the provider is the stable part of a host name; individual cache nodes
        # come and go, partners do not
        providers = sorted({h.split(".")[0].rstrip("0123456789-") for h in hosts})

        out = [
            WatchValue(
                subject_id=subject.id,
                key="steampipe_hosts",
                value=_digest(hosts),
                label="{} узлов раздачи: {}".format(len(hosts), ", ".join(providers[:8])),
                detail="cell_id={}".format(self.cell_id),
                url="https://store.steampowered.com/",
                observed_at=now,
            )
        ]

        # Valve's own caches report a real utilisation percentage; partner CDNs
        # report 0, so they are excluded rather than dragging the number down
        loads = [
            float(s["load"])
            for s in servers
            if isinstance(s, dict) and isinstance(s.get("load"), (int, float)) and s["load"] > 0
        ]
        if loads:
            peak = max(loads)
            out.append(
                WatchValue(
                    subject_id=subject.id,
                    key="steampipe_load_max",
                    value="{:.0f}".format(peak),
                    label="загрузка кешей Valve: пик {:.0f}%, средняя {:.0f}%".format(
                        peak, sum(loads) / len(loads)
                    ),
                    detail="по {} узлам".format(len(loads)),
                    url="https://store.steampowered.com/",
                    observed_at=now,
                )
            )
        return out

    # -- delivery domains --------------------------------------------------- #

    def _steampipe_domains(self, subject: Subject, now) -> List[WatchValue]:
        try:
            payload = self.client.get_json(STEAMPIPE_DOMAINS_URL, cache_ttl=0)
        except HttpError as exc:
            log.info("GetSteamPipeDomains failed", extra={"error": str(exc)[:120]})
            return []
        response = payload.get("response") if isinstance(payload, dict) else None
        domains = response.get("domainlist") if isinstance(response, dict) else None
        if not isinstance(domains, list) or not domains:
            return []
        names = [str(d) for d in domains]
        return [
            WatchValue(
                subject_id=subject.id,
                key="steampipe_domains",
                value=_digest(names),
                label="{} доменов раздачи".format(len(names)),
                detail=", ".join(sorted(names)[:6]),
                url="https://store.steampowered.com/",
                observed_at=now,
            )
        ]

    # -- client update hosts ------------------------------------------------ #

    def _client_update_hosts(self, subject: Subject, now) -> List[WatchValue]:
        try:
            payload = self.client.get_json(CLIENT_UPDATE_HOSTS_URL, cache_ttl=0)
        except HttpError as exc:
            log.info("GetClientUpdateHosts failed", extra={"error": str(exc)[:120]})
            return []
        response = payload.get("response") if isinstance(payload, dict) else None
        blob = response.get("hosts_kv") if isinstance(response, dict) else None
        if not isinstance(blob, str) or not blob.strip():
            return []
        hosts = sorted(set(__import__("re").findall(r'"([a-z0-9.-]+\.[a-z]{2,})"', blob)))
        return [
            WatchValue(
                subject_id=subject.id,
                key="client_update_hosts",
                value=_digest([blob]),
                label="{} хостов обновления клиента".format(len(hosts)),
                detail=", ".join(hosts[:6]),
                url="https://store.steampowered.com/",
                observed_at=now,
            )
        ]
