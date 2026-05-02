"""
NRW (Germany / North Rhine-Westphalia) public-holiday calculations.

Pure functions, no IO. Holiday names are kept in German because they
reach the user via Jarvis's voice output.
"""

from __future__ import annotations

import datetime


def get_easter(year: int) -> datetime.date:
    """Compute the Easter date (Anonymus / Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def get_nrw_holidays(year: int) -> dict[datetime.date, str]:
    """Return the public holidays for NRW (Germany / North Rhine-Westphalia).

    Holiday names are intentionally kept in German because they are part
    of the data model surfaced to the user."""
    easter = get_easter(year)
    return {
        datetime.date(year, 1, 1):                 "Neujahr",
        easter - datetime.timedelta(days=2):       "Karfreitag",
        easter:                                    "Ostersonntag",
        easter + datetime.timedelta(days=1):       "Ostermontag",
        datetime.date(year, 5, 1):                 "Tag der Arbeit",
        easter + datetime.timedelta(days=39):      "Christi Himmelfahrt",
        easter + datetime.timedelta(days=49):      "Pfingstsonntag",
        easter + datetime.timedelta(days=50):      "Pfingstmontag",
        easter + datetime.timedelta(days=60):      "Fronleichnam",
        datetime.date(year, 10, 3):                "Tag der deutschen Einheit",
        datetime.date(year, 11, 1):                "Allerheiligen",
        datetime.date(year, 12, 25):               "1. Weihnachtstag",
        datetime.date(year, 12, 26):               "2. Weihnachtstag",
    }


def check_free_day(today: datetime.date | None = None) -> tuple[bool, str]:
    """Check whether `today` is a weekend day or NRW public holiday.

    Returns (True, German-label) or (False, '').

    Argument is optional and defaults to `datetime.date.today()` so the
    server.py call sites don't need to change. Tests can pass an explicit
    date instead of monkeypatching the date module."""
    today = today or datetime.date.today()
    weekday = today.weekday()  # 5 = Saturday, 6 = Sunday
    if weekday == 5:
        return True, "Samstag"
    if weekday == 6:
        return True, "Sonntag"
    holidays = get_nrw_holidays(today.year)
    if today in holidays:
        return True, holidays[today]
    return False, ""
