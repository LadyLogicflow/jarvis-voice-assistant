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
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field

import settings as S

log = S.log


def _norm(s: str) -> str:
    """Lowercase + strip diacritics for fuzzy name matching (ü→u, ä→a, ö→o, ß→ss)."""
    s = s.lower().replace("ß", "ss")
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def _name_tokens(s: str) -> set[str]:
    """Splits a name into normalized tokens, stripping punctuation.
    "Dr. Martin Türk" → {"martin", "turk"}  (min 2 chars, no titles).
    """
    import re
    _titles = {"dr", "prof", "mr", "mrs", "ms", "herr", "frau", "ing"}
    tokens = set()
    for tok in re.split(r"[\s,.\-/]+", s):
        n = _norm(tok)
        if len(n) >= 2 and n not in _titles:
            tokens.add(n)
    return tokens


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
    # Issue #212: Stichwort-Zusammenfassung der letzten eingehenden Mail.
    last_mail_topic: str = ""
    # Issue #109: Steuerbescheide und Vorauszahlungsbescheide fuer diesen Mandanten.
    # Jeder Eintrag ist ein dict wie von analyze_steuerbescheid() zurueckgegeben.
    tax_assessments: list[dict] = field(default_factory=list)
    advance_payments: list[dict] = field(default_factory=list)


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
    try:
        import memory_search as _ms
        import asyncio as _asyncio

        def _reindex_profile() -> None:
            for _note_text in profile.notes:
                if _note_text.strip():
                    _did = _ms.make_doc_id("person_note", f"{profile.contact_id}:{_note_text}")
                    _ms.index_text(
                        f"Notiz zu {profile.name}: {_note_text.strip()}",
                        "person", _did,
                        {"person_name": profile.name, "person_id": profile.contact_id},
                    )
            for _pt in profile.open_points:
                if _pt.strip():
                    _did = _ms.make_doc_id("person_open", f"{profile.contact_id}:{_pt}")
                    _ms.index_text(
                        f"Offener Punkt mit {profile.name}: {_pt.strip()}",
                        "person", _did,
                        {"person_name": profile.name, "person_id": profile.contact_id},
                    )

        try:
            _loop = _asyncio.get_running_loop()
            _loop.run_in_executor(None, _reindex_profile)
        except RuntimeError:
            _reindex_profile()
    except Exception:
        pass


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


def search_by_name(name: str) -> list[PersonProfile]:
    """Sucht Profile deren Vorname oder Nachname den Suchbegriff enthaelt.

    Die Suche ist case-insensitiv und verwendet Teil-String-Matching auf dem
    ``name``-Feld (Display-Name) der Profile. Ein leeres ``name``-Argument
    liefert eine leere Liste zurueck.

    Args:
        name: Suchbegriff (z.B. "Sandra", "Mueller").

    Returns:
        Liste aller passenden PersonProfile-Objekte, leer wenn kein Treffer.
    """
    if not name:
        return []
    _load()
    clean = name.strip()
    needle = _norm(clean)

    # Stufe 1: normalisierter Substring
    hits = [p for p in _persons.values() if p.name and needle in _norm(p.name)]
    if hits:
        return hits

    # Stufe 2: Token-Set
    tokens = _name_tokens(clean)
    if tokens:
        hits = [p for p in _persons.values() if p.name and tokens.issubset(_name_tokens(p.name))]
        if hits:
            return hits

    return []


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


# ---------------------------------------------------------------------------
# Issue #109 — Steuerbescheid-Persistenz
# ---------------------------------------------------------------------------

def _find_or_create_mandant(mandant: str) -> PersonProfile:
    """Sucht einen Mandanten case-insensitiv (Teilstring); legt ihn an falls
    kein Treffer.

    Args:
        mandant: Name wie vom LLM extrahiert.

    Returns:
        PersonProfile des gefundenen oder neu angelegten Mandanten.
    """
    _load()
    clean = mandant.strip()

    # Stufe 1: normalisierter Substring-Match (Umlaute ausgeglichen)
    needle = _norm(clean)
    matches = [p for p in _persons.values() if p.name and needle in _norm(p.name)]
    if matches:
        if len(matches) > 1:
            log.warning("persons_db: Mehrdeutiger Substring-Match %r — erster verwendet", mandant)
        log.info("persons_db: Mandant per Substring gefunden: %r → %s", mandant, matches[0].name)
        return matches[0]

    # Stufe 2: Token-Set-Match — alle signifikanten Namens-Tokens muessen
    # im Profil vorkommen (behandelt Wortfolge, Titel, OCR-Varianten)
    needle_tokens = _name_tokens(clean)
    if needle_tokens:
        token_matches = [
            p for p in _persons.values()
            if p.name and needle_tokens.issubset(_name_tokens(p.name))
        ]
        if token_matches:
            if len(token_matches) > 1:
                log.warning("persons_db: Mehrdeutiger Token-Match %r — erster verwendet", mandant)
            log.info("persons_db: Mandant per Token-Match gefunden: %r → %s", mandant, token_matches[0].name)
            return token_matches[0]

    # Stufe 3: Fuzzy-Match via difflib (Schwelle 0.75) — fuer OCR-Fehler
    import difflib
    candidates = [p for p in _persons.values() if p.name]
    if candidates:
        best = max(candidates, key=lambda p: difflib.SequenceMatcher(None, needle, _norm(p.name)).ratio())
        ratio = difflib.SequenceMatcher(None, needle, _norm(best.name)).ratio()
        if ratio >= 0.75:
            log.info("persons_db: Mandant per Fuzzy-Match (%.0f%%): %r → %s", ratio * 100, mandant, best.name)
            return best

    # Kein Treffer — neu anlegen
    cid = new_id()
    profile = PersonProfile(contact_id=cid, name=clean)
    _persons[cid] = profile
    _save()
    log.info("persons_db: Mandant neu angelegt: %s (%s)", clean, cid)
    return profile


