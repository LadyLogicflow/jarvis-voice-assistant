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
import os

import settings as S

# Tool modules — each one is mostly stateless, just function calls.
import browser_tools
import google_calendar_tools
import imap_mail_tools
import mail_actions
import mail_tools
import notes_tools
from prompt import llm_text, pick_address
import screen_capture
import session_state
import todoist_tools

log = S.log


def _format_phone_tts(number: str) -> str:
    """Format a phone number for natural TTS output.

    Removes clutter, ensures digit groups are space-separated so
    ElevenLabs reads them in short bursts rather than one long stream.
    '+4917612345678' → '0176 12 34 56 78'
    '+49 211 123456' → '0211 12 34 56'
    """
    import re as _re
    n = number.strip()
    # Replace leading +49 with 0 for German numbers
    n = _re.sub(r"^\+49\s*", "0", n)
    # Strip all non-digit characters to normalize
    digits = _re.sub(r"\D", "", n)
    if not digits:
        return number
    # Keep leading 0 prefix, then group remaining digits in pairs
    prefix = digits[:4] if len(digits) >= 10 else digits[:3]
    rest = digits[len(prefix):]
    groups = [rest[i:i+2] for i in range(0, len(rest), 2)]
    return prefix + " " + " ".join(groups) if groups else prefix


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


def _person_block_for_mail(mail_data: dict) -> str:
    """Wenn der Sender in persons_db gepflegt ist, liefere einen
    knappen Kontext-Block fuer den LLM-Prompt: bevorzugte Anrede,
    Funktion, offene Punkte. Sonst leerer String."""
    try:
        import persons_db
        from email.utils import parseaddr
        sender_full = mail_data.get("sender", "")
        addr = parseaddr(sender_full)[1].lower()
        if not addr:
            return ""
        profile = persons_db.find_by_email(addr)
        if not profile:
            return ""
        bits: list[str] = []
        if profile.anrede:
            bits.append(f"Bevorzugte Anrede: {profile.anrede}")
        if profile.funktion:
            bits.append(f"Funktion: {profile.funktion}")
        if profile.open_points:
            bits.append(
                "Offene Punkte mit dieser Person: "
                + "; ".join(profile.open_points[:3])
            )
        if not bits:
            return ""
        return "\n\nZUSATZWISSEN ZUM EMPFAENGER:\n- " + "\n- ".join(bits)
    except Exception:
        return ""


