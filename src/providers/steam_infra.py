"""Steam infrastructure watchers.

The premise: when Valve rolls an update out, the plumbing feels it before
anyone posts about it. Everyone downloads at once, so the content caches load
up; matchmaking strains, so search times climb. None of that is an
announcement, but all of it is public and keyless.

``IContentServerDirectoryService/GetServersForSteamPipe``
    The content-delivery servers *near the caller*. Measured 2026-09-17: the
    ``cell_id`` parameter is ignored, the answer depends on the caller's
    address (``fra1``/``sto2`` from Europe, ``atl``/``iad`` from a US runner),
    and two identical back-to-back calls return different sets. The host set is
    therefore **not watched** -- it produced a false alert on its first day.

    The load figure is real and steady (31-32 across five calls ten seconds
    apart) but it describes whichever regional caches answered. It is stored
    per region, so a delta only ever compares readings from the same place;
    otherwise a run on a European runner followed by one on a US runner would
    read as a collapse.

``ISteamDirectory/GetSteamPipeDomains``
    The domains content is served from. Changes rarely; a new one appearing is
    worth knowing about.

``IContentServerDirectoryService/GetClientUpdateHosts``
    Where the Steam client fetches its own updates. A ~1 KB KV blob, watched as
    a digest: a change means the client-update path was touched.
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

STEAMPIPE_SERVERS_URL = (
    "https://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/"
)
STEAMPIPE_DOMAINS_URL = "https://api.steampowered.com/ISteamDirectory/GetSteamPipeDomains/v1/"
CLIENT_UPDATE_HOSTS_URL = (
    "https://api.steampowered.com/IContentServerDirectoryService/GetClientUpdateHosts/v1/"
)


#: per-region prefix, so a load reading is only ever compared with one from the
#: same datacentre
LOAD_KEY_PREFIX = "steampipe_load_"


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

        # Valve's own caches report a real utilisation percentage; partner CDNs
        # report 0, so they are excluded rather than dragging the number down
        loads = [
            float(s["load"])
            for s in servers
            if isinstance(s, dict) and isinstance(s.get("load"), (int, float)) and s["load"] > 0
        ]
        if not loads:
            return []

        region = self._region_of(hosts)
        peak = max(loads)
        return [
            WatchValue(
                subject_id=subject.id,
                key="{}{}".format(LOAD_KEY_PREFIX, region),
                value="{:.0f}".format(peak),
                label="загрузка кешей Valve ({}): пик {:.0f}%, средняя {:.0f}%".format(
                    region, peak, sum(loads) / len(loads)
                ),
                detail="по {} узлам".format(len(loads)),
                url="https://store.steampowered.com/",
                observed_at=now,
            )
        ]

    @staticmethod
    def _region_of(hosts: List[str]) -> str:
        """The datacentre most of these nodes live in.

        Host names look like ``cache3-fra1.steamcontent.com``; the suffix after
        the dash is the site. Readings are keyed by it so a delta never compares
        two different parts of the world.
        """
        from collections import Counter

        sites = Counter()
        for host in hosts:
            name = host.split(".")[0]
            if "-" in name:
                sites[name.rsplit("-", 1)[1]] += 1
        return sites.most_common(1)[0][0] if sites else "unknown"

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
        names = sorted(str(d) for d in domains)
        return [
            WatchValue(
                subject_id=subject.id,
                key="steampipe_domains",
                # the sorted list, not a digest: a digest can say that something
                # changed but never what, which is the first thing anyone asks
                value="|".join(names),
                label="{} доменов раздачи".format(len(names)),
                detail=", ".join(names[:6]),
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
                value="|".join(hosts),
                label="{} хостов обновления клиента".format(len(hosts)),
                detail=", ".join(hosts[:6]),
                url="https://store.steampowered.com/",
                observed_at=now,
            )
        ]
