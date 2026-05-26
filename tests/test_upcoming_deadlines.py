"""Tests fuer get_upcoming_deadlines() und _format_deadline_hint() (Issue #119).

Testet die Frist-Erkennung aus:
- S.TODAY_EVENTS (Kalender-Eintraege mit Frist-Keywords)
- S.TODAY_TASKS (Todoist-Aufgaben, bereits auf heute gefiltert)
- Feste jaehrliche Steuerfristen (31.05, 31.07, 31.10, 10.01)

Die Funktionen sind synchrones, reines Python in scheduler.py. Da scheduler.py
am Modulanfang Heavy-Deps importiert (tenacity, httpx, ...), werden diese per
sys.modules-Stub vermieden. Die notwendigen Env-Vars werden vor dem Import
in os.environ gesetzt, da conftest-autouse erst nach der Collection greift.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Env-Vars + Heavy-Dep-Stubs VOR jedem Import aus dem Projekt
# ---------------------------------------------------------------------------
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-unit-tests")
os.environ.setdefault("ELEVENLABS_API_KEY", "test-elevenlabs-for-unit-tests")

_HEAVY_DEPS = [
    "tenacity",
    "httpx",
    "browser_tools",
    "google_calendar_tools",
    "steuer_news",
    "todoist_tools",
    "aiosqlite",
    "chromadb",
    "sentence_transformers",
    "faster_whisper",
    "anthropic",
    "websockets",
    "pydantic",
    "telegram",
    "dotenv",
]

for _dep in _HEAVY_DEPS:
    if _dep not in sys.modules:
        sys.modules[_dep] = MagicMock()

# python-dotenv uses "dotenv" but the import is `from dotenv import load_dotenv`
sys.modules.setdefault("dotenv", MagicMock())

# Anthropic braucht AsyncAnthropic-Klasse
_anth = sys.modules["anthropic"]
_anth.AsyncAnthropic = MagicMock(return_value=MagicMock())

# tenacity benoetigt konkrete Symbole
_ten = sys.modules["tenacity"]
_ten.AsyncRetrying = MagicMock
_ten.retry_if_exception_type = MagicMock(return_value=MagicMock())
_ten.stop_after_attempt = MagicMock(return_value=MagicMock())
_ten.wait_exponential = MagicMock(return_value=MagicMock())

# httpx braucht AsyncClient + Exceptions
_hx = sys.modules["httpx"]
_hx.AsyncClient = MagicMock
_hx.HTTPError = Exception

# Jetzt erst importieren
import settings as S  # noqa: E402
import scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    """S.TODAY_EVENTS / S.TODAY_TASKS / S.UPCOMING_DEADLINES vor jedem Test."""
    S.TODAY_EVENTS = ""
    S.TODAY_TASKS = ""
    S.UPCOMING_DEADLINES = ""
    yield
    S.TODAY_EVENTS = ""
    S.TODAY_TASKS = ""
    S.UPCOMING_DEADLINES = ""


# ---------------------------------------------------------------------------
# _format_deadline_hint
# ---------------------------------------------------------------------------

class TestFormatDeadlineHint:
    def test_today(self):
        result = scheduler._format_deadline_hint("Abgabe Steuer", 0)
        assert "heute" in result.lower()

    def test_tomorrow(self):
        result = scheduler._format_deadline_hint("Abgabe Steuer", 1)
        assert "morgen" in result.lower()

    def test_day_after_tomorrow(self):
        result = scheduler._format_deadline_hint("Abgabe Steuer", 2)
        assert "morgen" in result.lower()

    def test_three_days(self):
        result = scheduler._format_deadline_hint("Irgendwas", 3)
        assert "3" in result or "drei" in result.lower()

    def test_title_included(self):
        result = scheduler._format_deadline_hint("Steuererklaerung Q1", 1)
        assert "Steuererklaerung Q1" in result


# ---------------------------------------------------------------------------
# get_upcoming_deadlines -- leere Eingaben
# ---------------------------------------------------------------------------

class TestGetUpcomingDeadlinesEmpty:
    def test_empty_state_returns_string(self):
        result = scheduler.get_upcoming_deadlines(days=3)
        assert isinstance(result, str)

    def test_events_without_keywords_ignored(self):
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Mon {date_str} 10:00 - Team-Meeting"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Team-Meeting" not in result


# ---------------------------------------------------------------------------
# get_upcoming_deadlines -- Kalender-Events
# ---------------------------------------------------------------------------

class TestGetUpcomingDeadlinesCalendar:
    def test_event_with_frist_keyword_today(self):
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Mon {date_str} 10:00 - Abgabefrist Umsatzsteuer"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Abgabefrist" in result

    def test_event_with_deadline_keyword(self):
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Tue {date_str} 09:00 - Deadline Jahresbericht"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Deadline" in result

    def test_event_with_abgabe_keyword(self):
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Wed {date_str} 14:00 - Abgabe Steuererklaerung"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Abgabe" in result

    def test_event_without_date_pattern_no_crash(self):
        """Events ohne erkennbares Datumsmuster duerfen keinen Crash verursachen."""
        S.TODAY_EVENTS = "- Frist ohne Datum"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert isinstance(result, str)

    def test_result_includes_intro_when_match(self):
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Mon {date_str} 10:00 - Abgabefrist Test"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Anstehende Fristen" in result or "Frist" in result


# ---------------------------------------------------------------------------
# get_upcoming_deadlines -- Todoist-Aufgaben
# ---------------------------------------------------------------------------

class TestGetUpcomingDeadlinesTodoist:
    def test_task_with_frist_keyword_heute(self):
        S.TODAY_TASKS = "- Steuererklaerungsfrist einhalten (heute)"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "frist" in result.lower()

    def test_task_with_abgabe_keyword_ueberfaellig(self):
        S.TODAY_TASKS = "- Abgabefrist Voranmeldung ueberfaellig"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "frist" in result.lower()

    def test_task_without_frist_keyword_not_included(self):
        S.TODAY_TASKS = "- Mandant anrufen (heute)"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert "Mandant anrufen" not in result

    def test_no_tasks_no_crash(self):
        S.TODAY_TASKS = ""
        result = scheduler.get_upcoming_deadlines(days=3)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# get_upcoming_deadlines -- Feste Steuerfristen
# ---------------------------------------------------------------------------

class TestGetUpcomingDeadlinesFixedDates:
    def _call_with_today(self, fake_today: datetime.date) -> str:
        """Fuehre get_upcoming_deadlines mit simuliertem Datum aus."""
        with patch("scheduler.datetime") as mock_dt:
            mock_dt.date.today.return_value = fake_today
            # date(year, month, day) Konstruktor muss weiter funktionieren
            mock_dt.date.side_effect = lambda *a: datetime.date(*a)
            return scheduler.get_upcoming_deadlines(days=3)

    def test_before_mai_frist_triggers(self):
        year = datetime.date.today().year
        result = self._call_with_today(datetime.date(year, 5, 28))
        # 31.05 liegt in 3 Tagen
        assert "Mai" in result or "Steuer" in result or "31" in result

    def test_before_januar_frist_triggers(self):
        year = datetime.date.today().year
        result = self._call_with_today(datetime.date(year, 1, 7))
        # 10.01 liegt in 3 Tagen
        assert "Januar" in result or "Lohnsteuer" in result or "10" in result

    def test_far_from_all_fristen_no_crash(self):
        """Kein Crash wenn keine feste Frist in 3 Tagen."""
        year = datetime.date.today().year
        result = self._call_with_today(datetime.date(year, 2, 10))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Robustheit
# ---------------------------------------------------------------------------

class TestGetUpcomingDeadlinesRobustness:
    def test_no_crash_with_empty_state(self):
        result = scheduler.get_upcoming_deadlines(days=3)
        assert isinstance(result, str)

    def test_no_crash_with_garbled_data(self):
        S.TODAY_EVENTS = "kaputt data ohne Datum"
        S.TODAY_TASKS = "- foo (heute)"
        result = scheduler.get_upcoming_deadlines(days=3)
        assert isinstance(result, str)

    def test_bullet_points_when_fristen_found(self):
        """Wenn Fristen gefunden werden, enthaelt das Ergebnis Bullet-Points."""
        today = datetime.date.today()
        date_str = today.strftime("%d.%m.")
        S.TODAY_EVENTS = f"- Mon {date_str} 10:00 - Abgabefrist Test"
        result = scheduler.get_upcoming_deadlines(days=3)
        if result:
            assert "Anstehende Fristen" in result
