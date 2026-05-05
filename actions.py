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
from prompt import pick_address
import screen_capture
import session_state
import todoist_tools

log = S.log


def _load_business_context() -> str:
    """Catrins business_context.md als Hintergrund fuer Mail-Antworten.

    Datei liegt im Workspace-Root, gitignored. Wird bei jedem Aufruf
    frisch gelesen — Catrin kann waehrend der Server laeuft Aenderungen
    einpflegen. Gibt leeren String zurueck wenn die Datei fehlt."""
    import os
    path = os.path.join(os.path.dirname(__file__), "business_context.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.warning(f"_load_business_context failed: {type(e).__name__}: {e}")
        return ""


async def _generate_draft_body(mail_data: dict, instruction: str = "") -> str:
    """Lass Claude einen Antwort-Entwurf basierend auf Original-Mail
    erstellen. instruction ist optional — wenn leer, schlaegt Jarvis
    proaktiv eine sinnvolle Antwort vor und nutzt dabei den
    business_context.md falls vorhanden.

    Liefert reinen Mail-Text — ODER einen NEED_INPUT-Marker, wenn
    Claude erkennt dass er ohne Eckpunkte von Catrin kein guter
    Vorschlag liefern kann (z.B. weil weder Mail noch Kontext einen
    Sachverhalt nahelegen, dem er einfach folgen koennte)."""
    business = _load_business_context()
    business_block = (
        f"\n\nGESCHAEFTLICHER KONTEXT (nutze diese Hinweise wenn die "
        f"Original-Mail einen Sachverhalt anspricht der dort beschrieben ist):\n\n"
        f"{business}\n"
        if business else ""
    )
    sys_prompt = (
        f"Du bist Jarvis, der Butler-Assistent von {S.USER_NAME} "
        f"({S.USER_ROLE}). Erstelle eine PROFESSIONELLE deutsche E-Mail-"
        f"Antwort im Namen von {S.USER_NAME}. Stil: foermlich, knapp, "
        f"klar, ohne Floskeln. Format: passende Anrede ('Sehr geehrte Frau X' "
        f"/ 'Sehr geehrter Herr Y' / 'Hallo X' wenn der Tonfall der Original-"
        f"Mail das nahelegt), 1-3 Saetze Inhalt, Gruss-Zeile ('Mit freundlichen "
        f"Gruessen' oder 'Beste Gruesse'), {S.USER_NAME}. KEINE Tags, KEINE "
        f"Erklaerungen davor oder dahinter, NUR der Mail-Text."
        f"{business_block}"
        f"\n\nWICHTIG — Wenn KEIN Vorschlag moeglich:\n"
        f"Wenn die Original-Mail einen Sachverhalt anspricht den weder der "
        f"GESCHAEFTLICHE KONTEXT abdeckt noch Du aus dem Mail-Inhalt allein "
        f"sinnvoll beantworten kannst (z.B. weil die Mail eine konkrete "
        f"Entscheidung von {S.USER_NAME} verlangt: Termin-Zusage, inhaltliche "
        f"Stellungnahme, Bewertung), dann erfinde KEINE Antwort. Antworte "
        f"stattdessen NUR mit der Zeile:\n"
        f"NEED_INPUT: <eine kurze Frage was Du von {S.USER_NAME} brauchst, "
        f"max 80 Zeichen>\n"
        f"Beispiel: 'NEED_INPUT: Soll ich den Termin am Donnerstag zusagen?'\n"
        f"Beispiel: 'NEED_INPUT: Wie sind die Konditionen die ich bestaetigen soll?'"
    )
    if instruction:
        user_msg = (
            f"Original-Mail von: {mail_data.get('sender', '')}\n"
            f"Betreff: {mail_data.get('subject', '')}\n"
            f"Inhalt:\n{(mail_data.get('text', '') or '')[:1500]}\n\n"
            f"---\n"
            f"Konkrete Anweisung von {S.USER_NAME} fuer die Antwort: {instruction}"
        )
    else:
        user_msg = (
            f"Original-Mail von: {mail_data.get('sender', '')}\n"
            f"Betreff: {mail_data.get('subject', '')}\n"
            f"Inhalt:\n{(mail_data.get('text', '') or '')[:1500]}\n\n"
            f"---\n"
            f"Schlage proaktiv eine sinnvolle Antwort vor — nutze dazu den "
            f"GESCHAEFTLICHEN KONTEXT oben falls die Mail einen darin "
            f"beschriebenen Sachverhalt betrifft. Wenn Du KEINEN sinnvollen "
            f"Vorschlag liefern kannst, antworte mit NEED_INPUT statt eine "
            f"Antwort zu erfinden."
        )
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"_generate_draft_body failed: {type(e).__name__}: {e}")
        return ""


