"""
Telefon-Wahl via macOS tel:-URL (Issue #67).

Catrin sagt "rufe Mueller an" -> Lookup in persons_db + Apple Contacts
-> bei mehreren Nummern Auswahl-Liste -> tel:-URL via `open` triggert
FaceTime / iPhone-Continuity. Catrin fuehrt das Gespraech selbst,
Jarvis spricht NICHT mit dem Gegenueber.

API:
- find_callable(query) -> list[(name, label, number)]
- start_call(number) -> bool  (offnet tel:-URL via macOS open)
"""

from __future__ import annotations

import asyncio
import subprocess

import contacts
import persons_db
import settings as S

log = S.log


async def find_callable(query: str) -> list[tuple[str, str, str]]:
    """Sucht nach Personen mit dem Namen `query` und liefert eine
    Liste von (Name, Label, Nummer)-Tripeln. Quelle: persons_db UND
    Apple Contacts.

    Disambiguation:
    - mehrere Personen mit dem Namen + alle ihre Nummern
    - leere Liste wenn niemand passt
    """
    if not query:
        return []
    seen_ids: set[str] = set()
    out: list[tuple[str, str, str]] = []

    # 1. persons_db (kann zusaetzliche Felder haben die Apple nicht hat)
    for p in persons_db.all_profiles():
        if query.lower() not in p.name.lower():
            continue
        seen_ids.add(p.contact_id)
        if p.primary_phone:
            out.append((p.name, "primary", p.primary_phone))
        for ph in p.secondary_phones:
            out.append((p.name, "weitere", ph))

    # 2. Apple Contacts
    apple_matches = await contacts.find_contacts_by_name(query)
    for c in apple_matches:
        if c.id in seen_ids:
            continue  # bereits ueber persons_db abgedeckt
        # Apple-Kontakt hat keine Label-Zuordnung in unserer Bridge,
        # nur die Nummern. Erste Nummer ist konventionell die wichtigste.
        for i, ph in enumerate(c.phones):
            label = "primary" if i == 0 else "weitere"
            out.append((c.name, label, ph))

    return out


async def start_call(number: str) -> bool:
    """Triggert einen Telefonanruf via tel:-URL. macOS oeffnet das mit
    FaceTime (welches den Anruf an's verbundene iPhone leitet, wenn
    Continuity aktiv ist). Catrin nimmt am Mac/iPhone das Gespraech
    auf, Jarvis ist nicht beteiligt.

    Liefert True wenn das `open`-Kommando erfolgreich abgesetzt wurde
    (sagt aber nichts darueber ob der Anruf wirklich zustande kommt)."""
    if not number:
        return False
    # Wir senden die unnormalisierte Nummer — macOS' tel:-Handler
    # versteht alle ueblichen Schreibweisen.
    url = f"tel:{number}"
    loop = asyncio.get_running_loop()

    def _blocking():
        return subprocess.run(
            ["open", url],
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        result = await loop.run_in_executor(None, _blocking)
        if result.returncode == 0:
            log.info(f"phone: dialed {number!r}")
            return True
        log.warning(f"phone: open returned {result.returncode}: {result.stderr.strip()}")
        return False
    except Exception as e:
        log.warning(f"phone.start_call failed: {type(e).__name__}: {e}")
        return False
