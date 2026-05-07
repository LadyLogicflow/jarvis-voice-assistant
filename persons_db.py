"""
Persistente Personen-DB (Issue #55).

Eigene Schicht ueber Apple Kontakte.app: speichert Catrins Anreden,
Funktionen, letzten Kontakt-Anlaesse und offenen Punkte. Kontakte.app
liefert nur Name/Email/Phone — alles drueber lebt hier.

Keying: Apple-Kontakt-ID (stable across name changes). Wenn ein
Kontakt nur in der DB existiert (manuell ergaenzt) ohne Apple-Pendant,
ist die ID einfach ein UUID-String.

Persistenz: JSON in .jarvis_persons.json (gitignored).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field

import settings as S

log = S.log


@dataclass
class PersonProfile:
    """Catrins zusaetzliches Wissen ueber eine Person — was nicht in
    Apple Kontakte steht."""
    contact_id: str                       # Apple-Kontakt-ID oder UUID
    name: str                             # Display-Name (Cache aus Kontakten)
    anrede: str = ""                      # "Herr Mueller" / "Du, Max" / "Sehr geehrte Frau Schmidt"
    funktion: str = ""                    # "Mandant Steuererklaerung" / "Kollege HILO"
    last_contact: str = ""                # ISO-Datum oder freier Text
    open_points: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    primary_email: str = ""
    secondary_emails: list[str] = field(default_factory=list)
    primary_phone: str = ""
    secondary_phones: list[str] = field(default_factory=list)


_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_persons.json")
_persons: dict[str, PersonProfile] = {}
_loaded = False


def _load() -> None:
    """Einmal beim ersten Zugriff aus JSON laden."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(_DB_PATH):
        return
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for cid, item in raw.items():
            if isinstance(item, dict):
                _persons[cid] = PersonProfile(**{k: v for k, v in item.items() if k in PersonProfile.__dataclass_fields__})
        log.info(f"persons_db: loaded {len(_persons)} profiles from disk")
    except Exception as e:
        log.warning(f"persons_db._load failed: {type(e).__name__}: {e}")


def _save() -> None:
    """Atomar schreiben — never crash the request path."""
    try:
        tmp = _DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {cid: asdict(p) for cid, p in _persons.items()},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, _DB_PATH)
    except Exception as e:
        log.warning(f"persons_db._save failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get(contact_id: str) -> PersonProfile | None:
    _load()
    return _persons.get(contact_id)


def all_profiles() -> list[PersonProfile]:
    _load()
    return list(_persons.values())


def upsert(profile: PersonProfile) -> None:
    """Einfuegen oder aktualisieren."""
    _load()
    _persons[profile.contact_id] = profile
    _save()


def delete(contact_id: str) -> bool:
    _load()
    if contact_id in _persons:
        del _persons[contact_id]
        _save()
        return True
    return False


def find_by_email(email: str) -> PersonProfile | None:
    """Sucht ein Profil dessen primary oder secondary Email gleich
    der gegebenen ist (case-insensitive)."""
    if not email:
        return None
    target = email.lower().strip()
    _load()
    for p in _persons.values():
        if p.primary_email.lower() == target:
            return p
        if any(e.lower() == target for e in p.secondary_emails):
            return p
    return None


def find_by_phone_normalized(normalized: str) -> PersonProfile | None:
    if not normalized:
        return None
    _load()
    from contacts import normalize_phone
    for p in _persons.values():
        if normalize_phone(p.primary_phone) == normalized:
            return p
        if any(normalize_phone(x) == normalized for x in p.secondary_phones):
            return p
    return None


def new_id() -> str:
    """Gibt eine UUID fuer Profile zurueck die kein Apple-Kontakt-Pendant
    haben (manuell ergaenzte Personen)."""
    return f"manual-{uuid.uuid4()}"


def add_open_point(contact_id: str, point: str) -> bool:
    _load()
    p = _persons.get(contact_id)
    if not p:
        return False
    if point not in p.open_points:
        p.open_points.append(point)
        _save()
    return True


def add_note(contact_id: str, note: str) -> bool:
    _load()
    p = _persons.get(contact_id)
    if not p:
        return False
    p.notes.append(note)
    _save()
    return True


def add_secondary_email(contact_id: str, email: str) -> bool:
    """Addiert eine sekundaere Email (z.B. nach Email-Drift-Update).
    Vorhandene primary bleibt."""
    _load()
    p = _persons.get(contact_id)
    if not p:
        return False
    if email and email.lower() not in [e.lower() for e in p.secondary_emails]:
        p.secondary_emails.append(email)
        _save()
    return True


def promote_email_to_primary(contact_id: str, new_primary: str) -> bool:
    """Setzt eine neue primaere Email; alte primaere wandert in
    secondary_emails (falls noch nicht drin)."""
    _load()
    p = _persons.get(contact_id)
    if not p or not new_primary:
        return False
    if p.primary_email and p.primary_email.lower() != new_primary.lower():
        if p.primary_email.lower() not in [e.lower() for e in p.secondary_emails]:
            p.secondary_emails.append(p.primary_email)
    p.primary_email = new_primary
    _save()
    return True


def add_secondary_phone(contact_id: str, phone: str) -> bool:
    _load()
    p = _persons.get(contact_id)
    if not p:
        return False
    from contacts import normalize_phone
    norm_new = normalize_phone(phone)
    norm_existing = {normalize_phone(x) for x in [p.primary_phone] + p.secondary_phones if x}
    if phone and norm_new not in norm_existing:
        p.secondary_phones.append(phone)
        _save()
    return True
