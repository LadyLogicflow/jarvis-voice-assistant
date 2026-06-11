"""Tests für appointment_briefing.py v2 — strukturierter Personenkontext (Issue #248)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(summary: str = "Meeting mit Müller", start: str = "2026-06-11T10:00:00+02:00",
                end: str = "2026-06-11T11:00:00+02:00") -> dict:
    return {
        "id": "evt-001",
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "location": "",
    }


# ---------------------------------------------------------------------------
# Import module under test (lazy, after conftest patches env)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ab():
    """Import appointment_briefing once per module run."""
    import appointment_briefing
    return appointment_briefing


# ---------------------------------------------------------------------------
# _format_html_v2
# ---------------------------------------------------------------------------

def test_format_html_v2_with_context(ab):
    """Wenn ctx vorhanden, erscheint er als <pre>-Block im HTML."""
    event = _make_event()
    ctx_text = "Profil: Max Müller\nLetzte Mail: Besprechung 2026-05-01"
    person_contexts = [("Max Müller", ctx_text)]

    result = ab._format_html_v2(event, person_contexts)

    assert "<pre>" in result
    assert "Max Müller" in result
    assert "Letzte Mail" in result
    assert "In 15 Min:" in result
    assert "10:00" in result


def test_format_html_v2_empty_context(ab):
    """Wenn ctx leer ist, erscheint der Fallback-Text."""
    event = _make_event()
    person_contexts = [("Anna Bauer", "")]

    result = ab._format_html_v2(event, person_contexts)

    assert "Anna Bauer" in result
    assert "Kein Kontext verfügbar" in result
    assert "<pre>" not in result


def test_format_html_v2_no_persons(ab):
    """Leere Personenliste ergibt Hinweis-Text, kein Crash."""
    event = _make_event()
    result = ab._format_html_v2(event, [])

    assert "Keine Personendaten verfügbar" in result
    assert "In 15 Min:" in result


def test_format_html_v2_multiple_persons(ab):
    """Mehrere Personen werden alle ausgegeben."""
    event = _make_event()
    person_contexts = [
        ("Person A", "Kontext A"),
        ("Person B", ""),
    ]
    result = ab._format_html_v2(event, person_contexts)

    assert "Kontext A" in result
    assert "Person B" in result
    assert "Kein Kontext verfügbar" in result


# ---------------------------------------------------------------------------
# _format_telegram_v2
# ---------------------------------------------------------------------------

def test_format_telegram_v2_with_context(ab):
    """ctx-Zeilen erscheinen im Telegram-Output."""
    event = _make_event()
    ctx_text = "Profil: Max Müller\nFirma: Mustermann GmbH"
    person_contexts = [("Max Müller", ctx_text)]

    result = ab._format_telegram_v2(event, person_contexts)

    assert "Profil: Max Müller" in result
    assert "Firma: Mustermann GmbH" in result
    assert "In 15 Min:" in result


def test_format_telegram_v2_empty_context(ab):
    """Leerer ctx ergibt Fallback-Zeile in Telegram."""
    event = _make_event()
    person_contexts = [("Klaus Schmidt", "")]

    result = ab._format_telegram_v2(event, person_contexts)

    assert "Klaus Schmidt" in result
    assert "Kein Kontext verfügbar" in result


def test_format_telegram_v2_no_persons(ab):
    """Leere Personenliste — kein Crash, Hinweis-Text erscheint."""
    event = _make_event()
    result = ab._format_telegram_v2(event, [])

    assert "Keine Personendaten verfügbar" in result
    assert "In 15 Min:" in result


def test_format_telegram_v2_multiline_ctx(ab):
    """Mehrzeiliger ctx wird zeilenweise übernommen."""
    event = _make_event()
    ctx_text = "Zeile 1\nZeile 2\nZeile 3"
    person_contexts = [("Test Person", ctx_text)]

    result = ab._format_telegram_v2(event, person_contexts)
    lines = result.splitlines()

    assert "Zeile 1" in lines
    assert "Zeile 2" in lines
    assert "Zeile 3" in lines


# ---------------------------------------------------------------------------
# build_and_send_briefing — integration smoke
# ---------------------------------------------------------------------------

def _run(coro):
    """Hilfsfunktion: Coroutine synchron ausführen (kein pytest-asyncio nötig)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_build_and_send_briefing_calls_enrich(ab):
    """build_and_send_briefing ruft enrich_mail_with_person_context auf."""
    import sys

    event = _make_event("Meeting mit Petra Schulz")

    mock_pc = MagicMock()
    mock_pc.enrich_mail_with_person_context = AsyncMock(return_value="Kontext-Block Petra")

    mock_pdb = MagicMock()
    mock_pdb.find_by_name.return_value = None

    mock_gc = MagicMock()
    mock_gc.find_contacts_by_name = AsyncMock(return_value=[])

    mock_tgb = MagicMock()
    mock_tgb.send_user_text = AsyncMock()

    mock_server = MagicMock()
    mock_server.broadcast_to_all_sessions = AsyncMock()

    with patch.object(ab, "_extract_names", new=AsyncMock(return_value=["Petra Schulz"])):
        with patch.dict(sys.modules, {
            "person_context": mock_pc,
            "persons_db": mock_pdb,
            "google_contacts_tools": mock_gc,
            "telegram_bot": mock_tgb,
            "server": mock_server,
        }):
            _run(ab.build_and_send_briefing(event))

    mock_pc.enrich_mail_with_person_context.assert_called_once_with("", "Petra Schulz")
