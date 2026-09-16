"""A pragmatic parser for the subset of the OSM ``opening_hours`` syntax that
real food venues actually use.

Supported: ``24/7``; weekday selectors (``Mo``, ``Mo-Fr``, ``Sa-Mo`` wrapping,
``Mo,We,Fr``); one or more ``HH:MM-HH:MM`` ranges per rule; overnight ranges
(``Fr-Sa 18:00-02:00``); ``off``/``closed`` rules; rules separated by ``;`` or
``||``; quoted comments; ``PH``/``SH`` rules (ignored).

Anything else -- month/date selectors, week numbers, ``sunset``, nth-weekday --
makes the whole spec *unknown* rather than silently wrong. Unknown propagates to
``ASSUME_OPEN_WHEN_UNKNOWN`` so the operator decides whether to poll or skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Set, Tuple

DAY_INDEX = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}
ALL_DAYS = frozenset(range(7))

_QUOTED = re.compile(r'"[^"]*"')
_TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_DAY_TOKEN = re.compile(r"^(mo|tu|we|th|fr|sa|su)(?:\s*-\s*(mo|tu|we|th|fr|sa|su))?$")
_UNSUPPORTED = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|sunrise|sunset|dusk|dawn|week|easter)\b"
)


@dataclass
class _Rule:
    days: Set[int]
    intervals: List[Tuple[int, int]] = field(default_factory=list)  # minutes, end may exceed 1440
    closed: bool = False


@dataclass
class OpeningHours:
    raw: str
    rules: List[_Rule]
    parsable: bool
    always_open: bool = False

    def is_open_at(self, moment: datetime) -> Optional[bool]:
        """``True``/``False``, or ``None`` when the spec could not be understood."""
        if not self.parsable:
            return None
        if self.always_open:
            return True
        if not self.rules:
            return None

        weekday = moment.weekday()
        minutes = moment.hour * 60 + moment.minute
        state: Optional[bool] = None

        for rule in self.rules:
            if weekday not in rule.days:
                continue
            if rule.closed:
                state = False
                continue
            hit = any(start <= minutes < min(end, 1440) for start, end in rule.intervals)
            state = True if hit else False

        if state:
            return True

        # a range that started yesterday and runs past midnight
        yesterday = (weekday - 1) % 7
        for rule in self.rules:
            if rule.closed or yesterday not in rule.days:
                continue
            for start, end in rule.intervals:
                if end > 1440 and minutes < (end - 1440):
                    return True

        # OSM semantics: a weekday that no rule mentions is a closed day.
        return False if state is None else state


def _parse_days(token: str) -> Optional[Set[int]]:
    days: Set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        match = _DAY_TOKEN.match(part)
        if not match:
            return None
        start = DAY_INDEX[match.group(1)]
        if match.group(2):
            end = DAY_INDEX[match.group(2)]
            index = start
            days.add(index)
            while index != end:  # inclusive, wrapping (Sa-Mo)
                index = (index + 1) % 7
                days.add(index)
        else:
            days.add(start)
    return days or None


def _parse_intervals(token: str) -> Optional[List[Tuple[int, int]]]:
    intervals: List[Tuple[int, int]] = []
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        match = _TIME_RANGE.match(part)
        if not match:
            return None
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if m1 > 59 or m2 > 59 or h1 > 48 or h2 > 48:
            return None
        start = h1 * 60 + m1
        end = h2 * 60 + m2
        if end <= start:  # overnight: 18:00-02:00 -> 1080..1560
            end += 1440
        intervals.append((start, end))
    return intervals or None


def parse(spec: Optional[str]) -> OpeningHours:
    raw = (spec or "").strip()
    if not raw:
        return OpeningHours(raw, [], parsable=False)

    cleaned = _QUOTED.sub(" ", raw).lower().replace("||", ";")
    if cleaned.replace(" ", "") in {"24/7", "24/7open", "open"}:
        return OpeningHours(raw, [], parsable=True, always_open=True)

    rules: List[_Rule] = []
    for chunk in cleaned.split(";"):
        chunk = chunk.strip().rstrip(",")
        if not chunk:
            continue
        # public / school holidays: no reliable calendar here, ignore the rule
        if chunk.startswith("ph") or chunk.startswith("sh"):
            continue
        if _UNSUPPORTED.search(chunk):
            return OpeningHours(raw, [], parsable=False)

        if chunk in {"24/7", "open"}:
            rules.append(_Rule(set(ALL_DAYS), [(0, 1440)]))
            continue

        closed = False
        for marker in (" off", " closed"):
            if chunk.endswith(marker):
                closed = True
                chunk = chunk[: -len(marker)].strip()
                break
        if chunk in {"off", "closed"}:
            rules.append(_Rule(set(ALL_DAYS), closed=True))
            continue

        tokens = chunk.split()
        if not tokens:
            continue

        day_part: List[str] = []
        time_part: List[str] = []
        for token in tokens:
            if _DAY_TOKEN.match(token.split(",")[0].strip()) and not time_part:
                day_part.append(token)
            else:
                time_part.append(token)

        days = _parse_days(",".join(day_part)) if day_part else set(ALL_DAYS)
        if days is None:
            return OpeningHours(raw, [], parsable=False)

        if closed and not time_part:
            rules.append(_Rule(days, closed=True))
            continue

        time_text = "".join(time_part).replace(" ", "")
        if time_text in {"24/7", "24:00-24:00", "00:00-24:00"}:
            rules.append(_Rule(days, [(0, 1440)]))
            continue
        intervals = _parse_intervals(time_text)
        if intervals is None:
            return OpeningHours(raw, [], parsable=False)
        rules.append(_Rule(days, intervals, closed=False))

    if not rules:
        return OpeningHours(raw, [], parsable=False)
    return OpeningHours(raw, rules, parsable=True)


def is_open(spec: Optional[str], moment: datetime) -> Optional[bool]:
    """Convenience wrapper: ``True``, ``False`` or ``None`` (unknown)."""
    return parse(spec).is_open_at(moment)


def any_open(specs: Sequence[Optional[str]], moment: datetime) -> Optional[bool]:  # pragma: no cover
    states = [is_open(spec, moment) for spec in specs]
    if any(state is True for state in states):
        return True
    if all(state is None for state in states):
        return None
    return False
