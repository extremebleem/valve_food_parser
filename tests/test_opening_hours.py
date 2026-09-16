"""OSM ``opening_hours`` subset parser -- the "don't poll closed venues" gate."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.opening_hours import is_open, parse

WED = datetime(2026, 9, 16, 12, 0)   # Wednesday 12:00
SAT = datetime(2026, 9, 19, 12, 0)
SUN = datetime(2026, 9, 20, 12, 0)


@pytest.mark.parametrize(
    "spec,moment,expected",
    [
        ("24/7", datetime(2026, 9, 16, 3, 0), True),
        ("Mo-Fr 08:00-17:00", WED, True),
        ("Mo-Fr 08:00-17:00", datetime(2026, 9, 16, 19, 0), False),
        ("Mo-Fr 08:00-17:00", SAT, False),
        ("Mo-Fr 11:00-14:00,17:00-22:00", datetime(2026, 9, 16, 15, 0), False),
        ("Mo-Fr 11:00-14:00,17:00-22:00", datetime(2026, 9, 16, 18, 0), True),
        ("Fr-Sa 18:00-02:00", datetime(2026, 9, 19, 1, 0), True),
        ("Fr-Sa 18:00-02:00", datetime(2026, 9, 19, 12, 0), False),
        ("Mo-Su 10:00-20:00; Su off", SUN, False),
        ("Mo-Su 10:00-20:00; Su off", WED, True),
        ("Mo,We,Fr 10:00-18:00", datetime(2026, 9, 17, 11, 0), False),
        ("Mo,We,Fr 10:00-18:00", WED, True),
        ("Sa-Mo 09:00-15:00", datetime(2026, 9, 20, 10, 0), True),
        ("Su 10:00-16:00", datetime(2026, 9, 20, 11, 0), True),
        ("Mo-Fr 08:00-17:00 || Sa 10:00-14:00", SAT, True),
        ('Mo-Fr 08:00-17:00; PH off', WED, True),
        ("Mo-Su 00:00-24:00", datetime(2026, 9, 16, 4, 0), True),
    ],
)
def test_known_specs(spec, moment, expected):
    assert is_open(spec, moment) is expected


@pytest.mark.parametrize(
    "spec",
    [
        "",
        None,
        "   ",
        "Jan-Mar Mo-Fr 10:00-18:00",
        "sunrise-sunset",
        "Mo-Fr 8-17",
        "week 1-53 Mo 10:00-12:00",
        "nonsense",
        "Mo-Fr 25:99-99:99",
    ],
)
def test_unparsable_specs_report_unknown_not_false(spec):
    """Unknown must never be mistaken for closed -- that would silently stop
    polling a venue we simply do not have hours for."""
    assert is_open(spec, WED) is None


def test_quoted_comments_are_stripped():
    assert is_open('Mo-Fr 08:00-17:00 "call ahead"', WED) is True


def test_boundaries_are_half_open():
    assert is_open("Mo-Fr 08:00-17:00", datetime(2026, 9, 16, 8, 0)) is True
    assert is_open("Mo-Fr 08:00-17:00", datetime(2026, 9, 16, 17, 0)) is False


def test_parse_exposes_always_open():
    assert parse("24/7").always_open is True
    assert parse("Mo-Fr 08:00-17:00").always_open is False
    assert parse("").parsable is False


def test_later_rules_override_earlier_ones():
    spec = "Mo-Su 10:00-22:00; We 10:00-14:00"
    assert is_open(spec, datetime(2026, 9, 16, 16, 0)) is False  # Wednesday
    assert is_open(spec, datetime(2026, 9, 17, 16, 0)) is True   # Thursday
