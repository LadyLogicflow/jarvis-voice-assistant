"""
Automatische Personen-Kontextverknuepfung aus allen Quellen.

Scannt Aufgaben-Titel und Kalender-Event-Beschreibungen auf bekannte
Kontaktnamen und schreibt automatisch datierte Notizen in persons_db.
Keine Rueckfrage, kein manueller Aufwand.

Aufgerufen nach dem Laden von Tasks und Kalender-Events.
"""
from __future__ import annotations

import datetime
import logging
import re

log = logging.getLogger("jarvis.person_enrichment")

_MIN_NAME_LEN = 4  # Kuerzere Tokens koennen zu viele False-Positives erzeugen


def _build_name_index() -> list[tuple[str, object]]:
    """Baut einen nach Laenge absteigend sortierten Index (search_term, profile).

    Laengere Treffer werden bevorzugt, damit 'Mueller-Schmidt' vor 'Mueller' matcht.
    Nur Namen mit mindestens _MIN_NAME_LEN Zeichen werden indiziert.
    """
    import persons_db
    result: list[tuple[str, object]] = []
    seen_terms: set[str] = set()
    for p in persons_db.all_profiles():
        if not p.name:
            continue
        # Vollstaendiger Name zuerst
        full = p.name.strip()
        if len(full) >= _MIN_NAME_LEN and full.lower() not in seen_terms:
            result.append((full.lower(), p))
            seen_terms.add(full.lower())
        # Einzelne Namens-Tokens (Vor- und Nachname)
        for part in full.split():
            part_clean = part.strip("-/")
            if len(part_clean) >= _MIN_NAME_LEN and part_clean.lower() not in seen_terms:
                result.append((part_clean.lower(), p))
                seen_terms.add(part_clean.lower())
    result.sort(key=lambda x: -len(x[0]))
    return result


def find_persons_in_text(text: str) -> list:
    """Gibt alle PersonProfile-Objekte zurueck deren Namen als Wort in text vorkommen."""
    if not text:
        return []
    found: list = []
    seen_ids: set[str] = set()
    for term, profile in _build_name_index():
        if profile.contact_id in seen_ids:
            continue
        # Wortgrenze-Match: vermeidet "Schmidt" in "Schmidtchen"
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
            found.append(profile)
            seen_ids.add(profile.contact_id)
    return found


def enrich_from_texts(
    texts: list[str],
    source_label: str,
    today: str | None = None,
) -> int:
    """Scannt eine Liste von Texten und verknuepft Personen-Profile automatisch.

    Args:
        texts:        Liste von Texten (Aufgaben-Titel, Event-Titel, etc.)
        source_label: Bezeichnung der Quelle fuer den Notiz-Text (z.B. "Aufgabe", "Termin")
        today:        ISO-Datum; Default: heute

    Returns:
        Anzahl der aktualisierten Profile.
    """
    import persons_db
    if today is None:
        today = datetime.date.today().isoformat()
    updated = 0
    for text in texts:
        if not text:
            continue
        for profile in find_persons_in_text(text):
            note = f"{today}: {source_label} — {text}"
            if note not in profile.notes:
                profile.notes.append(note)
                persons_db.upsert(profile)
                updated += 1
                log.debug("person_enrichment: '%s' -> %s (%s)", text, profile.name, source_label)
    return updated
