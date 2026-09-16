"""Steam Web API watchers.

Two endpoints, both official, both keyless, both verified live:

``ISteamApps/UpToDateCheck``
    Returns the version a dedicated server must run. It changes the moment
    Valve pushes a build live -- typically before the announcement post, which
    is the whole point of watching it.

``ISteamNews/GetNewsForApp``
    The app's news feed. Watched for *beta and preview* channels
    (SteamOS/Steam Deck, Steam client) where a post routinely precedes the
    stable release by days or weeks.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import utcnow
from ..subjects import Subject, SubjectKind, WatchValue
from .base import ProviderError, WatchProvider

log = get_logger(__name__)

UP_TO_DATE_URL = "https://api.steampowered.com/ISteamApps/UpToDateCheck/v1/"
NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"

#: words that mark a post as a pre-release channel -- these are the ones with
#: real lead time over a stable release
BETA_MARKERS = ("beta", "preview", "release candidate", " rc ", "experimental")

#: A Steam news feed mixes Valve's own announcements with syndicated articles
#: from gaming news sites. Only the first kind is evidence of anything: appid
#: 753 for instance returns nothing but PCGamesN articles. Verified 2026-09-17.
OFFICIAL_FEEDNAMES = frozenset(
    {
        "steam_community_announcements",
        "steam_updates",
        "steam_hardware_blog",
    }
)


class SteamVersionProvider(WatchProvider):
    name = "steam_version"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=2.0)
        self.client.cache_ttl = 0

    def supports(self, subject: Subject) -> bool:
        return subject.kind == SubjectKind.STEAM_APP and subject.external_id.isdigit()

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                UP_TO_DATE_URL,
                params={"appid": subject.external_id, "version": "1"},
                cache_ttl=0,
            )
        except HttpError as exc:
            raise ProviderError("UpToDateCheck failed for {}: {}".format(subject.name, exc)) from exc

        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            log.info("steam version: unusable payload", extra={"subject": subject.name})
            return []
        if not response.get("success"):
            # normal for apps without a dedicated server; not an error
            log.debug("steam version unavailable", extra={"subject": subject.name})
            return []

        raw = response.get("required_version")
        if raw is None:
            return []
        try:
            version = str(int(raw))
        except (TypeError, ValueError):
            version = str(raw)

        message = str(response.get("message") or "").strip()
        return [
            WatchValue(
                subject_id=subject.id,
                key="required_version",
                value=version,
                label=message or "build {}".format(version),
                # label already carries the message; repeating it as detail
                # printed the same line twice in the notification
                detail="",
                url=subject.url,
                observed_at=utcnow(),
            )
        ]


class SteamNewsProvider(WatchProvider):
    name = "steam_news"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.client = client_from_settings(settings, rate_limit_rps=2.0)
        self.client.cache_ttl = 0
        self.count = 15

    def supports(self, subject: Subject) -> bool:
        return subject.kind == SubjectKind.STEAM_FEED and subject.external_id.isdigit()

    def read(self, subject: Subject) -> List[WatchValue]:
        try:
            payload = self.client.get_json(
                NEWS_URL,
                params={
                    "appid": subject.external_id,
                    "count": self.count,
                    "maxlength": 400,
                },
                cache_ttl=0,
            )
        except HttpError as exc:
            raise ProviderError("GetNewsForApp failed for {}: {}".format(subject.name, exc)) from exc

        items = self._items(payload)
        official = [i for i in items if self._is_official(i)]
        if not official:
            if items:
                log.info(
                    "steam news: feed carries no official posts",
                    extra={
                        "subject": subject.name,
                        "feeds": sorted({str(i.get("feedname")) for i in items})[:5],
                    },
                )
            return []
        items = official

        values: List[WatchValue] = []
        newest = items[0]
        values.append(self._value(subject, "latest_news", newest))

        # A pre-release post is the signal with the longest lead time, so it is
        # tracked separately: a stable post must not mask an unnoticed beta.
        beta = next((i for i in items if self._looks_prerelease(i)), None)
        if beta is not None:
            values.append(self._value(subject, "latest_prerelease", beta))
        return values

    @staticmethod
    def _items(payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        news = payload.get("appnews")
        if not isinstance(news, dict):
            return []
        items = news.get("newsitems")
        if not isinstance(items, list):
            return []
        clean = [i for i in items if isinstance(i, dict) and i.get("gid")]
        clean.sort(key=lambda i: int(i.get("date") or 0), reverse=True)
        return clean

    @staticmethod
    def _is_official(item: Dict[str, Any]) -> bool:
        return str(item.get("feedname") or "").strip().lower() in OFFICIAL_FEEDNAMES

    @classmethod
    def _looks_prerelease(cls, item: Dict[str, Any]) -> bool:
        title = str(item.get("title") or "").lower()
        return any(marker in title for marker in BETA_MARKERS)

    @staticmethod
    def _value(subject: Subject, key: str, item: Dict[str, Any]) -> WatchValue:
        title = str(item.get("title") or "").strip()
        when: Optional[str] = None
        try:
            when = datetime.datetime.fromtimestamp(
                int(item.get("date") or 0), datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError):
            when = None
        return WatchValue(
            subject_id=subject.id,
            key=key,
            value=str(item.get("gid")),
            label=title[:200],
            detail=" · ".join(x for x in (when, str(item.get("feedlabel") or "")) if x),
            url=str(item.get("url") or subject.url),
            observed_at=utcnow(),
        )
