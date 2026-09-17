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

#: depots whose download is tiny are launcher shells, not content. Watching them
#: adds a line to every notification and says nothing.
MIN_INTERESTING_DOWNLOAD = 1_000_000


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


def parse_depots(text: str) -> Dict[str, Dict[str, Any]]:
    """Per-depot manifest ids from app_info_print output.

    This is how Steam itself knows what to fetch: ``app_info`` gives a manifest
    id per depot per branch, the manifest lists the files and chunks, and the
    chunks come from the CDN. Watching the manifest id is therefore strictly
    more informative than the build id -- the build id says something changed,
    the manifest says *which* depot did, and its download size says how much.
    """
    start = text.find('"depots"')
    if start < 0:
        return {}
    end = text.find('"branches"', start)
    block = text[start : end if end > 0 else len(text)]

    depots: Dict[str, Dict[str, Any]] = {}
    depot = branch = section = None
    for raw in block.splitlines():
        line = raw.strip()
        indent = len(raw) - len(raw.lstrip("\t"))
        section_match = _SECTION.match(line)
        if section_match:
            name = section_match.group(1)
            if indent == 2 and name.isdigit():
                depot, branch, section = name, None, None
                depots[depot] = {"os": "", "manifests": {}}
            elif indent == 3:
                section = name
            elif indent == 4 and section == "manifests":
                branch = name
            continue
        pair = _KEY_VALUE.match(line)
        if not pair or not depot:
            continue
        key, value = pair.groups()
        if key == "oslist":
            depots[depot]["os"] = value
        elif branch and key in ("gid", "download", "size"):
            depots[depot]["manifests"].setdefault(branch, {})[key] = value
    return depots


class SteamDepotProvider(WatchProvider):
    name = "steam_depot"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.steamcmd = find_steamcmd()
        self.timeout = int(os.environ.get("STEAMCMD_TIMEOUT_SECONDS", "180"))
        #: appid -> raw app_info block, filled by prefetch and used for one run
        self._cache: Dict[str, str] = {}

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

    def prefetch(self, subjects: List[Subject]) -> None:
        """Ask for every watched app in one steamcmd session.

        Most of the cost is the session itself -- connecting and logging in --
        not the query, so asking for two apps at once is measurably cheaper
        than two sessions.
        """
        appids = [s.external_id for s in subjects if self.supports(s)]
        if len(appids) < 2:
            return
        try:
            combined = self._run(appids)
        except ProviderError as exc:
            log.info("steamcmd prefetch failed, falling back to per-app", extra={"error": str(exc)[:160]})
            return
        self._cache = self._split_by_app(combined, appids)

    @staticmethod
    def _split_by_app(text: str, appids: List[str]) -> Dict[str, str]:
        """Cut a multi-app dump into one block per app.

        app_info_print emits each app as a top-level `"<appid>"` object, so the
        start of the next one marks the end of the previous.
        """
        marks = []
        for appid in appids:
            index = text.find('"{}"\n'.format(appid))
            if index >= 0:
                marks.append((index, appid))
        marks.sort()
        blocks: Dict[str, str] = {}
        for position, (start, appid) in enumerate(marks):
            end = marks[position + 1][0] if position + 1 < len(marks) else len(text)
            blocks[appid] = text[start:end]
        return blocks

    def app_info(self, appid: str) -> str:
        cached = self._cache.get(str(appid))
        if cached:
            return cached
        return self._run([str(appid)])

    def _run(self, appids: List[str]) -> str:
        assert self.steamcmd
        command = [self.steamcmd, "+login", "anonymous"]
        for appid in appids:
            command += ["+app_info_print", str(appid)]
        command.append("+quit")
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

        # Per-depot manifests: which part of the game actually moved. The
        # download size comes along so the notification can say how big it is.
        entries = []
        for depot, info in sorted(parse_depots(text).items(), key=lambda kv: int(kv[0])):
            public = info["manifests"].get("public") or {}
            gid = public.get("gid")
            if not gid:
                continue
            download = public.get("download") or "0"
            if download.isdigit() and int(download) < MIN_INTERESTING_DOWNLOAD:
                continue
            entries.append("{}:{}:{}:{}".format(depot, info["os"] or "any", gid, download))
        if entries:
            values.append(
                WatchValue(
                    subject_id=subject.id,
                    key="depot_manifests",
                    value="|".join(entries),
                    label="{} депотов с контентом".format(len(entries)),
                    detail=", ".join(e.split(":")[0] for e in entries[:8]),
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


def describe_manifest_change(old: str, new: str) -> List[str]:
    """Which depots moved, with their platform and download size.

    Entries look like ``2347773:linux:8639120305802825922:4604235144``. Only the
    manifest id is compared: a depot whose id is unchanged did not move, and
    reprinting it would bury the one that did.
    """

    def index(text: str) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for part in str(text).split("|"):
            bits = part.split(":")
            if len(bits) >= 3:
                out[bits[0]] = bits
        return out

    before, after = index(old), index(new)
    lines: List[str] = []
    for depot in sorted(set(before) | set(after), key=lambda d: int(d) if d.isdigit() else 0):
        was, now = before.get(depot), after.get(depot)
        if was and now and was[2] == now[2]:
            continue
        if now is None:
            lines.append("депот {} больше не публикуется".format(depot))
            continue
        platform = now[1]
        size = _format_size(now[3]) if len(now) > 3 else ""
        lines.append(
            "депот {} ({}){}{}".format(
                depot, platform, "" if was else " — новый", ", закачка " + size if size else ""
            )
        )
    return lines


def _format_size(raw: str) -> str:
    """Download size in the unit that reads naturally, or nothing at all.

    Anything under a megabyte is a launcher stub; printing "0.0 GB" for it is
    worse than printing nothing.
    """
    if not str(raw).isdigit():
        return ""
    size = int(raw)
    if size >= 1_000_000_000:
        return "{:.1f} ГБ".format(size / 1e9)
    if size >= 1_000_000:
        return "{:.0f} МБ".format(size / 1e6)
    return ""


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
