"""
Gmail Settings API — Abwesenheitsnotiz (Vacation Responder).

Nutzt dieselben OAuth-Credentials wie google_calendar_tools.py.
Benoetigt zusaetzlichen Scope:
    https://www.googleapis.com/auth/gmail.settings.basic

WICHTIG: Nach Hinzufuegen dieses Scopes muss die Autorisierung einmalig
neu durchgefuehrt werden:
    python3 scripts/google-auth.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("jarvis.gmail")

# Gleiche Scope-Liste wie google_calendar_tools, plus gmail.settings.basic.
# MUSS mit dem SCOPES in google_calendar_tools.py und scripts/google-auth.py
# uebereinstimmen, damit ein gemeinsames token.json verwendet werden kann.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CREDS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")

_token_refresh_lock = threading.Lock()


def _get_gmail_service():  # type: ignore[no-untyped-def]  # googleapiclient Resource
    """Baut den Gmail-Service-Client auf und erneuert das Token bei Bedarf.

    Raises:
        RuntimeError: Wenn token.json fehlt oder kein refresh_token vorhanden.
    """
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            with _token_refresh_lock:
                creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
                if not creds.valid:
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
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
                            "erneut ausfuehren."
                        )
        else:
            raise RuntimeError(
                "Google Gmail nicht autorisiert. "
                "Bitte 'python3 scripts/google-auth.py' ausfuehren."
            )
    return build("gmail", "v1", credentials=creds)


def _date_to_ms(date_str: str) -> int | None:
    """Wandelt 'YYYY-MM-DD' in Unix-Timestamp (Millisekunden) um.

    Args:
        date_str: Datum als ISO-String 'YYYY-MM-DD', oder leerer String.

    Returns:
        Unix-Timestamp in Millisekunden, oder None wenn date_str leer ist.

    Raises:
        ValueError: Wenn date_str nicht das Format 'YYYY-MM-DD' hat.
    """
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _set_vacation_sync(
    enabled: bool,
    subject: str,
    body: str,
    start_date: str,
    end_date: str,
) -> str:
    """Synchroner Kern fuer set_vacation — laeuft im Thread-Pool."""
    try:
        service = _get_gmail_service()
    except RuntimeError as e:
        return str(e)

    vacation_body: dict = {"enableAutoReply": enabled}

    if enabled:
        vacation_body["responseSubject"] = subject
        vacation_body["responseBodyPlainText"] = body
        vacation_body["restrictToContacts"] = False
        vacation_body["restrictToDomain"] = False

        try:
            start_ms = _date_to_ms(start_date)
            end_ms = _date_to_ms(end_date)
        except ValueError as e:
            return f"Ungültiges Datum: {e}"

        if start_ms is not None:
            vacation_body["startTime"] = str(start_ms)
        if end_ms is not None:
            # Gmail endTime is exclusive — add one day so the user's
            # stated end date is fully included (e.g. "bis 30. Mai"
            # means the responder is active through end of May 30).
            vacation_body["endTime"] = str(end_ms + 24 * 60 * 60 * 1000)

    try:
        service.users().settings().updateVacation(
            userId="me", body=vacation_body
        ).execute()
    except Exception as e:
        log.warning("Gmail updateVacation fehlgeschlagen: %s", e)
        return f"Abwesenheitsnotiz konnte nicht gesetzt werden: {e}"

    if enabled:
        parts = []
        if start_date:
            parts.append(f"ab {start_date}")
        if end_date:
            parts.append(f"bis {end_date}")
        zeitraum = " ".join(parts) if parts else "sofort, bis zur manuellen Deaktivierung"
        log.info(
            "Abwesenheitsnotiz aktiviert: betreff=%r zeitraum=%s",
            subject,
            zeitraum,
        )
        return (
            f"Abwesenheitsnotiz aktiviert ({zeitraum}). "
            f"Betreff: \"{subject}\"."
        )
    else:
        log.info("Abwesenheitsnotiz deaktiviert.")
        return "Abwesenheitsnotiz deaktiviert."


def _get_vacation_sync() -> dict:
    """Synchroner Kern fuer get_vacation — laeuft im Thread-Pool."""
    try:
        service = _get_gmail_service()
        return service.users().settings().getVacation(userId="me").execute()
    except RuntimeError as e:
        return {"error": str(e)}
    except Exception as e:
        log.warning("Gmail getVacation fehlgeschlagen: %s", e)
        return {"error": str(e)}


async def set_vacation(
    enabled: bool,
    subject: str = "",
    body: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Abwesenheitsnotiz setzen oder deaktivieren.

    Args:
        enabled: True um die Abwesenheitsnotiz zu aktivieren, False zum
            Deaktivieren.
        subject: Betreff der Abwesenheitsnotiz (nur bei enabled=True relevant).
        body: Text der Abwesenheitsnotiz (nur bei enabled=True relevant).
        start_date: Startdatum im Format 'YYYY-MM-DD', oder leer fuer sofort.
        end_date: Enddatum im Format 'YYYY-MM-DD', oder leer fuer unbefristet.

    Returns:
        Statusmeldung als String.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _set_vacation_sync, enabled, subject, body, start_date, end_date
    )


async def get_vacation() -> dict:
    """Aktuelle Abwesenheitsnotiz-Einstellungen abrufen.

    Returns:
        Dict mit den Gmail-Vacation-Einstellungen, oder {"error": "..."} bei
        Fehler. Relevante Felder: enableAutoReply, responseSubject,
        responseBodyPlainText, startTime, endTime.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_vacation_sync)
