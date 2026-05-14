"""
Jarvis — Google Calendar Integration
Reads upcoming events and can add new ones.
Requires token.json (generated once via scripts/google-auth.py).
"""

import asyncio
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import dateparser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import settings as S

log = logging.getLogger("jarvis.calendar")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CREDS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")

# Fix #61: Module-level lock verhindert gleichzeitige Token-Refreshes
# durch parallele Coroutinen (Race Condition).
_token_refresh_lock = threading.Lock()


def _get_service():  # type: ignore[no-untyped-def]  # googleapiclient Resource
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Fix #61: Lock um den Refresh-Block — nur ein Thread darf
            # gleichzeitig den Token erneuern.
            with _token_refresh_lock:
                # Nochmals pruefen ob ein paralleler Thread den Token
                # inzwischen schon erneuert hat.
                creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
                if not creds.valid:
                    # Fix #85: refresh_token kann None sein wenn das OAuth-
                    # Access widerrufen wurde. In diesem Fall wuerde
                    # creds.refresh(Request()) mit einem unklaren RefreshError
                    # fehlschlagen. Stattdessen explizit pruefen und eine
                    # sprechende Exception werfen.
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        # Fix #61: Atomares Schreiben via tmp-Datei + os.replace()
                        # verhindert korruptes token.json bei Crash mid-write.
                        tmp_fd, tmp_path = tempfile.mkstemp(
                            dir=os.path.dirname(TOKEN_PATH), suffix=".tmp"
                        )
                        try:
                            with os.fdopen(tmp_fd, "w") as f:
                                f.write(creds.to_json())
                            os.replace(tmp_path, TOKEN_PATH)
                        except Exception:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                            raise
                    else:
                        raise RuntimeError(
                            "Google OAuth-Token abgelaufen und kein refresh_token "
                            "vorhanden. Bitte 'python3 scripts/google-auth.py' "
                            "erneut ausführen."
                        )
        else:
            raise RuntimeError(
                "Google-Kalender nicht autorisiert. "
                "Bitte 'python3 scripts/google-auth.py' ausfuehren."
            )
    return build("calendar", "v3", credentials=creds)


async def get_events(days: int = 7, max_results: int = 10) -> str:
    """Fetch upcoming calendar events."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_events, days, max_results)


def _fetch_events(days: int, max_results: int) -> str:
    try:
        service = _get_service()
    except RuntimeError as e:
        return str(e)

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as e:
        return f"Kalender-Fehler: {e}"

    events = result.get("items", [])
    if not events:
        return "KEINE_TERMINE"

    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        # Fix #61: datetime.fromisoformat() akzeptiert 'Z'-Suffix erst ab
        # Python 3.11 — vorher in '+00:00' umwandeln.
        try:
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_str = dt.strftime("%a %d.%m. %H:%M")
            else:
                dt = datetime.strptime(start, "%Y-%m-%d")
                start_str = dt.strftime("%a %d.%m.")
        except Exception:
            start_str = start
        lines.append(f"• {start_str} — {e.get('summary', '(kein Titel)')}")

    return f"Kalender — nächste {len(lines)} Termine:\n" + "\n".join(lines)


async def add_event(title: str, when: str, duration_h: float = 1.0) -> str:
    """Add a calendar event. 'when' is a natural-language string parsed via dateparser.

    Raises:
        ValueError: Wenn das Datum nicht geparst werden kann oder in der
                    Vergangenheit liegt.
        RuntimeError: Wenn der Google-Kalender nicht autorisiert ist.
        Exception: Bei API-Fehlern vom Google Calendar.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _add_event, title, when, duration_h)


def _add_event(title: str, when: str, duration_h: float) -> str:
    # Fix #60 + #70: dateparser mit PREFER_DATES_FROM='future' und
    # RETURN_AS_TIMEZONE_AWARE=True damit Wochentage und Uhrzeiten
    # korrekt in die Zukunft aufgeloest werden.
    dt = dateparser.parse(
        when,
        languages=["de", "en"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DAY_OF_MONTH": "first",
            "TIMEZONE": "Europe/Berlin",
        },
    )
    # Fix #73: Statt silent None/leeren String wirft _add_event jetzt eine
    # sprechende Exception, die der Aufrufer in actions.py abfangen kann.
    if not dt:
        raise ValueError(
            f"Datum '{when}' konnte nicht verstanden werden."
        )

    # Fix #60: Explizite Prüfung ob das geparste Datum in der
    # Vergangenheit liegt. PREFER_DATES_FROM='future' hilft, aber bei
    # manchen Formulierungen kann dateparser trotzdem eine vergangene
    # Zeit liefern.
    now_aware = datetime.now(dt.tzinfo)
    if dt < now_aware:
        raise ValueError(
            f"Der Termin '{when}' liegt in der Vergangenheit "
            f"({dt.strftime('%d.%m.%Y %H:%M')}). "
            f"Bitte ein zukünftiges Datum angeben."
        )

    # Fix #73: Datum + Uhrzeit vor dem API-Call loggen.
    log.info(
        "Kalender-Eintrag wird angelegt: titel=%r datum=%s uhrzeit=%s",
        title,
        dt.strftime("%d.%m.%Y"),
        dt.strftime("%H:%M"),
    )

    service = _get_service()  # wirft RuntimeError wenn nicht autorisiert

    end_dt = dt + timedelta(hours=duration_h)
    event = {
        "summary": title,
        "start": {"dateTime": dt.isoformat(), "timeZone": "Europe/Berlin"},
        "end":   {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Berlin"},
    }
    # Fix #73: Bei API-Fehler Exception werfen statt leeren String
    # zurückgeben — der Aufrufer in actions.py entscheidet über die
    # Fehlermeldung an Catrin.
    created = service.events().insert(calendarId="primary", body=event).execute()
    event_id = created.get("id", "")
    log.info("Kalender-Eintrag angelegt: id=%s titel=%r", event_id, title)
    return f"Termin angelegt: {title} am {dt.strftime('%d.%m. um %H:%M')} Uhr"


async def create_event_at(title: str, start_dt: "datetime", duration_min: int) -> str:
    """Create a calendar event at an exact datetime. Returns the event_id on
    success, raises on failure."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_event_at, title, start_dt, duration_min)


def _create_event_at(title: str, start_dt: "datetime", duration_min: int) -> str:
    service = _get_service()
    end_dt = start_dt + timedelta(minutes=duration_min)
    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Berlin"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Berlin"},
    }
    created = service.events().insert(calendarId="primary", body=event).execute()
    event_id = created.get("id", "")
    log.info("Planner: event created id=%s titel=%r", event_id, title)
    return event_id


async def delete_event(event_id: str) -> bool:
    """Delete a calendar event by its ID. Returns True on success."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _delete_event, event_id)


def _delete_event(event_id: str) -> bool:
    try:
        service = _get_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        log.info("Planner: event deleted id=%s", event_id)
        return True
    except Exception as e:
        log.warning("Planner: delete_event failed id=%s: %s", event_id, e)
        return False


async def get_events_raw(start_dt: "datetime", end_dt: "datetime") -> list[dict]:
    """Return raw event dicts between start_dt and end_dt (tz-aware)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_events_raw, start_dt, end_dt)


def _get_events_raw(start_dt: "datetime", end_dt: "datetime") -> list[dict]:
    try:
        service = _get_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except Exception as e:
        log.warning("get_events_raw failed: %s", e)
        return []
