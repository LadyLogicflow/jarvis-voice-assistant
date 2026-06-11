"""
Tests fuer _format_full_context und den neuen include_header-Parameter
von _format_mail_history (Issue #247).

Prueft:
- Leerer Kontext liefert leeren String
- Nur Profil zeigt Header mit Name und Funktion
- Mail-Sektion erscheint mit Ueberschrift und Einrueckung
- Kalender-Sektion erscheint wenn vorhanden
- Todoist-Sektion erscheint wenn vorhanden
- Offene-Punkte-Sektion erscheint wenn vorhanden
- Leere Felder erzeugen keine Sektionen
- Vollstaendiger Block mit allen Quellen
- include_header=False in _format_mail_history
"""

from __future__ import annotations

import sys
import types

# ---------------------------------------------------------------------------
# Minimal stubs damit person_context.py importierbar ist ohne echte Deps
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    for name in ("persons_db", "mail_intelligence", "google_calendar_tools", "todoist_tools"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    if "settings" not in sys.modules:
        s = types.ModuleType("settings")
        s.HAIKU_MODEL = "claude-haiku-4-5"
        s.TODOIST_TOKEN = ""
        s.ai = None
        sys.modules["settings"] = s
        sys.modules["S"] = s

    if "prompt" not in sys.modules:
        p = types.ModuleType("prompt")
        p.llm_text = lambda r: ""
        # call_qwen stub required since Issue #249 (person_context imports it)
        async def _call_qwen_stub(system, user, max_tokens=400):
            return ""
        p.call_qwen = _call_qwen_stub
        sys.modules["prompt"] = p


_install_stubs()

from person_context import _format_full_context, _format_mail_history  # noqa: E402


# ---------------------------------------------------------------------------
# Hilfsdaten
# ---------------------------------------------------------------------------

_MAIL_ROW = {
    "mail_date": "2026-06-11",
    "subject": "Rückmeldung Herr Bosch",
    "raw_summary": "Projekt ok",
}

_PROFILE = {
    "name": "Thomas Ulbrich",
    "funktion": "Direktionsleitung",
    "open_points": ["Vorauszahlung Q3 noch offen"],
}

_PROFILE_NO_FUNKTION = {
    "name": "Thomas Ulbrich",
    "funktion": "",
    "open_points": [],
}


# ---------------------------------------------------------------------------
# test_empty_context_returns_empty
# ---------------------------------------------------------------------------

class TestEmptyContext:
    def test_empty_context_returns_empty(self):
        result = _format_full_context({})
        assert result == ""

    def test_all_empty_lists_returns_empty(self):
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "",
        })
        assert result == ""


# ---------------------------------------------------------------------------
# test_profile_only_shows_header
# ---------------------------------------------------------------------------

class TestProfileOnly:
    def test_profile_only_shows_header_with_funktion(self):
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Thomas Ulbrich" in result
        assert "Direktionsleitung" in result
        # Format: "👤 Name — Funktion"
        assert "—" in result

    def test_profile_without_funktion_shows_name_only(self):
        result = _format_full_context({
            "profile": _PROFILE_NO_FUNKTION,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Thomas Ulbrich" in result
        # Kein Bindestrich-Trenner wenn keine Funktion
        first_line = result.splitlines()[0]
        assert "—" not in first_line

    def test_sender_name_without_profile_and_no_data_returns_empty(self):
        """sender_name allein ohne Profil und ohne Daten-Sektionen: leerer String."""
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Maria Muster",
        })
        assert result == ""

    def test_sender_name_with_data_shows_header(self):
        """sender_name ohne Profil, aber mit Mail-Daten: Header wird ausgegeben."""
        result = _format_full_context({
            "profile": None,
            "mail_rows": [_MAIL_ROW],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Maria Muster",
        })
        assert "Maria Muster" in result


# ---------------------------------------------------------------------------
# test_mails_shown_under_header
# ---------------------------------------------------------------------------

