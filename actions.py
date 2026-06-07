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
import html as _html_module
import os
import subprocess
import sys

import settings as S

# Tool modules — each one is mostly stateless, just function calls.
import browser_tools
import google_calendar_tools
import imap_mail_tools
import jarvis_quotes
import mail_actions
import mail_tools
import notes_tools
from prompt import _sanitize, llm_text, pick_address
import screen_capture
import session_state
import todoist_tools
import weather_tools

log = S.log

# Issue #131: Lock fuer atomaren Read-Modify-Write auf TASKS_COMPLETED_TODAY.
# Schuetzt den Date-Check + Reset + Inkrement gegen gleichzeitige DONETASK-
# Requests (z.B. WebSocket und Telegram parallel).
_tasks_completed_lock = asyncio.Lock()


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
            f"Original-Mail von: {_sanitize(mail_data.get('sender', ''))}\n"
            f"Betreff: {_sanitize(mail_data.get('subject', ''))}\n"
            f"Inhalt:\n{_sanitize((mail_data.get('text', '') or '')[:1500])}\n\n"
            f"---\n"
            f"Konkrete Anweisung von {S.USER_NAME} fuer die Antwort: {instruction}"
        )
    else:
        user_msg = (
            f"Original-Mail von: {_sanitize(mail_data.get('sender', ''))}\n"
            f"Betreff: {_sanitize(mail_data.get('subject', ''))}\n"
            f"Inhalt:\n{_sanitize((mail_data.get('text', '') or '')[:1500])}\n\n"
            f"---\n"
            f"Schlage proaktiv eine sinnvolle Antwort vor — nutze dazu den "
            f"GESCHAEFTLICHEN KONTEXT oben falls die Mail einen darin "
            f"beschriebenen Sachverhalt betrifft. Wenn Du KEINEN sinnvollen "
            f"Vorschlag liefern kannst, antworte mit NEED_INPUT statt eine "
            f"Antwort zu erfinden."
        )
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
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
            model=S.HAIKU_MODEL,
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


def _fmt_date(s: str) -> str:
    """Konvertiert YYYY-MM-DD → DD.MM.YYYY; andere Formate unverändert."""
    if s and len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return f"{s[8:10]}.{s[5:7]}.{s[0:4]}"
        except Exception:
            pass
    return s


def _build_person_card_html(
    name: str,
    mnr: str = "",
    steuernr: str = "",
    idnr: str = "",
    funktion: str = "",
    anrede: str = "",
    last_contact: str = "",
    open_points: list | None = None,
    notes: list | None = None,
    tax_assessments: list | None = None,
    advance_payments: list | None = None,
    tasks: list | None = None,
    completed_tasks: list | None = None,
) -> str:
    """HTML-Kachel fuer die JARVIS-Web-UI (LOOKUP_CONTACT)."""
    def esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = [f'<div class="p-card-name">{esc(name)}</div>']

    badges = []
    if mnr:
        badges.append(f'<span class="p-badge">Nr.&nbsp;{esc(mnr)}</span>')
    if steuernr:
        badges.append(f'<span class="p-badge">StNr&nbsp;{esc(steuernr)}</span>')
    if idnr:
        badges.append(f'<span class="p-badge">IdNr&nbsp;{esc(idnr)}</span>')
    if badges:
        parts.append('<div class="p-card-meta">' + "".join(badges) + "</div>")

    rows = []
    if funktion:
        rows.append(f'<div class="p-row"><span class="p-lbl">Funktion</span><span>{esc(funktion)}</span></div>')
    if anrede:
        rows.append(f'<div class="p-row"><span class="p-lbl">Anrede</span><span>{esc(anrede)}</span></div>')
    if last_contact:
        rows.append(f'<div class="p-row"><span class="p-lbl">Letzter Kontakt</span><span>{esc(_fmt_date(last_contact))}</span></div>')
    if rows:
        parts.append('<div class="p-card-rows">' + "".join(rows) + "</div>")

    if open_points:
        bullets = "".join(f'<div class="p-bullet">{esc(pt)}</div>' for pt in open_points)
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Offene Punkte</div>{bullets}</div>')

    if notes:
        bullets = "".join(f'<div class="p-bullet">{esc(n)}</div>' for n in notes[:3])
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Notizen</div>{bullets}</div>')

    def _betrag_str(ta: dict) -> str:
        betrag = ta.get("betrag_eur")
        if betrag is None:
            return ""
        try:
            b = float(betrag)
            richtung = "Erstattung" if b >= 0 else "Nachzahlung"
            s = f"{abs(b):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
            return f" · {richtung} {s}"
        except (ValueError, TypeError):
            return f" · {betrag} €"

    if tax_assessments:
        items = []
        for ta in sorted(tax_assessments, key=lambda x: str(x.get("steuerjahr", "")), reverse=True):
            steuerart = esc(ta.get("steuerart", "?"))
            jahr = esc(str(ta.get("steuerjahr", "?")))
            datum = _fmt_date(ta.get("ausstellungsdatum") or "")
            datum_str = esc(f" vom {datum}") if datum else ""
            b_str = esc(_betrag_str(ta))
            faellig = _fmt_date(ta.get("zahlungstermin") or "")
            f_str = esc(f", fällig {faellig}") if faellig and faellig != "null" else ""
            items.append(f'<div class="p-bullet"><b>Letzter {steuerart}-Bescheid:</b> {steuerart} {jahr}{datum_str}{b_str}{f_str}</div>')
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Steuerbescheide</div>{"".join(items)}</div>')

    if advance_payments:
        items = []
        for ap in sorted(advance_payments, key=lambda x: str(x.get("vorauszahlungsjahr", "")), reverse=True):
            steuerart = esc(ap.get("steuerart", "?"))
            jahr = esc(str(ap.get("vorauszahlungsjahr", "?")))
            datum = _fmt_date(ap.get("ausstellungsdatum") or "")
            datum_str = esc(f" vom {datum}") if datum else ""
            q_parts = []
            for q, label in [("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4")]:
                val = ap.get(q)
                if val is not None:
                    try:
                        s = f"{abs(float(val)):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
                        q_parts.append(f"{label}: {s}")
                    except (ValueError, TypeError):
                        q_parts.append(f"{label}: {val} €")
            q_str = esc("  " + "  ".join(q_parts)) if q_parts else ""
            items.append(f'<div class="p-bullet"><b>Vorauszahlungsbescheid {steuerart} {jahr}</b>{datum_str}{q_str}</div>')
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Vorauszahlungen</div>{"".join(items)}</div>')

    if tasks:
        from datetime import date as _date
        _today = _date.today().isoformat()
        items = []
        for task in tasks:
            content = esc(task.get("content", "(ohne Titel)"))
            due_date = (task.get("due") or {}).get("date", "")
            if due_date and due_date < _today:
                flag = ' <span style="color:#e05252;font-weight:bold">⚠ überfällig</span>'
            elif due_date == _today:
                flag = ' <span style="color:#e08a00;font-weight:bold">(heute)</span>'
            elif due_date:
                flag = esc(f" (fällig: {_fmt_date(due_date)})")
            else:
                flag = ""
            items.append(f'<div class="p-bullet">{content}{flag}</div>')
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Offene Aufgaben</div>{"".join(items)}</div>')

    if completed_tasks:
        items = []
        for task in completed_tasks:
            content = esc(task.get("content", "(ohne Titel)"))
            completed_at = task.get("completed_at", "")
            date_str = esc(f" (erledigt: {_fmt_date(completed_at[:10])})" if completed_at else "")
            items.append(f'<div class="p-bullet" style="color:#888">✓ {content}{date_str}</div>')
        parts.append(f'<div class="p-card-section"><div class="p-section-title">Erledigte Aufgaben</div>{"".join(items)}</div>')

    return '<div class="p-card">' + "".join(parts) + "</div>"


