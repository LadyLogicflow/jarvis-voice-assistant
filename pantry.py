"""
Stammliste / Vorratscheck (Issue #204).

Persistente Liste von Haushaltsartikeln mit Status:
  vorhanden  -- im Haus, nicht auf Bring! uebertragen
  fast_leer  -- beim naechsten Donnerstags-Check auf Bring!
  leer       -- sofort auf Bring!

Datei: .jarvis_pantry.json (gitignored)
"""
from __future__ import annotations
import json, os, unicodedata
import settings as S

log = S.log
_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_pantry.json")

# Category -> list of item names (for display grouping)
_DEFAULT: dict[str, list[str]] = {
    "Gewuerze & Wuerzmittel": [
        "Salz", "Pfeffer", "Zucker", "Essig", "Senf", "Ketchup",
        "Sojasauce", "Rinderbouillon", "Gemuesebruehe",
        "Tomatenmark", "Paprikamark",
    ],
    "Fette & Oele": ["Butter", "Margarine", "Oel", "Olivenoel"],
    "Getreide & Staerke": [
        "WeizenMehl Type 405", "WeizenMehl Type 550", "Dinkelmehl Type 603",
        "Reis", "Nudeln", "Haferflocken", "Paniermehl", "Backpulver", "Natron",
    ],
    "Konserven & Huelsenfruechte": [
        "Tomaten (Dose/Passata)", "Kichererbsen", "Bohnen",
        "Linsen", "Thunfisch", "Kokosmilch",
    ],
    "Kuehlschrank-Basics": [
        "Eier", "Milch", "Sahne", "Parmesan", "Gouda", "Creme fraiche", "Joghurt",
    ],
    "Gemuese & Alliums": ["Zwiebeln", "Knoblauch", "Kartoffeln", "Karotten"],
    "Suesswaren & Backzutaten": ["Honig", "Vanille", "Schokolade (70%+)"],
    "Nuesse": ["Mandeln", "Walnuesse"],
    "Drogerie": [
        "Toilettenpapier", "Feuchttuecher",
        "Duschgel Knut", "Duschgel Catrin",
        "Deo Knut", "Deo Catrin",
        "Waschmittel", "Spuelmaschinentabs", "Spuelmittel",
    ],
}

# Flat dict: item_name -> status
_pantry: dict[str, str] = {}
_loaded = False


def _norm(s: str) -> str:
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    return s


def _load() -> None:
    global _pantry, _loaded
    if _loaded:
        return
    if os.path.exists(_PATH):
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                _pantry = json.load(f)
            _loaded = True
            return
        except Exception as e:
            log.warning(f"pantry: load failed: {e}")
    # First-time init: all DEFAULT items as "vorhanden"
    _pantry = {}
    for items in _DEFAULT.values():
        for item in items:
            _pantry[item] = "vorhanden"
    _save()
    _loaded = True


def _save() -> None:
    try:
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pantry, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
    except Exception as e:
        log.warning(f"pantry: save failed: {e}")


def get_status(item: str) -> str | None:
    """Fuzzy-match item against Stammliste. Returns status or None if not found."""
    _load()
    q = _norm(item)
    # Exact match first
    for key, status in _pantry.items():
        if _norm(key) == q:
            return status
    # Substring match
    for key, status in _pantry.items():
        kn = _norm(key)
        if q in kn or kn in q:
            return status
    return None


def set_status(item: str, status: str) -> bool:
    """Set status for an existing item (fuzzy match). Returns True if found."""
    _load()
    q = _norm(item)
    for key in list(_pantry.keys()):
        if _norm(key) == q or q in _norm(key) or _norm(key) in q:
            _pantry[key] = status
            _save()
            return True
    return False


def add_item(item: str, status: str = "vorhanden") -> None:
    """Add a new item to the pantry (or overwrite existing)."""
    _load()
    _pantry[item.strip()] = status
    _save()


def remove_item(item: str) -> bool:
    """Remove item by fuzzy match. Returns True if found and removed."""
    _load()
    q = _norm(item)
    for key in list(_pantry.keys()):
        if _norm(key) == q or q in _norm(key) or _norm(key) in q:
            del _pantry[key]
            _save()
            return True
    return False


def get_all() -> dict[str, str]:
    """Return a copy of the full pantry dict."""
    _load()
    return dict(_pantry)


def get_items_by_status(status: str) -> list[str]:
    """Return all item names with the given status."""
    _load()
    return [k for k, v in _pantry.items() if v == status]


def get_grouped() -> dict[str, list[tuple[str, str]]]:
    """Returns items grouped by category (from _DEFAULT), plus 'Sonstige' for unlisted."""
    _load()
    result: dict[str, list[tuple[str, str]]] = {}
    assigned: set[str] = set()
    for cat, items in _DEFAULT.items():
        entries = []
        for item in items:
            status = get_status(item)
            if status is not None:
                # find actual key
                q = _norm(item)
                for key in _pantry:
                    if _norm(key) == q or q in _norm(key) or _norm(key) in q:
                        entries.append((key, _pantry[key]))
                        assigned.add(key)
                        break
        if entries:
            result[cat] = entries
    # Sonstige
    sonstige = [(k, v) for k, v in _pantry.items() if k not in assigned]
    if sonstige:
        result["Sonstige"] = sonstige
    return result
