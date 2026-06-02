"""
Tages-Aktivitaetslog fuer JARVIS (Issue #145).

Thread-sicherer In-Memory-Log der taeglich gezaehlten und aufgelisteten
autonomen Aktionen. Wird beim Morgen-Briefing zurueckgesetzt und beim
Abend-Briefing als JARVIS-Eigenleistungs-Block ausgegeben.
"""

from __future__ import annotations

import threading
from typing import Union

_lock = threading.Lock()

_counters: dict = {
    "mail_triage":       0,
    "followup_saved":    0,
    "followup_resolved": 0,
    "contact_enriched":  0,
    "draft_created":     [],   # list[str] — Namen / Betreff
    "calendar_added":    [],   # list[str] — Event-Zusammenfassungen
}


def log_action(category: str, detail: str = "") -> None:
    """Zaehlt eine Kategorie hoch oder haengt einen Detail-String an.

    Args:
        category: Eines der definierten Schluessel in _counters.
        detail:   Fuer Listen-Kategorien (draft_created, calendar_added)
                  der zu speichernde Text; wird bei Zaehler-Kategorien ignoriert.
    """
    with _lock:
        if category not in _counters:
            return
        val = _counters[category]
        if isinstance(val, list):
            _counters[category].append(detail)
        else:
            _counters[category] += 1


def get_daily_summary() -> dict:
    """Gibt eine Kopie der aktuellen Tages-Zaehler zurueck.

    Returns:
        Dict mit allen Kategorien; Listen werden flach kopiert.
    """
    with _lock:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _counters.items()}


def reset() -> None:
    """Setzt alle Zaehler und Listen auf ihre Anfangswerte zurueck.

    Wird taeglich beim Morgen-Briefing aufgerufen.
    """
    with _lock:
        for k, v in _counters.items():
            _counters[k] = [] if isinstance(v, list) else 0
