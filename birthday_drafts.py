"""
Geburtstags-Entwurf-Tracker (Issue #144).

Verhindert doppelte Entwuerfe: speichert pro Kontakt + Jahr ob ein
Glueckwunsch-Entwurf bereits erstellt wurde. Persistenz in JSON.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import settings as S

log = S.log

DRAFTS_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_birthday_drafts.json")

# In-Memory-Cache: {"{contact_name}:{year}": {"subject": str, "created": str}}
_drafts: dict[str, dict] = {}
_loaded: bool = False


def _load() -> None:
    """Lazy-load the drafts store from disk."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(DRAFTS_PATH):
        return
    try:
        with open(DRAFTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _drafts.update(data)
        log.info(f"birthday_drafts: loaded {len(_drafts)} entries")
    except Exception as e:
        log.warning(f"birthday_drafts._load failed: {type(e).__name__}: {e}")


def _save() -> None:
    """Atomic write — never crash the caller."""
    try:
        tmp = DRAFTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_drafts, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DRAFTS_PATH)
    except Exception as e:
        log.warning(f"birthday_drafts._save failed: {type(e).__name__}: {e}")


def _key(contact_name: str, year: int) -> str:
    return f"{contact_name}:{year}"


def was_draft_created(contact_name: str, year: int) -> bool:
    """Returns True if a birthday draft was already created for this
    contact in the given year.

    Args:
        contact_name: Display name of the contact.
        year: Calendar year (e.g. 2026).

    Returns:
        True when a draft entry exists; False otherwise.
    """
    _load()
    return _key(contact_name, year) in _drafts


def mark_draft_created(contact_name: str, year: int, subject: str) -> None:
    """Record that a birthday draft was created for this contact in
    the given year.

    Args:
        contact_name: Display name of the contact.
        year: Calendar year (e.g. 2026).
        subject: Subject line of the draft (for reference / logging).
    """
    import datetime
    _load()
    _drafts[_key(contact_name, year)] = {
        "subject": subject,
        "created": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save()
    log.info(f"birthday_drafts: marked draft for {contact_name!r} ({year}): {subject!r}")