async def _generate_draft_body(mail_data: dict, instruction: str = "") -> str:
    """Lass Claude einen Antwort-Entwurf basierend auf Original-Mail
    erstellen. instruction ist optional — wenn leer, schlaegt Jarvis
    proaktiv eine sinnvolle Antwort vor und nutzt dabei den
    business_context.md + Personen-DB falls vorhanden.

    Liefert reinen Mail-Text — ODER einen NEED_INPUT-Marker, wenn
    Claude erkennt dass er ohne Eckpunkte von Catrin kein guter
    Vorschlag liefern kann (z.B. weil weder Mail noch Kontext einen
    Sachverhalt nahelegen, dem er einfach folgen koennte)."""
    business = _load_business_context()
    person = _person_block_for_mail(mail_data)
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
        f"{person}"
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
        return llm_text(resp).strip()
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
        return llm_text(resp).strip()
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

    elif t == "WEATHER":
        city = p.strip() or S.CITY
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8) as _c:
                _r = await _c.get(
                    f"https://wttr.in/{city}?format=j1",
                    headers={"User-Agent": "curl"},
                )
                _r.raise_for_status()
                _d = _r.json()
            _DAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
            _MONTHS_DE = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
            lines = [f"Wetter für {city}:"]
            for i, day in enumerate(_d.get("weather", [])[:3]):
                from datetime import date as _date, timedelta as _td
                day_dt = _date.today() + _td(days=i)
                day_name = "Heute" if i == 0 else ("Morgen" if i == 1 else _DAYS_DE[day_dt.weekday()])
                max_c = day.get("maxtempC", "?")
                min_c = day.get("mintempC", "?")
                desc = day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "") if day.get("hourly") else ""
                rain = max(int(h.get("chanceofrain", 0)) for h in day.get("hourly", [{}]))
                lines.append(f"{day_name}: {min_c}–{max_c}°C, {desc}, Regen {rain}%")
            return "\n".join(lines)
        except Exception as e:
            return f"Wetter für {city} konnte nicht abgerufen werden: {e}"

    elif t == "MAIL":
        loop = asyncio.get_running_loop()
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
        # Fix #73: Exception fangen und Catrin eine klare Fehlermeldung
        # geben statt still zu scheitern (silent fail).
        try:
            return await google_calendar_tools.add_event(title, when)
        except Exception as e:
            log.warning("ADDCAL fehlgeschlagen: %s: %s", type(e).__name__, e)
            return f"Termin konnte nicht angelegt werden: {e}"

    elif t == "VACATION":
        # Issue #111: Abwesenheitsnotiz via Gmail Settings API setzen/deaktivieren.
        # Payload ist ein JSON-Objekt: {"enabled": bool, "subject": str,
        # "body": str, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}.
        import json as _json
        import gmail_tools
        try:
            vac = _json.loads(p) if p.strip().startswith("{") else {}
        except _json.JSONDecodeError:
            vac = {}
        enabled = bool(vac.get("enabled", True))
        if enabled and not vac.get("subject", "").strip():
            return "Abwesenheitsnotiz kann nicht aktiviert werden — Betreff fehlt. Bitte noch einmal mit Betreff und Text angeben."
        try:
            return await gmail_tools.set_vacation(
                enabled=enabled,
                subject=vac.get("subject", ""),
                body=vac.get("body", ""),
                start_date=vac.get("start", ""),
                end_date=vac.get("end", ""),
            )
        except Exception as e:
            log.warning("VACATION fehlgeschlagen: %s: %s", type(e).__name__, e)
            return f"Abwesenheitsnotiz konnte nicht gesetzt werden: {e}"

    elif t == "NOTE":
        parts = p.split("|", 1)
        title = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, notes_tools.add_note, title, body)

    elif t == "STEUERNEWS":
        # Use cached brief if fresh, otherwise fetch live.
        # Issue #91: RSS-Feeds werden nur einmal gefetcht; das Ergebnis
        # wird an refresh_steuer_brief weitergereicht und die Hashes erst
        # nach der Brief-Generierung via commit_seen persistiert.
        from scheduler import refresh_steuer_brief  # local import: avoid cycles
        import steuer_news as _sn
        today = datetime.date.today().isoformat()
        if not (S.STEUER_BRIEF and S.STEUER_BRIEF_DATE == today):
            # 1x fetchen -- Ergebnis fuer Seen-Check UND Brief-Generierung
            raw, current_hashes = await _sn._fetch_feeds_raw()
            seen = _sn._load_seen_hashes()
            if current_hashes and current_hashes.issubset(seen):
                return (
                    "Die Steuernews sind dieselben wie zuletzt \u2014 "
                    "soll ich sie trotzdem vorlesen?"
                )
            # Brief mit bereits geholtem Raw-Text generieren (kein 2. Fetch)
            await refresh_steuer_brief(raw=raw)
            # Hashes erst jetzt persistieren (nach erfolgreicher Generierung)
            _sn.commit_seen(current_hashes)
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

    elif t == "SUMMARIZE_MAIL":
        # Kurze inhaltliche Zusammenfassung der aktiven Mail (2-3 Saetze)
        # statt wortwoertlichem Vorlesen. Optional payload "account|uid"
        # fuer eine andere als die aktive Mail; sonst Default.
        active = session_state.get("default").active_mail
        if p and "|" in p:
            acc_name, uid_str = p.split("|", 1)
            acc_name, uid_str = acc_name.strip(), uid_str.strip()
            try:
                uid_int = int(uid_str)
            except ValueError:
                return f"SUMMARIZE_MAIL: ungueltige UID {uid_str!r}"
        elif active:
            acc_name, uid_int = active.account, active.uid
        else:
            return f"Es liegt gerade keine Mail zur Diskussion vor, {pick_address()}."
        result = await mail_actions.read_mail_body(acc_name, uid_int)
        if "error" in result:
            return f"Mail konnte nicht geladen werden: {result['error']}"
        body = result.get("text", "") or "(kein lesbarer Textinhalt)"
        sender = result.get("sender", "")
        subject = result.get("subject", "")
        sys_prompt = (
            "Du bist Jarvis, der britisch-hoefliche KI-Butler. Fasse die folgende "
            "E-Mail in 2-3 knappen Saetzen zusammen — was steht drin, was wird verlangt. "
            "Ton: trocken, knapp, Butler-Stil. KEINE Begruessung, KEINE direkte Anrede, "
            "KEINE eckigen Klammern. NUR die Zusammenfassung."
        )
        user_msg = f"Von: {sender}\nBetreff: {subject}\n\n{body}"
        try:
            resp = await S.ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=sys_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            summary = llm_text(resp).strip()
        except Exception as e:
            log.warning(f"SUMMARIZE_MAIL Claude error: {type(e).__name__}: {e}")
            return f"Zusammenfassung fehlgeschlagen: {type(e).__name__}"
        if not summary:
            summary = "Zur Mail liegt keine Zusammenfassung vor."
        return (
            f"Mail von {sender}, Betreff: {subject}.\n\n"
            f"Zusammenfassung: {summary}\n\n"
            f"Soll ich die beantworten?"
        )

    elif t == "DELETE_MAIL":
        active = session_state.get("default").active_mail
        if not active:
            return f"Keine aktive Mail zum Löschen, {pick_address()}."
        ok, folder = await mail_actions.delete_mail(active.account, active.uid)
        session_state.clear_active_mail("default")
        if ok:
            return f"Mail gelöscht (in {folder} verschoben)."
        return f"Löschen fehlgeschlagen: {folder}"

    elif t == "REMEMBER_SENDER":
        # Fügt den Absender der aktiven Mail als mark_read-Regel in
        # mail_triage_rules.json ein. Payload: optional "email@domain.tld"
        # override; default: active_mail.sender.
        import json as _json
        import re as _re
        active = session_state.get("default").active_mail
        raw_sender = p.strip() if p.strip() else (active.sender if active else "")
        if not raw_sender:
            return f"Keinen Absender gefunden, {pick_address()}."
        # Extract email address from "Name <email>" format
        m = _re.search(r"<([^>]+)>", raw_sender)
        email_addr = m.group(1).strip() if m else raw_sender.strip()
        # Use domain for broader matching, or full address if it's generic
        domain = email_addr.split("@")[-1] if "@" in email_addr else email_addr
        rule_match = f"@{domain}" if domain else email_addr
        rules_path = os.path.join(os.path.dirname(__file__), "mail_triage_rules.json")
        try:
            with open(rules_path, encoding="utf-8") as f:
                rules_data = _json.load(f)
        except Exception:
            rules_data = {"rules": []}
        # Avoid duplicates
        existing = [r.get("from_contains", "") for r in rules_data.get("rules", [])]
        if rule_match in existing:
            return f"Absender {rule_match!r} ist bereits in den Regeln."
        rules_data.setdefault("rules", []).append({
            "name": f"auto: {domain}",
            "from_contains": rule_match,
            "action": "mark_read",
        })
        tmp = rules_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(rules_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, rules_path)
        if active:
            session_state.clear_active_mail("default")
        return f"Gemerkt — zukünftige Mails von {rule_match} werden still als gelesen markiert."

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
        # RFC 2822: Reply-To hat Vorrang vor From fuer Antwort-Adresse
        # (Issue #74). Fallback-Kette: reply_to -> sender (aus active) ->
        # sender-Feld aus dem geladenen Mail-Body.
        to_addr = (
            mail_data.get("reply_to", "").strip()
            or active.sender
            or mail_data.get("sender", "")
        )
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
            f"Mein Vorschlag (Antwort an: {to_addr}):\n\n{draft_body}\n\n"
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

    elif t == "PLAN_NOW":
        import planner
        return await planner.plan_now()

    elif t == "IMPORT_MAIL_HISTORY":
        import email as _email
        import email.utils as _email_utils
        import re as _re
        import uuid as _uuid
        import aioimaplib
        import memory_search
        import persons_db as _pdb
        from email.utils import parseaddr
        from persons_db import PersonProfile
        from mail_monitor import (
            _classify, _baseline_uid, _uids_in_range, _decode_header,
        )

        account_name = "HILO"
        months = 3
        if p:
            m_acc = _re.search(r"account[=\s]+(\S+)", p, _re.I)
            m_mon = _re.search(r"months?[=\s]+(\d+)", p, _re.I)
            if m_acc:
                account_name = m_acc.group(1)
            elif _re.match(r"[A-Za-z]", p.strip()):
                account_name = p.strip().split()[0]
            if m_mon:
                months = int(m_mon.group(1))
            else:
                digits = _re.search(r"\d+", p)
                if digits:
                    months = int(digits.group())

        acc = next(
            (a for a in S.MAIL_MONITOR_ACCOUNTS
             if a.get("name", "").lower() == account_name.lower()),
            None,
        )
        if not acc:
            return (
                f"Konto '{account_name}' nicht konfiguriert. "
                f"Verfügbar: {', '.join(a.get('name','?') for a in S.MAIL_MONITOR_ACCOUNTS) or 'keins'}."
            )
        if not acc.get("password"):
            return f"Kein Passwort fuer Konto '{account_name}' in der .env hinterlegt."

        cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=months * 30)
        folder = acc.get("folder", "INBOX")

        cls = aioimaplib.IMAP4_SSL if acc.get("ssl", True) else aioimaplib.IMAP4
        client = cls(host=acc["host"], port=acc["port"], timeout=30)
        total = handlungsbedarf_count = contacts_saved = 0
        try:
            await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
            login_resp = await asyncio.wait_for(
                client.login(acc["user"], acc["password"]), timeout=30
            )
            if login_resp[0] != "OK":
                return f"Login fehlgeschlagen fuer {acc['user']!r}."
            sel = await client.select(folder)
            if sel[0] != "OK":
                return f"Ordner '{folder}' konnte nicht geöffnet werden."

            # Apple iCloud rejects UID SEARCH — use STATUS UIDNEXT + UID FETCH range
            # (same approach as mail_monitor._baseline_uid / _uids_in_range).
            max_uid = await _baseline_uid(client, folder)
            if max_uid <= 0:
                return f"Keine Mails in '{account_name}'."
            low_uid = max(0, max_uid - 5000)  # scan at most 5000 recent UIDs
            all_uids = await _uids_in_range(client, low_uid, max_uid)
            if not all_uids:
                return f"Keine Mails in den letzten {months} Monaten in '{account_name}'."

            # Pass 1: fetch headers sequentially (one UID at a time — Apple-safe,
            # same as mail_monitor._process_new_uids). Apply date filter here.
            parsed: list[tuple[str, str, str]] = []  # (sender_raw, sender_email, subject)
            _sem = asyncio.Semaphore(10)

            for uid in all_uids:
                typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[HEADER]")
                if typ != "OK" or not data:
                    continue
                byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
                if not byte_items:
                    continue
                raw = max(byte_items, key=len)
                msg = _email.message_from_bytes(raw)
                date_str = msg.get("Date", "")
                if date_str:
                    try:
                        mail_dt = _email_utils.parsedate_to_datetime(date_str)
                        if mail_dt.tzinfo is None:
                            mail_dt = mail_dt.replace(tzinfo=datetime.timezone.utc)
                        if mail_dt < cutoff:
                            continue
                    except Exception:
                        pass
                from_parsed = parseaddr(msg.get("From", ""))
                sender_raw = msg.get("From", "")
                sender_email = (from_parsed[1] or "").lower().strip()
                subject = _decode_header(msg.get("Subject", ""))
                if not sender_raw and not subject:
                    continue
                total += 1
                parsed.append((sender_raw, sender_email, subject))

            # Pass 2: classify in parallel (rate-limited via semaphore).
            async def _classify_sem(s_raw: str, subj: str) -> str:
                async with _sem:
                    return await _classify(s_raw, subj, "")

            categories = await asyncio.gather(
                *[_classify_sem(sr, sj) for sr, _, sj in parsed],
                return_exceptions=True,
            )

            today = datetime.date.today().isoformat()
            for (sender_raw, sender_email, subject), category in zip(parsed, categories):
                if not isinstance(category, str) or category != "handlungsbedarf":
                    continue
                handlungsbedarf_count += 1
                if sender_email:
                    if not _pdb.find_by_email(sender_email):
                        display = parseaddr(sender_raw)[0] or sender_email
                        _pdb.upsert(PersonProfile(
                            contact_id=str(_uuid.uuid4()),
                            name=display,
                            primary_email=sender_email,
                            last_contact=today,
                        ))
                        contacts_saved += 1
                try:
                    doc_id = memory_search.make_doc_id(
                        "mail", f"{account_name}:{sender_email}:{subject}"
                    )
                    memory_search.index_text(
                        text=f"Mail von {sender_raw}: {subject}",
                        source="mail",
                        doc_id=doc_id,
                        metadata={
                            "sender_email": sender_email,
                            "account": account_name,
                            "category": "handlungsbedarf",
                        },
                    )
                except Exception as e:
                    log.debug("IMPORT_MAIL_HISTORY: index_text failed: %s", e)

        except Exception as e:
            log.warning("IMPORT_MAIL_HISTORY error: %s", e)
            return f"Fehler beim Postfach-Import: {e}"
        finally:
            try:
                await client.logout()
            except Exception:
                pass

        return (
            f"{total} Mails analysiert, {handlungsbedarf_count} Handlungsbedarf, "
            f"{contacts_saved} neue Kontakte gespeichert."
        )

    elif t == "SYNC_CONTACTS":
        from contacts_carddav import sync_icloud_contacts
        import persons_db
        from persons_db import PersonProfile, upsert, get
        contacts = await sync_icloud_contacts()
        if not contacts:
            return "Keine Kontakte geladen — bitte ICLOUD_APPLE_ID und ICLOUD_APP_PASSWORD in der .env prüfen."
        for c in contacts:
            existing = persons_db.get(c.id)
            if existing:
                if c.emails and not existing.primary_email:
                    existing.primary_email = c.emails[0]
                    existing.secondary_emails = c.emails[1:]
                if c.phones and not existing.primary_phone:
                    existing.primary_phone = c.phones[0]
                    existing.secondary_phones = c.phones[1:]
                if c.organization and not existing.funktion:
                    existing.funktion = c.organization
                persons_db.upsert(existing)
            else:
                persons_db.upsert(PersonProfile(
                    contact_id=c.id,
                    name=c.name,
                    primary_email=c.emails[0] if c.emails else "",
                    secondary_emails=c.emails[1:],
                    primary_phone=c.phones[0] if c.phones else "",
                    secondary_phones=c.phones[1:],
                    funktion=c.organization,
                ))
        return f"{len(contacts)} Kontakte aus iCloud geladen."

    elif t == "WEEKLY_OUTLOOK":
        # On-demand-Wochenausblick (gleicher Inhalt wie der
        # Sonntag-18:00-Trigger). Nutzt den scheduler-Helper damit
        # die Logik konsistent bleibt.
        from scheduler import build_weekly_outlook
        text = await build_weekly_outlook()
        if not text:
            return f"Aktuell habe ich nichts Konkretes fuer die naechste Woche, {pick_address()}."
        return text

    elif t == "MEMORIZE":
        # "Merk dir: ..." — speichert eine Notiz. Detect:
        # - kind: vorliebe / abneigung / notiz
        # - person-bezogen ("zu Mueller", "fuer Schmidt", "von Schulz")
        # - Aufgaben-Charakter (Imperativ + Zeit) -> Vorschlag "als Aufgabe?"
        import notes_db
        import persons_db
        text = p.strip()
        if not text:
            return f"Was soll ich mir merken, {pick_address()}?"
        # Person-Reference detection
        import re as _re
        person_id = ""
        person_name = ""
        m = _re.search(r"\b(?:zu|fuer|für|von|mit|bei)\s+([A-ZÄÖÜ][\wÄÖÜäöüß-]+(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+)?)", text)
        if m:
            candidate = m.group(1).strip()
            for prof in persons_db.all_profiles():
                if candidate.lower() in prof.name.lower():
                    person_id = prof.contact_id
                    person_name = prof.name
                    break
        # Kind detection
        lower = text.lower()
        kind = "notiz"
        if any(p_ in lower for p_ in ("ich mag", "ich bevorzuge", "ich liebe",
                                      "ich trinke gerne", "ich esse gerne")):
            kind = "vorliebe"
        elif any(p_ in lower for p_ in ("ich hasse", "ich mag nicht", "ich kann nicht",
                                        "ich vertrage nicht", "ich brauche nicht")):
            kind = "abneigung"
        # Aufgaben-Charakter heuristisch erkennen
        looks_like_task = bool(_re.search(
            r"\b(morgen|heute|naechste\s+woche|am\s+\w+tag|um\s+\d|bis\s+\w+tag|"
            r"anrufen|schreiben|abgeben|pruefen|beantworten|erinnern|kuendigen|"
            r"reservieren|bestellen|ueberweisen)\b",
            lower,
        ))
        def _near_dup(new: str, existing: list[str]) -> bool:
            """True when new text is semantically redundant with an existing entry.
            Uses token containment: if >= 75% of the shorter text's tokens
            appear in the longer text, treat as duplicate."""
            a = set(_re.sub(r'[^\w]', ' ', new.lower()).split())
            if len(a) < 2:
                return False
            for ex in existing:
                b = set(_re.sub(r'[^\w]', ' ', ex.lower()).split())
                if not b:
                    continue
                overlap = len(a & b) / min(len(a), len(b))
                if overlap >= 0.75:
                    return True
            return False

        # Spezialfall: explizite Anrede-Pflege via "Anrede fuer X: ..."
        # Diese setzt PersonProfile.anrede statt nur add_note.
        if person_id and "anrede" in lower:
            idx = lower.find("anrede")
            after = _re.sub(r'^[:\s,]*(f[uü]r\s+)?', '', text[idx + len("anrede"):], flags=_re.I)
            # strip person-name aus dem Praefix wenn drin
            if person_name and person_name.lower() in after.lower()[:len(person_name) + 5]:
                after = after[after.lower().find(person_name.lower()) + len(person_name):].lstrip(":, ")
            if after.strip():
                prof = persons_db.get(person_id)
                if prof:
                    prof.anrede = after.strip()
                    persons_db.upsert(prof)
                    return f"Anrede fuer {person_name} gespeichert: {after.strip()}"
        # Speichern — nur wenn kein Duplikat vorhanden
        if person_id:
            prof = persons_db.get(person_id)
            if prof and _near_dup(text, prof.notes):
                return f"Das habe ich bereits bei {person_name} notiert, {pick_address()}."
            persons_db.add_note(person_id, text)
            stored_where = f"bei {person_name}"
        else:
            existing_texts = [n.text for n in notes_db.all_notes()]
            if _near_dup(text, existing_texts):
                return f"Das ist mir bereits bekannt, {pick_address()}."
            notes_db.add(text, kind=kind)
            stored_where = (
                "in den Vorlieben" if kind == "vorliebe"
                else "in den Abneigungen" if kind == "abneigung"
                else "in den Notizen"
            )
        # Antwort + ggf. Aufgaben-Vorschlag
        if looks_like_task:
            return (
                f"Notiert {stored_where}. Das klingt nach einer Aufgabe — "
                f"soll ich das auch in Todoist anlegen?"
            )
        return f"Notiert {stored_where}."

    elif t == "RECALL":
        # Issue #56 + #57: Zweistufige Suche — erst Volltext (schnell),
        # dann semantisch via ChromaDB/sentence-transformers (Issue #57).
        import notes_db
        import persons_db
        import conversation
        import memory_search
        query = p.strip()
        if not query:
            return f"Wonach soll ich suchen, {pick_address()}?"
        q = query.lower()
        results: list[str] = []
        seen_texts: set[str] = set()  # Deduplizierung zwischen Volltext + Semantik

        def _add_result(text: str) -> None:
            key = text.strip()[:100]
            if key not in seen_texts:
                seen_texts.add(key)
                results.append(text)

        # 1. Personen-bezogene Notizen + offene Punkte (Volltext)
        for prof in persons_db.all_profiles():
            if q in prof.name.lower():
                for note in prof.notes[-5:]:
                    _add_result(f"Notiz zu {prof.name}: {note}")
                for pt in prof.open_points:
                    _add_result(f"Offen mit {prof.name}: {pt}")
            else:
                for note in prof.notes:
                    if q in note.lower():
                        _add_result(f"Notiz zu {prof.name}: {note}")
                for pt in prof.open_points:
                    if q in pt.lower():
                        _add_result(f"Offen mit {prof.name}: {pt}")
        # 2. Allgemeine Notizen (Volltext)
        for n in notes_db.find(query):
            _add_result(f"{n.kind.capitalize()}: {n.text}")
        # 3. Todoist offene Tasks (Volltext)
        if S.TODOIST_TOKEN and S.TODOIST_TOKEN != "YOUR_TODOIST_API_TOKEN":
            try:
                tasks_text = await todoist_tools.get_tasks(
                    S.TODOIST_TOKEN, max_tasks=50,
                    project_ids=S.TODOIST_PROJECT_IDS or None,
                    section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
                )
                if tasks_text and tasks_text != "KEINE_TASKS":
                    for line in tasks_text.splitlines():
                        if line.startswith("•") and q in line.lower():
                            _add_result(f"Todoist: {line.lstrip('• ').strip()}")
            except Exception as e:
                log.warning(f"RECALL todoist failed: {type(e).__name__}: {e}")
        # 4. Conversation-History (Volltext, letzte 50 Turns)
        history = conversation.load_persistent_history()
        for msg in history:
            content = (msg.get("content") or "")
            if q in content.lower():
                role = "Du" if msg.get("role") == "user" else "Jarvis"
                snippet = content.strip()[:120]
                _add_result(f"Frueher ({role}): {snippet}")
        # 5. Semantische Suche via ChromaDB (Issue #57)
        # Ergänzt Volltext-Treffer um bedeutungsähnliche Einträge.
        try:
            semantic_hits = memory_search.search(query, n_results=5)
            for hit in semantic_hits:
                # Nur bei hinreichender Ähnlichkeit (score >= 0.7) und
                # nur wenn nicht schon durch Volltext gefunden.
                if hit.get("score", 0) >= 0.7:
                    text = hit.get("text", "").strip()
                    if text:
                        _add_result(f"[Erinnerung] {text[:200]}")
        except Exception as e:
            log.warning(f"RECALL semantic search failed: {type(e).__name__}: {e}")

        if not results:
            return f"Ich finde nichts zu {query}, {pick_address()}."
        return f"Zu {query} habe ich:\n" + "\n".join(f"- {r}" for r in results[:15])

    elif t == "LOOKUP_CONTACT":
        # "Was ist die Telefonnummer von X?" / "Wer ist X?".
        # Sucht in persons_db + Apple Kontakte (Substring auf Name).
        import contacts
        import persons_db
        query = p.strip()
        if not query:
            return f"Wen suchst Du, {pick_address()}?"
        try:
            apple_hits = await contacts.find_contacts_by_name(query)
        except Exception as e:
            log.warning(f"LOOKUP_CONTACT contacts failed: {type(e).__name__}: {e}")
            apple_hits = []
        # Merge mit persons_db (Profile haben evtl. Anrede/Funktion + extra Phones)
        results: list[dict] = []
        seen_ids: set[str] = set()
        for prof in persons_db.all_profiles():
            if query.lower() not in prof.name.lower():
                continue
            seen_ids.add(prof.contact_id)
            results.append({
                "name": prof.name,
                "emails": [prof.primary_email] + prof.secondary_emails if prof.primary_email else prof.secondary_emails,
                "phones": [prof.primary_phone] + prof.secondary_phones if prof.primary_phone else prof.secondary_phones,
                "anrede": prof.anrede,
                "funktion": prof.funktion,
            })
        for c in apple_hits:
            if c.id in seen_ids:
                continue
            results.append({
                "name": c.name,
                "emails": list(c.emails),
                "phones": list(c.phones),
                "anrede": "",
                "funktion": c.organization,
            })
        if not results:
            return f"Ich finde niemanden mit dem Namen {query} in deinen Kontakten."
        if len(results) > 1:
            names = "\n".join(f"- {r['name']}" for r in results[:8])
            return (
                f"Mehrere Treffer fuer {query}:\n{names}\n"
                f"Sag praeziser welchen Du meinst."
            )
        # Genau ein Treffer
        r = results[0]
        bits: list[str] = [r["name"]]
        if r["funktion"]:
            bits.append(f"({r['funktion']})")
        out_parts = [" ".join(bits) + "."]
        if r["emails"]:
            email_list = [e for e in r["emails"] if e]
            if email_list:
                if len(email_list) == 1:
                    out_parts.append(f"Mail: {email_list[0]}.")
                else:
                    out_parts.append("Mails: " + ", ".join(email_list) + ".")
        if r["phones"]:
            phone_list = [_format_phone_tts(pp) for pp in r["phones"] if pp]
            if phone_list:
                if len(phone_list) == 1:
                    out_parts.append(f"Telefon: {phone_list[0]}.")
                else:
                    out_parts.append("Telefon: " + ", ".join(phone_list) + ".")
        if r["anrede"]:
            out_parts.append(f"Bevorzugte Anrede: {r['anrede']}.")
        return " ".join(out_parts)

    elif t == "CONTACTS_INFO":
        # Aggregierte Statistik ueber Apple Kontakte + persons_db.
        # Nutze wenn {addr} fragt "Wie viele Kontakte habe ich?",
        # "Kontakte-Statistik", "Wie viele Mandanten habe ich gepflegt?".
        import contacts
        import persons_db
        try:
            apple = await contacts.read_all_contacts()
        except Exception as e:
            log.warning(f"CONTACTS_INFO contacts.read_all_contacts failed: "
                        f"{type(e).__name__}: {e}")
            return (
                f"Ich kann gerade nicht auf die Apple-Kontakte zugreifen, "
                f"{pick_address()}. Stelle sicher dass die Berechtigung "
                f"in Systemeinstellungen → Datenschutz → Kontakte "
                f"fuer Terminal/Python aktiviert ist."
            )
        if not apple:
            return (
                f"Apple Kontakte liefert keine Eintraege, {pick_address()}. "
                f"Vermutlich fehlt die Berechtigung — pruefe Systemeinstellungen "
                f"→ Datenschutz → Kontakte."
            )
        total = len(apple)
        with_mail = sum(1 for c in apple if c.emails)
        with_phone = sum(1 for c in apple if c.phones)
        in_db = len(persons_db.all_profiles())
        lines = [
            f"Insgesamt {total} Kontakte in Apple Kontakte.",
            f"Davon {with_mail} mit Mailadresse, {with_phone} mit Telefonnummer.",
        ]
        if in_db:
            lines.append(f"In der Personen-DB hast Du {in_db} Profile zusaetzlich gepflegt.")
        return " ".join(lines)

    elif t == "CALL":
        # "rufe X an" — Lookup, eine Nummer -> direkt waehlen, mehrere
        # Nummern -> Liste mit Indizes zurueckgeben, Catrin sagt "die
        # erste" / "Mobil" -> CALL_DIAL.
        import phone
        query = p.strip()
        if not query:
            return f"Wen soll ich anrufen, {pick_address()}?"
        results = await phone.find_callable(query)
        if not results:
            return f"Ich finde niemanden mit dem Namen {query} in deinen Kontakten."
        if len(results) == 1:
            name, label, number = results[0]
            ok = await phone.start_call(number)
            session_state.clear_pending_person("default")  # falls noch was offen
            return (f"Rufe {name} an: {number}." if ok
                    else f"Konnte den Anruf nicht starten — die Nummer {number} ist im Speicher.")
        # Mehrere Nummern -> Auswahl
        # Stash in session_state.pending_person als "call_choices"-Hack ist haesslich;
        # sauberer: PendingCall in session_state. Aber pragmatisch: in active_mail-Slot
        # missbrauchen waere falsch. Ich nutze pending_person mit kind="call_choice".
        # Dazu speichere ich die Liste als JSON-string in extra_phones (uebergangsweise).
        import json as _json
        choices_json = _json.dumps(results)
        session_state.set_pending_person(
            "default",
            session_state.PendingPersonAction(
                kind="call_choice",
                name=query,
                extra_phones=[choices_json],
            ),
        )
        lines = [f"{i + 1}. {name} ({label}): {number}"
                 for i, (name, label, number) in enumerate(results)]
        return (
            f"Mehrere Nummern fuer {query}:\n"
            + "\n".join(lines)
            + "\nWelche soll ich waehlen?"
        )

    elif t == "CALL_DIAL":
        # Catrin hat aus der Auswahl-Liste eine Nummer gewaehlt.
        # Payload kann sein: "1" / "2" / "die erste" / "Mobil" / die Nummer selbst
        import phone
        state = session_state.get("default")
        pending = state.pending_person
        if not pending or pending.kind != "call_choice" or not pending.extra_phones:
            return f"Es liegt keine Telefonnummern-Auswahl vor, {pick_address()}."
        import json as _json
        try:
            choices = _json.loads(pending.extra_phones[0])
        except Exception:
            session_state.clear_pending_person("default")
            return "Die Auswahl-Liste ist beschaedigt — sag bitte nochmal 'rufe X an'."
        chosen = None
        sel = p.strip().lower()
        # Index?
        try:
            idx = int(sel.split()[0]) - 1
            if 0 <= idx < len(choices):
                chosen = choices[idx]
        except (ValueError, IndexError):
            pass
        if chosen is None:
            # Label-Match (z.B. "primary" / "Mobil") oder Direktwahl
            for c in choices:
                _name, label, number = c
                if (sel in label.lower()
                        or sel in number
                        or sel in {"erste", "1.", "ersten"} and choices.index(c) == 0
                        or sel in {"zweite", "2.", "zweiten"} and choices.index(c) == 1):
                    chosen = c
                    break
        if chosen is None:
            return f"Konnte aus '{p}' keine Nummer ableiten — sag '1', '2' oder den Label-Namen."
        name, label, number = chosen
        session_state.clear_pending_person("default")
        ok = await phone.start_call(number)
        return (f"Rufe {name} an: {number}." if ok
                else f"Konnte den Anruf nicht starten — die Nummer {number}.")

    elif t == "ACCEPT_PERSON_ACTION":
        # Bestaetigt den vorgeschlagenen Personen-Update aus
        # contact_sync. Drei Faelle: new_person / email_drift / phone_drift.
        import contacts
        import contact_sync  # noqa: F401  (touch import for traceability)
        import persons_db
        state = session_state.get("default")
        pending = state.pending_person
        if not pending:
            return f"Es liegt kein Personen-Vorschlag vor, {pick_address()}."

        if pending.kind == "new_person":
            # Apple Kontakt anlegen — mit organization wenn von Claude
            # geraten. Anrede + Funktion gehen in die persons_db
            # (Apple Contacts hat die Felder nicht 1:1).
            phones = pending.extra_phones or ([pending.new_phone] if pending.new_phone else [])
            new_id = await contacts.create_contact(
                name=pending.name,
                emails=[pending.new_email] if pending.new_email else None,
                phones=phones,
                organization=pending.organization,
            )
            cid = new_id or persons_db.new_id()
            persons_db.upsert(persons_db.PersonProfile(
                contact_id=cid,
                name=pending.name,
                anrede=pending.anrede,
                funktion=pending.funktion,
                primary_email=pending.new_email,
                secondary_phones=phones[1:] if len(phones) > 1 else [],
                primary_phone=phones[0] if phones else "",
            ))
            session_state.clear_pending_person("default")
            extras = []
            if pending.funktion:
                extras.append(f"Funktion: {pending.funktion}")
            if pending.anrede:
                extras.append(f"Anrede: {pending.anrede}")
            extra_str = " (" + ", ".join(extras) + ")" if extras else ""
            return f"{pending.name} ist angelegt{extra_str}."

        if pending.kind == "email_drift":
            # Email an Kontakt anhaengen + persons_db updaten
            import telegram_bot
            old_emails = getattr(pending, "old_emails", [])
            old_repr = old_emails[0] if old_emails else "(unbekannt)"
            await contacts.add_email_to_contact(pending.contact_id, pending.new_email)
            existing = persons_db.get(pending.contact_id)
            if existing:
                persons_db.promote_email_to_primary(pending.contact_id, pending.new_email)
            else:
                persons_db.upsert(persons_db.PersonProfile(
                    contact_id=pending.contact_id,
                    name=pending.name,
                    primary_email=pending.new_email,
                ))
            session_state.clear_pending_person("default")
            # Telegram-Bestaetigung (Issue #115)
            tg_msg = (
                f"✅ Kontakt aktualisiert: {pending.name}\n"
                f"Alt: {old_repr} → Neu: {pending.new_email}"
            )
            await telegram_bot.send_user_text(tg_msg)
            return f"Adresse aktualisiert. {pending.new_email} ist die neue primaere Mail von {pending.name}."

        if pending.kind == "phone_drift":
            import telegram_bot
            await contacts.add_phone_to_contact(pending.contact_id, pending.new_phone)
            existing = persons_db.get(pending.contact_id)
            if existing:
                persons_db.add_secondary_phone(pending.contact_id, pending.new_phone)
            else:
                persons_db.upsert(persons_db.PersonProfile(
                    contact_id=pending.contact_id,
                    name=pending.name,
                    primary_phone=pending.new_phone,
                ))
            session_state.clear_pending_person("default")
            # Telegram-Bestaetigung (Issue #115)
            tg_msg = (
                f"✅ Kontakt aktualisiert: {pending.name}\n"
                f"Neue Telefonnummer: {pending.new_phone}"
            )
            await telegram_bot.send_user_text(tg_msg)
            return f"Nummer {pending.new_phone} bei {pending.name} eingetragen."

        session_state.clear_pending_person("default")
        return "Unbekannter Personen-Vorschlag — verworfen."

    elif t == "DECLINE_PERSON_ACTION":
        session_state.clear_pending_person("default")
        return f"Verworfen, {pick_address()}."

    elif t == "ACCEPT_CALENDAR_INVITE":
        # Vorgeschlagenen Kalender-Eintrag anlegen + Mail markieren.
        state = session_state.get("default")
        cal = state.pending_calendar
        active = state.active_mail
        if not cal:
            return f"Es liegt keine Termin-Einladung zur Annahme vor, {pick_address()}."
        title = cal.summary or "Termin"
        when = cal.when_human or cal.dtstart
        if not when:
            return "Termin hat keine erkennbare Zeit — bitte manuell anlegen."
        try:
            result = await google_calendar_tools.add_event(title, when)
        except Exception as e:
            log.warning(f"ACCEPT_CALENDAR_INVITE failed: {type(e).__name__}: {e}")
            return f"Termin konnte nicht angelegt werden: {type(e).__name__}"
        # Mail markieren + State leeren
        if active:
            await mail_actions.mark_mail_read(active.account, active.uid)
            session_state.clear_active_mail("default")
        session_state.clear_pending_calendar("default")
        return f"Termin '{title}' am {when} angelegt. Mail abgehakt."

    elif t == "DECLINE_CALENDAR_INVITE":
        state = session_state.get("default")
        active = state.active_mail
        session_state.clear_pending_calendar("default")
        if active:
            await mail_actions.mark_mail_read(active.account, active.uid)
            session_state.clear_active_mail("default")
        return "Einladung abgelehnt, Mail markiert."

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
        # Bonus: wenn Sender in persons_db, gib Claude die Funktion mit
        # damit der Task praeziser benannt werden kann
        # ("Rueckruf Steuerberater Mueller" statt nur "Rueckruf Mueller")
        try:
            import persons_db
            from email.utils import parseaddr
            addr = parseaddr(mail_data.get("sender", ""))[1].lower()
            profile = persons_db.find_by_email(addr) if addr else None
        except Exception:
            profile = None
        sender_block = (
            f"Absender: {mail_data['sender']}"
            + (f" — {profile.funktion}" if profile and profile.funktion else "")
        )
        user_msg = (
            f"{sender_block}\n"
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
            task_text = llm_text(resp).strip().strip('"\'').strip()
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
