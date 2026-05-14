"""
Kontakte-Bridge fuer Jarvis (Issue #55, #115).

Primaeres Backend: Google Contacts (People API) — plattformuebergreifend,
funktioniert auf Raspberry Pi (Linux) und macOS.
Fallback: Apple Kontakte.app via AppleScript (nur macOS, wenn Google nicht
verfuegbar).

Google Contacts erfordert einmalige OAuth-Autorisierung via
scripts/google-auth.py mit dem Scope https://www.googleapis.com/auth/contacts.
Catrin muss token.json einmalig loeschen und scripts/google-auth.py neu
ausfuehren damit der neue Scope aktiv wird.

API:
- read_all_contacts() -> list[Contact]
- find_contacts_by_name(query) -> list[Contact]  (substring match)
- find_contact_by_email(email) -> Contact | None
- find_contact_by_phone(normalized_phone) -> Contact | None
- add_email_to_contact(contact_id, new_email, label="") -> bool
- add_phone_to_contact(contact_id, new_phone, label="") -> bool
- create_contact(name, emails, phones) -> contact_id | None

Telefonnummern werden vor Vergleichen normalisiert (alle Nicht-Ziffern
weg, fuehrendes "+49"/"0049" -> "0").
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import settings as S

# Apple Contacts is only accessible on macOS via osascript.
# On Raspberry Pi / Linux all operations return empty results gracefully.
_MACOS = sys.platform == "darwin"

log = S.log

# ---------------------------------------------------------------------------
# Backend-Auswahl: Google Contacts bevorzugt (plattformuebergreifend).
# Voraussetzung: google-api-python-client installiert UND token.json vorhanden.
# ---------------------------------------------------------------------------
_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")

try:
    import google_contacts_tools as _google_contacts
    _USE_GOOGLE = os.path.exists(_TOKEN_PATH)
    if _USE_GOOGLE:
        log.info("contacts: Google Contacts Backend aktiv")
    else:
        log.info(
            "contacts: google_contacts_tools importiert, aber token.json "
            "fehlt — Apple Contacts Fallback"
        )
except ImportError:
    _google_contacts = None  # type: ignore[assignment]
    _USE_GOOGLE = False
    log.info("contacts: google_contacts_tools nicht verfuegbar — Apple Contacts Fallback")


@dataclass
class Contact:
    """Ein Kontakt mit den Feldern, die Jarvis braucht.

    id: Apple Contacts UUID oder Google People-API resourceName.
    """
    id: str
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    organization: str = ""


# Normalisierungs-Helper fuer Telefonnummern: identisch behandeln egal
# wie der User sie eingegeben hat.
_PHONE_DROP = re.compile(r"[^\d+]")


def normalize_phone(phone: str) -> str:
    """Normalisiert eine Telefonnummer fuer den Vergleich.
    +49 / 0049 / 0 werden alle zu '0' am Anfang. Alles andere wird auf
    Ziffern reduziert."""
    if not phone:
        return ""
    s = phone.strip()
    s = _PHONE_DROP.sub("", s)
    if s.startswith("+49"):
        s = "0" + s[3:]
    elif s.startswith("0049"):
        s = "0" + s[4:]
    elif s.startswith("+"):
        # Andere Vorwahl: + bleibt drin (zur Unterscheidung)
        pass
    return s


# ---------------------------------------------------------------------------
# AppleScript-Helper (Fallback fuer macOS ohne Google-Tokens)
# ---------------------------------------------------------------------------
async def _run_osascript(script: str, timeout: float = 15.0) -> str:
    """Run osascript and return stdout. Raises RuntimeError on non-zero."""
    loop = asyncio.get_running_loop()

    def _blocking():
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
        return result.stdout

    return await loop.run_in_executor(None, _blocking)


# AppleScript dump: alle Kontakte als JSON-String. Wir bauen die JSON
# inline in AppleScript zusammen damit wir keinen brittlen Text-Parser
# brauchen. Felder pro Kontakt: id, name, emails, phones, organization.
_DUMP_ALL_AS = '''
tell application "Contacts"
    set out to "["
    set first_person to true
    repeat with p in people
        set p_id to id of p
        set p_name to name of p
        if p_name is missing value then
            set p_name to ""
        end if
        set p_org to organization of p
        if p_org is missing value then
            set p_org to ""
        end if
        set emails_str to ""
        set first_email to true
        repeat with e in emails of p
            set e_val to value of e
            if first_email is true then
                set first_email to false
            else
                set emails_str to emails_str & ","
            end if
            set emails_str to emails_str & "\\"" & my js_escape(e_val) & "\\""
        end repeat
        set phones_str to ""
        set first_phone to true
        repeat with ph in phones of p
            set ph_val to value of ph
            if first_phone is true then
                set first_phone to false
            else
                set phones_str to phones_str & ","
            end if
            set phones_str to phones_str & "\\"" & my js_escape(ph_val) & "\\""
        end repeat
        if first_person is true then
            set first_person to false
        else
            set out to out & ","
        end if
        set out to out & "{\\"id\\":\\"" & my js_escape(p_id) & "\\","
        set out to out & "\\"name\\":\\"" & my js_escape(p_name) & "\\","
        set out to out & "\\"organization\\":\\"" & my js_escape(p_org) & "\\","
        set out to out & "\\"emails\\":[" & emails_str & "],"
        set out to out & "\\"phones\\":[" & phones_str & "]}"
    end repeat
    set out to out & "]"
    return out
end tell

on js_escape(s)
    set s to s as text
    set out to ""
    repeat with c in characters of s
        set ch to c as text
        set n to ASCII number of ch
        if ch is "\\\\" then
            set out to out & "\\\\\\\\"
        else if ch is "\\"" then
            set out to out & "\\\\\\""
        else if n = 9 or n = 10 or n = 13 then
            -- Tab, LF, CR — iCloud-Sync can inject these; replace with space
            set out to out & " "
        else if n < 32 then
            set out to out & " "
        else
            set out to out & ch
        end if
    end repeat
    return out
end js_escape
'''


_CONTACTS_CACHE: list[Contact] = []
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache_lock = asyncio.Lock()


async def read_all_contacts(force_refresh: bool = False) -> list[Contact]:
    """Hol alle Kontakte. Cache fuer 5 Minuten.

    Primaeres Backend: Google Contacts (plattformuebergreifend, Pi + Mac).
    Fallback: Apple Kontakte.app via AppleScript (nur macOS).

    Verwendet asyncio.Lock mit Double-Checked Locking (Issue #97):
    Der schnelle Pfad (Cache gueltig) prueft ohne Lock. Nur beim
    tatsaechlichen Refresh wird der Lock gehalten, damit gleichzeitige
    async-Aufrufe (z.B. Mail-Klassifikation + Drift-Detection) keinen
    doppelten API-Aufruf ausloesen.
    """
    if _USE_GOOGLE:
        # Google Contacts: Contact-Objekte sind strukturell identisch
        google_contacts = await _google_contacts.read_all_contacts(
            force_refresh=force_refresh
        )
        return [
            Contact(
                id=c.id,
                name=c.name,
                emails=c.emails,
                phones=c.phones,
                organization=c.organization,
            )
            for c in google_contacts
        ]

    if not _MACOS:
        return []
    global _CONTACTS_CACHE, _CACHE_TIMESTAMP
    now = time.time()
    # Fast path: Cache gueltig — kein Lock noetig.
    if (not force_refresh
            and _CONTACTS_CACHE
            and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS):
        return _CONTACTS_CACHE

    async with _cache_lock:
        # Double-check innerhalb des Locks: ein anderer Coroutine koennte
        # den Cache bereits aktualisiert haben waehrend wir gewartet haben.
        now = time.time()
        if (not force_refresh
                and _CONTACTS_CACHE
                and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS):
            return _CONTACTS_CACHE

        try:
            raw = await _run_osascript(_DUMP_ALL_AS, timeout=30.0)
            try:
                data = json.loads(raw.strip())
            except json.JSONDecodeError as e:
                log.warning(
                    f"contacts: JSON parse failed: {e}, "
                    f"raw[:200]={raw[:200]!r}"
                )
                return _CONTACTS_CACHE  # stale-on-error
        except Exception as e:
            log.warning(f"contacts.read_all_contacts failed: "
                        f"{type(e).__name__}: {e}")
            return _CONTACTS_CACHE  # stale-on-error
        contacts = [
            Contact(
                id=item.get("id", ""),
                name=item.get("name", "").strip(),
                emails=[e.lower() for e in item.get("emails", []) if e],
                phones=[p for p in item.get("phones", []) if p],
                organization=item.get("organization", "").strip(),
            )
            for item in data
        ]
        if not contacts:
            log.info(
                "contacts: loaded 0 contacts — "
                "Kontakte.app leer oder AppleScript-Ergebnis leer"
            )
        else:
            log.info(
                f"contacts: loaded {len(contacts)} from Apple Contacts.app"
            )
        _CONTACTS_CACHE = contacts
        _CACHE_TIMESTAMP = now
        return contacts


async def find_contacts_by_name(query: str) -> list[Contact]:
    """Substring-Match (case-insensitive) auf Kontakt-Namen und Organisation.

    Firmen-Kontakte haben oft keinen Personennamen — deshalb wird zusaetzlich
    auf c.organization gematcht (Issue #71).
    Liefert ALLE Treffer — Disambiguation macht der Aufrufer.

    Delegiert an Google Contacts wenn verfuegbar.
    """
    if _USE_GOOGLE:
        google_contacts = await _google_contacts.find_contacts_by_name(query)
        return [
            Contact(
                id=c.id, name=c.name, emails=c.emails,
                phones=c.phones, organization=c.organization,
            )
            for c in google_contacts
        ]
    if not query:
        return []
    q = query.lower().strip()
    all_contacts = await read_all_contacts()
    return [
        c for c in all_contacts
        if q in c.name.lower() or q in c.organization.lower()
    ]


async def find_contact_by_email(email: str) -> Contact | None:
    """Exakter Match auf Email-Adresse (case-insensitive).

    Delegiert an Google Contacts wenn verfuegbar.
    """
    if _USE_GOOGLE:
        gc = await _google_contacts.find_contact_by_email(email)
        if gc is None:
            return None
        return Contact(
            id=gc.id, name=gc.name, emails=gc.emails,
            phones=gc.phones, organization=gc.organization,
        )
    if not email:
        return None
    target = email.lower().strip()
    all_contacts = await read_all_contacts()
    for c in all_contacts:
        if target in c.emails:
            return c
    return None


async def find_contact_by_phone(phone: str) -> Contact | None:
    """Match auf normalisierte Telefonnummer."""
    if not phone:
        return None
    target = normalize_phone(phone)
    if not target:
        return None
    all_contacts = await read_all_contacts()
    for c in all_contacts:
        for p in c.phones:
            if normalize_phone(p) == target:
                return c
    return None


# ---------------------------------------------------------------------------
# Schreib-Operationen — werden NUR mit Catrins Bestaetigung gerufen.
# ---------------------------------------------------------------------------
def _escape_as(s: str) -> str:
    """AppleScript-Escape fuer doppelt-gequotete Strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def add_email_to_contact(contact_id: str, new_email: str,
                                label: str = "work") -> bool:
    """Haengt eine Mail-Adresse an einen Kontakt an (vorhandene bleiben).

    Delegiert an Google Contacts wenn verfuegbar, sonst Apple Contacts.
    """
    if _USE_GOOGLE:
        # update_contact_email mit leerem old_email = Append-Modus
        return await _google_contacts.update_contact_email(
            contact_id, "", new_email
        )
    if not contact_id or not new_email:
        return False
    script = f'''
tell application "Contacts"
    set p to first person whose id is "{_escape_as(contact_id)}"
    make new email at end of emails of p with properties {{label:"{_escape_as(label)}", value:"{_escape_as(new_email)}"}}
    save
end tell
'''
    try:
        await _run_osascript(script, timeout=10.0)
        # Cache invalidieren damit naechste Lese-Op den neuen Stand sieht
        global _CACHE_TIMESTAMP
        _CACHE_TIMESTAMP = 0
        log.info(f"contacts: added email {new_email!r} to contact {contact_id}")
        return True
    except Exception as e:
        log.warning(f"contacts.add_email_to_contact failed: "
                    f"{type(e).__name__}: {e}")
        return False


async def add_phone_to_contact(contact_id: str, new_phone: str,
                                label: str = "mobile") -> bool:
    """Haengt eine Telefonnummer an einen Kontakt an (vorhandene bleiben).

    Delegiert an Google Contacts wenn verfuegbar, sonst Apple Contacts.
    """
    if _USE_GOOGLE:
        return await _google_contacts.update_contact_phone(contact_id, new_phone)
    if not contact_id or not new_phone:
        return False
    script = f'''
tell application "Contacts"
    set p to first person whose id is "{_escape_as(contact_id)}"
    make new phone at end of phones of p with properties {{label:"{_escape_as(label)}", value:"{_escape_as(new_phone)}"}}
    save
end tell
'''
    try:
        await _run_osascript(script, timeout=10.0)
        global _CACHE_TIMESTAMP
        _CACHE_TIMESTAMP = 0
        log.info(f"contacts: added phone {new_phone!r} to contact {contact_id}")
        return True
    except Exception as e:
        log.warning(f"contacts.add_phone_to_contact failed: "
                    f"{type(e).__name__}: {e}")
        return False


async def create_contact(
    name: str,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    organization: str = "",
) -> Optional[str]:
    """Legt einen neuen Kontakt an. Liefert die ID oder None.

    Delegiert an Google Contacts wenn verfuegbar, sonst Apple Contacts.
    """
    if _USE_GOOGLE:
        primary_email = (emails or [""])[0]
        return await _google_contacts.create_contact(
            name=name,
            email=primary_email,
            phones=phones or [],
            organization=organization,
        )
    if not name:
        return None
    parts = name.split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    email_lines = "\n".join(
        f'    make new email at end of emails of new_p with properties {{label:"work", value:"{_escape_as(e)}"}}'
        for e in (emails or [])
    )
    phone_lines = "\n".join(
        f'    make new phone at end of phones of new_p with properties {{label:"mobile", value:"{_escape_as(p)}"}}'
        for p in (phones or [])
    )
    org_line = (
        f'    set organization of new_p to "{_escape_as(organization)}"'
        if organization else ""
    )
    script = f'''
tell application "Contacts"
    set new_p to make new person with properties {{first name:"{_escape_as(first)}", last name:"{_escape_as(last)}"}}
{email_lines}
{phone_lines}
{org_line}
    save
    return id of new_p
end tell
'''
    try:
        out = await _run_osascript(script, timeout=10.0)
        new_id = out.strip()
        global _CACHE_TIMESTAMP
        _CACHE_TIMESTAMP = 0
        log.info(f"contacts: created {name!r} -> {new_id}")
        return new_id
    except Exception as e:
        log.warning(f"contacts.create_contact failed: "
                    f"{type(e).__name__}: {e}")
        return None
