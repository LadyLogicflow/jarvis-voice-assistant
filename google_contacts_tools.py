"""
Google Contacts (People API) — Cross-platform replacement for contacts.py AppleScript.
Works on Raspberry Pi (Linux) and macOS.
Requires OAuth scope: https://www.googleapis.com/auth/contacts

Nutzt dieselben OAuth-Credentials wie google_calendar_tools.py (token.json +
credentials.json im Projekt-Root). Catrin muss token.json einmalig loeschen
und scripts/google-auth.py neu ausfuehren damit der contacts-Scope aktiv wird.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import settings as S

log = logging.getLogger("jarvis.contacts_google")

# OAuth-Scopes — alle drei Scopes muessen in einem token.json stehen damit
# Kalender (Issue #55), Gmail-Einstellungen (Issue #111) und Contacts
# (Issue #115) alle mit demselben Token funktionieren.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/contacts",
]

TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CREDS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")

# Thread-Lock fuer Token-Refresh (analog zu google_calendar_tools.py, Fix #61)
_token_refresh_lock = threading.Lock()


@dataclass
class Contact:
    """Ein Google-Kontakt mit den Feldern, die Jarvis braucht.

    id entspricht dem People-API resourceName (z.B. 'people/c12345').
    """
    id: str           # resourceName, z.B. "people/c1234567890"
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    organization: str = ""


# ---------------------------------------------------------------------------
# Service-Factory
# ---------------------------------------------------------------------------

def _get_service():
    """Baut einen authentifizierten People-API-Service.

    Analog zu _get_service() in google_calendar_tools.py — Token-Refresh
    mit Lock gegen Race Conditions.

    Raises:
        RuntimeError: Wenn kein gueltiges token.json vorhanden ist.
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
                "Google Contacts nicht autorisiert. "
                "Bitte 'python3 scripts/google-auth.py' ausfuehren."
            )
    return build("people", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CONTACTS_CACHE: list[Contact] = []
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL_SECONDS = 300  # 5 Minuten — identisch zu contacts.py
_cache_lock = asyncio.Lock()


def _parse_contact(person: dict) -> Contact:
    """Wandelt ein People-API person-Dict in ein Contact-Objekt um."""
    resource_name = person.get("resourceName", "")

    # Name
    names = person.get("names", [])
    display_name = names[0].get("displayName", "").strip() if names else ""

    # Emails (lowercase, ohne Leerzeichen)
    emails = [
        e.get("value", "").lower().strip()
        for e in person.get("emailAddresses", [])
        if e.get("value")
    ]

    # Telefonnummern
    phones = [
        p.get("value", "").strip()
        for p in person.get("phoneNumbers", [])
        if p.get("value")
    ]

    # Organisation
    orgs = person.get("organizations", [])
    organization = orgs[0].get("name", "").strip() if orgs else ""

    return Contact(
        id=resource_name,
        name=display_name,
        emails=emails,
        phones=phones,
        organization=organization,
    )


async def read_all_contacts(force_refresh: bool = False) -> list[Contact]:
    """Laedt alle Google-Kontakte. Cache fuer 5 Minuten.

    Verwendet asyncio.Lock mit Double-Checked Locking (analog zu contacts.py,
    Issue #97) um doppelte API-Aufrufe bei parallelen Coroutinen zu vermeiden.

    Returns:
        Liste aller Kontakte. Bei Fehler: gecachte Liste (stale-on-error).
    """
    global _CONTACTS_CACHE, _CACHE_TIMESTAMP
    now = time.time()

    # Fast path: Cache gueltig — kein Lock noetig.
    if (not force_refresh
            and _CONTACTS_CACHE
            and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS):
        return _CONTACTS_CACHE

    async with _cache_lock:
        # Double-check innerhalb des Locks
        now = time.time()
        if (not force_refresh
                and _CONTACTS_CACHE
                and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS):
            return _CONTACTS_CACHE

        loop = asyncio.get_running_loop()
        try:
            contacts = await loop.run_in_executor(None, _fetch_all_contacts_sync)
        except Exception as e:
            log.warning(
                "google_contacts: read_all_contacts failed: "
                "%s: %s", type(e).__name__, e
            )
            return _CONTACTS_CACHE  # stale-on-error

        if not contacts:
            log.info(
                "google_contacts: loaded 0 contacts — "
                "Google Contacts leer oder API-Ergebnis leer"
            )
        else:
            log.info("google_contacts: loaded %d contacts", len(contacts))

        _CONTACTS_CACHE = contacts
        _CACHE_TIMESTAMP = now
        return contacts


def _fetch_all_contacts_sync() -> list[Contact]:
    """Synchroner Fetch aller Kontakte (wird im ThreadPoolExecutor ausgefuehrt)."""
    service = _get_service()
    contacts: list[Contact] = []
    page_token = None

    while True:
        kwargs: dict = {
            "resourceName": "people/me",
            "pageSize": 1000,
            "personFields": "names,emailAddresses,phoneNumbers,organizations",
        }
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.people().connections().list(**kwargs).execute()
        connections = result.get("connections", [])
        contacts.extend(_parse_contact(p) for p in connections)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return contacts


# ---------------------------------------------------------------------------
# Lese-Operationen
# ---------------------------------------------------------------------------

async def find_contacts_by_name(query: str) -> list[Contact]:
    """Substring-Match (case-insensitive) auf Name und Organisation.

    Args:
        query: Suchstring.

    Returns:
        Alle Kontakte deren Name oder Organisation den Suchstring enthalten.
    """
    if not query:
        return []
    q = query.lower().strip()
    all_contacts = await read_all_contacts()
    return [
        c for c in all_contacts
        if q in c.name.lower() or q in c.organization.lower()
    ]


async def find_contact_by_email(email: str) -> Optional[Contact]:
    """Exakter Match auf Email-Adresse (case-insensitive).

    Args:
        email: Zu suchende Email-Adresse.

    Returns:
        Erster Kontakt mit dieser Email, oder None.
    """
    if not email:
        return None
    target = email.lower().strip()
    all_contacts = await read_all_contacts()
    for c in all_contacts:
        if target in c.emails:
            return c
    return None


# ---------------------------------------------------------------------------
# Schreib-Operationen — werden NUR mit Catrins Bestaetigung gerufen.
# ---------------------------------------------------------------------------

def _invalidate_cache() -> None:
    """Setzt den Cache-Timestamp zurueck damit die naechste Lese-Op frische
    Daten holt."""
    global _CACHE_TIMESTAMP
    _CACHE_TIMESTAMP = 0.0


async def update_contact_email(
    resource_name: str, old_email: str, new_email: str
) -> bool:
    """Ersetzt eine Email-Adresse in einem Google-Kontakt.

    Liest den aktuellen Kontakt, tauscht old_email gegen new_email,
    und sendet updateContact() mit dem aktuellen etag.

    Args:
        resource_name: People-API resourceName (z.B. 'people/c12345').
        old_email: Bisherige Email-Adresse (case-insensitive Match).
        new_email: Neue Email-Adresse.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    if not resource_name or not new_email:
        return False

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            _update_contact_email_sync,
            resource_name,
            old_email,
            new_email,
        )
        if result:
            _invalidate_cache()
        return result
    except Exception as e:
        log.warning(
            "google_contacts.update_contact_email failed: %s: %s",
            type(e).__name__, e
        )
        return False


def _update_contact_email_sync(
    resource_name: str, old_email: str, new_email: str
) -> bool:
    service = _get_service()
    person = service.people().get(
        resourceName=resource_name,
        personFields="names,emailAddresses,phoneNumbers,metadata",
    ).execute()

    email_list = person.get("emailAddresses", [])
    new_lower = new_email.lower().strip()

    if not old_email:
        # Append-Modus: immer als neue Adresse hinzufügen
        email_list.append({"value": new_lower})
    else:
        # Ersetze-Modus: alten Wert suchen und austauschen
        old_lower = old_email.lower().strip()
        replaced = False
        for entry in email_list:
            if entry.get("value", "").lower().strip() == old_lower:
                entry["value"] = new_lower
                replaced = True
                break
        if not replaced:
            email_list.append({"value": new_lower})

    person["emailAddresses"] = email_list

    service.people().updateContact(
        resourceName=resource_name,
        updatePersonFields="emailAddresses",
        body=person,
    ).execute()
    log.info(
        "google_contacts: updated email %r -> %r for %s",
        old_email, new_email, resource_name
    )
    return True


async def update_contact_phone(resource_name: str, new_phone: str) -> bool:
    """Haengt eine neue Telefonnummer an einen Google-Kontakt an.

    Vorhandene Nummern bleiben erhalten (additive Aenderung).

    Args:
        resource_name: People-API resourceName.
        new_phone: Neue Telefonnummer.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    if not resource_name or not new_phone:
        return False

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            _update_contact_phone_sync,
            resource_name,
            new_phone,
        )
        if result:
            _invalidate_cache()
        return result
    except Exception as e:
        log.warning(
            "google_contacts.update_contact_phone failed: %s: %s",
            type(e).__name__, e
        )
        return False


