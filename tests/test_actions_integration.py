"""Integration tests for `actions.execute_action`.

Each test patches the relevant tool with a fast in-process stub so the
action handler is exercised end-to-end without actually hitting the
network, AppleScript, the browser, or the Anthropic API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import actions
import settings as S


# ---- Helper -------------------------------------------------------------

async def _exec(action_type: str, payload: str = "") -> str:
    return await actions.execute_action({"type": action_type, "payload": payload})


# ---- SEARCH -------------------------------------------------------------

async def test_search_action_returns_summary():
    fake = {"title": "Pasta Rezepte", "url": "https://chefkoch.de", "content": "Lecker."}
    with patch.object(actions.browser_tools, "search_and_read", new=AsyncMock(return_value=fake)):
        result = await _exec("SEARCH", "pasta rezepte")
    assert "Pasta Rezepte" in result
    assert "chefkoch.de" in result


async def test_search_action_handles_error():
    fake = {"error": "timeout"}
    with patch.object(actions.browser_tools, "search_and_read", new=AsyncMock(return_value=fake)):
        result = await _exec("SEARCH", "x")
    assert "fehlgeschlagen" in result.lower()


# ---- BROWSE -------------------------------------------------------------

async def test_browse_action_returns_summary():
    fake = {"title": "Page", "content": "Body of the page"}
    with patch.object(actions.browser_tools, "visit", new=AsyncMock(return_value=fake)):
        result = await _exec("BROWSE", "https://example.com")
    assert "Body of the page" in result


# ---- OPEN ---------------------------------------------------------------

async def test_open_action_success_returns_geoeffnet():
    with patch.object(actions.browser_tools, "open_url",
                      new=AsyncMock(return_value={"success": True, "url": "https://x.com"})):
        result = await _exec("OPEN", "https://x.com")
    assert result.startswith("Geoeffnet")


async def test_open_action_rejects_unsafe_url():
    with patch.object(actions.browser_tools, "open_url",
                      new=AsyncMock(return_value={"success": False, "url": "file:///x", "error": "rejected"})):
        result = await _exec("OPEN", "file:///etc/passwd")
    assert "kann ich nicht oeffnen" in result


# ---- NEWS ---------------------------------------------------------------

async def test_news_action_returns_text():
    with patch.object(actions.browser_tools, "fetch_news",
                      new=AsyncMock(return_value="Tagesschau Aktuelle Meldungen:\n• A\n• B")):
        result = await _exec("NEWS")
    assert "Tagesschau" in result


# ---- MAIL ---------------------------------------------------------------

async def test_mail_action_no_mails():
    with patch.object(actions.mail_tools, "get_unread_mails", return_value="KEINE_MAILS"):
        result = await _exec("MAIL")
    assert result == "KEINE_MAILS"


async def test_mail_action_with_unread():
    canned = "Ungelesen insgesamt: 3\n\n---\nVon: Mueller\nBetreff: Frist\n"
    with patch.object(actions.mail_tools, "get_unread_mails", return_value=canned):
        result = await _exec("MAIL")
    assert "Mueller" in result


# ---- TASKS / ADDTASK / DONETASK ----------------------------------------

async def test_tasks_action_no_token(monkeypatch):
    monkeypatch.setattr(S, "TODOIST_TOKEN", "")
    result = await _exec("TASKS")
    assert "nicht konfiguriert" in result


async def test_tasks_action_with_token(monkeypatch):
    monkeypatch.setattr(S, "TODOIST_TOKEN", "fake-token")
    with patch.object(actions.todoist_tools, "get_tasks",
                      new=AsyncMock(return_value="Todoist — 2 offene Aufgaben:\n• A\n• B")):
        result = await _exec("TASKS")
    assert "2 offene" in result


async def test_addtask_action_with_due(monkeypatch):
    monkeypatch.setattr(S, "TODOIST_TOKEN", "fake-token")
    captured = {}

    async def fake_add(token, content, due):
        captured.update(token=token, content=content, due=due)
        return f"Aufgabe angelegt: {content} — fällig {due}"

    with patch.object(actions.todoist_tools, "add_task", new=fake_add):
        result = await _exec("ADDTASK", "Steuererklaerung pruefen | morgen")
    assert captured == {"token": "fake-token", "content": "Steuererklaerung pruefen", "due": "morgen"}
    assert "angelegt" in result


async def test_addtask_action_without_due(monkeypatch):
    monkeypatch.setattr(S, "TODOIST_TOKEN", "fake-token")
    with patch.object(actions.todoist_tools, "add_task",
                      new=AsyncMock(return_value="Aufgabe angelegt: Foo")):
        result = await _exec("ADDTASK", "Foo")
    assert "angelegt" in result


async def test_donetask_action(monkeypatch):
    monkeypatch.setattr(S, "TODOIST_TOKEN", "fake-token")
    with patch.object(actions.todoist_tools, "complete_task",
                      new=AsyncMock(return_value="Erledigt: Foo")):
        result = await _exec("DONETASK", "Foo")
    assert "Erledigt" in result


# ---- CALENDAR / ADDCAL --------------------------------------------------

async def test_calendar_action_returns_events():
    with patch.object(actions.google_calendar_tools, "get_events",
                      new=AsyncMock(return_value="Kalender — naechste 2 Termine:\n• ...")):
        result = await _exec("CALENDAR")
    assert "Kalender" in result


async def test_addcal_action_with_when():
    captured = {}

    async def fake_add(title, when):
        captured.update(title=title, when=when)
        return f"Termin angelegt: {title} am {when}"

    with patch.object(actions.google_calendar_tools, "add_event", new=fake_add):
        result = await _exec("ADDCAL", "Mandantengespraech | morgen 14 Uhr")
    assert captured == {"title": "Mandantengespraech", "when": "morgen 14 Uhr"}
    assert "angelegt" in result


# ---- NOTE ---------------------------------------------------------------

async def test_note_action_with_body():
    with patch.object(actions.notes_tools, "add_note",
                      return_value="Notiz angelegt: Mandant Mueller"):
        result = await _exec("NOTE", "Mandant Mueller | Hat angerufen")
    assert "angelegt" in result


# ---- STEUERNEWS ---------------------------------------------------------

async def test_steuernews_uses_cached_brief():
    import datetime as dt
    S.STEUER_BRIEF = "Cached brief from earlier today"
    S.STEUER_BRIEF_DATE = dt.date.today().isoformat()
    result = await _exec("STEUERNEWS")
    assert result == "Cached brief from earlier today"


async def test_steuernews_refreshes_when_no_cache():
    import datetime as dt
    import scheduler
    S.STEUER_BRIEF = ""
    S.STEUER_BRIEF_DATE = ""

    async def fake_refresh():
        S.STEUER_BRIEF = "Fresh brief"
        S.STEUER_BRIEF_DATE = dt.date.today().isoformat()

    with patch.object(scheduler, "refresh_steuer_brief", new=fake_refresh):
        result = await _exec("STEUERNEWS")
    assert result == "Fresh brief"


# ---- Unknown action -----------------------------------------------------

async def test_unknown_action_returns_empty():
    result = await _exec("DOES_NOT_EXIST")
    assert result == ""