class TestMailsSection:
    def test_mails_shown_under_header(self):
        result = _format_full_context({
            "profile": None,
            "mail_rows": [_MAIL_ROW],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Letzte Mails" in result
        assert "11.06.2026" in result
        assert "Rückmeldung Herr Bosch" in result

    def test_mail_entries_indented(self):
        result = _format_full_context({
            "profile": None,
            "mail_rows": [_MAIL_ROW],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        lines = result.splitlines()
        # Die Eintragszeile (nicht die Überschrift) soll mit "  •" beginnen
        bullet_lines = [l for l in lines if l.strip().startswith("•")]
        assert len(bullet_lines) >= 1
        assert bullet_lines[0].startswith("  •")

    def test_max_3_mails_shown(self):
        rows = [
            {"mail_date": f"2026-06-{10 + i:02d}", "subject": f"Mail {i}", "raw_summary": f"Inhalt {i}"}
            for i in range(6)
        ]
        result = _format_full_context({
            "profile": None,
            "mail_rows": rows,
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Person",
        })
        bullet_lines = [l for l in result.splitlines() if l.strip().startswith("•")]
        assert len(bullet_lines) == 3


# ---------------------------------------------------------------------------
# test_calendar_shown_when_present
# ---------------------------------------------------------------------------

class TestCalendarSection:
    def test_calendar_shown_when_present(self):
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": ["• 15.06.2026 — Besprechung Jahresabschluss"],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Termine" in result
        assert "Besprechung Jahresabschluss" in result

    def test_calendar_max_3_entries(self):
        entries = [f"Termin {i}" for i in range(6)]
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": entries,
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        # Zähle Eintragszeilen unter der Kalender-Überschrift
        lines = result.splitlines()
        cal_idx = next((i for i, l in enumerate(lines) if "Termine" in l), None)
        assert cal_idx is not None
        cal_entry_lines = []
        for l in lines[cal_idx + 1:]:
            if l.strip().startswith("•") or l.startswith("  "):
                cal_entry_lines.append(l)
            else:
                break
        assert len(cal_entry_lines) <= 3

    def test_empty_calendar_not_shown(self):
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Termine" not in result


# ---------------------------------------------------------------------------
# test_todoist_shown_when_present
# ---------------------------------------------------------------------------

class TestTodoistSection:
    def test_todoist_shown_when_present(self):
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": ["Steuererklärung 2024 prüfen"],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Offene Aufgaben" in result
        assert "Steuererklärung 2024 prüfen" in result

    def test_todoist_max_3_tasks(self):
        tasks = [f"Aufgabe {i}" for i in range(6)]
        result = _format_full_context({
            "profile": None,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": tasks,
            "sender_name": "Thomas Ulbrich",
        })
        assert "Aufgabe 5" not in result

    def test_empty_todoist_not_shown(self):
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Offene Aufgaben" not in result


# ---------------------------------------------------------------------------
# test_open_points_shown_when_present
# ---------------------------------------------------------------------------

class TestOpenPointsSection:
    def test_open_points_shown_when_present(self):
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Offene Punkte" in result
        assert "Vorauszahlung Q3 noch offen" in result

    def test_open_points_max_3(self):
        profile = {
            "name": "Test",
            "funktion": "Test",
            "open_points": [f"Punkt {i}" for i in range(6)],
        }
        result = _format_full_context({
            "profile": profile,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Test",
        })
        assert "Punkt 5" not in result

    def test_no_open_points_section_omitted(self):
        result = _format_full_context({
            "profile": {"name": "Test", "funktion": "Test", "open_points": []},
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Test",
        })
        assert "Offene Punkte" not in result


# ---------------------------------------------------------------------------
# test_empty_sections_omitted
# ---------------------------------------------------------------------------

class TestEmptySectionsOmitted:
    def test_empty_sections_omitted(self):
        """Leere Felder erzeugen keine Sektions-Ueberschriften."""
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [],
            "calendar_entries": [],
            "todoist_tasks": [],
            "sender_name": "Thomas Ulbrich",
        })
        assert "Letzte Mails" not in result
        assert "Termine" not in result
        assert "Offene Aufgaben" not in result


# ---------------------------------------------------------------------------
# test_full_all_sources
# ---------------------------------------------------------------------------

class TestFullAllSources:
    def test_full_all_sources(self):
        """Vollstaendiger Block mit allen Quellen."""
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [_MAIL_ROW],
            "calendar_entries": ["• 15.06.2026 — Besprechung Jahresabschluss"],
            "todoist_tasks": ["Steuererklärung 2024 prüfen"],
            "sender_name": "Thomas Ulbrich",
        })
        # Alle Sektionen vorhanden
        assert "Thomas Ulbrich" in result
        assert "Direktionsleitung" in result
        assert "Letzte Mails" in result
        assert "11.06.2026" in result
        assert "Termine" in result
        assert "Besprechung Jahresabschluss" in result
        assert "Offene Aufgaben" in result
        assert "Steuererklärung 2024 prüfen" in result
        assert "Offene Punkte" in result
        assert "Vorauszahlung Q3 noch offen" in result

    def test_section_order(self):
        """Reihenfolge: Header, Mails, Kalender, Aufgaben, Offene Punkte."""
        result = _format_full_context({
            "profile": _PROFILE,
            "mail_rows": [_MAIL_ROW],
            "calendar_entries": ["Termin"],
            "todoist_tasks": ["Aufgabe"],
            "sender_name": "Thomas Ulbrich",
        })
        lines = result.splitlines()
        text = "\n".join(lines)

        pos_header = text.index("Thomas Ulbrich")
        pos_mails = text.index("Letzte Mails")
        pos_termine = text.index("Termine")
        pos_aufgaben = text.index("Offene Aufgaben")
        pos_punkte = text.index("Offene Punkte")

        assert pos_header < pos_mails < pos_termine < pos_aufgaben < pos_punkte


# ---------------------------------------------------------------------------
# test_format_mail_history_include_header_false
# ---------------------------------------------------------------------------

class TestFormatMailHistoryIncludeHeader:
    def test_include_header_true_has_header(self):
        rows = [_MAIL_ROW]
        result = _format_mail_history(rows, "Thomas Ulbrich", include_header=True)
        first_line = result.splitlines()[0]
        # Header-Zeile enthaelt "letzte Mails"
        assert "letzte Mails" in first_line

    def test_include_header_false_no_header(self):
        rows = [_MAIL_ROW]
        result = _format_mail_history(rows, "Thomas Ulbrich", include_header=False)
        # Erste (und einzige) Zeile ist direkt ein Eintrag, kein Header
        assert "letzte Mails" not in result
        assert result.startswith("•")

    def test_include_header_default_is_true(self):
        """Standardverhalten bleibt unveraendert (rueckwaertskompatibel)."""
        rows = [_MAIL_ROW]
        result_default = _format_mail_history(rows, "Thomas Ulbrich")
        result_explicit = _format_mail_history(rows, "Thomas Ulbrich", include_header=True)
        assert result_default == result_explicit

    def test_include_header_false_empty_input_returns_empty(self):
        result = _format_mail_history([], "Thomas Ulbrich", include_header=False)
        assert result == ""
