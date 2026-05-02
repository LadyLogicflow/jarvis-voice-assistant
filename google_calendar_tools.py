"""
Jarvis — Google Calendar Integration
Reads upcoming events and can add new ones.
Requires token.json (generated once via scripts/google-auth.py).
"""

import os
import asyncio
from datetime import datetime, timedelta, timezone

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CREDS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")


def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("Google-Kalender nicht autorisiert. Bitte 'python3 scripts/google-auth.py' ausfuehren.")
    return build("calendar", "v3", credentials=creds)


async def get_events(days: int = 7, max_results: int = 10) -> str:
    """Fetch upcoming calendar events."""
    loop = asyncio.get_event_loop()
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
        # Format datetime nicely
        try:
            if "T" in start:
                dt = datetime.fromisoformat(start)
                start_str = dt.strftime("%a %d.%m. %H:%M")
            else:
                dt = datetime.strptime(start, "%Y-%m-%d")
                start_str = dt.strftime("%a %d.%m.")
        except Exception:
            start_str = start
        lines.append(f"• {start_str} — {e.get('summary', '(kein Titel)')}")

    return f"Kalender — nächste {len(lines)} Termine:\n" + "\n".join(lines)


async def add_event(title: str, when: str, duration_h: float = 1.0) -> str:
    """Add a calendar event. 'when' is a natural-language string parsed via dateparser."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_event, title, when, duration_h)


def _add_event(title: str, when: str, duration_h: float) -> str:
    try:
        import dateparser
        dt = dateparser.parse(when, languages=["de", "en"])
        if not dt:
            return f"Datum '{when}' konnte nicht verstanden werden."
    except ImportError:
        # Fallback: morgen 10 Uhr
        dt = datetime.now() + timedelta(days=1)
        dt = dt.replace(hour=10, minute=0, second=0, microsecond=0)

    try:
        service = _get_service()
    except RuntimeError as e:
        return str(e)

    end_dt = dt + timedelta(hours=duration_h)
    event = {
        "summary": title,
        "start": {"dateTime": dt.isoformat(), "timeZone": "Europe/Berlin"},
        "end":   {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Berlin"},
    }
    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Termin angelegt: {title} am {dt.strftime('%d.%m. um %H:%M')} Uhr"
    except Exception as e:
        return f"Fehler beim Anlegen: {e}"