def save_tax_assessment(mandant: str, data: dict) -> None:
    """Speichert einen Steuerbescheid-Datensatz beim Mandanten.

    Sucht den Mandanten case-insensitiv; legt ein neues Profil an wenn keins
    gefunden wird. Doppelte Eintraege (gleicher Typ + Steuerart + Steuerjahr
    + Ausstellungsdatum) werden uebersprungen.

    Args:
        mandant: Name des Steuerpflichtigen.
        data:    Strukturiertes dict wie von analyze_steuerbescheid() fuer Typ
                 "Steuerbescheid" zurueckgegeben.
    """
    profile = _find_or_create_mandant(mandant)
    # Duplikat-Check auf Typ + Steuerart + Steuerjahr + Ausstellungsdatum
    key = (
        data.get("typ", ""),
        data.get("steuerart", ""),
        str(data.get("steuerjahr", "")),
        data.get("ausstellungsdatum", ""),
    )
    for existing in profile.tax_assessments:
        existing_key = (
            existing.get("typ", ""),
            existing.get("steuerart", ""),
            str(existing.get("steuerjahr", "")),
            existing.get("ausstellungsdatum", ""),
        )
        if existing_key == key:
            log.info("persons_db: Steuerbescheid bereits gespeichert, uebersprungen: %s", key)
            return
    profile.tax_assessments.append({k: v for k, v in data.items() if k != "summary"})
    _save()
    log.info("persons_db: Steuerbescheid gespeichert fuer %s: %s", mandant, key)


def save_advance_payment(mandant: str, data: dict) -> None:
    """Speichert einen Vorauszahlungsbescheid beim Mandanten.

    Analog zu save_tax_assessment. Duplikat-Check auf Steuerart +
    Vorauszahlungsjahr + Ausstellungsdatum.

    Args:
        mandant: Name des Steuerpflichtigen.
        data:    Strukturiertes dict fuer Typ "Vorauszahlungsbescheid".
    """
    profile = _find_or_create_mandant(mandant)
    key = (
        data.get("steuerart", ""),
        str(data.get("vorauszahlungsjahr", "")),
        data.get("ausstellungsdatum", ""),
    )
    for existing in profile.advance_payments:
        existing_key = (
            existing.get("steuerart", ""),
            str(existing.get("vorauszahlungsjahr", "")),
            existing.get("ausstellungsdatum", ""),
        )
        if existing_key == key:
            log.info("persons_db: Vorauszahlungsbescheid bereits gespeichert, uebersprungen: %s", key)
            return
    profile.advance_payments.append({k: v for k, v in data.items() if k != "summary"})
    _save()
    log.info("persons_db: Vorauszahlungsbescheid gespeichert fuer %s: %s", mandant, key)


async def generate_mail_topic(subject: str, body_snippet: str) -> str:
    """Generiert 3-5 deutsche Stichworte fuer den Inhalt einer Mail via Haiku.

    Ruft das Haiku-Modell mit Betreff und max. 300 Zeichen Body-Snippet auf.
    Im Fehlerfall wird der Betreff auf 60 Zeichen gekuerzt als Fallback genutzt.

    Args:
        subject:      Mail-Betreff.
        body_snippet: Erster Teil des Mail-Bodys (wird intern auf 300 Zeichen
                      begrenzt).

    Returns:
        Kommagetrennte Stichworte als String, max. 60 Zeichen.
    """
    import settings as S

    body_cut = (body_snippet or "")[:300].strip()
    prompt_user = f"Betreff: {subject}\n\nInhalt: {body_cut}" if body_cut else f"Betreff: {subject}"

    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=80,
            system=(
                "Fasse den Inhalt dieser E-Mail in maximal 5 deutschen Stichworten zusammen "
                "(kein ganzer Satz, keine Anrede). "
                "Antworte NUR mit den Stichworten, kommagetrennt."
            ),
            messages=[{"role": "user", "content": prompt_user}],
        )
        text = ""
        if resp and resp.content:
            text = resp.content[0].text.strip()
        # Auf max. 60 Zeichen kuerzen (sauber am letzten Komma)
        if len(text) > 60:
            text = text[:60]
            last_comma = text.rfind(",")
            if last_comma > 20:
                text = text[:last_comma]
        return text or subject[:60]
    except Exception as e:
        log.debug("generate_mail_topic: Haiku-Aufruf fehlgeschlagen: %s: %s", type(e).__name__, e)
        return subject[:60]


def get_tax_assessments(mandant: str) -> list[dict]:
    """Liefert alle Steuerbescheide eines Mandanten.

    Sucht case-insensitiv per Teilstring-Matching auf dem Namen. Bei mehreren
    Treffern werden alle Eintraege aller passenden Profile zusammengefuehrt.

    Args:
        mandant: Such-Name (z.B. "Mueller", "mueller", "Hans Mueller").

    Returns:
        Liste aller gespeicherten Steuerbescheid-Dicts; leer wenn kein Treffer.
    """
    if not mandant:
        return []
    _load()
    needle = mandant.strip().lower()
    result: list[dict] = []
    for p in _persons.values():
        if p.name and needle in p.name.lower():
            result.extend(p.tax_assessments)
    return result
