"""
Tests fuer _format_mail_history und _parse_mail_date (Issue #246).

Prueft:
- Leerer Input liefert leeren String
- Datum wird korrekt von YYYY-MM-DD nach DD.MM.YYYY formatiert
- Maximal 3 Eintraege werden ausgegeben
- Fehlende Felder fuhren nicht zu einem Crash
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs damit person_context.py importierbar ist ohne echte Deps
# ---------------------------------------------------------------------------

def _install_stubs():
    """Registriert minimale Stub-Module fuer optionale Imports."""
    for name in ("persons_db", "mail_intelligence", "google_calendar_tools", "todoist_tools"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    # settings stub
    if "settings" not in sys.modules:
        s = types.ModuleType("settings")
        s.HAIKU_MODEL = "claude-haiku-4-5"
        s.TODOIST_TOKEN = ""
        s.ai = None
        sys.modules["settings"] = s
        sys.modules["S"] = s

    # prompt stub
    if "prompt" not in sys.modules:
        p = types.ModuleType("prompt")
        p.llm_text = lambda r: ""
        # call_qwen stub required since Issue #249 (person_context imports it)
        async def _call_qwen_stub(system, user, max_tokens=400):
            return ""
        p.call_qwen = _call_qwen_stub
        sys.modules["prompt"] = p


_install_stubs()

# Importiere erst nach dem Stub-Setup
import person_context  # noqa: E402  (must come after stubs)
from person_context import _format_mail_history, _parse_mail_date  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_mail_date
# ---------------------------------------------------------------------------

class TestParseMailDate:
    def test_standard_iso_date(self):
        assert _parse_mail_date("2026-06-11") == "11.06.2026"

    def test_iso_datetime_prefix(self):
        """Nur die ersten 10 Zeichen werden genutzt."""
        assert _parse_mail_date("2026-06-11T09:45:00") == "11.06.2026"

    def test_single_digit_day_and_month(self):
        assert _parse_mail_date("2026-01-03") == "03.01.2026"

    def test_empty_string_returns_dash(self):
        assert _parse_mail_date("") == "—"

    def test_none_like_empty_returns_dash(self):
        # Robustheit gegenueber None-artigen Werten
        assert _parse_mail_date("") == "—"

    def test_unknown_format_returned_as_is(self):
        raw = "gestern"
        assert _parse_mail_date(raw) == raw


# ---------------------------------------------------------------------------
# _format_mail_history
# ---------------------------------------------------------------------------

class TestFormatMailHistoryEmpty:
    def test_empty_list_returns_empty_string(self):
        result = _format_mail_history([], "Thomas Ulbrich")
        assert result == ""

    def test_empty_sender_name_uses_fallback(self):
        row = {"mail_date": "2026-06-11", "subject": "Test", "raw_summary": "Inhalt"}
        result = _format_mail_history([row], "")
        assert "Kontakt" in result


class TestFormatMailHistoryDate:
    def test_date_formatted_correctly(self):
        row = {
            "mail_date": "2026-06-11",
            "subject": "Rueckmeldung Herr Bosch",
            "raw_summary": "Projekt ok, Verkauf nach Freigabe",
        }
        result = _format_mail_history([row], "Thomas Ulbrich")
        assert "11.06.2026" in result

    def test_header_contains_sender_name(self):
        row = {"mail_date": "2026-06-07", "subject": "Fwd: Auftrag", "raw_summary": "Zur Info"}
        result = _format_mail_history([row], "Thomas Ulbrich")
        assert "Thomas Ulbrich" in result

    def test_subject_and_summary_in_output(self):
        row = {
            "mail_date": "2026-06-07",
            "subject": "Fwd: Auftrag Boettcher AG",
            "raw_summary": "Zur Info weitergeleitet",
        }
        result = _format_mail_history([row], "Thomas Ulbrich")
        assert "Fwd: Auftrag Boettcher AG" in result
        assert "Zur Info weitergeleitet" in result

    def test_bullet_point_prefix(self):
        row = {"mail_date": "2026-06-11", "subject": "S", "raw_summary": "R"}
        result = _format_mail_history([row], "X")
        lines = result.splitlines()
        # Zweite Zeile ist der erste Eintrag
        assert lines[1].startswith("•")


class TestFormatMailHistoryMax3:
    def test_exactly_3_with_more_inputs(self):
        rows = [
            {"mail_date": f"2026-06-{10 + i:02d}", "subject": f"Mail {i}", "raw_summary": f"Inhalt {i}"}
            for i in range(6)
        ]
        result = _format_mail_history(rows, "Test Person")
        # Header-Zeile + 3 Eintraege = 4 Zeilen
        lines = [l for l in result.splitlines() if l.strip()]
        assert len(lines) == 4

    def test_fewer_than_3_all_included(self):
        rows = [
            {"mail_date": "2026-06-11", "subject": "A", "raw_summary": "x"},
            {"mail_date": "2026-06-10", "subject": "B", "raw_summary": "y"},
        ]
        result = _format_mail_history(rows, "Person")
        lines = [l for l in result.splitlines() if l.strip()]
        assert len(lines) == 3  # Header + 2


class TestFormatMailHistoryMissingFields:
    def test_no_subject_no_summary_does_not_crash(self):
        row = {"mail_date": "2026-06-11"}
        result = _format_mail_history([row], "Person")
        assert result != ""
        assert "kein Betreff" in result

    def test_no_raw_summary_falls_back_to_content(self):
        row = {
            "mail_date": "2026-06-11",
            "subject": "Betreff",
            "content": "Fallback-Inhalt aus content-Feld",
        }
        result = _format_mail_history([row], "Person")
        assert "Fallback-Inhalt aus content-Feld" in result

    def test_no_mail_date_does_not_crash(self):
        row = {"subject": "Test ohne Datum", "raw_summary": "Kein Datum vorhanden"}
        result = _format_mail_history([row], "Person")
        assert result != ""

    def test_none_values_in_fields_do_not_crash(self):
        row = {"mail_date": None, "subject": None, "raw_summary": None, "content": None}
        result = _format_mail_history([row], "Person")
        assert result != ""

    def test_extra_unknown_fields_ignored(self):
        row = {
            "mail_date": "2026-06-11",
            "subject": "Test",
            "raw_summary": "Inhalt",
            "unknown_field": "sollte ignoriert werden",
            "another_field": 42,
        }
        result = _format_mail_history([row], "Person")
        assert "Test" in result

    def test_long_summary_gets_truncated(self):
        long_text = "A" * 200
        row = {"mail_date": "2026-06-11", "subject": "S", "raw_summary": long_text}
        result = _format_mail_history([row], "Person")
        # Zeile darf nicht 200+ Zeichen des Rohwerts enthalten
        assert "A" * 200 not in result
        assert "..." in result
