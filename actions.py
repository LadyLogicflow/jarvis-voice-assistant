"""
Action handler dispatcher.

`execute_action(action)` is the single entry point that maps an
`{"type": "...", "payload": "..."}` dict into the right tool call.
The action types are documented in the system prompt produced by
`prompt.build_system_prompt()`.

Action results are returned as plain strings; the websocket layer
decides whether to speak them directly, summarize via Claude, or
emit a hardcoded butler line for the empty-result sentinels
(KEINE_MAILS / KEINE_TERMINE / KEINE_TASKS).
"""

from __future__ import annotations

import asyncio
import datetime

import settings as S

# Tool modules — each one is mostly stateless, just function calls.
import browser_tools
import google_calendar_tools
import imap_mail_tools
import mail_actions
import mail_tools
import notes_tools
import screen_capture
import session_state
import todoist_tools

log = S.log


# Sentinels returned by tool helpers when there's nothing to report.
# `process_message` in server.py uses this dict to speak a hardcoded
# butler line (no extra LLM round-trip needed).
EMPTY_REPLIES = {
    "KEINE_MAILS":   f"Ihr Posteingang ist leer, {S.USER_ADDRESS}. Eine seltene Erscheinung.",
    "KEINE_TERMINE": f"Ihr Kalender ist die naechsten Tage frei, {S.USER_ADDRESS}. Erholung in Sicht.",
    "KEINE_TASKS":   f"Keine offenen Aufgaben, {S.USER_ADDRESS}. Eine angenehme Lage.",
}


async def execute_action(action: dict) -> str:
    """Dispatch one [ACTION:TYPE] payload to the appropriate tool.
    Returns the tool's text result (or one of the KEINE_* sentinels)."""
    t = action["type"]
    p = action["payload"]

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Suche fehlgeschlagen: {result.get('error', '')}"

    elif t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Seite nicht erreichbar: {result.get('error', '')}"

    elif t == "OPEN":
        result = await browser_tools.open_url(p)
        if not result.get("success"):
            return f"Diese URL kann ich nicht oeffnen, {S.USER_ADDRESS}. Nur http- und https-Adressen sind erlaubt."
        return f"Geoeffnet: {p}"

    elif t == "SCREEN":
        return await screen_capture.describe_screen(S.ai)

    elif t == "NEWS":
        return await browser_tools.fetch_news(S.NEWS_URL, S.NEWS_SOURCE_NAME)

    elif t == "MAIL":
        loop = asyncio.get_event_loop()
        if S.MAIL_BACKEND == "imap":
            if not (S.IMAP_HOST and S.IMAP_USER and S.IMAP_PASSWORD):
                return ("IMAP-Backend ausgewaehlt aber unvollstaendig konfiguriert. "
                        "Pruefe imap_host / imap_user in config.json und IMAP_PASSWORD in .env.")
            result = await loop.run_in_executor(
                None,
                lambda: imap_mail_tools.get_unread_mails_imap(
                    host=S.IMAP_HOST, user=S.IMAP_USER, password=S.IMAP_PASSWORD,
                    port=S.IMAP_PORT, use_ssl=S.IMAP_SSL, folder=S.IMAP_FOLDER, max_count=5,
                ),
            )
        else:
            result = await loop.run_in_executor(None, mail_tools.get_unread_mails, 5)
        if result == "KEINE_MAILS":
            return "KEINE_MAILS"
        return result

    elif t == "TASKS":
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        return await todoist_tools.get_tasks(
            S.TODOIST_TOKEN,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )

    elif t == "ADDTASK":
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        # Payload format: "content | due | bereich"
        # bereich (optional) is one of: privat, hilo, dihag — pins the
        # task to the matching project (and HILO section).
        parts = [x.strip() for x in p.split("|")]
        content = parts[0] if parts else ""
        due = parts[1] if len(parts) > 1 else ""
        bereich = parts[2].lower() if len(parts) > 2 else ""
        project_id = S.TODOIST_PROJECTS.get(bereich) if bereich else None
        section_id = (
            S.TODOIST_PROJECTS.get("hilo_section") if bereich == "hilo" else None
        )
        return await todoist_tools.add_task(
            S.TODOIST_TOKEN, content, due,
            project_id=project_id, section_id=section_id,
        )

    elif t == "DONETASK":
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        return await todoist_tools.complete_task(
            S.TODOIST_TOKEN, p,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )

    elif t == "CALENDAR":
        return await google_calendar_tools.get_events(days=S.CALENDAR_DAYS)

    elif t == "ADDCAL":
        parts = p.split("|", 1)
        title = parts[0].strip()
        when = parts[1].strip() if len(parts) > 1 else "morgen 10 Uhr"
        return await google_calendar_tools.add_event(title, when)

    elif t == "NOTE":
        parts = p.split("|", 1)
        title = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, notes_tools.add_note, title, body)

    elif t == "STEUERNEWS":
        # Use cached brief if fresh, otherwise fetch live.
        from scheduler import refresh_steuer_brief  # local import: avoid cycles
        today = datetime.date.today().isoformat()
        if S.STEUER_BRIEF and S.STEUER_BRIEF_DATE == today:
            return S.STEUER_BRIEF
        await refresh_steuer_brief()
        return S.STEUER_BRIEF if S.STEUER_BRIEF else "Keine neuen Veroeffentlichungen abrufbar."

    elif t == "READ_MAIL":
        # Vorlesen der aktuellen Mail (active_mail aus session_state).
        # Optional payload: "account|uid" um eine andere als die aktive
        # Mail zu adressieren. Default: 'default'-Slot — den schreibt
        # mail_monitor.broadcast_active_mail immer mit, unabhaengig
        # davon ob WebSocket-Sessions registriert sind.
        active = session_state.get("default").active_mail
        if p and "|" in p:
            acc_name, uid_str = p.split("|", 1)
            acc_name, uid_str = acc_name.strip(), uid_str.strip()
            try:
                uid_int = int(uid_str)
            except ValueError:
                return f"READ_MAIL: ungueltige UID {uid_str!r}"
        elif active:
            acc_name, uid_int = active.account, active.uid
        else:
            return f"Es liegt gerade keine Mail zur Diskussion vor, {S.USER_ADDRESS}."
        result = await mail_actions.read_mail_body(acc_name, uid_int)
        if "error" in result:
            return f"Mail konnte nicht geladen werden: {result['error']}"
        body = result["text"] or "(kein lesbarer Textinhalt)"
        return (
            f"Mail von {result['sender']}, Betreff: {result['subject']}.\n\n"
            f"{body}\n\n"
            f"Soll ich die beantworten?"
        )

    elif t == "MARK_MAIL_READ":
        # IMAP \Seen setzen + active_mail leeren. Optional payload
        # "account|uid"; Default: die aktive Mail.
        active = session_state.get("default").active_mail
        if p and "|" in p:
            acc_name, uid_str = p.split("|", 1)
            acc_name, uid_str = acc_name.strip(), uid_str.strip()
            try:
                uid_int = int(uid_str)
            except ValueError:
                return f"MARK_MAIL_READ: ungueltige UID {uid_str!r}"
        elif active:
            acc_name, uid_int = active.account, active.uid
        else:
            return "Keine aktive Mail zum Markieren."
        ok = await mail_actions.mark_mail_read(acc_name, uid_int)
        session_state.clear_active_mail("default")
        return ("Erledigt — Mail ist als gelesen markiert."
                if ok else "Markierung fehlgeschlagen, ist aber im Auge behalten.")

    return ""
