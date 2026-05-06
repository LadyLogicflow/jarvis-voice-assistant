"""
Drift-Detection beim Mail-Eingang (Issue #55).

Wird vom mail_monitor nach jeder klassifizierten Mail aufgerufen.
Vergleicht Sender-Daten mit Apple Kontakte.app + persons_db und
liefert ein dict mit dem zu fragenden Vorschlag (oder None wenn
nichts zu tun).

Ergebnis-Typen:
  None
    Nichts zu tun — Person bekannt + alle Daten passen.
  {"kind": "new_person", "name", "email", "phones"}
    Sender ist nicht in den Kontakten — Vorschlag anlegen.
  {"kind": "email_drift", "contact": Contact, "old_email", "new_email"}
    Person bekannt, aber unter anderer Email als bisher.
  {"kind": "phone_drift", "contact": Contact, "new_phone"}
    Person bekannt, aber Signatur enthaelt eine Nummer die nicht in
    den Kontakten steht.

Telefon-Extraktion aus Body via Regex (deutsche Formate).
"""

from __future__ import annotations

import re
from email.utils import parseaddr

import contacts
import persons_db
import settings as S

log = S.log


# Deutsche Telefon-Patterns: +49..., 0049..., 0... — mit Trennzeichen
# (Leerzeichen, Bindestrich, Schraegstrich, Klammern). Mindestens 6
# Ziffern damit wir nicht jede Zahl im Text fangen.
_PHONE_REGEX = re.compile(
    r"""(?x)
    (?:\+49|0049|0)
    [\s\-/().]*
    (?:\d[\s\-/().]*){6,}
    """,
)


def _extract_text_from_email(msg) -> str:
    """Best-effort plain text aus einer email.Message — fuer
    Signatur-Extraktion."""
    if msg is None:
        return ""
    if not msg.is_multipart():
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        except Exception:
            pass
        return str(msg.get_payload() or "")
    text_parts: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype != "text/plain":
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text_parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            continue
    return "\n".join(text_parts)


def extract_phones(text: str) -> list[str]:
    """Findet alle Telefonnummern (deutsches Format) im Text. Gibt sie
    in der Original-Schreibweise zurueck (nicht normalisiert)."""
    if not text:
        return []
    found: list[str] = []
    for m in _PHONE_REGEX.finditer(text):
        raw = m.group(0).strip()
        # Letzte Punktuation am Ende abschneiden
        raw = raw.rstrip(".,;:!?)")
        if raw and raw not in found:
            found.append(raw)
    return found


async def check_mail_for_drift(msg, sender_addr: str, sender_name: str) -> dict | None:
    """Pruefe eine eingehende Mail auf Person-/Email-/Phone-Drift.

    Reihenfolge:
      1. Sender-Email in Apple Contacts? -> Person bekannt.
         Suche im Body Telefonnummern -> wenn neue Nummer dabei: phone_drift.
      2. Sender-Email NICHT in Apple Contacts, aber Sender-Name matched
         eine Person dort -> email_drift.
      3. Weder Email noch Name -> new_person Vorschlag (mit den im Body
         gefundenen Telefonnummern als Bonus).
    """
    if not sender_addr:
        return None
    sender_addr = sender_addr.lower().strip()
    body_text = _extract_text_from_email(msg)
    body_phones = extract_phones(body_text)

    # 1. Email bekannt?
    contact = await contacts.find_contact_by_email(sender_addr)
    if contact:
        # Phone-Drift: vergleiche Body-Telefonnummern mit den Kontakt-Nummern
        existing_norm = {contacts.normalize_phone(p) for p in contact.phones}
        for raw in body_phones:
            norm = contacts.normalize_phone(raw)
            if norm and norm not in existing_norm:
                return {
                    "kind": "phone_drift",
                    "contact": contact,
                    "new_phone": raw,
                }
        return None  # Person bekannt, nichts Neues

    # 2. Email unbekannt — Name suchen
    if sender_name:
        candidates = await contacts.find_contacts_by_name(sender_name)
        if len(candidates) == 1:
            return {
                "kind": "email_drift",
                "contact": candidates[0],
                "old_emails": list(candidates[0].emails),
                "new_email": sender_addr,
            }
        if len(candidates) > 1:
            # Mehrdeutig — eher als neue Person behandeln, Catrin entscheidet
            log.info(f"contact_sync: name {sender_name!r} matches "
                     f"{len(candidates)} contacts; treating as new person")

    # 3. Niemand passt -> Vorschlag neue Person
    return {
        "kind": "new_person",
        "name": sender_name or sender_addr,
        "email": sender_addr,
        "phones": body_phones,
    }