def _update_contact_phone_sync(resource_name: str, new_phone: str) -> bool:
    service = _get_service()
    person = service.people().get(
        resourceName=resource_name,
        personFields="names,emailAddresses,phoneNumbers,metadata",
    ).execute()

    phone_list = person.get("phoneNumbers", [])
    phone_list.append({"value": new_phone})
    person["phoneNumbers"] = phone_list

    service.people().updateContact(
        resourceName=resource_name,
        updatePersonFields="phoneNumbers",
        body=person,
    ).execute()
    log.info(
        "google_contacts: added phone %r to %s",
        new_phone, resource_name
    )
    return True


async def create_contact(
    name: str,
    email: str,
    phones: list[str],
    organization: str = "",
) -> Optional[str]:
    """Legt einen neuen Google-Kontakt an.

    Args:
        name: Vollstaendiger Name.
        email: Primaere Email-Adresse.
        phones: Liste von Telefonnummern (kann leer sein).
        organization: Firma / Organisation (optional).

    Returns:
        resourceName des neuen Kontakts, oder None bei Fehler.
    """
    if not name:
        return None

    loop = asyncio.get_running_loop()
    try:
        resource_name = await loop.run_in_executor(
            None,
            _create_contact_sync,
            name,
            email,
            phones,
            organization,
        )
        if resource_name:
            _invalidate_cache()
        return resource_name
    except Exception as e:
        log.warning(
            "google_contacts.create_contact failed: %s: %s",
            type(e).__name__, e
        )
        return None


def _create_contact_sync(
    name: str,
    email: str,
    phones: list[str],
    organization: str = "",
) -> Optional[str]:
    service = _get_service()

    body: dict = {
        "names": [{"displayName": name}],
    }
    if email:
        body["emailAddresses"] = [{"value": email.lower().strip()}]
    if phones:
        body["phoneNumbers"] = [{"value": p} for p in phones if p]
    if organization:
        body["organizations"] = [{"name": organization}]

    created = service.people().createContact(body=body).execute()
    resource_name = created.get("resourceName")
    log.info(
        "google_contacts: created %r -> %s",
        name, resource_name
    )
    return resource_name