async def _revise_draft_body(old_body: str, instruction: str) -> str:
    """Ueberarbeite den bestehenden Entwurf basierend auf einer
    konkreten Aenderungs-Anweisung. Liefert reinen Mail-Text."""
    sys_prompt = (
        "Du bist Jarvis. Ueberarbeite den folgenden E-Mail-Entwurf gemaess "
        "der Anweisung. Behalte Anrede, Schluss und Catrin als Absenderin. "
        "Behalte den professionellen, knappen Ton. NUR der ueberarbeitete "
        "Mail-Text, keine Erklaerung und kein 'Hier der ueberarbeitete Entwurf:'."
    )
    user_msg = (
        f"Aktueller Entwurf:\n{old_body}\n\n"
        f"---\n"
        f"Anweisung: {instruction}"
    )
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"_revise_draft_body failed: {type(e).__name__}: {e}")
        return ""


# Sentinels returned by tool helpers when there's nothing to report.
# Format-strings (NOT f-strings) so the address is randomized at use-
# time via empty_reply() — module-level f-strings would freeze it.
_EMPTY_REPLY_TEMPLATES = {
    "KEINE_MAILS":   "Ihr Posteingang ist leer, {addr}. Eine seltene Erscheinung.",
    "KEINE_TERMINE": "Ihr Kalender ist die naechsten Tage frei, {addr}. Erholung in Sicht.",
    "KEINE_TASKS":   "Keine offenen Aufgaben, {addr}. Eine angenehme Lage.",
}


# Sentinel keys (used by callers to detect empty results before they
# call empty_reply()).
EMPTY_REPLY_KEYS = frozenset(_EMPTY_REPLY_TEMPLATES)


def empty_reply(sentinel: str) -> str:
    """Render an empty-reply sentinel into spoken text with a freshly
    chosen address."""
    template = _EMPTY_REPLY_TEMPLATES.get(sentinel)
    if template is None:
        return ""
    return template.format(addr=pick_address())


# Backwards-compat shim: existing callers do `if action_result in
# EMPTY_REPLIES: msg = EMPTY_REPLIES[action_result]`. Wrap as a
# membership-checkable proxy that resolves on lookup.
class _EmptyRepliesProxy:
    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in _EMPTY_REPLY_TEMPLATES
    def __getitem__(self, key: str) -> str:
        return empty_reply(key)

