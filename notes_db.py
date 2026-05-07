"""
Allgemeiner Notizen-Speicher (Issue #56).

Catrin sagt "merk dir: ..." -> Notiz wird in dieser DB abgelegt.
Person-bezogene Notizen ("zu Mueller: ...") gehen alternativ direkt
in persons_db.notes; dieser Speicher hier ist fuer den Rest:
Vorlieben, Abneigungen, allgemeine Notizen ohne Person-Bezug.

Persistenz: JSON in .jarvis_notes.json (gitignored).

API:
- add(text, kind="notiz", tags=[]) -> Note
- all_notes() -> list[Note]
- find(query) -> list[Note]  (Volltext-Substring, case-insensitive)
- find_by_kind(kind) -> list[Note]
- find_recent(days=7) -> list[Note]
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from dataclasses import asdict, dataclass, field

import settings as S

log = S.log


@dataclass
class Note:
    """Eine Notiz von Catrin.

    kind:
      'notiz'    — allgemeine Sachnotiz
      'vorliebe' — etwas was Catrin mag / bevorzugt
      'abneigung'— etwas was sie nicht mag / nicht will
    """
    id: str
    text: str
    kind: str = "notiz"
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    person_contact_id: str = ""  # leer wenn nicht-Person-bezogen


_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_notes.json")
_notes: list[Note] = []
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(_DB_PATH):
        return
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _notes.append(Note(**{k: v for k, v in item.items() if k in Note.__dataclass_fields__}))
        log.info(f"notes_db: loaded {len(_notes)} notes from disk")
    except Exception as e:
        log.warning(f"notes_db._load failed: {type(e).__name__}: {e}")


def _save() -> None:
    try:
        tmp = _DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(n) for n in _notes], f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DB_PATH)
    except Exception as e:
        log.warning(f"notes_db._save failed: {type(e).__name__}: {e}")


def add(
    text: str,
    kind: str = "notiz",
    tags: list[str] | None = None,
    person_contact_id: str = "",
) -> Note:
    _load()
    note = Note(
        id=str(uuid.uuid4()),
        text=text.strip(),
        kind=kind,
        tags=list(tags or []),
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        person_contact_id=person_contact_id,
    )
    _notes.append(note)
    _save()
    return note


def all_notes() -> list[Note]:
    _load()
    return list(_notes)


def find(query: str) -> list[Note]:
    """Volltext-Substring-Suche (case-insensitive) in Text + Tags."""
    if not query:
        return []
    q = query.lower().strip()
    _load()
    return [
        n for n in _notes
        if q in n.text.lower() or any(q in t.lower() for t in n.tags)
    ]


def find_by_kind(kind: str) -> list[Note]:
    _load()
    return [n for n in _notes if n.kind == kind]


def find_recent(days: int = 7) -> list[Note]:
    _load()
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    out: list[Note] = []
    for n in _notes:
        try:
            ts = datetime.datetime.fromisoformat(n.created_at)
            if ts >= cutoff:
                out.append(n)
        except Exception:
            pass
    return out


def delete(note_id: str) -> bool:
    _load()
    for i, n in enumerate(_notes):
        if n.id == note_id:
            del _notes[i]
            _save()
            return True
    return False
