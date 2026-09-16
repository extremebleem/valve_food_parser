"""GitHub watchers for Valve's public repositories.

Split by what each signal actually means:

* ``latest_release`` -- change detection. A release *is* the event, so it
  answers "it happened", with a short lead over the store page.
* ``commits_24h`` -- a rate, fed to the statistical engine. A burst of commits
  is the thing that *precedes* a release, which is what we are really after.

Unauthenticated the API allows 60 requests/hour, which is enough for this watch
list but not much else. In GitHub Actions ``GITHUB_TOKEN`` is free and raises
that to 1000/hour, so the provider uses it when present.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..http import HttpError, client_from_settings
from ..logging_utils import get_logger
from ..models import utcnow
from ..subjects import Subject, SubjectKind, WatchValue
from .base import ProviderError
from .watch_base import WatchProvider

log = get_logger(__name__)

API = "https://api.github.com"


class GitHubProvider(WatchProvider):
    name = "github"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.client = client_from_settings(settings, rate_limit_rps=3.0)
        self.client.cache_ttl = 0
        self.commit_window_hours = int(os.environ.get("GITHUB_COMMIT_WINDOW_HOURS", "24"))

    def supports(self, subject: Subject) -> bool:
        return subject.kind == SubjectKind.GITHUB_REPO and "/" in subject.external_id

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, allow_404: bool = False) -> Any:
        try:
            return self.client.get_json(
                "{}{}".format(API, path), params=params, headers=self._headers(), cache_ttl=0
            )
        except HttpError as exc:
            if allow_404 and exc.status == 404:
                return None
            if exc.status == 403 and "rate limit" in (exc.body or "").lower():
                raise ProviderError(
                    "GitHub rate limit hit ({}). Set GITHUB_TOKEN to raise it.".format(path)
                ) from exc
            raise ProviderError("GitHub {} failed: {}".format(path, exc)) from exc

    def read(self, subject: Subject) -> List[WatchValue]:
        repo = subject.external_id
        now = utcnow()
        values: List[WatchValue] = []

        # NOTE: /tags has no documented ordering and in practice does not
        # return the newest one -- ValveSoftware/Proton answers with
        # "proton-sdk-sniper-3.0.20250519..." while its latest release is
        # "proton-11.0-2". A value that flaps produces false change alerts, so
        # tags are deliberately not watched. Repositories without releases are
        # covered by the commit-rate signal instead.

        release = self._get("/repos/{}/releases/latest".format(repo), allow_404=True)
        if isinstance(release, dict) and release.get("tag_name"):
            marker = " (pre-release)" if release.get("prerelease") else ""
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="latest_release",
                    value=str(release["tag_name"]),
                    label="релиз {}{}".format(release.get("name") or release["tag_name"], marker),
                    detail=str(release.get("published_at") or "")[:10],
                    url=str(release.get("html_url") or subject.url),
                    observed_at=now,
                )
            )

        since = (now - timedelta(hours=self.commit_window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        commits = self._get(
            "/repos/{}/commits".format(repo), {"since": since, "per_page": 100}, allow_404=True
        )
        if isinstance(commits, list):
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="commits_{}h".format(self.commit_window_hours),
                    value=str(len(commits)),
                    label="{} коммит(ов) за {} ч".format(len(commits), self.commit_window_hours),
                    detail=repo,
                    url="https://github.com/{}/commits".format(repo),
                    observed_at=now,
                )
            )
        return values


#: keys whose change is an event worth an immediate notification. Everything
#: else read by this provider is a rate, handled by the statistical engine.
GITHUB_CHANGE_KEYS = frozenset({"latest_release"})