EMPTY_REPLIES = _EmptyRepliesProxy()


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
            return f"Diese URL kann ich nicht oeffnen, {pick_address()}. Nur http- und https-Adressen sind erlaubt."
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
            return f"Es liegt gerade keine Mail zur Diskussion vor, {pick_address()}."
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

    elif t == "DRAFT_REPLY":
        # Initialer Antwort-Entwurf zur aktiven Mail. Payload OPTIONAL:
        # wenn leer, schlaegt Jarvis proaktiv basierend auf
        # business_context.md eine sinnvolle Antwort vor. Wenn gegeben,
        # ist's Catrins konkrete Anweisung (z.B. "Termin verschiebt
        # sich auf Donnerstag 14 Uhr").
        active = session_state.get("default").active_mail
        if not active:
            return f"Keine Mail aktiv, {pick_address()}."
        instruction = p.strip()
        mail_data = await mail_actions.read_mail_body(active.account, active.uid)
        if "error" in mail_data:
            return f"Mail konnte nicht geladen werden: {mail_data['error']}"
        draft_body = await _generate_draft_body(mail_data, instruction)
        if not draft_body:
            return "Konnte den Entwurf nicht erstellen."
        # NEED_INPUT-Marker: Claude konnte ohne Eckpunkte keinen
        # Vorschlag bauen. Frage Catrin nach.
        if draft_body.startswith("NEED_INPUT:"):
            question = draft_body.split(":", 1)[1].strip()
            return (
                f"Hier habe ich keinen passenden Standard-Sachverhalt — "
                f"{question} Sag mir Eckpunkte, dann baue ich den Entwurf."
            )
        # Ablage im Pending-Slot.
        acc = mail_actions._account_by_name(active.account)
        from_addr = (acc or {}).get("user", "")
        to_addr = active.sender or mail_data.get("sender", "")
        subject = active.subject or mail_data.get("subject", "")
        session_state.set_pending_draft("default", session_state.PendingDraft(
            account=active.account,
            to=to_addr,
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
            body=draft_body,
            in_reply_to=active.message_id,
            references=active.references,
        ))
        return (
            f"Mein Vorschlag:\n\n{draft_body}\n\n"
            f"Soll ich das so freigeben?"
        )

    elif t == "DRAFT_REVISE":
        # Aenderungs-Anweisung auf den aktiven Pending-Draft anwenden.
        # Payload = Catrins Aenderungs-Anweisung.
        pending = session_state.get("default").pending_draft
        if not pending:
            return f"Es liegt kein Entwurf zur Ueberarbeitung vor, {pick_address()}."
        instruction = p.strip()
        if not instruction:
            return "Welche Aenderung soll ich vornehmen?"
        new_body = await _revise_draft_body(pending.body, instruction)
        if not new_body:
            return "Konnte den Entwurf nicht ueberarbeiten."
        pending.body = new_body
        session_state.set_pending_draft("default", pending)
        return (
            f"Neuer Vorschlag:\n\n{new_body}\n\n"
            f"Soll ich das so freigeben?"
        )

    elif t == "DRAFT_APPROVE":
        # IMAP APPEND in Drafts + Original-Mail markieren + State leeren.
        pending = session_state.get("default").pending_draft
        if not pending:
            return f"Es liegt kein Entwurf zum Freigeben vor, {pick_address()}."
        acc = mail_actions._account_by_name(pending.account)
        from_addr = (acc or {}).get("user", "")
        msg_bytes = mail_actions.build_reply_message(
            from_addr=from_addr,
            to_addr=pending.to,
            subject=pending.subject,
            body=pending.body,
            in_reply_to=pending.in_reply_to,
            references=pending.references,
        )
        ok, folder = await mail_actions.append_to_drafts(pending.account, msg_bytes)
        if not ok:
            return f"Konnte den Entwurf nicht ablegen: {folder}"
        # Original-Mail markieren falls noch aktiv.
        active = session_state.get("default").active_mail
        if active:
            await mail_actions.mark_mail_read(active.account, active.uid)
            session_state.clear_active_mail("default")
        session_state.clear_pending_draft("default")
        return (
            f"Entwurf liegt im {folder}-Ordner deines {pending.account}-Kontos. "
            f"Du kannst ihn jetzt aus Apple Mail senden."
        )

    elif t == "DRAFT_CANCEL":
        pending = session_state.get("default").pending_draft
        session_state.clear_pending_draft("default")
        if not pending:
            return "Kein Entwurf zum Verwerfen."
        return f"Vergessen, {pick_address()}."

    elif t == "MAIL_TO_TASK":
        # Aufgabe aus aktueller Mail generieren + in Todoist-Inbox
        # ablegen + Mail markieren. Benutzt active_mail aus session_state.
        active = session_state.get("default").active_mail
        if not active:
            return f"Keine Mail aktiv, {pick_address()}."
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        # Body holen damit der Aufgaben-Generator Kontext hat.
        mail_data = await mail_actions.read_mail_body(active.account, active.uid)
        if "error" in mail_data:
            return f"Mail konnte nicht geladen werden: {mail_data['error']}"
        # Claude formuliert eine praegnante Aufgaben-Beschreibung.
        gen_prompt = (
            "Du bist Jarvis. Erstelle aus der folgenden Mail eine PRAEGNANTE, "
            "AKTIONALE Aufgabenbeschreibung in der Imperativform — maximal 80 "
            "Zeichen. Beispiele: 'Rueckruf bei Mueller', 'Frist Steuererklaerung "
            "bis 31.5. pruefen', 'Vertrag Anlage A unterzeichnen'. "
            "Antworte NUR mit dem Aufgabentext, KEINE Begruessung, KEINE Erklaerung, "
            "KEINE Anfuehrungszeichen, KEINE Tags."
        )
        user_msg = (
            f"Absender: {mail_data['sender']}\n"
            f"Betreff: {mail_data['subject']}\n"
            f"Inhalt: {mail_data['text'][:600]}"
        )
        try:
            resp = await S.ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=gen_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            task_text = resp.content[0].text.strip().strip('"\'').strip()
        except Exception as e:
            log.warning(f"MAIL_TO_TASK Claude error: {type(e).__name__}: {e}")
            # Fallback: Subject-only.
            task_text = (mail_data['subject'] or "Mail-Aufgabe")[:80]
        if not task_text:
            task_text = (mail_data['subject'] or "Mail-Aufgabe")[:80]
        # In Todoist-Inbox (kein project_id) ablegen.
        result = await todoist_tools.add_task(S.TODOIST_TOKEN, task_text)
        # Mail markieren + State clearen.
        await mail_actions.mark_mail_read(active.account, active.uid)
        session_state.clear_active_mail("default")
        if result.startswith("Aufgabe angelegt"):
            return f"Aufgabe im Eingang angelegt: {task_text}. Mail ist abgehakt."
        return f"Aufgabe vermerkt — {result}"

    return ""
