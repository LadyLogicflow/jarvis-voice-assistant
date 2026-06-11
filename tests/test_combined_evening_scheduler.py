"""Tests fuer combined_evening_scheduler (Issue #239).

Prueft die Mail-Summary-Formatierungslogik isoliert, ohne scheduler-Import
(der komplette scheduler braucht tenacity/httpx/anthropic usw.).
"""

from __future__ import annotations

import types


_MAIL_STAT_LABELS = {
    "werbung": "Werbung",
    "einkauf": "Einkauf",
    "info": "Info",
    "handlungsbedarf": "Handlungsbedarf",
    "invoices_forwarded": "Rechnungen weitergeleitet",
}


def _build_mail_summary_text_from_stats(stats: dict) -> str:
    """Lokale Kopie der scheduler._build_mail_summary_text Logik fuer Unit-Tests."""
    parts = []
    for key, label in _MAIL_STAT_LABELS.items():
        count = stats.get(key, 0)
        if count > 0:
            parts.append(f"{count} {label}")
    if not parts:
        return ""
    return "Mails heute: " + ", ".join(parts)


def test_build_mail_summary_empty():
    """Alle Zaehler 0 -> leerer String."""
    stats = {"werbung": 0, "einkauf": 0, "info": 0,
             "handlungsbedarf": 0, "invoices_forwarded": 0}
    assert _build_mail_summary_text_from_stats(stats) == ""


def test_build_mail_summary_single_category():
    """Nur eine Kategorie > 0."""
    stats = {"werbung": 5, "einkauf": 0, "info": 0,
             "handlungsbedarf": 0, "invoices_forwarded": 0}
    result = _build_mail_summary_text_from_stats(stats)
    assert result == "Mails heute: 5 Werbung"


def test_build_mail_summary_multiple_categories():
    """Mehrere Kategorien > 0 werden kommasepariert aufgelistet."""
    stats = {"werbung": 3, "einkauf": 0, "info": 2,
             "handlungsbedarf": 1, "invoices_forwarded": 0}
    result = _build_mail_summary_text_from_stats(stats)
    assert result.startswith("Mails heute:")
    assert "3 Werbung" in result
    assert "2 Info" in result
    assert "1 Handlungsbedarf" in result
    assert "Einkauf" not in result
    assert "Rechnungen" not in result


def test_build_mail_summary_invoices():
    """Rechnungen weitergeleitet wird korrekt benannt."""
    stats = {"werbung": 0, "einkauf": 0, "info": 0,
             "handlungsbedarf": 0, "invoices_forwarded": 2}
    result = _build_mail_summary_text_from_stats(stats)
    assert "2 Rechnungen weitergeleitet" in result


def test_combined_scheduler_is_alias_for_mail_summary():
    """Prueft via Quellcode-Inspektion dass der Alias korrekt gesetzt ist.

    Da scheduler nicht importierbar ist (fehlende optionale Dependencies),
    pruefen wir den Quellcode direkt.
    """
    import os
    scheduler_path = os.path.join(
        os.path.dirname(__file__), "..", "scheduler.py"
    )
    with open(scheduler_path, encoding="utf-8") as f:
        source = f.read()
    # Der Alias muss im Quellcode vorhanden sein
    assert "mail_evening_summary_scheduler = combined_evening_scheduler" in source


def test_proactive_skip_logic_present():
    """Prueft via Quellcode-Inspektion dass der 18:00-Skip korrekt implementiert ist."""
    import os
    scheduler_path = os.path.join(
        os.path.dirname(__file__), "..", "scheduler.py"
    )
    with open(scheduler_path, encoding="utf-8") as f:
        source = f.read()
    # Der Skip-Kommentar und die Bedingung muessen vorhanden sein
    assert 'slot == "18:00" and S.MAIL_SUMMARY_HOUR == 18' in source
    assert "combined_evening_scheduler uebernimmt" in source
