"""Unit tests for holiday-related pure functions in server.py.

These tests don't touch the network or filesystem — get_easter is
deterministic and get_nrw_holidays is built on top of it.
"""

from __future__ import annotations

import datetime as dt

import pytest

import server


# Easter dates verified against an external table (e.g. timeanddate.com).
@pytest.mark.parametrize(
    "year,expected",
    [
        (2024, dt.date(2024, 3, 31)),
        (2025, dt.date(2025, 4, 20)),
        (2026, dt.date(2026, 4, 5)),
        (2027, dt.date(2027, 3, 28)),
        (2030, dt.date(2030, 4, 21)),
    ],
)
def test_get_easter(year, expected):
    assert server.get_easter(year) == expected


def test_nrw_holidays_2026_contents():
    holidays = server.get_nrw_holidays(2026)
    # The 13 NRW holidays the implementation defines.
    assert len(holidays) == 13
    # Spot-check the fixed-date ones.
    assert holidays[dt.date(2026, 1, 1)] == "Neujahr"
    assert holidays[dt.date(2026, 5, 1)] == "Tag der Arbeit"
    assert holidays[dt.date(2026, 10, 3)] == "Tag der deutschen Einheit"
    assert holidays[dt.date(2026, 12, 25)] == "1. Weihnachtstag"
    assert holidays[dt.date(2026, 12, 26)] == "2. Weihnachtstag"


def test_nrw_holidays_2026_easter_chain():
    """Karfreitag, Ostermontag, Christi Himmelfahrt, Pfingsten,
    Fronleichnam are all keyed off Easter."""
    holidays = server.get_nrw_holidays(2026)
    easter = server.get_easter(2026)
    assert holidays[easter - dt.timedelta(days=2)] == "Karfreitag"
    assert holidays[easter] == "Ostersonntag"
    assert holidays[easter + dt.timedelta(days=1)] == "Ostermontag"
    assert holidays[easter + dt.timedelta(days=39)] == "Christi Himmelfahrt"
    assert holidays[easter + dt.timedelta(days=49)] == "Pfingstsonntag"
    assert holidays[easter + dt.timedelta(days=50)] == "Pfingstmontag"
    assert holidays[easter + dt.timedelta(days=60)] == "Fronleichnam"


def test_check_free_day_saturday():
    """check_free_day() optionally accepts an explicit date — preferred
    over monkeypatching the date module."""
    is_free, label = server.check_free_day(dt.date(2026, 5, 2))  # Saturday
    assert is_free is True
    assert label == "Samstag"


def test_check_free_day_sunday():
    is_free, label = server.check_free_day(dt.date(2026, 5, 3))  # Sunday
    assert is_free is True
    assert label == "Sonntag"


def test_check_free_day_workday():
    is_free, label = server.check_free_day(dt.date(2026, 5, 4))  # Monday
    assert is_free is False
    assert label == ""


def test_check_free_day_holiday():
    is_free, label = server.check_free_day(dt.date(2026, 12, 25))  # 1. Weihnachtstag
    assert is_free is True
    assert label == "1. Weihnachtstag"
