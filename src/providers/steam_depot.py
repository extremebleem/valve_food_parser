"""Depot build ids, read from Steam's own product-info cache.

This is the signal SteamDB is built on, and the earliest one that exists: a
build id changes the moment Valve pushes content, before any announcement and
before anyone dumps the files.

It is not reachable over the Web API. ``IGCVersion_730`` answers with zeros,
``UpToDateCheck`` returns the *server* version (a different numbering),
``GetDepotPatchInfo`` returns an empty object even when handed both manifest
ids, and CDN paths need a manifest id that only PICS provides. The one route
is Valve's own client: ``steamcmd +login anonymous +app_info_print <appid>``.
Anonymous login is supported by Steam and needs no account.

Verified against appid 730 on 2026-09-17: ``public`` at build 25218825,
updated 2026-09-09 22:49 UTC, alongside twelve pinned version branches. A new
version branch appearing tends to precede the public push, so the *set* of
branch names is watched as well as the public build id.

Without steamcmd on PATH the provider stays dormant and everything else runs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..logging_utils import get_logger
from ..models import utcnow
from ..subjects import Subject, SubjectKind, WatchValue
from .base import ProviderError, WatchProvider

log = get_logger(__name__)

CANDIDATE_PATHS = (
    "steamcmd",
    "steamcmd.sh",
    os.path.expanduser("~/steamcmd/steamcmd.sh"),
    "/usr/games/steamcmd",
    "/home/steamcmd/steamcmd.sh",
    "./steamcmd/steamcmd.sh",
)

_KEY_VALUE = re.compile(r'^"([^"]+)"\s+"([^"]*)"$')
_SECTION = re.compile(r'^"([^"]+)"$')


def find_steamcmd() -> Optional[str]:
    explicit = os.environ.get("STEAMCMD_PATH", "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for candidate in CANDIDATE_PATHS:
        found = shutil.which(candidate) if "/" not in candidate else (
            candidate if os.path.exists(candidate) else None
        )
        if found:
            return found
    return None


def parse_branches(text: str) -> Dict[str, Dict[str, str]]:
    """Pull the ``depots > branches`` block out of app_info_print output.

    A tolerant, block-scoped reader rather than a full VDF parser: the output
    also contains free-form log lines from steamcmd itself, and a strict parser
    would choke on them.
    """
    start = text.find('"branches"')
    if start < 0:
        return {}
    try:
        open_at = text.index("{", start)
    except ValueError:
        return {}

    depth = 0
    end = len(text)
    for index in range(open_at, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break

    branches: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw in text[open_at:end].splitlines():
        line = raw.strip()
        if not line or line in ("{", "}"):
            continue
        section = _SECTION.match(line)
        if section:
            current = section.group(1)
            branches.setdefault(current, {})
            continue
        pair = _KEY_VALUE.match(line)
        if pair and current:
            branches[current][pair.group(1)] = pair.group(2)
    return {name: data for name, data in branches.items() if data}


class SteamDepotProvider(WatchProvider):
    name = "steam_depot"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.steamcmd = find_steamcmd()
        self.timeout = int(os.environ.get("STEAMCMD_TIMEOUT_SECONDS", "180"))

    @property
    def enabled(self) -> bool:
        if not self.steamcmd:
            log.info("steamcmd not found; depot signals disabled")
            return False
        return True

    def supports(self, subject: Subject) -> bool:
        return (
            bool(self.steamcmd)
            and subject.kind == SubjectKind.STEAM_APP
            and subject.external_id.isdigit()
            and bool(subject.meta.get("watch_depot"))
        )

    def app_info(self, appid: str) -> str:
        assert self.steamcmd
        command = [
            self.steamcmd,
            "+login",
            "anonymous",
            "+app_info_print",
            str(appid),
            "+quit",
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
                cwd=os.path.dirname(self.steamcmd) or None,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("steamcmd timed out after {}s".format(self.timeout)) from exc
        except OSError as exc:
            raise ProviderError("cannot run steamcmd: {}".format(exc)) from exc

        text = result.stdout.decode("utf-8", "replace")
        if "Connecting anonymously to Steam Public...OK" not in text.replace("\x1b[0m", ""):
            if '"branches"' not in text:
                raise ProviderError(
                    "steamcmd did not reach Steam (exit {})".format(result.returncode)
                )
        return text

    def read(self, subject: Subject) -> List[WatchValue]:
        text = self.app_info(subject.external_id)
        branches = parse_branches(text)
        if not branches:
            log.info("no branch data in app_info", extra={"subject": subject.name})
            return []

        now = utcnow()
        values: List[WatchValue] = []

        public = branches.get("public") or {}
        build = public.get("buildid")
        if build:
            updated = public.get("timeupdated")
            when = ""
            if updated and updated.isdigit():
                when = datetime.fromtimestamp(int(updated), timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="depot_public_buildid",
                    value=str(build),
                    label="билд {}{}".format(build, " от {}".format(when) if when else ""),
                    detail="ветка public",
                    url=subject.url,
                    observed_at=now,
                )
            )

        # A new pinned version branch usually shows up before the public push,
        # so membership of the set matters, not just the public build.
        names = sorted(branches)
        values.append(
            WatchValue(
                subject_id=subject.id,
                key="depot_branches",
                value="|".join(names),
                label="{} веток депота".format(len(names)),
                detail=", ".join(names[:10]),
                url=subject.url,
                observed_at=now,
            )
        )
        return values


def describe_branch_change(old: str, new: str) -> List[str]:
    """Which branches appeared or disappeared, for the notification body."""
    before = {b for b in str(old).split("|") if b}
    after = {b for b in str(new).split("|") if b}
    lines = []
    for name in sorted(after - before):
        lines.append("+ {}".format(name))
    for name in sorted(before - after):
        lines.append("− {}".format(name))
    return lines
