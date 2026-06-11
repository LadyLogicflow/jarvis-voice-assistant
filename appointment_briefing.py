"""
15-Minuten-Termin-Briefing (Issue #222).

Kurz vor einem Kalendertermin werden Namen aus dem Termin extrahiert,
Kontext aus Google Contacts, Personengedächtnis, Mails und Todoist
zusammengestellt und als strukturierte Kachel auf WebUI und Telegram
ausgegeben. Keine Sprachausgabe.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re

import settings as S

log = logging.getLogger("jarvis")

_BRIEFING_LOOKAHEAD_MIN = 17   # Termin ab jetzt + 17 min erkannt
_BRIEFING_WINDOW_MIN    = 13   # … bis jetzt + 13 min
_POLL_INTERVAL_SEC      = 5 * 60

# Event-IDs die bereits ein Briefing erhalten haben.
_briefed_event_ids: set[str] = set()

_NAME_EXTRACT_SYSTEM = (
    "Du extrahierst Personennamen aus Kalendertermin-Daten. "
    "Antworte AUSSCHLIESSLICH mit einem JSON-Array von Strings: [\"Name1\", \"Name2\"]. "
    "Extrahiere NUR Personennamen (Vor- und/oder Nachname). "
    "Keine Firmennamen, keine Orte, keine Berufsbezeichnungen allein. "
    "Wenn keine Namen erkennbar sind: []. Kein Markdown, nur das JSON-Array."
)


async def _extract_names(event: dict) -> list[str]:
    """Extrahiert Personennamen aus Termin-Titel, Beschreibung und Teilnehmern."""
    summary = event.get("summary", "")
    description = event.get("description", "") or ""
    attendees = event.get("attendees", []) or []
    attendee_names = [
        a.get("displayName", "") or a.get("email", "")
        for a in attendees
        if a.get("self") is not True  # eigene Adresse ausschliessen
    ]
    user_msg = f"Titel: {summary}\nBeschreibung: {description[:300]}\nTeilnehmer: {', '.join(attendee_names[:10])}"
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=120,
            system=_NAME_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip() if resp.content else "[]"
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        names = json.loads(raw)
        if isinstance(names, list):
            return [str(n).strip() for n in names if n]
    except Exception as e:
        log.warning("appointment_briefing: Namens-Extraktion fehlgeschlagen: %s: %s",
                    type(e).__name__, e)
    return []


async def _get_person_context(name: str) -> dict:
    """Sammelt Kontext zu einer Person aus allen verfügbaren Quellen."""
    result: dict = {"name": name, "contact": None, "profile": None,
                    "last_mail": "", "tasks": []}
    # 1. persons_db
    try:
        import persons_db as _pdb
        profile = _pdb.find_by_name(name)
        if profile:
            result["profile"] = profile
    except Exception as e:
        log.debug("appointment_briefing: persons_db lookup %r: %s", name, e)

    # 2. Google Contacts
    try:
        import google_contacts_tools as _gc
        contacts = await _gc.find_contacts_by_name(name)
        if contacts:
            result["contact"] = contacts[0]
    except Exception as e:
        log.debug("appointment_briefing: contacts lookup %r: %s", name, e)

    # 3. Letzte Mail aus persons_db (falls Profil gefunden)
    if result["profile"] and result["profile"].notes:
        mail_notes = [n for n in result["profile"].notes if "Mail" in n]
        if mail_notes:
            result["last_mail"] = mail_notes[-1][:120]

    # 4. Todoist-Aufgaben mit Name im Titel
    try:
        import todoist_tools as _td
        if S.TODOIST_TOKEN:
            tasks = await _td.get_tasks(S.TODOIST_TOKEN)
            name_lower = name.lower()
            name_parts = name_lower.split()
            matched = [
                t for t in (tasks or [])
                if any(part in t.get("content", "").lower() for part in name_parts)
            ]
            result["tasks"] = matched[:3]
    except Exception as e:
        log.debug("appointment_briefing: todoist lookup %r: %s", name, e)

    return result


def _format_time(start_info: dict) -> str:
    """Gibt HH:MM aus einem Google-Calendar-Start-Dict zurück."""
    dt = start_info.get("dateTime", "")
    if "T" in dt:
        try:
            return dt[11:16]
        except Exception:
            pass
    return start_info.get("date", "")


def _format_html(event: dict, persons: list[dict]) -> str:
    """Baut die HTML-Kachel für die WebUI."""
    summary = event.get("summary", "Termin")
    start_info = event.get("start", {})
    end_info = event.get("end", {})
    time_str = _format_time(start_info)
    end_str = _format_time(end_info)
    location = event.get("location", "") or ""

    time_line = f"{time_str}–{end_str}" if end_str else time_str
    if location:
        time_line += f" | {location}"

    html = (
        f"<b>📅 In 15 Min: {summary}</b><br>"
        f"🕐 {time_line}<br>"
    )

    if not persons:
        html += "<br><i>Keine Personendaten verfügbar.</i>"
        return html

    for p in persons:
        name = p["name"]
        contact = p.get("contact")
        profile = p.get("profile")
        last_mail = p.get("last_mail", "")
        tasks = p.get("tasks", [])

        meta_parts = []
        if contact:
            if contact.organization:
                meta_parts.append(contact.organization)
            if contact.phones:
                meta_parts.append(contact.phones[0])
        elif profile and profile.funktion:
            meta_parts.append(profile.funktion)

        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        html += f"<br><b>👤 {name}{meta}</b><br>"

        if last_mail:
            html += f"📧 {last_mail}<br>"
        else:
            html += "📧 Keine bekannte E-Mail-Kommunikation<br>"

        for task in tasks:
            due = task.get("due", {}) or {}
            due_str = f" (fällig {due.get('date', '')})" if due.get("date") else ""
            html += f"✅ {task.get('content', '')}{due_str}<br>"

    return html


def _format_telegram(event: dict, persons: list[dict]) -> str:
    """Baut die strukturierte Telegram-Nachricht."""
    summary = event.get("summary", "Termin")
    start_info = event.get("start", {})
    end_info = event.get("end", {})
    time_str = _format_time(start_info)
    end_str = _format_time(end_info)
    location = event.get("location", "") or ""

    time_line = f"{time_str}–{end_str}" if end_str else time_str
    if location:
        time_line += f" | {location}"

    lines = [
        f"📅 *In 15 Min: {summary}*",
        f"🕐 {time_line}",
    ]

    if not persons:
        lines.append("\n_Keine Personendaten verfügbar._")
        return "\n".join(lines)

    for p in persons:
        name = p["name"]
        contact = p.get("contact")
        profile = p.get("profile")
        last_mail = p.get("last_mail", "")
        tasks = p.get("tasks", [])

        meta_parts = []
        if contact:
            if contact.organization:
                meta_parts.append(contact.organization)
            if contact.phones:
                meta_parts.append(contact.phones[0])
        elif profile and profile.funktion:
            meta_parts.append(profile.funktion)

        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        lines.append(f"\n👤 *{name}*{meta}")

        if last_mail:
            lines.append(f"📧 {last_mail}")
        else:
            lines.append("📧 Keine bekannte E-Mail-Kommunikation")

        for task in tasks:
            due = task.get("due", {}) or {}
            due_str = f" (fällig {due.get('date', '')})" if due.get("date") else ""
            lines.append(f"✅ {task.get('content', '')}{due_str}")

    return "\n".join(lines)


def _format_html_v2(event: dict, person_contexts: list[tuple[str, str]]) -> str:
    """Baut die HTML-Kachel für die WebUI mit strukturiertem Personenkontext."""
    summary = event.get("summary", "Termin")
    start_info = event.get("start", {})
    end_info = event.get("end", {})
    time_str = _format_time(start_info)
    end_str = _format_time(end_info)
    location = event.get("location", "") or ""

    time_line = f"{time_str}–{end_str}" if end_str else time_str
    if location:
        time_line += f" | {location}"

    html = (
        f"<b>📅 In 15 Min: {summary}</b><br>"
        f"🕐 {time_line}<br>"
    )

    if not person_contexts:
        html += "<br><i>Keine Personendaten verfügbar.</i>"
        return html

    for name, ctx in person_contexts:
        if ctx:
            html += f"<br><pre>{_html.escape(ctx)}</pre>"
        else:
            html += f"<br><b>👤 {name}</b><br><i>Kein Kontext verfügbar.</i><br>"

    return html


def _format_telegram_v2(event: dict, person_contexts: list[tuple[str, str]]) -> str:
    """Baut die strukturierte Telegram-Nachricht mit strukturiertem Personenkontext."""
    summary = event.get("summary", "Termin")
    start_info = event.get("start", {})
    end_info = event.get("end", {})
    time_str = _format_time(start_info)
    end_str = _format_time(end_info)
    location = event.get("location", "") or ""

    time_line = f"{time_str}–{end_str}" if end_str else time_str
    if location:
        time_line += f" | {location}"

    lines = [
        f"📅 *In 15 Min: {summary}*",
        f"🕐 {time_line}",
    ]

    if not person_contexts:
        lines.append("\n_Keine Personendaten verfügbar._")
        return "\n".join(lines)

    for name, ctx in person_contexts:
        if ctx:
            lines.append("")
            lines.extend(ctx.splitlines())
        else:
            lines.append(f"\n👤 *{name}* — Kein Kontext verfügbar.")

    return "\n".join(lines)


async def build_and_send_briefing(event: dict) -> None:
    """Erstellt das Briefing für einen Termin und sendet es auf WebUI + Telegram."""
    import person_context as _pc

    names = await _extract_names(event)
    log.info("appointment_briefing: event=%r names=%r",
             event.get("summary"), names)

    async def _get_context_for_name(name: str) -> tuple[str, str]:
        """Gibt (name, kontext_text) zurück."""
        email = ""
        # Versuche E-Mail aus persons_db zu ermitteln
        try:
            import persons_db as _pdb
            profile = _pdb.find_by_name(name)
            if profile and profile.emails:
                email = profile.emails[0]
        except Exception:
            pass
        # Fallback: Google Contacts
        if not email:
            try:
                import google_contacts_tools as _gc
                contacts = await _gc.find_contacts_by_name(name)
                if contacts and contacts[0].emails:
                    email = contacts[0].emails[0]
            except Exception:
                pass
        try:
            ctx = await _pc.enrich_mail_with_person_context(email, name)
        except Exception as exc:
            log.warning("appointment_briefing: context lookup failed for %r: %s", name, exc)
            ctx = ""
        return name, ctx

    person_contexts: list[tuple[str, str]] = []
    if names:
        ctx_tasks = [_get_context_for_name(n) for n in names[:5]]
        results = await asyncio.gather(*ctx_tasks, return_exceptions=True)
        person_contexts = [
            r if isinstance(r, tuple) else (names[i], "")
            for i, r in enumerate(results)
        ]

    html_msg = _format_html_v2(event, person_contexts)
    tg_msg = _format_telegram_v2(event, person_contexts)

    try:
        from server import broadcast_to_all_sessions
        await broadcast_to_all_sessions(html_msg)
    except Exception as e:
        log.debug("appointment_briefing: WebUI-Broadcast fehlgeschlagen: %s", e)

    # Telegram
    try:
        import telegram_bot as _tgb
        await _tgb.send_user_text(tg_msg)
    except Exception as e:
        log.warning("appointment_briefing: Telegram fehlgeschlagen: %s: %s",
                    type(e).__name__, e)


async def appointment_briefing_scheduler() -> None:
    """Long-running Task: alle 5 Minuten Kalender auf 15-Minuten-Termine prüfen.

    Sendet für jeden noch nicht gebrieften Termin eine Kachel auf WebUI und
    Telegram. Respektiert Quiet-Hours (Telegram) und Mac-Quiet-Hours (WebUI).
    """
    import datetime
    import google_calendar_tools as _gcal

    log.info("appointment_briefing_scheduler: gestartet (alle 5 Minuten, Fenster %d-%d min)",
             _BRIEFING_WINDOW_MIN, _BRIEFING_LOOKAHEAD_MIN)

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            window_start = now + datetime.timedelta(minutes=_BRIEFING_WINDOW_MIN)
            window_end = now + datetime.timedelta(minutes=_BRIEFING_LOOKAHEAD_MIN)
            events = await _gcal.get_events_raw(window_start, window_end)

            for event in events:
                event_id = event.get("id", "")
                if not event_id or event_id in _briefed_event_ids:
                    continue
                _briefed_event_ids.add(event_id)
                log.info("appointment_briefing_scheduler: Briefing für %r",
                         event.get("summary"))
                asyncio.create_task(build_and_send_briefing(event))

            # Unbegrenztes Wachstum verhindern
            if len(_briefed_event_ids) > 500:
                ids_list = list(_briefed_event_ids)
                _briefed_event_ids.clear()
                _briefed_event_ids.update(ids_list[-250:])

        except Exception as e:
            log.warning("appointment_briefing_scheduler: %s: %s",
                        type(e).__name__, e)

        await asyncio.sleep(_POLL_INTERVAL_SEC)
