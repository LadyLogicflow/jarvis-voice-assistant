"""
Microsoft 365 Kalender-Integration via ICS-Feed.

Liest Termine aus einem oeffentlich freigegebenen ICS-Link (aus Outlook).
Kein OAuth, kein Azure — nur HTTP-Fetch + ICS-Parser.

Konfiguration: microsoft_calendar_ics_url in config.json (gitignored).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

import httpx
from icalendar import Calendar, vDatetime

import settings as S

log = logging.getLogger("jarvis.ms_calendar")

_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _to_aware(dt: datetime.date) -> datetime.datetime:
    """Konvertiert date oder naive datetime zu UTC-aware datetime."""
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    # date only (all-day event)
    return datetime.datetime.combine(dt, datetime.time.min, tzinfo=datetime.timezone.utc)


def _parse_ics(ics_bytes: bytes, days: int,
               time_min: datetime.datetime | None = None,
               time_max: datetime.datetime | None = None) -> list[tuple[datetime.datetime, str]]:
    cal = Calendar.from_ical(ics_bytes)
    now = datetime.datetime.now(datetime.timezone.utc)
    t_min = time_min if time_min is not None else now
    t_max = time_max if time_max is not None else (now + datetime.timedelta(days=days))
    events: list[tuple[datetime.datetime, str]] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        start = _to_aware(dtstart.dt)
        if not (t_min <= start <= t_max):
            continue

        summary = str(component.get("SUMMARY", "(kein Titel)"))
        location = str(component.get("LOCATION", "")).strip() if component.get("LOCATION") else ""
        all_day = not isinstance(dtstart.dt, datetime.datetime)

        start_local = start.astimezone()
        day_label = _WEEKDAYS_DE[start_local.weekday()]
        date_str = start_local.strftime("%d.%m.")

        if all_day:
            line = f"{day_label} {date_str}: {summary}"
        else:
            time_str = start_local.strftime("%H:%M")
            dtend = component.get("DTEND")
            end_str = ""
            if dtend and isinstance(dtend.dt, datetime.datetime):
                end_local = _to_aware(dtend.dt).astimezone()
                end_str = f"–{end_local.strftime('%H:%M')}"
            line = f"{day_label} {date_str} {time_str}{end_str}: {summary}"

        if location:
            line += f" ({location})"
        events.append((start, line))

    events.sort(key=lambda x: x[0])
    return events


async def get_events(days: int = 7,
                     time_min: datetime.datetime | None = None,
                     time_max: datetime.datetime | None = None) -> str:
    url = S.MICROSOFT_CALENDAR_ICS_URL
    if not url:
        return ""

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ics_bytes = resp.content
    except Exception as e:
        log.warning("ICS fetch fehlgeschlagen: %s", e)
        return f"DIHAG-Kalender nicht erreichbar: {e}"

    try:
        events = _parse_ics(ics_bytes, days, time_min, time_max)
    except Exception as e:
        log.warning("ICS parse fehlgeschlagen: %s", e)
        return f"DIHAG-Kalender konnte nicht verarbeitet werden: {e}"

    if not events:
        return "Keine DIHAG-Termine im angefragten Zeitraum."
    return "DIHAG-Kalender:\n" + "\n".join(line for _, line in events)
