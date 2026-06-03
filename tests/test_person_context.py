"""
Tests fuer person_context.py (Issue #163).

Prueft:
- Parallel-Abfrage aller Quellen via asyncio.gather
- Graceful fallback bei Quellen-Fehlern
- Kein Output fuer unbekannte Personen (alle Quellen leer)
- Synthese-Aufruf nur wenn mindestens eine Quelle Daten liefert
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _run(coro):
    """Fuehrt eine Coroutine synchron aus (fuer Tests ohne pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_profile(
    name="Sandra Muster",
    funktion="Mandantin ESt",
    notes=None,
    open_points=None,
):
    p = MagicMock()
    p.name = name
    p.funktion = funktion
    p.anrede = ""
    p.last_contact = "2026-05-01"
    p.notes = notes or ["2026-05-01: Mail empfangen"]
    p.open_points = open_points or ["ESt 2025 ausstehend"]
    p.tax_assessments = []
    return p


def _make_llm_response(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


# ---------------------------------------------------------------------------
# Tests fuer _query_persons_db
# ---------------------------------------------------------------------------

class TestQueryPersonsDb:
    def test_found_by_email(self):
        import person_context
        profile = _make_profile()
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.return_value = profile
        mock_pdb.search_by_name.return_value = []
        with patch.object(person_context, "persons_db", mock_pdb):
            result = person_context._query_persons_db("sandra@example.com", "Sandra Muster")
        assert result is not None
        assert result["name"] == "Sandra Muster"
        assert result["funktion"] == "Mandantin ESt"
        assert "ESt 2025 ausstehend" in result["open_points"]

    def test_found_by_name_fallback(self):
        import person_context
        profile = _make_profile()
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.return_value = None
        mock_pdb.search_by_name.return_value = [profile]
        with patch.object(person_context, "persons_db", mock_pdb):
            result = person_context._query_persons_db("", "Sandra Muster")
        assert result is not None
        assert result["name"] == "Sandra Muster"

    def test_not_found_returns_none(self):
        import person_context
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.return_value = None
        mock_pdb.search_by_name.return_value = []
        with patch.object(person_context, "persons_db", mock_pdb):
            result = person_context._query_persons_db("unknown@example.com", "Unbekannt")
        assert result is None

    def test_exception_returns_none(self):
        import person_context
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.side_effect = RuntimeError("DB error")
        with patch.object(person_context, "persons_db", mock_pdb):
            result = person_context._query_persons_db("sandra@example.com", "Sandra")
        assert result is None

    def test_none_module_returns_none(self):
        import person_context
        with patch.object(person_context, "persons_db", None):
            result = person_context._query_persons_db("x@example.com", "X")
        assert result is None


# ---------------------------------------------------------------------------
# Tests fuer _query_mail_knowledge
# ---------------------------------------------------------------------------

class TestQueryMailKnowledge:
    def test_returns_matching_rows(self):
        """Zeilen die die Absender-E-Mail enthalten werden zurueckgegeben."""
        import person_context
        rows = [
            {
                "sender": "sandra@example.com",
                "sender_name": "Sandra Muster",
                "mail_date": "2026-04-01",
                "content": "Steuererklarung 2025",
                "raw_summary": "Mandantin fragt nach Stand",
            }
        ]
        mock_mi = MagicMock()
        mock_mi.search_knowledge.return_value = rows
        with patch.object(person_context, "mail_intelligence", mock_mi):
            result = person_context._query_mail_knowledge("sandra@example.com", "Sandra")
        assert len(result) == 1
        assert result[0]["sender"] == "sandra@example.com"

    def test_filters_out_unrelated_rows(self):
        """Zeilen anderer Absender werden gefiltert (wenn Email-Suche erfolgte)."""
        import person_context
        # search_knowledge wird mit sandra@example.com aufgerufen,
        # aber die Zeile hat einen anderen Absender -> gefiltert
        rows = [
            {"sender": "other@example.com", "mail_date": "2026-04-01", "content": "X"},
        ]
        mock_mi = MagicMock()
        # Erster Aufruf (per Email) -> unpassende Zeile
        # Zweiter Aufruf (per Name-Fallback) -> leer (weil Name-Suche kein Ergebnis bringt)
        mock_mi.search_knowledge.side_effect = [rows, []]
        with patch.object(person_context, "mail_intelligence", mock_mi):
            result = person_context._query_mail_knowledge("sandra@example.com", "Sandra")
        # Zeile mit anderem Absender MUSS gefiltert sein
        assert all(r.get("sender") != "other@example.com" for r in result)

    def test_exception_returns_empty(self):
        import person_context
        mock_mi = MagicMock()
        mock_mi.search_knowledge.side_effect = RuntimeError("DB down")
        with patch.object(person_context, "mail_intelligence", mock_mi):
            result = person_context._query_mail_knowledge("sandra@example.com", "Sandra")
        assert result == []

    def test_none_module_returns_empty(self):
        import person_context
        with patch.object(person_context, "mail_intelligence", None):
            result = person_context._query_mail_knowledge("x@example.com", "X")
        assert result == []


# ---------------------------------------------------------------------------
# Tests fuer _query_calendar
# ---------------------------------------------------------------------------

class TestQueryCalendar:
    def test_returns_matching_events(self):
        import person_context
        calendar_output = (
            "Kalender — 2 Termine:\n"
            "• Mo 01.04. 10:00 — Sandra Muster Beratung\n"
            "• Di 02.04. 14:00 — Team Meeting"
        )
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = calendar_output
        with patch.object(person_context, "google_calendar_tools", mock_cal):
            result = _run(person_context._query_calendar("Sandra Muster"))
        assert len(result) == 1
        assert "Sandra Muster" in result[0]

    def test_empty_name_returns_empty(self):
        import person_context
        result = _run(person_context._query_calendar(""))
        assert result == []

    def test_no_events_returns_empty(self):
        import person_context
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = "KEINE_TERMINE"
        with patch.object(person_context, "google_calendar_tools", mock_cal):
            result = _run(person_context._query_calendar("Sandra"))
        assert result == []

    def test_calendar_unavailable_returns_empty(self):
        import person_context
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = "Kalender nicht erreichbar"
        with patch.object(person_context, "google_calendar_tools", mock_cal):
            result = _run(person_context._query_calendar("Sandra"))
        assert result == []

    def test_exception_returns_empty(self):
        import person_context
        mock_cal = MagicMock()
        mock_cal._fetch_events.side_effect = RuntimeError("API fail")
        with patch.object(person_context, "google_calendar_tools", mock_cal):
            result = _run(person_context._query_calendar("Sandra"))
        assert result == []

    def test_none_module_returns_empty(self):
        import person_context
        with patch.object(person_context, "google_calendar_tools", None):
            result = _run(person_context._query_calendar("Sandra"))
        assert result == []


# ---------------------------------------------------------------------------
# Tests fuer _query_todoist
# ---------------------------------------------------------------------------

class TestQueryTodoist:
    def test_returns_matching_tasks(self):
        import person_context
        todoist_output = (
            "Todoist — 3 offene Aufgaben:\n"
            "• Sandra Muster Jahresabschluss (fällig: 2026-06-30)\n"
            "• Steuererklarung pruefen\n"
            "• Sandra Rueckruf (heute)"
        )
        mock_td = MagicMock()
        mock_td.get_tasks = AsyncMock(return_value=todoist_output)
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        with (
            patch.object(person_context, "todoist_tools", mock_td),
            patch.object(person_context, "S", mock_s),
        ):
            result = _run(person_context._query_todoist("Sandra"))
        # Both "Sandra Muster" and "Sandra Rueckruf" match "Sandra"
        assert any("Sandra" in t for t in result)

    def test_no_token_returns_empty(self):
        import person_context
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = ""
        with patch.object(person_context, "S", mock_s):
            result = _run(person_context._query_todoist("Sandra"))
        assert result == []

    def test_no_tasks_returns_empty(self):
        import person_context
        mock_td = MagicMock()
        mock_td.get_tasks = AsyncMock(return_value="KEINE_TASKS")
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        with (
            patch.object(person_context, "todoist_tools", mock_td),
            patch.object(person_context, "S", mock_s),
        ):
            result = _run(person_context._query_todoist("Sandra"))
        assert result == []

    def test_none_module_returns_empty(self):
        import person_context
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        with (
            patch.object(person_context, "todoist_tools", None),
            patch.object(person_context, "S", mock_s),
        ):
            result = _run(person_context._query_todoist("Sandra"))
        assert result == []


# ---------------------------------------------------------------------------
# Tests fuer enrich_mail_with_person_context (Haupt-API)
# ---------------------------------------------------------------------------

class TestEnrichMailWithPersonContext:
    def test_returns_empty_for_unknown_person(self):
        """Keine Daten aus keiner Quelle -> leerer String."""
        import person_context
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.return_value = None
        mock_pdb.search_by_name.return_value = []
        mock_mi = MagicMock()
        mock_mi.search_knowledge.return_value = []
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = "KEINE_TERMINE"
        mock_td = MagicMock()
        mock_td.get_tasks = AsyncMock(return_value="KEINE_TASKS")
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        with (
            patch.object(person_context, "persons_db", mock_pdb),
            patch.object(person_context, "mail_intelligence", mock_mi),
            patch.object(person_context, "google_calendar_tools", mock_cal),
            patch.object(person_context, "todoist_tools", mock_td),
            patch.object(person_context, "S", mock_s),
        ):
            result = _run(person_context.enrich_mail_with_person_context(
                sender_email="unknown@example.com",
                sender_name="Unbekannt",
            ))
        assert result == ""

    def test_returns_empty_when_no_sender(self):
        """Kein Absender -> sofort leerer String (kein IO)."""
        import person_context
        result = _run(person_context.enrich_mail_with_person_context(
            sender_email="",
            sender_name="",
        ))
        assert result == ""

    def test_returns_synthesis_when_profile_found(self):
        """Profil gefunden -> Synthese-LLM wird aufgerufen und Text zurueckgegeben."""
        import person_context
        profile = _make_profile()
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.return_value = profile
        mock_mi = MagicMock()
        mock_mi.search_knowledge.return_value = []
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = "KEINE_TERMINE"
        mock_td = MagicMock()
        mock_td.get_tasks = AsyncMock(return_value="KEINE_TASKS")
        llm_response = _make_llm_response(
            "Sandra Muster ist Mandantin für die Einkommensteuer. "
            "Die Einkommensteuererklärung 2025 steht noch aus."
        )
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        mock_s.ai.messages.create = AsyncMock(return_value=llm_response)
        with (
            patch.object(person_context, "persons_db", mock_pdb),
            patch.object(person_context, "mail_intelligence", mock_mi),
            patch.object(person_context, "google_calendar_tools", mock_cal),
            patch.object(person_context, "todoist_tools", mock_td),
            patch.object(person_context, "S", mock_s),
        ):
            result = _run(person_context.enrich_mail_with_person_context(
                sender_email="sandra@example.com",
                sender_name="Sandra Muster",
            ))
        assert "Sandra" in result

    def test_graceful_fallback_on_source_exception(self):
        """Wenn persons_db eine Exception wirft, laeuft mail_knowledge trotzdem."""
        import person_context
        mock_pdb = MagicMock()
        mock_pdb.find_by_email.side_effect = RuntimeError("persons_db crashed")
        mock_mi = MagicMock()
        mock_mi.search_knowledge.return_value = [
            {
                "sender": "sandra@example.com",
                "mail_date": "2026-05-01",
                "content": "Rueckruf erbeten",
                "raw_summary": "Mandantin bittet um Rueckruf",
            }
        ]
        mock_cal = MagicMock()
        mock_cal._fetch_events.return_value = "KEINE_TERMINE"
        mock_td = MagicMock()
        mock_td.get_tasks = AsyncMock(return_value="KEINE_TASKS")
        llm_response = _make_llm_response("Keine weiteren Informationen.")
        mock_s = MagicMock()
        mock_s.TODOIST_TOKEN = "test-token"
        mock_s.ai.messages.create = AsyncMock(return_value=llm_response)
        with (
            patch.object(person_context, "persons_db", mock_pdb),
            patch.object(person_context, "mail_intelligence", mock_mi),
            patch.object(person_context, "google_calendar_tools", mock_cal),
            patch.object(person_context, "todoist_tools", mock_td),
            patch.object(person_context, "S", mock_s),
        ):
            # Should not raise — persons_db exception must be swallowed
            result = _run(person_context.enrich_mail_with_person_context(
                sender_email="sandra@example.com",
                sender_name="Sandra Muster",
            ))
        assert isinstance(result, str)

    def test_all_sources_queried_in_parallel(self):
        """Alle 4 Quellen werden aufgerufen (parallel via asyncio.gather)."""
        import person_context
        call_order: list[str] = []

        async def fake_calendar(name):
            call_order.append("calendar")
            return []

        async def fake_todoist(name):
            call_order.append("todoist")
            return []

        def fake_persons(email, name):
            call_order.append("persons")
            return None

        def fake_mail(email, name):
            call_order.append("mail")
            return []

        with (
            patch.object(person_context, "_query_persons_db", side_effect=fake_persons),
            patch.object(person_context, "_query_mail_knowledge", side_effect=fake_mail),
            patch.object(person_context, "_query_calendar", new=fake_calendar),
            patch.object(person_context, "_query_todoist", new=fake_todoist),
        ):
            _run(person_context.enrich_mail_with_person_context(
                sender_email="x@example.com",
                sender_name="X",
            ))

        # All four sources must have been called
        assert "persons" in call_order
        assert "mail" in call_order
        assert "calendar" in call_order
        assert "todoist" in call_order