def _build_person_card_telegram(
    name: str,
    mnr: str = "",
    steuernr: str = "",
    idnr: str = "",
    funktion: str = "",
    anrede: str = "",
    last_contact: str = "",
    open_points: list | None = None,
    notes: list | None = None,
    tax_assessments: list | None = None,
    advance_payments: list | None = None,
    tasks: list | None = None,
    completed_tasks: list | None = None,
    html: bool = False,
) -> str:
    """Formatierter Telegram-Text.

    Wenn ``html=True``, wird Telegram HTML-Formatierung verwendet:
    fetter Name, fette Abschnittskoepfe, Monospace fuer Steuernummern.
    Alle Nutzerdaten werden mit ``html.escape()`` gesichert.
    """
    if html:
        esc = _html_module.escape

        header_name = f"<b>{esc(name)}</b>"
        meta = []
        if mnr:
            meta.append(f"Nr. {esc(mnr)}")
        if steuernr:
            meta.append(f"StNr <code>{esc(steuernr)}</code>")
        if meta:
            header_name += "  ·  " + "  ·  ".join(meta)
        lines = [header_name]
        if idnr:
            lines.append(f"IdNr: <code>{esc(idnr)}</code>")

        info = []
        if funktion:
            info.append(f"Funktion: {esc(funktion)}")
        if anrede:
            info.append(f"Anrede: {esc(anrede)}")
        if last_contact:
            info.append(f"Letzter Kontakt: {esc(_fmt_date(last_contact))}")
        if info:
            lines.append("")
            lines.extend(info)

        if open_points:
            lines.append("\n<b>Offene Punkte</b>")
            lines.extend(f"• {esc(pt)}" for pt in open_points)

        if notes:
            lines.append("\n<b>Notizen</b>")
            lines.extend(f"• {esc(n)}" for n in notes[:3])

        def _ta_line_html(ta: dict) -> str:
            steuerart = esc(str(ta.get("steuerart", "?")))
            jahr = esc(str(ta.get("steuerjahr", "?")))
            betrag = ta.get("betrag_eur")
            faellig = ta.get("zahlungstermin") or ""
            if betrag is not None:
                try:
                    b = float(betrag)
                    richtung = "Erstattung" if b >= 0 else "Nachzahlung"
                    s = f"{abs(b):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
                    b_info = f" · {esc(richtung)} {s}"
                except (ValueError, TypeError):
                    b_info = f" · {esc(str(betrag))} €"
            else:
                b_info = ""
            f_info = f" · fällig {esc(_fmt_date(faellig))}" if faellig and faellig != "null" else ""
            return f"• {steuerart} {jahr}{b_info}{f_info}"

        if tax_assessments:
            lines.append("\n<b>Steuerbescheide</b>")
            lines.extend(_ta_line_html(ta) for ta in tax_assessments)

        if advance_payments:
            lines.append("\n<b>Vorauszahlungen</b>")
            for ap in advance_payments:
                lines.append(f"• {esc(str(ap.get('steuerart','?')))} {esc(str(ap.get('vorauszahlungsjahr','?')))}")

        if tasks:
            from datetime import date as _date
            _today = _date.today().isoformat()
            lines.append("\n<b>Offene Aufgaben</b>")
            for task in tasks:
                content = esc(task.get("content", "(ohne Titel)"))
                due_date = (task.get("due") or {}).get("date", "")
                if due_date and due_date < _today:
                    flag = " ⚠ überfällig"
                elif due_date == _today:
                    flag = " (heute)"
                elif due_date:
                    flag = f" (fällig: {esc(_fmt_date(due_date))})"
                else:
                    flag = ""
                lines.append(f"• {content}{flag}")

        if completed_tasks:
            lines.append("\n<b>Erledigte Aufgaben</b>")
            for task in completed_tasks:
                content = esc(task.get("content", "(ohne Titel)"))
                completed_at = task.get("completed_at", "")
                date_str = f" (erledigt: {esc(_fmt_date(completed_at[:10]))})" if completed_at else ""
                lines.append(f"✓ {content}{date_str}")

        return "\n".join(lines)

    # --- plain-text fallback (html=False) ---
    header = name
    meta = []
    if mnr:
        meta.append(f"Nr. {mnr}")
    if steuernr:
        meta.append(f"StNr {steuernr}")
    if meta:
        header += "  ·  " + "  ·  ".join(meta)
    lines = [header]
    if idnr:
        lines.append(f"IdNr: {idnr}")

    info = []
    if funktion:
        info.append(f"Funktion: {funktion}")
    if anrede:
        info.append(f"Anrede: {anrede}")
    if last_contact:
        info.append(f"Letzter Kontakt: {_fmt_date(last_contact)}")
    if info:
        lines.append("")
        lines.extend(info)

    if open_points:
        lines.append("\n── Offene Punkte ──")
        lines.extend(f"• {pt}" for pt in open_points)

    if notes:
        lines.append("\n── Notizen ──")
        lines.extend(f"• {n}" for n in notes[:3])

    def _ta_line(ta: dict) -> str:
        steuerart = ta.get("steuerart", "?")
        jahr = ta.get("steuerjahr", "?")
        betrag = ta.get("betrag_eur")
        faellig = ta.get("zahlungstermin") or ""
        if betrag is not None:
            try:
                b = float(betrag)
                richtung = "Erstattung" if b >= 0 else "Nachzahlung"
                s = f"{abs(b):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
                b_info = f" · {richtung} {s}"
            except (ValueError, TypeError):
                b_info = f" · {betrag} €"
        else:
            b_info = ""
        f_info = f" · fällig {_fmt_date(faellig)}" if faellig and faellig != "null" else ""
        return f"• {steuerart} {jahr}{b_info}{f_info}"

    if tax_assessments:
        lines.append("\n── Steuerbescheide ──")
        lines.extend(_ta_line(ta) for ta in tax_assessments)

    if advance_payments:
        lines.append("\n── Vorauszahlungen ──")
        for ap in advance_payments:
            lines.append(f"• {ap.get('steuerart','?')} {ap.get('vorauszahlungsjahr','?')}")

    if tasks:
        from datetime import date as _date
        _today = _date.today().isoformat()
        lines.append("\n── Offene Aufgaben ──")
        for task in tasks:
            content = task.get("content", "(ohne Titel)")
            due_date = (task.get("due") or {}).get("date", "")
            if due_date and due_date < _today:
                flag = " ⚠ überfällig"
            elif due_date == _today:
                flag = " (heute)"
            elif due_date:
                flag = f" (fällig: {_fmt_date(due_date)})"
            else:
                flag = ""
            lines.append(f"• {content}{flag}")

    if completed_tasks:
        lines.append("\n── Erledigte Aufgaben ──")
        for task in completed_tasks:
            content = task.get("content", "(ohne Titel)")
            completed_at = task.get("completed_at", "")
            date_str = f" (erledigt: {_fmt_date(completed_at[:10])})" if completed_at else ""
            lines.append(f"✓ {content}{date_str}")

    return "\n".join(lines)


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
        image_b64 = action.get("image_b64", "")
        if image_b64:
            return await screen_capture.describe_screen_from_b64(image_b64, S.ai)
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
        raw = await todoist_tools.get_tasks(
            S.TODOIST_TOKEN,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )
        if not raw or raw == "KEINE_TASKS":
            return raw or "Keine offenen Aufgaben."

        # Aufgaben auf heute/überfällig reduzieren und mit Personen-Kontext anreichern
        import persons_db as _pdb
        profiles = _pdb.all_profiles()

        task_lines = [l for l in raw.splitlines() if l.startswith("•")]
        due_lines = [l for l in task_lines if "(heute)" in l or "überfällig" in l]
        if not due_lines:
            due_lines = task_lines  # Fallback: alle zeigen wenn nichts heute fällig

        output: list[str] = []
        for line in due_lines:
            output.append(line)
            line_lower = line.lower()
            for prof in profiles:
                # Prüfen ob Vor- oder Nachname im Aufgabentext vorkommt
                name_parts = [n for n in prof.name.split() if len(n) > 2]
                if not any(part.lower() in line_lower for part in name_parts):
                    continue
                context: list[str] = []
                if prof.funktion:
                    context.append(prof.funktion)
                if prof.last_contact:
                    context.append(f"letzter Kontakt: {prof.last_contact}")
                for op in prof.open_points:
                    context.append(f"offen: {op}")
                for ta in prof.tax_assessments:
                    steuerart = ta.get("steuerart", "?")
                    jahr = ta.get("steuerjahr", "?")
                    betrag = ta.get("betrag_eur")
                    faellig = ta.get("zahlungstermin") or ""
                    if betrag is not None:
                        try:
                            b = float(betrag)
                            richtung = "Erstattung" if b >= 0 else "Nachzahlung"
                            betrag_str = f"{abs(b):,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")
                            context.append(f"Bescheid {steuerart} {jahr}: {richtung} {betrag_str}" + (f", fällig {faellig}" if faellig and faellig != "null" else ""))
                        except (ValueError, TypeError):
                            context.append(f"Bescheid {steuerart} {jahr}")
                for ap in prof.advance_payments:
                    steuerart = ap.get("steuerart", "?")
                    jahr = ap.get("vorauszahlungsjahr", "?")
                    context.append(f"Vorauszahlung {steuerart} {jahr}")
                if context:
                    output.append(f"  → {prof.name}: " + " | ".join(context))
                break  # nur erstes Profil pro Aufgabe

        total = len(due_lines)
        overdue = sum(1 for l in due_lines if "überfällig" in l)
        header = f"Aufgaben heute ({total} fällig, {overdue} überfällig):"
        return header + "\n" + "\n".join(output)

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
        # Default: morgen — wenn kein Datum genannt wird
        if not due:
            due = "morgen"
        project_id = S.TODOIST_PROJECTS.get(bereich) if bereich else None
        # Immer in die konfigurierte Default-Section (Fr. Essberger),
        # es sei denn ein anderer Bereich hat eine eigene Section.
        section_id = (
            S.TODOIST_PROJECTS.get("hilo_section") if bereich == "hilo"
            else S.TODOIST_DEFAULT_SECTION or None
        )
        # Aufgabe immer Catrin zuweisen
        my_id = await todoist_tools._my_id(S.TODOIST_TOKEN)
        return await todoist_tools.add_task(
            S.TODOIST_TOKEN, content, due,
            project_id=project_id, section_id=section_id,
            assignee_id=my_id,
        )

    elif t == "DONETASK":
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        result = await todoist_tools.complete_task(
            S.TODOIST_TOKEN, p,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )
        # Abschluss-Ritual (Issue #121): Tageszaehler fuer abgeschlossene Tasks.
        # Reset automatisch wenn der erste DONETASK eines neuen Tages kommt.
        # Issue #131: Lock schuetzt den atomaren Date-Check + Reset + Inkrement.
        # Die Todoist-API-Anfrage (oben) laeuft bewusst AUSSERHALB des Locks,
        # damit bei parallelen Requests kein Lock-Contention auf I/O entsteht.
        import datetime as _dt
        _today = _dt.date.today().isoformat()
        async with _tasks_completed_lock:
            if S._tasks_completed_date != _today:
                S.TASKS_COMPLETED_TODAY = 0
                S._tasks_completed_date = _today
            if result and "fehlgeschlagen" not in result.lower() and "nicht gefunden" not in result.lower():
                S.TASKS_COMPLETED_TODAY += 1
        return result

    elif t == "CALENDAR":
        import microsoft_calendar_tools
        import datetime as _dt

        # Compute time_min / time_max from optional payload ("heute" /
        # "diese Woche" / "nächste Woche"). Falls kein Payload: default.
        time_min = None
        time_max = None
        _range = p.strip().lower()
        _today = _dt.date.today()
        _now = _dt.datetime.now(_dt.timezone.utc)

        if _range in ("heute", "today"):
            time_min = _dt.datetime.combine(_today, _dt.time.min, tzinfo=_dt.timezone.utc)
            time_max = _dt.datetime.combine(_today, _dt.time.max, tzinfo=_dt.timezone.utc)
        elif _range in ("diese woche", "diese_woche", "woche", "this week"):
            _sunday = _today + _dt.timedelta(days=(6 - _today.weekday()))
            time_min = _now
            time_max = _dt.datetime.combine(_sunday, _dt.time.max, tzinfo=_dt.timezone.utc)
        elif "nächste" in _range or "naechste" in _range or "next week" in _range:
            _next_monday = _today + _dt.timedelta(days=(7 - _today.weekday()))
            _next_sunday = _next_monday + _dt.timedelta(days=6)
            time_min = _dt.datetime.combine(_next_monday, _dt.time.min, tzinfo=_dt.timezone.utc)
            time_max = _dt.datetime.combine(_next_sunday, _dt.time.max, tzinfo=_dt.timezone.utc)

        results = []
        google_result = await google_calendar_tools.get_events(
            days=S.CALENDAR_DAYS, time_min=time_min, time_max=time_max,
        )
        if google_result:
            results.append(google_result)
        if S.MICROSOFT_CALENDAR_ICS_URL:
            ms_result = await microsoft_calendar_tools.get_events(
                days=S.CALENDAR_DAYS, time_min=time_min, time_max=time_max,
            )
            if ms_result:
                results.append(ms_result)
        return "\n\n".join(results) if results else "Keine Termine im angefragten Zeitraum."

    elif t == "ADDCAL":
        parts = p.split("|", 1)
        title = parts[0].strip()
        when = parts[1].strip() if len(parts) > 1 else "morgen 10 Uhr"
        # Fix #73: Exception fangen und Catrin eine klare Fehlermeldung
        # geben statt still zu scheitern (silent fail).
        try:
            result = await google_calendar_tools.add_event(title, when)
            import activity_log as _al
            _al.log_action("calendar_added", title)
            return result
        except Exception as e:
            log.warning("ADDCAL fehlgeschlagen: %s: %s", type(e).__name__, e)
            error_text = f"Termin konnte nicht angelegt werden: {e}"
            film_q = jarvis_quotes.quote_maybe("error_film", 0.4)
            return f"{film_q} {error_text}".strip() if film_q else error_text

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
            error_text = f"Abwesenheitsnotiz konnte nicht gesetzt werden: {e}"
            film_q = jarvis_quotes.quote_maybe("error_film", 0.4)
            return f"{film_q} {error_text}".strip() if film_q else error_text

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
        user_msg = f"Von: {_sanitize(sender)}\nBetreff: {_sanitize(subject)}\n\n{_sanitize(body)}"
        try:
            resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
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

    elif t == "MARK_MAIL_WERBUNG":
        # Werbung-Shortcut: als gelesen markieren + in Werbung-Ordner verschieben.
        # Liest werbung_folder + account_folder_map aus mail_triage_rules.json.
        active = session_state.get("default").active_mail
        if not active:
            return "Keine aktive Mail zum Verschieben."
        import mail_triage as _triage
        _rules = _triage._load_rules()
        _folder = _rules.get("werbung_folder", "INBOX.Gelesen_automatisch")
        _overrides = _rules.get("account_folder_map", {}).get(active.account, {})
        _folder = _overrides.get(_folder, _folder)
        await mail_actions.mark_mail_read(active.account, active.uid)
        moved = await mail_actions.move_mail(active.account, active.uid, _folder)
        session_state.clear_active_mail("default")
        if moved:
            return f"Erledigt — als Werbung markiert und nach '{_folder}' verschoben."
        return "Als gelesen markiert, Verschieben hat leider nicht geklappt."

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

            # Pass 1: fetch headers concurrently (semaphore-limited, Apple-safe).
            # Replaces the previous sequential for-loop; reduces ~5000 round-trips
            # to ~500 elapsed slots at concurrency=10. (#135)
            _IMPORT_CONCURRENCY = 10
            _fetch_sem = asyncio.Semaphore(_IMPORT_CONCURRENCY)
            _classify_sem = asyncio.Semaphore(10)
            parsed: list[tuple[str, str, str]] = []  # (sender_raw, sender_email, subject)
            _parsed_lock = asyncio.Lock()

            async def _fetch_one(uid: int) -> None:
                async with _fetch_sem:
                    typ, data = await client.uid(
                        "fetch", str(uid), "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]"
                    )
                if typ != "OK" or not data:
                    return
                byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
                if not byte_items:
                    return
                raw = max(byte_items, key=len)
                msg = _email.message_from_bytes(raw)
                date_str = msg.get("Date", "")
                if date_str:
                    try:
                        mail_dt = _email_utils.parsedate_to_datetime(date_str)
                        if mail_dt.tzinfo is None:
                            mail_dt = mail_dt.replace(tzinfo=datetime.timezone.utc)
                        if mail_dt < cutoff:
                            return
                    except Exception:
                        pass
                from_parsed = parseaddr(msg.get("From", ""))
                sender_raw = msg.get("From", "")
                sender_email = (from_parsed[1] or "").lower().strip()
                subject = _decode_header(msg.get("Subject", ""))
                if not sender_raw and not subject:
                    return
                async with _parsed_lock:
                    parsed.append((sender_raw, sender_email, subject))

            await asyncio.gather(*[_fetch_one(uid) for uid in all_uids])
            total = len(parsed)

            # Pass 2: classify in parallel (rate-limited via semaphore).
            async def _classify_one(s_raw: str, subj: str) -> str:
                async with _classify_sem:
                    return await _classify(s_raw, subj, "")

            categories = await asyncio.gather(
                *[_classify_one(sr, sj) for sr, _, sj in parsed],
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
        if any(phrase in lower for phrase in ("ich mag", "ich bevorzuge", "ich liebe",
                                      "ich trinke gerne", "ich esse gerne")):
            kind = "vorliebe"
        elif any(phrase in lower for phrase in ("ich hasse", "ich mag nicht", "ich kann nicht",
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

        # 1. Personen-Notizen + offene Punkte — nur Keyword-Treffer im Notiztext,
        # NICHT auf Personennamen-Match (das ist LOOKUP_CONTACT's Aufgabe).
        for prof in persons_db.all_profiles():
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
            _unc_q = jarvis_quotes.quote_maybe("uncertainty", 0.25)
            base = f"Ich finde nichts zu {query}, {pick_address()}."
            return f"{base} {_unc_q}".strip() if _unc_q else base
        return f"Zu {query} habe ich:\n" + "\n".join(f"- {r}" for r in results[:15])

    elif t == "MAIL_LOG":
        import activity_log as _al
        entries = _al.get_daily_summary().get("mail_processed", [])
        if not entries:
            return (
                f"Heute habe ich noch keine Mails verarbeitet, {pick_address()}. "
                f"Der Monitor laeuft und wird Sie informieren, sobald etwas ankommt."
            )
        lines = "\n".join(f"- {e}" for e in entries)
        return f"Folgende Mails habe ich heute verarbeitet:\n{lines}"

    elif t == "TAGESABSCHLUSS":
        import activity_log as _al
        import datetime as _dt
        parts: list[str] = []

        mail_entries = _al.get_daily_summary().get("mail_processed", [])
        if mail_entries:
            mail_lines = "\n".join(f"- {e}" for e in mail_entries)
            parts.append(f"Mail-Aktivitäten heute:\n{mail_lines}")
        else:
            parts.append("Heute keine Mails vom Monitor verarbeitet.")

        _today = _dt.date.today()
        _time_min = _dt.datetime.combine(_today, _dt.time.min, tzinfo=_dt.timezone.utc)
        _time_max = _dt.datetime.combine(_today, _dt.time.max, tzinfo=_dt.timezone.utc)
        try:
            cal_result = await google_calendar_tools.get_events(
                days=1, time_min=_time_min, time_max=_time_max,
            )
            if cal_result and cal_result.strip():
                parts.append(f"Termine heute:\n{cal_result.strip()}")
        except Exception as _e:
            log.warning("TAGESABSCHLUSS calendar failed: %s: %s", type(_e).__name__, _e)

        if S.TODOIST_TOKEN and S.TODOIST_TOKEN != "YOUR_TODOIST_API_TOKEN":
            try:
                raw_tasks = await todoist_tools.get_tasks(
                    S.TODOIST_TOKEN,
                    project_ids=S.TODOIST_PROJECT_IDS or None,
                    section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
                )
                if raw_tasks and raw_tasks != "KEINE_TASKS":
                    task_lines = [l for l in raw_tasks.splitlines() if l.startswith("•")]
                    due_lines = [l for l in task_lines if "(heute)" in l or "überfällig" in l]
                    if due_lines:
                        parts.append("Offene Aufgaben heute:\n" + "\n".join(due_lines))
            except Exception as _e:
                log.warning("TAGESABSCHLUSS tasks failed: %s: %s", type(_e).__name__, _e)

        if not parts:
            _unc_q = jarvis_quotes.quote_maybe("uncertainty", 0.25)
            base = f"Ein ruhiger Tag, {pick_address()}. Nichts Besonderes zu berichten."
            return f"{base} {_unc_q}".strip() if _unc_q else base

        summary = "\n\n".join(parts)
        # Issue #199: JARVIS-Abschlusszitat im Marvel-Stil
        _closing = jarvis_quotes.quote("closing")
        preamble = f"{_closing}\n\n" if _closing else ""
        # Issue #200: optionales Film-Abschlusszitat mit Cooldown
        _closing_film = jarvis_quotes.quote_maybe("closing_film", 0.3)
        suffix = f" {_closing_film}" if _closing_film else ""
        return f"{preamble}Tagesabschluss, {pick_address()}:\n\n{summary}{suffix}"

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
        _query_norm = persons_db._norm(query)
        for prof in persons_db.all_profiles():
            if _query_norm not in persons_db._norm(prof.name):
                continue
            seen_ids.add(prof.contact_id)
            results.append({
                "name": prof.name,
                "anrede": prof.anrede,
                "funktion": prof.funktion,
                "last_contact": getattr(prof, "last_contact", ""),
                "open_points": list(getattr(prof, "open_points", [])),
                "notes": list(getattr(prof, "notes", [])),
                "tax_assessments": list(getattr(prof, "tax_assessments", [])),
                "advance_payments": list(getattr(prof, "advance_payments", [])),
            })
        for c in apple_hits:
            if c.id in seen_ids:
                continue
            results.append({
                "name": c.name,
                "anrede": "",
                "funktion": c.organization,
                "last_contact": "",
                "open_points": [],
                "notes": [],
                "tax_assessments": [],
                "advance_payments": [],
            })
        if not results:
            return f"Ich finde niemanden mit dem Namen {query} in Ihren Kontakten."
        if len(results) > 1:
            names = "\n".join(f"- {r['name']}" for r in results[:8])
            return (
                f"Mehrere Treffer fuer {query}:\n{names}\n"
                f"Sag praeziser welchen Du meinst."
            )
        # Genau ein Treffer — vollstaendiges Wissen, keine Kontaktdaten
        r = results[0]
        out_parts: list[str] = []

        bits: list[str] = [r["name"]]
        if r.get("funktion"):
            bits.append(f"({r['funktion']})")
        out_parts.append(" ".join(bits) + ".")

        if r.get("anrede"):
            out_parts.append(f"Anrede: {r['anrede']}.")
        if r.get("last_contact"):
            out_parts.append(f"Letzter Kontakt: {r['last_contact']}.")

        for pt in r.get("open_points", []):
            out_parts.append(f"Offener Punkt: {pt}")

        for note in r.get("notes", []):
            out_parts.append(f"Notiz: {note}")

        for ta in r.get("tax_assessments", []):
            steuerart = ta.get("steuerart", "?")
            jahr = ta.get("steuerjahr", "?")
            betrag = ta.get("betrag_eur")
            datum = ta.get("ausstellungsdatum", "")
            faellig = ta.get("zahlungstermin") or ""
            if betrag is not None:
                try:
                    b = float(betrag)
                    richtung = "Erstattung" if b >= 0 else "Nachzahlung"
                    betrag_str = f"{abs(b):,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")
                    betrag_info = f"{richtung} {betrag_str}"
                except (ValueError, TypeError):
                    betrag_info = f"Betrag {betrag}€"
            else:
                betrag_info = ""
            faellig_info = f", fällig {_fmt_date(faellig)}" if faellig and faellig != "null" else ""
            datum_info = f" vom {_fmt_date(datum)}" if datum else ""
            out_parts.append(
                f"Steuerbescheid {steuerart} {jahr}{datum_info}: {betrag_info}{faellig_info}."
            )

        for ap in r.get("advance_payments", []):
            steuerart = ap.get("steuerart", "?")
            jahr = ap.get("vorauszahlungsjahr", "?")
            datum = ap.get("ausstellungsdatum", "")
            datum_info = f" vom {_fmt_date(datum)}" if datum else ""
            out_parts.append(f"Vorauszahlungsbescheid {steuerart} {jahr}{datum_info}.")

        # Todoist-Aufgaben zur Person laden (offen + erledigt)
        _person_tasks: list[dict] = []
        _person_completed: list[dict] = []
        try:
            import todoist_tools as _td
            if S.TODOIST_TOKEN:
                _name_tokens = [tok for tok in r["name"].split() if len(tok) >= 3]
                # Offene Tasks
                _all_tasks = await _td._fetch_all_tasks(S.TODOIST_TOKEN)
                if isinstance(_all_tasks, list):
                    for _task in _all_tasks:
                        if _task.get("checked") or _task.get("is_deleted"):
                            continue
                        _content_low = _task.get("content", "").lower()
                        if any(tok.lower() in _content_low for tok in _name_tokens):
                            _person_tasks.append(_task)
                    _person_tasks.sort(key=lambda t: (t.get("due") or {}).get("date", "9999"))
                # Erledigte Tasks
                _completed_all = await _td._fetch_completed_tasks(S.TODOIST_TOKEN, limit=200)
                for _task in _completed_all:
                    _content_low = _task.get("content", "").lower()
                    if any(tok.lower() in _content_low for tok in _name_tokens):
                        _person_completed.append(_task)
                _person_completed.sort(
                    key=lambda t: t.get("completed_at", ""), reverse=True
                )
        except Exception as _te:
            log.warning(f"LOOKUP_CONTACT todoist: {_te}")

        # Mandanten-CSV: Mitgliedsnr + Steuernr fuer die Kachel
        try:
            import mandanten as _mand
            _mand_hit = _mand.find_by_name(r["name"])
            _mand_hit = _mand_hit[0] if _mand_hit else None
        except Exception:
            _mand_hit = None

        _card_kwargs = dict(
            name=r["name"],
            mnr=(_mand_hit or {}).get("mitgliedsnr", ""),
            steuernr=(_mand_hit or {}).get("steuernummer", ""),
            idnr=(_mand_hit or {}).get("id_nr", ""),
            funktion=r.get("funktion", ""),
            anrede=r.get("anrede", ""),
            last_contact=r.get("last_contact", ""),
            open_points=r.get("open_points", []),
            notes=r.get("notes", []),
            tax_assessments=r.get("tax_assessments", []),
            advance_payments=r.get("advance_payments", []),
            tasks=_person_tasks or None,
            completed_tasks=_person_completed or None,
        )
        S.PENDING_CARD_HTML = _build_person_card_html(**_card_kwargs)
        S.PENDING_TELEGRAM_TEXT = _build_person_card_telegram(**_card_kwargs, html=True)
        S.PENDING_TELEGRAM_PARSE_MODE = "HTML"

        # Kurzer gesprochener Text fuer den Orb + optionaler Hinweis
        spoken = f"Hier sind die Informationen zu {r['name']}."
        if _person_tasks or _person_completed:
            parts_spoken = []
            if _person_tasks:
                n = len(_person_tasks)
                parts_spoken.append(f"{n} offene{'n' if n != 1 else ''}")
            if _person_completed:
                n = len(_person_completed)
                parts_spoken.append(f"{n} erledigte{'n' if n != 1 else ''}")
            spoken += f" Todoist-Aufgaben: {' und '.join(parts_spoken)}."
        # Hinweis wenn offene Punkte vorhanden
        if r.get("open_points"):
            n = len(r["open_points"])
            spoken += f" Es gibt {n} offene{'n Punkt' if n == 1 else ' Punkte'}."
        elif r.get("tax_assessments"):
            # Naechste Faelligkeit als Hinweis
            for ta in r["tax_assessments"]:
                faellig = ta.get("zahlungstermin") or ""
                if faellig and faellig != "null":
                    spoken += (
                        f" Hinweis: {ta.get('steuerart','?')} {ta.get('steuerjahr','?')} "
                        f"fällig am {_fmt_date(faellig)}."
                    )
                    break
        return spoken

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
            return f"Ich finde niemanden mit dem Namen {query} in Ihren Kontakten."
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

    elif t == "ACCEPT_DOCTOLIB_APPOINTMENT":
        state = session_state.get("default")
        doc = state.pending_doctolib
        active = state.active_mail
        if not doc:
            return f"Es liegt keine Doctolib-Terminbestätigung vor, {pick_address()}."
        title = f"Arzttermin{' ' + doc.doctor if doc.doctor else ''}"
        when = doc.when_iso or doc.when_human
        try:
            await google_calendar_tools.add_event(title, when)
        except Exception as e:
            log.warning(f"ACCEPT_DOCTOLIB_APPOINTMENT calendar failed: {type(e).__name__}: {e}")
            return f"Termin konnte nicht eingetragen werden: {type(e).__name__}"
        # Notiz zu Catrins eigenem Personenprofil (falls vorhanden)
        try:
            import persons_db as _pdb
            _self_profiles = [p for p in _pdb.all_profiles() if "catrin" in p.name.lower() or "caterina" in p.name.lower()]
            if _self_profiles:
                note_text = f"Arzttermin {doc.doctor} am {doc.when_human}" if doc.doctor else f"Arzttermin am {doc.when_human}"
                _pdb.add_note(_self_profiles[0].contact_id, note_text)
        except Exception as e:
            log.warning(f"ACCEPT_DOCTOLIB_APPOINTMENT person note failed: {type(e).__name__}: {e}")
        if active:
            await mail_actions.mark_mail_read(active.account, active.uid)
            session_state.clear_active_mail("default")
        session_state.clear_pending_doctolib("default")
        return f"Termin '{title}' am {doc.when_human} eingetragen und Mail abgehakt."

    elif t == "DECLINE_DOCTOLIB_APPOINTMENT":
        state = session_state.get("default")
        active = state.active_mail
        session_state.clear_pending_doctolib("default")
        if active:
            await mail_actions.mark_mail_read(active.account, active.uid)
            session_state.clear_active_mail("default")
        return "Doctolib-Termin nicht eingetragen. Mail als gelesen markiert."

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
            f"{_sanitize(sender_block)}\n"
            f"Betreff: {_sanitize(mail_data['subject'])}\n"
            f"Inhalt: {_sanitize(mail_data['text'][:600])}"
        )
        try:
            resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
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

    elif t == "PROMISE_DONE":
        # Markiert ein offenes Vorhaben als erledigt (Issue #117).
        # Payload: entweder eine ID ("42") oder der Text des Vorhabens.
        import promise_tracker
        promise_id_str = p.strip()
        if promise_id_str.isdigit():
            await promise_tracker.mark_promise_done(int(promise_id_str))
            return f"Vorhaben als erledigt markiert, {pick_address()}."
        # Text-Matching: offenes Vorhaben anhand des Texts finden
        open_promises = await promise_tracker.get_open_promises(max_age_days=3)
        if not open_promises:
            return f"Ich habe keine offenen Vorhaben gespeichert, {pick_address()}."
        q = promise_id_str.lower()
        matched = [pr for pr in open_promises if q in pr["text"].lower()]
        if not matched:
            return (
                f"Ich finde kein Vorhaben das zu '{promise_id_str}' passt, "
                f"{pick_address()}."
            )
        for pr in matched:
            await promise_tracker.mark_promise_done(pr["id"])
        if len(matched) == 1:
            return f"'{matched[0]['text']}' ist erledigt — gut gemacht, {pick_address()}."
        return f"{len(matched)} Vorhaben als erledigt markiert, {pick_address()}."

    elif t == "CONTACT_NOTE":
        # Speichert eine Notiz zu einem Kontakt (Issue #120).
        # Payload-Format: "Name|Notiz"
        import notes_db
        import google_contacts_tools
        parts_note = p.split("|", 1)
        if len(parts_note) != 2 or not parts_note[0].strip() or not parts_note[1].strip():
            return (
                f"Bitte im Format 'Name|Notiz' angeben, {pick_address()}. "
                f"Beispiel: [ACTION:CONTACT_NOTE] Mueller|Hat wegen Betriebspruefung angerufen"
            )
        contact_name = parts_note[0].strip()
        note_text = parts_note[1].strip()

        # Kontakt in Google Contacts suchen um die resourceName-ID zu erhalten
        contact_id = ""
        try:
            hits = await google_contacts_tools.find_contacts_by_name(contact_name)
            if hits:
                contact_id = hits[0].id
        except Exception as e:
            log.warning(
                f"CONTACT_NOTE: find_contacts_by_name failed: {type(e).__name__}: {e}"
            )

        # Notiz in notes_db speichern (person_contact_id verknuepft sie mit dem Kontakt)
        try:
            note = notes_db.add(
                text=f"Zu {contact_name}: {note_text}",
                kind="notiz",
                tags=[contact_name.lower()],
                person_contact_id=contact_id,
            )
            log.info(
                f"CONTACT_NOTE: note {note.id} saved for contact {contact_name!r} "
                f"(contact_id={contact_id!r})"
            )
            addr_str = pick_address()
            if contact_id:
                return f"Notiz zu {contact_name} gespeichert, {addr_str}."
            else:
                return (
                    f"Notiz gespeichert, {addr_str}. "
                    f"Ich konnte {contact_name!r} nicht in den Kontakten finden — "
                    f"die Notiz ist ohne Kontakt-Verknuepfung abgelegt."
                )
        except Exception as e:
            log.warning(f"CONTACT_NOTE: notes_db.add failed: {type(e).__name__}: {e}")
            return f"Notiz konnte nicht gespeichert werden: {type(e).__name__}"

    elif t == "BRING_ADD":
        # Issue #123: Artikel zur Bring!-Einkaufsliste hinzufuegen.
        # Payload: "Artikel1,Artikel2,..." (kommagetrennt)
        if not S.BRING_EMAIL or not S.BRING_PASSWORD:
            return (
                f"Bring! ist nicht konfiguriert, {pick_address()}. "
                f"Bitte BRING_EMAIL und BRING_PASSWORD in der .env setzen."
            )
        raw = p.strip()
        if not raw:
            return f"Welche Artikel soll ich auf die Einkaufsliste setzen, {pick_address()}?"
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            return f"Ich konnte keine Artikel erkennen, {pick_address()}."
        try:
            import bring_tools
            count = await bring_tools.bring_add_items(items)
            if count == 0:
                return (
                    f"Die Artikel konnten nicht zur Einkaufsliste hinzugefuegt werden, "
                    f"{pick_address()}. Bitte Bring!-Zugangsdaten pruefen."
                )
            if count == len(items):
                if count == 1:
                    return f"'{items[0]}' wurde zur Einkaufsliste hinzugefuegt, {pick_address()}."
                return (
                    f"{count} Artikel wurden zur Bring!-Einkaufsliste hinzugefuegt: "
                    f"{', '.join(items)}."
                )
            return (
                f"{count} von {len(items)} Artikeln wurden hinzugefuegt "
                f"({', '.join(items[:count])})."
            )
        except Exception as e:
            log.warning(f"BRING_ADD: {type(e).__name__}: {e}")
            return f"Bring!-Fehler: {type(e).__name__}"

    elif t == "BRING_LIST":
        # Issue #123: Aktuelle Bring!-Liste abrufen + Angebotsabgleich.
        if not S.BRING_EMAIL or not S.BRING_PASSWORD:
            return (
                f"Bring! ist nicht konfiguriert, {pick_address()}. "
                f"Bitte BRING_EMAIL und BRING_PASSWORD in der .env setzen."
            )
        try:
            import bring_tools
            items = await bring_tools.bring_get_items()
            if not items:
                return f"Ihre Einkaufsliste ist leer, {pick_address()}."
            items_text = ", ".join(items)
            offer_hint = await bring_tools.bring_check_offers(items)
            if offer_hint:
                return (
                    f"Auf der Einkaufsliste: {items_text}. "
                    f"{offer_hint}."
                )
            return f"Auf der Einkaufsliste: {items_text}."
        except Exception as e:
            log.warning(f"BRING_LIST: {type(e).__name__}: {e}")
            return f"Bring!-Fehler: {type(e).__name__}"

    elif t == "PICNICORDER":
        # Issue #126: Bring!-Liste lesen und Artikel bei Picnic bestellen.
        if not S.PICNIC_EMAIL or not S.PICNIC_PASSWORD:
            return (
                f"Picnic ist nicht konfiguriert, {pick_address()}. "
                f"Bitte PICNIC_EMAIL und PICNIC_PASSWORD in der .env setzen."
            )
        try:
            import bring_tools
            import picnic_tools
            items = await bring_tools.bring_get_items()
            if not items:
                return f"Die Bring!-Einkaufsliste ist leer, {pick_address()}."
            added, not_found = await picnic_tools.picnic_add_items(items)
            parts: list[str] = []
            if added:
                parts.append(f"{len(added)} Artikel in den Picnic-Warenkorb gelegt: {', '.join(added)}")
            if not_found:
                parts.append(f"nicht gefunden: {', '.join(not_found)}")
            if not parts:
                return f"Kein Artikel konnte in den Picnic-Warenkorb gelegt werden, {pick_address()}."
            return ". ".join(parts) + f". Bitte die Bestellung direkt in der Picnic-App abschliessen, {pick_address()}."
        except Exception as e:
            log.warning(f"PICNICORDER: {type(e).__name__}: {e}")
            return f"Picnic-Fehler: {type(e).__name__}"

    elif t == "SPEISEPLAN_SHOW":
        # Bestehenden Plan anzeigen — kein Neuerstellen, keine Wunsch-Abfrage.
        import meal_plan as _mp
        if not S.MEAL_PLAN_WEEK:
            return (
                f"Es gibt noch keinen Speisenplan, {pick_address()}. "
                f"Sagen Sie 'Erstell einen Speiseplan' und ich frage Sie nach Ihren Wünschen."
            )
        S.PENDING_CARD_HTML = _mp.build_meal_plan_card_html()
        dates = sorted(S.MEAL_PLAN_WEEK.keys())
        n = len(dates)
        return f"Hier ist der aktuelle Plan, {pick_address()} — {n} Tage."

    elif t == "SPEISEPLAN_PDF":
        # Issue #181: Aktuellen Speiseplan als PDF per Telegram senden.
        import meal_plan as _mp
        if not S.MEAL_PLAN_WEEK:
            return (
                f"Es gibt noch keinen Speisenplan, {pick_address()}. "
                f"Bitte erst einen Speiseplan erstellen."
            )
        _pdf_ing = await _mp.get_ingredients_for_week()
        _pdf_cat = await _mp.categorize_ingredients(_pdf_ing) if _pdf_ing else None
        pdf_path = _mp.generate_meal_plan_pdf(categorized_ingredients=_pdf_cat)
        if pdf_path:
            try:
                import telegram_bot as _tb
                await _tb.send_user_document(pdf_path, caption="Speiseplan")
                return f"Der Speiseplan wurde als PDF an Ihr Telegram gesendet, {pick_address()}."
            except Exception as _pdf_exc:
                log.warning("SPEISEPLAN_PDF: Versand fehlgeschlagen: %s", _pdf_exc)
                return (
                    f"Das PDF wurde erstellt, konnte aber nicht gesendet werden, "
                    f"{pick_address()}. Bitte Telegram-Konfiguration prüfen."
                )
            finally:
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
        return (
            f"Der Speiseplan konnte leider nicht als PDF erstellt werden, "
            f"{pick_address()}."
        )

    elif t == "SPEISEPLAN":
        # On-demand: heute bis Freitag, oder benutzerdefinierter Zeitraum via daterange:.
        # Payload-Format: [daterange:YYYY-MM-DD:YYYY-MM-DD|]wuensche
        import meal_plan as _mp
        import re as _re
        payload = p.strip() if p else ""
        explicit_dates = None
        _dr_m = _re.match(r'daterange:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})\|?', payload)
        if _dr_m:
            _start = datetime.date.fromisoformat(_dr_m.group(1))
            _end = datetime.date.fromisoformat(_dr_m.group(2))
            explicit_dates = []
            _d = _start
            while _d <= _end:
                explicit_dates.append(_d)
                _d += datetime.timedelta(days=1)
            payload = payload[_dr_m.end():].strip()
        wishes = payload
        today_str = datetime.date.today().isoformat()
        if not wishes and not explicit_dates and S.MEAL_PLAN_WEEK and today_str in S.MEAL_PLAN_WEEK:
            return _mp.format_meal_plan_tts()
        log.info(f"SPEISEPLAN: generiere neu (dates={explicit_dates}, wishes={wishes[:60]!r})")
        plan = await _mp.generate_meal_plan(start_today=not explicit_dates, wishes=wishes,
                                            explicit_dates=explicit_dates)
        if not plan:
            return (
                f"Ich konnte den Speisenplan leider nicht erstellen, "
                f"{pick_address()}. Bitte spaeter erneut versuchen."
            )
        # Card-HTML fuer Web-Frontend
        S.PENDING_CARD_HTML = _mp.build_meal_plan_card_html()
        # Zutaten kategorisieren (async) und PDF generieren
        _all_ing = await _mp.get_ingredients_for_week()
        _cat_ing = await _mp.categorize_ingredients(_all_ing) if _all_ing else None
        pdf_path = _mp.generate_meal_plan_pdf(categorized_ingredients=_cat_ing)
        if pdf_path:
            try:
                import telegram_bot as _tb
                await _tb.send_user_document(pdf_path, caption="Speiseplan der Woche")
            except Exception as _pdf_exc:
                log.warning("SPEISEPLAN: PDF-Versand fehlgeschlagen: %s", _pdf_exc)
            finally:
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
        # Kurzer TTS-Text (kein vollständiges Vorlesen)
        dates = sorted(S.MEAL_PLAN_WEEK.keys())
        n = len(dates)
        return f"Plan ist fertig, {pick_address()} — {n} Tage, die Übersicht sehen Sie gleich."

    elif t == "SPEISEPLAN_SWAP":
        # Issue #125: Ein einzelnes Gericht tauschen.
        # Payload-Format: "Wochentag|Neues Gericht"
        # Beispiel: "Montag|Pasta mit Gemüse"
        import meal_plan as _mp
        if not S.MEAL_PLAN_WEEK:
            return (
                f"Es gibt noch keinen Speisenplan, {pick_address()}. "
                f"Bitte erst [ACTION:SPEISEPLAN] aufrufen."
            )
        parts = p.split("|", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return (
                f"Bitte im Format 'Wochentag|Neues Gericht' angeben, "
                f"{pick_address()}. Beispiel: Montag|Pasta mit Gemüse"
            )
        weekday_raw = parts[0].strip()
        new_dish = parts[1].strip()
        _WEEKDAY_MAP = {
            "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
            "freitag": 4, "samstag": 5, "sonntag": 6,
        }
        target_weekday = _WEEKDAY_MAP.get(weekday_raw.lower())
        if target_weekday is None:
            return (
                f"Wochentag '{weekday_raw}' nicht erkannt, {pick_address()}. "
                f"Bitte einen deutschen Wochentag angeben (z.B. Montag)."
            )
        # Datum mit diesem Wochentag im Plan suchen
        target_date = None
        for date_str in S.MEAL_PLAN_WEEK:
            try:
                d = datetime.date.fromisoformat(date_str)
                if d.weekday() == target_weekday:
                    target_date = date_str
                    break
            except ValueError:
                pass
        if not target_date:
            return (
                f"Fuer {weekday_raw} habe ich keinen Eintrag im Plan, "
                f"{pick_address()}."
            )
        # Gericht ersetzen; Rezept neu generieren via Claude
        old_dish = S.MEAL_PLAN_WEEK[target_date].get("dish", "")
        servings = S.MEAL_PLAN_WEEK[target_date].get("servings", S.MEAL_PLAN_SERVINGS_DEFAULT)
        try:
            from prompt import llm_text as _llm_text
            swap_prompt = (
                f"Du bist Jarvis. Erstelle ein vollstaendiges Rezept fuer "
                f"'{new_dish}' fuer {servings} Personen.\n\n"
                "Anforderungen: Kochzeit <= 60 Minuten, "
                + ("ausgewogen + diabetesgeeignet (kein Zucker, wenig einfache Kohlenhydrate). "
                   if S.MEAL_PLAN_DIABETES_MODE else "") +
                "Antworte AUSSCHLIESSLICH mit gueltigem JSON:\n"
                '{"recipe": "Schritt-fuer-Schritt-Anleitung als Text",'
                '"ingredients": ["Zutat 1", "Zutat 2", ...],'
                '"cook_time_minutes": <Ganzzahl>}'
            )
            resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
                max_tokens=1000,
                system=swap_prompt,
                messages=[{"role": "user", "content": f"Gericht: {new_dish}"}],
            )
            raw = _llm_text(resp).strip()
            if raw.startswith("```"):
                raw = "\n".join(
                    l for l in raw.splitlines() if not l.strip().startswith("```")
                )
            import json as _json
            new_entry_data = _json.loads(raw)
            S.MEAL_PLAN_WEEK[target_date] = {
                "dish": new_dish,
                "recipe": str(new_entry_data.get("recipe", "")),
                "servings": servings,
                "ingredients": [str(i) for i in new_entry_data.get("ingredients", [])],
                "cook_time_minutes": int(new_entry_data.get("cook_time_minutes", 45)),
            }
        except Exception as e:
            log.warning(f"SPEISEPLAN_SWAP recipe generation failed: {type(e).__name__}: {e}")
            # Minimaler Fallback: Gericht merken, Rezept leer
            S.MEAL_PLAN_WEEK[target_date]["dish"] = new_dish
            S.MEAL_PLAN_WEEK[target_date]["recipe"] = ""
            S.MEAL_PLAN_WEEK[target_date]["ingredients"] = []
        _mp.save_meal_plan()
        return (
            f"{weekday_raw} getauscht: '{old_dish}' -> '{new_dish}', "
            f"{pick_address()}."
        )

    elif t == "SPEISEPLAN_PREF":
        # Dauerhafte Speiseplan-Vorlieben speichern + Plan neu generieren.
        # Payload-Format (Pipe-getrennt, mehrere moeglich):
        #   avoid:Erbsen          → Zutat dauerhaft ausschliessen
        #   fish:Lachs,Forellen   → Erlaubte Fischarten setzen (leer = kein Fisch)
        #   fish_weekly:true/false → Fisch woechentlich erlaubt?
        import meal_prefs as _mprefs
        import meal_plan as _mp
        changes: list[str] = []
        for segment in p.split("|"):
            seg = segment.strip()
            if not seg:
                continue
            if seg.lower().startswith("avoid:"):
                item = seg[6:].strip()
                if item and _mprefs.add_avoid(item):
                    changes.append(f'"{item}" dauerhaft ausgeschlossen')
            elif seg.lower().startswith("fish:"):
                raw = seg[5:].strip()
                fish_list = [f.strip() for f in raw.split(",") if f.strip()]
                _mprefs.set_fish_allowed(fish_list)
                if fish_list:
                    changes.append(f"Fisch nur noch als: {', '.join(fish_list)}")
                else:
                    changes.append("Kein Fisch mehr im Plan")
            elif seg.lower().startswith("fish_weekly:"):
                val = seg[12:].strip().lower() in ("true", "ja", "yes", "1")
                _mprefs.set_fish_weekly(val)
                changes.append("Fisch " + ("wöchentlich erlaubt" if val else "nicht mehr wöchentlich"))

        if not changes:
            return (
                f"Ich konnte die Vorliebe nicht verstehen, {pick_address()}. "
                'Bitte nochmal konkreter - z.B. "keine Erbsen" oder "Fisch nur als Lachs".'
            )

        # Plan neu generieren — gesamten bestehenden Plan-Zeitraum verwenden,
        # damit nicht nur die Resttage neu erstellt werden.
        _regen_dates = None
        if S.MEAL_PLAN_WEEK:
            try:
                _regen_dates = [
                    datetime.date.fromisoformat(d)
                    for d in sorted(S.MEAL_PLAN_WEEK.keys())
                ]
            except Exception:
                _regen_dates = None
        plan = await _mp.generate_meal_plan(start_today=True, explicit_dates=_regen_dates)
        changes_text = " und ".join(changes)
        if not plan:
            return (
                f"Gespeichert: {changes_text}. Der neue Plan konnte leider nicht "
                f"erstellt werden, {pick_address()}."
            )
        _pref_ing = await _mp.get_ingredients_for_week()
        _pref_cat = await _mp.categorize_ingredients(_pref_ing) if _pref_ing else None
        pdf_path = _mp.generate_meal_plan_pdf(categorized_ingredients=_pref_cat)
        if pdf_path:
            try:
                import telegram_bot as _tb
                await _tb.send_user_document(pdf_path, caption="Speiseplan (aktualisiert)")
            except Exception as _pe:
                log.warning("SPEISEPLAN_PREF: PDF-Versand fehlgeschlagen: %s", _pe)
            finally:
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
        S.PENDING_CARD_HTML = _mp.build_meal_plan_card_html()
        return f"Verstanden — {changes_text}. Neuer Plan ist fertig, {pick_address()}."

    elif t == "EINKAUF_FREIGEBEN":
        # Issue #125: Einkaufsliste aus dem Wochenplan an Bring! uebergeben.
        import meal_plan as _mp
        if not S.MEAL_PLAN_WEEK:
            return (
                f"Es gibt noch keinen Speisenplan, {pick_address()}. "
                f"Bitte erst [ACTION:SPEISEPLAN] aufrufen."
            )
        if not S.BRING_EMAIL or not S.BRING_PASSWORD:
            return (
                f"Bring! ist nicht konfiguriert, {pick_address()}. "
                f"Bitte BRING_EMAIL und BRING_PASSWORD in der .env setzen."
            )
        ingredients = await _mp.get_ingredients_for_week()
        if not ingredients:
            return f"Keine Zutaten im Plan gefunden, {pick_address()}."
        try:
            import bring_tools
            count = await bring_tools.bring_add_items(ingredients)
            if count == 0:
                return (
                    f"Die Zutaten konnten nicht zur Einkaufsliste hinzugefuegt werden, "
                    f"{pick_address()}. Bitte Bring!-Zugangsdaten pruefen."
                )
            return (
                f"{count} Zutaten wurden auf die Bring!-Einkaufsliste uebertragen, "
                f"{pick_address()}. Viel Spass beim Einkaufen."
            )
        except Exception as e:
            log.warning(f"EINKAUF_FREIGEBEN: {type(e).__name__}: {e}")
            return f"Bring!-Fehler beim Uebertragen: {type(e).__name__}"

    elif t == "REZEPT_HEUTE":
        # Issue #125: Heutiges Rezept erneut ausgeben.
        import meal_plan as _mp
        recipe_text = await _mp.get_today_recipe()
        if not recipe_text:
            today_str = datetime.date.today().isoformat()
            if S.MEAL_PLAN_WEEK:
                return (
                    f"Fuer heute ({today_str}) habe ich kein Gericht im Plan, "
                    f"{pick_address()}."
                )
            return (
                f"Es gibt noch keinen Speisenplan, {pick_address()}. "
                f"Bitte erst [ACTION:SPEISEPLAN] aufrufen."
            )
        return recipe_text

    elif t == "LIDL_ANGEBOTE":
        # Alle aktuellen Lidl-Lebensmittelangebote abrufen (kein Watchlist-Filter).
        try:
            import offer_monitor
            items = await offer_monitor.fetch_offers_for_market("Lidl", "")
            if not items:
                return (
                    f"Ich konnte diese Woche keine Angebote von Lidl abrufen, "
                    f"{pick_address()}. Bitte spaeter nochmal versuchen."
                )
            lines = [f"Aktuelle Lidl-Angebote ({len(items)} Artikel):"]
            lines.extend(f"- {item}" for item in items[:40])
            if len(items) > 40:
                lines.append(f"... und {len(items) - 40} weitere.")
            return "\n".join(lines)
        except Exception as e:
            log.warning(f"LIDL_ANGEBOTE: {type(e).__name__}: {e}")
            return f"Lidl-Angebote konnten nicht abgerufen werden: {type(e).__name__}"

    elif t == "OFFERS":
        # Issue #122: Aktuelle Supermarkt-Angebote fuer die Watchlist abrufen.
        # Nutzt den offer_monitor mit 6h-Cache.
        if not S.OFFER_WATCHLIST or not S.OFFER_PLZ:
            return (
                f"Der Angebots-Monitor ist nicht konfiguriert, {pick_address()}. "
                f"Bitte 'offer_watchlist' und 'offer_plz' in der config.json setzen."
            )
        try:
            import offer_monitor
            matches = await offer_monitor.get_matching_offers(
                S.OFFER_WATCHLIST, S.OFFER_PLZ
            )
            if not matches:
                return (
                    f"Keine Angebote gefunden fuer die Merkliste "
                    f"({', '.join(S.OFFER_WATCHLIST)}) diese Woche, {pick_address()}."
                )
            lines = [f"Diese Woche im Angebot (PLZ {S.OFFER_PLZ}):"]
            for item, markets in matches.items():
                lines.append(f"{item}: {', '.join(markets)}")
            return "\n".join(lines)
        except Exception as e:
            log.warning(f"OFFERS: offer_monitor failed: {type(e).__name__}: {e}")
            return f"Angebots-Abfrage fehlgeschlagen: {type(e).__name__}"

    elif t == "MAIL_FORWARD_PENDING":
        # Issue #143 / #202: Empfaenger fuer Weiterleitung ermitteln und in
        # S.PENDING_MAIL_FORWARD speichern. Payload kann eine E-Mail-Adresse
        # oder ein Name sein. Bei einem Namen wird persons_db UND Google
        # Contacts durchsucht. Bei mehreren Treffern wird iterativ nachgefragt
        # (ein Kandidat nach dem anderen) statt alle auf einmal aufzulisten.
        import persons_db
        payload_stripped = p.strip() if p else ""
        addr = S.USER_ADDRESS
        if not payload_stripped:
            return f"Bitte nennen Sie den Empfaenger, {addr}."
        if "@" in payload_stripped:
            # Payload ist bereits eine E-Mail-Adresse
            S.PENDING_MAIL_FORWARD = {
                "to_addr": payload_stripped,
                "to_name": payload_stripped,
            }
            return (
                f"Weiterleitung vorbereitet: {payload_stripped}. "
                f"Bitte bestaetigen, {addr}."
            )
        # Payload ist ein Name — persons_db UND Google Contacts durchsuchen
        db_matches = persons_db.search_by_name(payload_stripped)
        try:
            import google_contacts_tools as _gct
            gc_matches = await _gct.find_contacts_by_name(payload_stripped)
        except Exception:
            gc_matches = []
        # Kandidatenliste aufbauen (dedup nach E-Mail)
        seen_emails: set[str] = set()
        candidates: list[dict] = []
        for profile in db_matches:
            if profile.primary_email:
                email_lower = profile.primary_email.lower()
                if email_lower not in seen_emails:
                    seen_emails.add(email_lower)
                    candidates.append({
                        "to_addr": profile.primary_email,
                        "to_name": profile.name,
                    })
        for contact in gc_matches:
            if contact.emails:
                email_lower = contact.emails[0].lower()
                if email_lower not in seen_emails:
                    seen_emails.add(email_lower)
                    candidates.append({
                        "to_addr": contact.emails[0],
                        "to_name": contact.name,
                    })
        if not candidates:
            return f"Kein Kontakt '{payload_stripped}' gefunden, Madam."
        if len(candidates) == 1:
            c = candidates[0]
            S.PENDING_MAIL_FORWARD = {
                "candidates": candidates,
                "current_index": 0,
            }
            return (
                f"Meinen Sie {c['to_name']} ({c['to_addr']}), Madam?"
            )
        # Mehrere Treffer — iterativ nachfragen, Kandidat fuer Kandidat
        S.PENDING_MAIL_FORWARD = {
            "candidates": candidates,
            "current_index": 0,
        }
        c = candidates[0]
        return f"Meinen Sie {c['to_name']} ({c['to_addr']}), Madam?"

    elif t == "MAIL_FORWARD_NEXT":
        # Issue #202: Nutzer hat "nein" gesagt — naechsten Kandidaten vorschlagen.
        addr = S.USER_ADDRESS
        if not S.PENDING_MAIL_FORWARD or "candidates" not in S.PENDING_MAIL_FORWARD:
            return f"Keine laufende Weiterleitungssuche, {addr}."
        idx = S.PENDING_MAIL_FORWARD.get("current_index", 0) + 1
        candidates = S.PENDING_MAIL_FORWARD["candidates"]
        if idx >= len(candidates):
            S.PENDING_MAIL_FORWARD = {}
            return "Kein weiterer passender Kontakt gefunden, Madam. Die Weiterleitung wird abgebrochen."
        S.PENDING_MAIL_FORWARD["current_index"] = idx
        c = candidates[idx]
        return f"Gibt es noch {c['to_name']} ({c['to_addr']}), Madam?"

    elif t == "PROACTIVE_DELIVER":
        # Issue #148: Liefert die ausstehende proaktive Benachrichtigung aus.
        if not S.PENDING_PROACTIVE:
            return f"Ich habe gerade keine ausstehende Meldung fuer Sie, {pick_address()}."
        text = S.PENDING_PROACTIVE.get("text", "")
        S.PENDING_PROACTIVE = {}
        return text

    elif t == "PROACTIVE_DECLINE":
        # Issue #148: Catrin hat abgelehnt — Meldung per Telegram schicken und State leeren.
        # Nur senden wenn Telegram die Meldung noch nicht hat (telegram_sent=False).
        if not S.PENDING_PROACTIVE:
            return f"Keine ausstehende Meldung, {pick_address()}."
        text = S.PENDING_PROACTIVE.get("text", "")
        already_sent = S.PENDING_PROACTIVE.get("telegram_sent", False)
        S.PENDING_PROACTIVE = {}
        if not already_sent:
            import telegram_bot as _tg
            asyncio.create_task(_tg.send_user_text(text))
        return f"Sehr wohl, ich schicke es Ihnen auf das Telefon, {pick_address()}."

    elif t == "MAIL_FORWARD_SEND":
        # Issue #143 / #202: Leitet die aktive Mail an den gespeicherten
        # Empfaenger weiter. Unterstuetzt sowohl Legacy-Format (to_addr/to_name)
        # als auch das neue Kandidaten-Format (candidates/current_index).
        if not S.PENDING_MAIL_FORWARD:
            return (
                "Kein Empfaenger gespeichert. "
                "Bitte zuerst Empfaenger nennen."
            )
        fwd = S.PENDING_MAIL_FORWARD
        if "candidates" in fwd:
            idx = fwd.get("current_index", 0)
            candidates = fwd.get("candidates", [])
            if idx < len(candidates):
                c = candidates[idx]
                to_addr = c["to_addr"]
                to_name = c["to_name"]
            else:
                S.PENDING_MAIL_FORWARD = {}
                return "Kein bestaetigter Empfaenger, Madam."
        else:
            to_addr = fwd.get("to_addr", "")
            to_name = fwd.get("to_name", to_addr)
        if not to_addr:
            S.PENDING_MAIL_FORWARD = {}
            return "Gespeicherter Empfaenger hat keine E-Mail-Adresse. Bitte erneut angeben."
        # Aktive Mail aus dem Default-Slot lesen
        active = session_state.get("default").active_mail
        if not active:
            return "Keine aktive Mail im Kontext."
        ok = await mail_actions.forward_mail(active.account, active.uid, to_addr)
        S.PENDING_MAIL_FORWARD = {}
        session_state.clear_active_mail("default")
        if ok:
            return f"Mail weitergeleitet an {to_name} ({to_addr})."
        return f"Weiterleitung an {to_name} fehlgeschlagen — bitte SMTP-Konfiguration pruefen."

    elif t == "DEBUG_PDF":
        # Diagnose: zeigt gespeicherte PDFs + Extraktions-Details.
        import glob, re as _re2
        lines: list[str] = []
        pdf_files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "jarvis_pdfs", "*.pdf")), key=os.path.getmtime, reverse=True)
        lines.append(f"Gespeicherte PDFs: {len(pdf_files)}")
        for pf in pdf_files[:5]:
            lines.append(f"  • {os.path.basename(pf)}")
        query = p.strip()
        if query:
            import persons_db as _pdb2
            tax_list = _pdb2.get_tax_assessments(query)
            adv_list = _pdb2.get_advance_payments(query) if hasattr(_pdb2, "get_advance_payments") else []
            lines.append(f"persons_db '{query}': {len(tax_list)} Steuerbescheide, {len(adv_list)} Vorauszahlungen")
        if pdf_files:
            try:
                from pdf_tools import extract_text, _extract_local
                txt = extract_text(pdf_files[0])
                # Zeige relevante Zeilen (Identifikation, Steuernr, Mandant, Betrag)
                keywords = ("identifikation", "steuernummer", "st.-nr", "bescheid ergeht",
                            "herrn", "frau", "nachzahlung", "erstattung", "festgesetzt",
                            "verbleibende", "fällig", "zahlungstermin")
                relevant = [ln.strip() for ln in txt.splitlines()
                            if any(kw in ln.lower() for kw in keywords)][:25]
                lines.append(f"\nPDF-Text (relevante Zeilen aus {os.path.basename(pdf_files[0])}):")
                lines.extend(f"  {ln}" for ln in relevant)
                data = _extract_local(txt)
                lines.append(f"\nExtraktion: typ={data.get('typ')!r} mandant={data.get('mandant')!r} "
                             f"steuerart={data.get('steuerart')!r} jahr={data.get('steuerjahr')!r} "
                             f"betrag={data.get('betrag_eur')}")
            except Exception as exc2:
                lines.append(f"\nPDF-Text-Extraktion Fehler: {exc2}")
        return "\n".join(lines)

    elif t == "CLEAR_TAX_DATA":
        # Löscht alle gespeicherten Steuerbescheide + Vorauszahlungen für einen Mandanten.
        import persons_db as _pdb3
        mandant_q = p.strip()
        if not mandant_q:
            return f"Für wen soll ich die Steuerdaten löschen, {pick_address()}?"
        needle = _pdb3._norm(mandant_q)
        cleared = []
        _pdb3._load()
        for prof in _pdb3._persons.values():
            if prof.name and needle in _pdb3._norm(prof.name):
                n_ta = len(prof.tax_assessments)
                n_ap = len(prof.advance_payments)
                prof.tax_assessments.clear()
                prof.advance_payments.clear()
                if n_ta or n_ap:
                    cleared.append(f"{prof.name}: {n_ta} Bescheide, {n_ap} Vorauszahlungen gelöscht")
        if cleared:
            _pdb3._save()
            return "Gelöscht:\n" + "\n".join(cleared)
        return f"Keine Steuerdaten für '{mandant_q}' gefunden."

    elif t == "INSTALL_DEPS":
        # Installiert fehlende Python-Pakete im laufenden venv via sys.executable.
        # Issue #169: subprocess.run wird im Thread-Pool ausgefuehrt damit der
        # Event-Loop waehrend der Installation (bis zu 5 Minuten) nicht blockiert.
        pkg = (p.strip() or "pymupdf").lower()
        try:
            loop = asyncio.get_running_loop()
            result_proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg],
                    capture_output=True, text=True, timeout=300,
                ),
            )
            if result_proc.returncode == 0:
                last_line = [l for l in result_proc.stdout.splitlines() if l.strip()][-1] if result_proc.stdout.strip() else "OK"
                return f"Installation erfolgreich: {last_line}"
            return f"Installation fehlgeschlagen (exit {result_proc.returncode}):\n{result_proc.stderr[-500:]}"
        except subprocess.TimeoutExpired:
            return "Timeout nach 5 Minuten — bitte nochmal versuchen."
        except Exception as exc_inst:
            return f"Fehler: {exc_inst}"

    elif t == "ANALYZE_ALL_PDFS":
        # Verarbeitet alle gespeicherten PDFs in jarvis_pdfs/ nach.
        import glob
        from pdf_tools import analyze_steuerbescheid
        pdf_files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "jarvis_pdfs", "*.pdf")), key=os.path.getmtime)
        if not pdf_files:
            return "Keine PDFs in jarvis_pdfs/ gefunden."
        ok, failed, skipped = 0, 0, 0
        for path_i in pdf_files:
            try:
                result_i = await analyze_steuerbescheid(path_i)
                if result_i.get("typ") in ("Steuerbescheid", "Vorauszahlungsbescheid") and result_i.get("mandant"):
                    ok += 1
                elif result_i.get("typ") == "fehler":
                    failed += 1
                else:
                    skipped += 1
            except Exception as exc_i:
                log.warning("ANALYZE_ALL_PDFS: %s: %s", os.path.basename(path_i), exc_i)
                failed += 1
        return (f"{len(pdf_files)} PDFs verarbeitet: {ok} gespeichert, "
                f"{skipped} übersprungen (Typ unbekannt), {failed} Fehler.")

    elif t == "ANALYZE_PDF":
        # Issue #109: Steuerbescheid-Analyse via PyMuPDF + Claude Haiku.
        # payload = absoluter Pfad zur PDF-Datei.
        path = p.strip()
        if not path:
            return f"Kein PDF-Pfad angegeben, {pick_address()}."
        if not os.path.exists(path):
            return f"PDF nicht gefunden: {path}"
        try:
            from pdf_tools import analyze_steuerbescheid
            result = await analyze_steuerbescheid(path)
            return result.get("summary", f"PDF analysiert: {os.path.basename(path)}")
        except Exception as exc:
            log.warning("ANALYZE_PDF: Analyse fehlgeschlagen: %s: %s", path, exc)
            return f"PDF-Analyse fehlgeschlagen ({os.path.basename(path)}): {type(exc).__name__}"

    elif t == "MAIL_KNOWLEDGE_SEARCH":
        # Issue #161: Suche im passiven E-Mail-Wissenspeicher.
        # payload = Suchbegriff
        query = p.strip()
        if not query:
            return f"Bitte einen Suchbegriff angeben, {pick_address()}."
        from mail_intelligence import search_knowledge
        results = search_knowledge(query, limit=10)
        if not results:
            return f"Keine gespeicherten Informationen zu '{query}' gefunden."
        lines = []
        for r in results:
            sender_label = r.get("sender_name") or r.get("sender") or "Unbekannt"
            lines.append(
                f"• {r['content']} "
                f"(von {sender_label}, {r['mail_date']}, "
                f"Betreff: {r['subject']})"
            )
        return "\n".join(lines)

    elif t == "RETRIAGE_INBOX":
        import mail_actions as _ma
        import mail_triage as _mt
        account_name = p.strip()
        if account_name:
            accounts_to_process = [account_name]
        else:
            accounts_to_process = [a["name"] for a in S.MAIL_MONITOR_ACCOUNTS] or ["HILO"]
        log.info(
            f"RETRIAGE_INBOX: accounts={accounts_to_process} "
            f"rules={[r['name'] for r in _mt.TRIAGE_RULES]}"
        )
        total_moved, total_errors = 0, 0
        processed_folders: list[str] = []
        for rule in _mt.TRIAGE_RULES:
            for _acc_name in accounts_to_process:
                _acc_obj = next((a for a in S.MAIL_MONITOR_ACCOUNTS if a["name"] == _acc_name), None)
                # DHL-Ordner kann per Konto überschrieben werden; Amazon-Ordner nicht
                if rule["name"] == "Pakete":
                    _folder = (_acc_obj.get("dhl_folder") or rule["folder"]) if _acc_obj else rule["folder"]
                else:
                    _folder = rule["folder"]
                log.info(f"RETRIAGE_INBOX: {_acc_name!r} rule={rule['name']!r} -> {_folder!r}")
                _m, _e = await _ma.retriage_inbox(_acc_name, _folder, rule["domains"])
                total_moved += _m
                total_errors += _e
            if rule["folder"] not in processed_folders:
                processed_folders.append(rule["folder"])
        if total_moved == 0 and total_errors == 0:
            folders_str = ", ".join(processed_folders)
            return f"Keine Mails zum Sortieren gefunden ({', '.join(accounts_to_process)}, Ordner: {folders_str})."
        parts = [f"{total_moved} Mail(s) sortiert ({', '.join(processed_folders)})"]
        if total_errors:
            parts.append(f"{total_errors} Fehler")
        return f"{', '.join(parts)} ({', '.join(accounts_to_process)})."

    elif t == "MAIL_KNOWLEDGE_RECENT":
        # Issue #161: Neueste Einträge im E-Mail-Wissenspeicher.
        # payload = Anzahl Tage (default 7)
        days = int(p.strip()) if p.strip().isdigit() else 7
        from mail_intelligence import get_recent_knowledge
        results = get_recent_knowledge(days=days, limit=20)
        if not results:
            return f"Keine gespeicherten E-Mail-Informationen der letzten {days} Tage."
        lines = []
        for r in results:
            sender_label = r.get("sender_name") or r.get("sender") or "Unbekannt"
            lines.append(
                f"• [{r['account']}] {r['raw_summary']} "
                f"({sender_label}, {r['mail_date']})"
            )
        return "\n".join(lines)

    elif t == "MANDANTEN_OVERVIEW":
        # Issue #176: Alle Mandanten mit gespeicherten Steuerbescheiden oder
        # Vorauszahlungen als HTML-Tabellenkachel anzeigen.
        import persons_db as _pdb_ov

        # Alle Profile durchsuchen und nur solche mit Steuerdaten sammeln.
        mandanten: list[dict] = []
        for prof in _pdb_ov.all_profiles():
            ta_list = list(getattr(prof, "tax_assessments", []))
            ap_list = list(getattr(prof, "advance_payments", []))
            if not ta_list and not ap_list:
                continue

            # Neuesten Steuerbescheid ermitteln (nach steuerjahr desc).
            latest_ta: dict | None = None
            if ta_list:
                latest_ta = max(ta_list, key=lambda x: str(x.get("steuerjahr", "")))

            # Nächsten Zahlungstermin aus Bescheiden extrahieren (frühestes Datum).
            next_faellig: str | None = None
            for ta in ta_list:
                faellig = (ta.get("zahlungstermin") or "").strip()
                if faellig and faellig != "null":
                    if next_faellig is None or faellig < next_faellig:
                        next_faellig = faellig

            mandanten.append({
                "name": prof.name,
                "latest_ta": latest_ta,
                "next_faellig": next_faellig,
                "n_ta": len(ta_list),
                "n_ap": len(ap_list),
            })

        if not mandanten:
            spoken = (
                f"Es sind noch keine Steuerbescheide oder Vorauszahlungen gespeichert, "
                f"{pick_address()}. Analysieren Sie zunächst ein PDF mit "
                f"'Analysiere das PDF'."
            )
            return spoken

        # Sortieren: Fälligkeit aufsteigend, None-Werte ans Ende.
        mandanten.sort(
            key=lambda m: (m["next_faellig"] is None, m["next_faellig"] or "")
        )

        def _esc(s: str) -> str:
            return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _betrag_cell(ta: dict) -> str:
            betrag = ta.get("betrag_eur")
            if betrag is None:
                return "—"
            try:
                b = float(betrag)
                richtung = "Erstattung" if b >= 0 else "Nachzahlung"
                s = f"{abs(b):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
                return _esc(f"{richtung} {s}")
            except (ValueError, TypeError):
                return _esc(f"{betrag} €")

        rows_html = ""
        for m in mandanten:
            ta = m["latest_ta"]
            if ta:
                bescheid_str = _esc(f"{ta.get('steuerart', '?')} {ta.get('steuerjahr', '?')}")
                betrag_str = _betrag_cell(ta)
            else:
                bescheid_str = "—"
                betrag_str = "—"

            faellig_raw = m["next_faellig"] or ""
            faellig_str = _esc(_fmt_date(faellig_raw)) if faellig_raw else "—"

            # Fälligkeits-Farbe: rot wenn überfällig, orange wenn < 30 Tage.
            faellig_class = ""
            if faellig_raw and faellig_raw != "null":
                try:
                    from datetime import date as _date
                    _today = _date.today().isoformat()
                    _delta_days = (
                        _date.fromisoformat(faellig_raw) - _date.fromisoformat(_today)
                    ).days
                    if _delta_days < 0:
                        faellig_class = ' style="color:#e74c3c;font-weight:bold"'
                    elif _delta_days <= 30:
                        faellig_class = ' style="color:#e67e22;font-weight:bold"'
                except Exception:
                    pass

            rows_html += (
                f"<tr>"
                f"<td>{_esc(m['name'])}</td>"
                f"<td>{bescheid_str}</td>"
                f"<td>{betrag_str}</td>"
                f"<td{faellig_class}>{faellig_str}</td>"
                f"</tr>"
            )

        n = len(mandanten)
        card_html = (
            '<div class="p-card">'
            '<div class="p-card-name">Mandanten-Übersicht</div>'
            f'<div class="p-card-meta">'
            f'<span class="p-badge">{n} Mandant{"en" if n != 1 else ""}</span>'
            f'</div>'
            '<div class="p-card-section">'
            '<table style="width:100%;border-collapse:collapse;font-size:0.9em">'
            '<thead><tr style="border-bottom:1px solid #555">'
            "<th style=\"text-align:left;padding:4px 6px\">Mandant</th>"
            "<th style=\"text-align:left;padding:4px 6px\">Letzter Bescheid</th>"
            "<th style=\"text-align:left;padding:4px 6px\">Betrag</th>"
            "<th style=\"text-align:left;padding:4px 6px\">Fälligkeit</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            "</table>"
            "</div>"
            "</div>"
        )
        S.PENDING_CARD_HTML = card_html
        return (
            f"Hier ist die Mandanten-Übersicht, {pick_address()}. "
            f"{n} Mandant{'en' if n != 1 else ''} mit gespeicherten Steuerdaten."
        )

    elif t == "JARVIS_UPDATE":
        # Issue #186: Sprachbefehl "Update dich" / "Aktualisiere dich"
        # git pull + pip install in run_in_executor (non-blocking), dann systemctl restart.
        # Issue #188: Commit-Status nach dem Update per Telegram melden.
        _update_address = pick_address()  # einmal ziehen, konsistent in Vor- und Nachricht

        async def _do_update() -> None:
            await asyncio.sleep(4)  # TTS-Puffer bevor Neustart
            project_dir = os.path.dirname(os.path.abspath(__file__))
            _loop = asyncio.get_event_loop()

            # Hash VOR dem Pull merken
            try:
                _pre = await _loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["git", "-C", project_dir, "log", "--oneline", "-1"],
                        capture_output=True, text=True, timeout=15,
                    ),
                )
                _hash_before = _pre.stdout.strip().split()[0] if _pre.returncode == 0 and _pre.stdout.strip() else ""
            except Exception as _ue:
                log.warning("JARVIS_UPDATE git log (pre): %s", _ue)
                _hash_before = ""

            # git pull
            _pull_ok = False
            _pull_up_to_date = False
            _pull_error = ""
            try:
                _res = await _loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["git", "-C", project_dir, "pull"],
                        capture_output=True, text=True, timeout=60,
                    ),
                )
                if _res.returncode == 0:
                    _pull_ok = True
                    _pull_up_to_date = "already up to date" in _res.stdout.lower()
                else:
                    _pull_error = (_res.stderr or _res.stdout or "unbekannter Fehler").strip()
            except Exception as _ue:
                log.warning("JARVIS_UPDATE git pull: %s", _ue)
                _pull_error = str(_ue)

            # Hash NACH dem Pull + Datum ermitteln (nur bei neuem Stand)
            _version_msg = ""
            if _pull_ok and not _pull_up_to_date:
                try:
                    _post = await _loop.run_in_executor(
                        None,
                        lambda: subprocess.run(
                            ["git", "-C", project_dir, "log", "--oneline", "-1",
                             "--format=%h %cd", "--date=format:%d.%m.%Y"],
                            capture_output=True, text=True, timeout=15,
                        ),
                    )
                    if _post.returncode == 0 and _post.stdout.strip():
                        _parts = _post.stdout.strip().split(None, 1)
                        _new_hash = _parts[0]
                        _new_date = _parts[1] if len(_parts) > 1 else ""
                        if _new_hash != _hash_before:
                            _version_msg = (
                                f"Version vom {_new_date}, Commit {_new_hash} ist aktiv."
                                if _new_date else f"Commit {_new_hash} ist aktiv."
                            )
                except Exception as _ue:
                    log.warning("JARVIS_UPDATE git log (post): %s", _ue)

            # Telegram-Folgenachricht senden
            try:
                import telegram_bot as _tb_upd
                if not _pull_ok:
                    _follow_up = (
                        f"Aktualisierung fehlgeschlagen: {_pull_error}. "
                        f"Ich starte mich trotzdem neu."
                    )
                elif _pull_up_to_date:
                    _follow_up = (
                        f"Ich bin bereits auf dem neuesten Stand. "
                        f"Ich starte mich trotzdem neu — bis gleich, {_update_address}."
                    )
                else:
                    _follow_up = (
                        f"Ich lade die neueste Version — bis gleich, {_update_address}. "
                        + (_version_msg if _version_msg else "")
                    ).rstrip()
                await _tb_upd.send_user_text(_follow_up)
            except Exception as _ue:
                log.warning("JARVIS_UPDATE telegram follow-up: %s", _ue)

            # pip install
            try:
                await _loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r",
                         os.path.join(project_dir, "requirements.txt")],
                        capture_output=True, timeout=300,
                    ),
                )
            except Exception as _ue:
                log.warning("JARVIS_UPDATE pip install: %s", _ue)
            try:
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", "jarvis"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as _ue:
                log.warning("JARVIS_UPDATE restart: %s", _ue)

        asyncio.create_task(_do_update())
        return f"Ich lade die neueste Version und starte mich neu — bis gleich, {_update_address}."

    elif t == "JARVIS_VERSION":
        # Issue #190: Aktuellen git-Commit-Hash + Datum synchron abfragen.
        # subprocess.run ist hier unbedenklich — der Aufruf dauert < 100 ms
        # und blockiert den Event-Loop nicht nennenswert.
        project_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            _res = subprocess.run(
                ["git", "-C", project_dir, "log", "-1",
                 "--format=%h %cd", "--date=format:%d.%m.%Y"],
                capture_output=True, text=True, timeout=5,
            )
            if _res.returncode == 0 and _res.stdout.strip():
                _parts = _res.stdout.strip().split(None, 1)
                _hash = _parts[0]
                _date = _parts[1] if len(_parts) > 1 else ""
                _version = f"Commit {_hash} vom {_date}" if _date else f"Commit {_hash}"
                return f"Ich laufe auf {_version}, {pick_address()}."
        except Exception as _ve:
            log.warning("JARVIS_VERSION git log: %s", _ve)
        return f"Die Versionsinfo ist leider nicht verfügbar, {pick_address()}."

    return ""
