"""
Persistente Speiseplan-Vorlieben fuer JARVIS.

Speichert dauerhaft in .jarvis_meal_prefs.json (gitignored):
  avoid       — Zutaten/Gerichte die niemals auf dem Plan erscheinen duerfen
  fish_allowed — Erlaubte Fischarten (leer = kein Fisch)
  fish_weekly  — False = Fisch nicht jede Woche (Standard: False)
"""

from __future__ import annotations

import json
import os

import settings as S

log = S.log

_PREFS_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_meal_prefs.json")

_DEFAULTS: dict = {
    "avoid": [],
    "fish_allowed": ["Lachs", "Forellen", "Dorado"],
    "fish_weekly": False,
}

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(_PREFS_PATH):
        try:
            with open(_PREFS_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            _cache = {**_DEFAULTS, **stored}
            log.info("meal_prefs: geladen (%d avoid, %d fish)",
                     len(_cache["avoid"]), len(_cache["fish_allowed"]))
            return _cache
        except Exception as exc:
            log.warning("meal_prefs: Ladefehler: %s", exc)
    _cache = dict(_DEFAULTS)
    return _cache


def _save() -> None:
    if _cache is None:
        return
    try:
        tmp = _PREFS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PREFS_PATH)
        log.info("meal_prefs: gespeichert")
    except Exception as exc:
        log.warning("meal_prefs: Speicherfehler: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_avoid(item: str) -> bool:
    """Fuegt eine Zutat/ein Gericht zur Verbotsliste hinzu. True = neu."""
    prefs = _load()
    item = item.strip()
    if not item or item.lower() in [x.lower() for x in prefs["avoid"]]:
        return False
    prefs["avoid"].append(item)
    _save()
    return True


def remove_avoid(item: str) -> bool:
    """Entfernt eine Zutat aus der Verbotsliste. True = wurde entfernt."""
    prefs = _load()
    before = len(prefs["avoid"])
    prefs["avoid"] = [x for x in prefs["avoid"] if x.lower() != item.strip().lower()]
    if len(prefs["avoid"]) < before:
        _save()
        return True
    return False


def get_avoid() -> list[str]:
    return list(_load()["avoid"])


def set_fish_allowed(fish_list: list[str]) -> None:
    prefs = _load()
    prefs["fish_allowed"] = [f.strip() for f in fish_list if f.strip()]
    _save()


def get_fish_allowed() -> list[str]:
    return list(_load()["fish_allowed"])


def set_fish_weekly(value: bool) -> None:
    prefs = _load()
    prefs["fish_weekly"] = value
    _save()


def avoid_hint() -> str:
    """Prompt-Hinweis: verbotene Zutaten."""
    avoid = get_avoid()
    if not avoid:
        return ""
    return (
        "ABSOLUTE VERBOTE — diese Zutaten und Gerichte duerfen NIEMALS "
        "im Plan erscheinen (auch nicht als Beilage oder Zutat): "
        + ", ".join(avoid)
    )


def fish_hint() -> str:
    """Prompt-Hinweis: Fisch-Regeln."""
    prefs = _load()
    fish_ok = prefs.get("fish_allowed", [])
    fish_weekly = prefs.get("fish_weekly", False)

    if not fish_ok:
        return "KEIN FISCH — keine Fischgerichte einplanen."

    weekly_rule = (
        ""
        if fish_weekly
        else "Fisch maximal 1x in diesem Plan (nicht jede Woche Fisch)."
    )
    return (
        f"Fisch NUR in diesen Varianten erlaubt: {', '.join(fish_ok)}. "
        f"Andere Fischarten sind verboten. {weekly_rule}"
    ).strip()


def summary() -> str:
    """Lesbare Zusammenfassung der gespeicherten Vorlieben."""
    prefs = _load()
    parts: list[str] = []
    if prefs["avoid"]:
        parts.append("Verboten: " + ", ".join(prefs["avoid"]))
    fish = prefs["fish_allowed"]
    if fish:
        freq = "wöchentlich erlaubt" if prefs["fish_weekly"] else "nicht jede Woche"
        parts.append(f"Fisch: {', '.join(fish)} ({freq})")
    else:
        parts.append("Kein Fisch")
    return "; ".join(parts) if parts else "Keine besonderen Vorlieben gespeichert."
