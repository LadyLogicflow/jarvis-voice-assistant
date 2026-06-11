"""
Kontextuelle Personenverknuepfung fuer Red-Mail-Benachrichtigungen (Issue #163).

Wenn JARVIS eine "rote Mail" (reply_needed=True) meldet, wird parallel aus
allen verfuegbaren Datenquellen ein kompaktes Kontext-Briefing zusammengestellt
und als Zusatztext an die Benachrichtigung angehaengt.

Datenquellen (alle parallel abgefragt):
1. persons_db  — Profil, Notizen, offene Punkte, Steuerbescheide
2. mail_intelligence — Mail-Wissen der letzten 6 Monate nach Absender
3. Google Calendar  — Vergangene + kuenftige Termine mit dieser Person
4. Todoist          — Offene (und kuerzlich abgeschlossene) Aufgaben

Ausgabe: max. 2-3 Saetze Deutsch, TTS-tauglich.
         Kein Output wenn keine Datenquelle etwas liefert (unbekannte Person).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import settings as S
from prompt import llm_text, call_llm

log = logging.getLogger("jarvis.person_context")

# ---------------------------------------------------------------------------
# Lazy module references — imported at function-call time so missing optional
# dependencies (google_calendar_tools, todoist_tools) don't break server
# startup. Tests patch these via sys.modules.
# ---------------------------------------------------------------------------
try:
    import persons_db
except ImportError:
    persons_db = None  # type: ignore[assignment]

try:
    import mail_intelligence
except ImportError:
    mail_intelligence = None  # type: ignore[assignment]

try:
    import google_calendar_tools
except ImportError:
    google_calendar_tools = None  # type: ignore[assignment]

try:
    import todoist_tools
except ImportError:
    todoist_tools = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Einzelne Datenquellen
# ---------------------------------------------------------------------------

def _query_persons_db(sender_email: str, sender_name: str) -> Optional[dict]:
    """Gibt das PersonProfile-Dict fuer den Absender zurueck oder None.

    Sucht zuerst per E-Mail, dann per Name (Teilstring).

    Args:
        sender_email: Normalisierte Absender-Adresse.
        sender_name: Anzeigename des Absenders.

    Returns:
        Dict mit Profildaten oder None wenn kein Profil gefunden.
    """
    try:
        if persons_db is None:
            return None
        profile = None
        if sender_email:
            profile = persons_db.find_by_email(sender_email)
        if profile is None and sender_name:
            # Erster Treffer genuegt fuer den Kontext
            matches = persons_db.search_by_name(sender_name)
            if matches:
                profile = matches[0]
        if profile is None:
            return None
        return {
            "name": profile.name,
            "funktion": profile.funktion,
            "anrede": profile.anrede,
            "last_contact": profile.last_contact,
            "notes": profile.notes[-3:] if profile.notes else [],
            "open_points": profile.open_points[-3:] if profile.open_points else [],
            "tax_assessments": profile.tax_assessments[-2:] if profile.tax_assessments else [],
        }
    except Exception as exc:
        log.debug("person_context: persons_db query failed: %s: %s", type(exc).__name__, exc)
        return None


def _query_mail_knowledge(sender_email: str, sender_name: str) -> list[dict]:
    """Liefert Mail-Wissenseintraege der letzten 180 Tage fuer den Absender.

    Sucht zuerst per E-Mail-Adresse, dann per Name wenn keine Treffer.

    Args:
        sender_email: Normalisierte Absender-Adresse.
        sender_name: Anzeigename des Absenders.

    Returns:
        Liste von Row-Dicts aus mail_knowledge (max. 10), leer bei Fehler.
    """
    try:
        if mail_intelligence is None:
            return []
        rows: list[dict] = []
        if sender_email:
            rows = mail_intelligence.search_knowledge(query=sender_email, limit=10)
            # Nur Eintraege die wirklich vom gesuchten Absender stammen
            rows = [r for r in rows if sender_email.lower() in (r.get("sender") or "").lower()]
        if not rows and sender_name:
            rows = mail_intelligence.search_knowledge(query=sender_name, limit=10)
        # Auf 180-Tage-Fenster filtern
        if rows:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
            rows = [r for r in rows if (r.get("mail_date") or "0000-00-00") >= cutoff]
        return rows[:10]
    except Exception as exc:
        log.debug("person_context: mail_knowledge query failed: %s: %s", type(exc).__name__, exc)
        return []


async def _query_calendar(sender_name: str) -> list[str]:
    """Liefert Kalender-Termine der letzten + naechsten 6 Monate die den
    Absendernamen erwaehnen.

    Args:
        sender_name: Anzeigename des Absenders als Such-Substring.

    Returns:
        Liste von lesbaren Termin-Strings, leer bei Fehler oder kein Treffer.
    """
    if not sender_name:
        return []
    try:
        if google_calendar_tools is None:
            return []
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        time_min = now - timedelta(days=180)
        time_max = now + timedelta(days=180)
        # _fetch_events liefert einen formatierten String — wir brauchen Rohzeilen
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            google_calendar_tools._fetch_events,
            0,               # days arg (unused when time_min/time_max gegeben)
            50,              # max_results
            time_min,
            time_max,
        )
        if not result or ("nicht erreichbar" in result):
            return []
        if result == "KEINE_TERMINE":
            return []
        # Zeilen filtern nach Vorkommen des Namens
        needle = sender_name.lower()
        matching = [
            line.strip()
            for line in result.splitlines()
            if needle in line.lower() and line.strip().startswith("•")
        ]
        return matching[:5]
    except Exception as exc:
        log.debug("person_context: calendar query failed: %s: %s", type(exc).__name__, exc)
        return []


async def _query_todoist(sender_name: str) -> list[str]:
    """Liefert offene Todoist-Aufgaben die den Absendernamen erwaehnen.

    Args:
        sender_name: Anzeigename des Absenders als Such-Substring.

    Returns:
        Liste von Aufgaben-Titeln (max. 5), leer bei Fehler oder kein Treffer.
    """
    if not sender_name or not S.TODOIST_TOKEN:
        return []
    try:
        if todoist_tools is None:
            return []
        result = await todoist_tools.get_tasks(token=S.TODOIST_TOKEN, max_tasks=50)
        if not result or result in ("KEINE_TASKS", "") or result.startswith("Todoist nicht"):
            return []
        needle = sender_name.lower()
        matching = [
            line.strip().lstrip("• ")
            for line in result.splitlines()
            if needle in line.lower() and line.strip().startswith("•")
        ]
        return matching[:5]
    except Exception as exc:
        log.debug("person_context: todoist query failed: %s: %s", type(exc).__name__, exc)
        return []


# ---------------------------------------------------------------------------
# Strukturierte Mail-History-Formatierung (Issue #246)
# ---------------------------------------------------------------------------

def _parse_mail_date(raw_date: str) -> str:
    """Konvertiert ein ISO-Datum (YYYY-MM-DD oder YYYY-MM-DDTHH:MM:SS) in DD.MM.YYYY.

    Gibt den Rohwert zurueck wenn das Format nicht erkannt wird.

    Args:
        raw_date: Datumsstring aus der Datenbank.

    Returns:
        Formatierter Datumsstring DD.MM.YYYY oder Rohwert bei unbekanntem Format.
    """
    if not raw_date:
        return "—"
    # Nur die ersten 10 Zeichen (YYYY-MM-DD) benutzen
    date_part = raw_date[:10]
    if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
        try:
            year, month, day = date_part.split("-")
            return f"{int(day):02d}.{int(month):02d}.{year}"
        except (ValueError, AttributeError):
            pass
    return raw_date


def _format_mail_history(
    mail_rows: list[dict],
    sender_name: str,
    include_header: bool = True,
) -> str:
    """Formatiert die letzten 3 Mails als strukturierte Liste mit Datum und Stichworten.

    Ersetzt den LLM-Fliesstext fuer den visuellen Kontext-Block (Telegram/Webfrontend).
    Kein LLM-Aufruf, kein TTS — reine Formatierung.

    Args:
        mail_rows: Liste von Row-Dicts aus mail_knowledge (Felder: mail_date,
                   subject, raw_summary, content). Kann leer sein.
        sender_name: Anzeigename des Absenders fuer den Header.
        include_header: Wenn True (default), wird der ``📋 Name – letzte Mails:``
                        Header vorangestellt. Wenn False, werden nur die
                        ``• Datum — ...`` Eintragszeilen zurueckgegeben
                        (fuer Einbettung in _format_full_context).

    Returns:
        Formatierter String mit max. 3 Mail-Eintraegen oder leerer String.

    Example:
        >>> rows = [{"mail_date": "2026-06-11", "subject": "Projekt ok",
        ...          "raw_summary": "Verkauf nach Freigabe bestätigt"}]
        >>> _format_mail_history(rows, "Thomas Ulbrich")
        '📋 Thomas Ulbrich – letzte Mails:\\n• 11.06.2026 — Projekt ok: Verkauf nach Freigabe bestätigt'
    """
    if not mail_rows:
        return ""

    header_name = sender_name.strip() if sender_name else "Kontakt"
    lines: list[str] = []
    if include_header:
        lines.append(f"\U0001f4cb {header_name} – letzte Mails:")

    for row in mail_rows[:3]:
        # Datum: YYYY-MM-DD -> DD.MM.YYYY
        raw_date = row.get("mail_date") or ""
        formatted_date = _parse_mail_date(raw_date)

        subject = (row.get("subject") or "").strip()
        # Kurzinhalt: raw_summary bevorzugt, sonst content (gekuerzt)
        short_content = (row.get("raw_summary") or row.get("content") or "").strip()
        # Auf max. 80 Zeichen kuerzen (77 Zeichen + "..." = 80)
        if len(short_content) > 80:
            short_content = short_content[:77].rstrip() + "..."

        if subject and short_content:
            line = f"• {formatted_date} — {subject}: {short_content}"
        elif subject:
            line = f"• {formatted_date} — {subject}"
        elif short_content:
            line = f"• {formatted_date} — {short_content}"
        else:
            # Kein Betreff, kein Inhalt: Zeile trotzdem ausgeben (kein Crash)
            line = f"• {formatted_date} — (kein Betreff)"

        lines.append(line)

    return "\n".join(lines)


def _format_full_context(context_data: dict) -> str:
    """Baut einen vollstaendigen strukturierten Kontext-Block aus allen Quellen.

    Nur Sektionen mit tatsaechlichen Daten werden ausgegeben. Wenn gar keine
    Daten vorhanden sind, wird ein leerer String zurueckgegeben.

    Args:
        context_data: Gesammelte Daten aus allen Quellen mit den Schluesseln:
                      - ``profile``: Dict mit Profilfeldern (name, funktion,
                        open_points) oder None.
                      - ``mail_rows``: Liste von Mail-Dicts.
                      - ``calendar_entries``: Liste von Termin-Strings.
                      - ``todoist_tasks``: Liste von Aufgaben-Strings.
                      - ``sender_name``: Anzeigename des Absenders.

    Returns:
        Mehrzeiliger String mit allen vorhandenen Sektionen oder leerer String.

    Example::

        👤 Thomas Ulbrich — Direktionsleitung
        📋 Letzte Mails:
          • 11.06.2026 — Rueckmeldung Herr Bosch: Projekt ok
        📅 Termine:
          • 15.06.2026 — Besprechung Jahresabschluss
        ✅ Offene Aufgaben:
          • Steuererklärung 2024 prüfen
        📌 Offene Punkte:
          • Vorauszahlung Q3 noch offen
    """
    profile = context_data.get("profile")
    mail_rows = context_data.get("mail_rows", [])
    calendar_entries = context_data.get("calendar_entries", [])
    todoist_tasks = context_data.get("todoist_tasks", [])
    sender_name = (context_data.get("sender_name") or "").strip()

    # --- Mail-Verlauf ---
    body_sections: list[str] = []

    if mail_rows:
        mail_lines = _format_mail_history(mail_rows, sender_name, include_header=False)
        if mail_lines:
            body_sections.append("\U0001f4cb Letzte Mails:")
            # Einrueckung: 2 Leerzeichen vor jedem Eintrag
            indented = "\n".join(f"  {l}" for l in mail_lines.splitlines())
            body_sections.append(indented)

    # --- Kalender-Termine ---
    if calendar_entries:
        body_sections.append("\U0001f4c5 Termine:")
        for entry in calendar_entries[:3]:
            body_sections.append(f"  • {entry}")

    # --- Todoist-Aufgaben ---
    if todoist_tasks:
        body_sections.append("✅ Offene Aufgaben:")
        for task in todoist_tasks[:3]:
            body_sections.append(f"  • {task}")

    # --- Offene Punkte aus Profil ---
    if profile and profile.get("open_points"):
        body_sections.append("\U0001f4cc Offene Punkte:")
        for point in profile["open_points"][:3]:
            body_sections.append(f"  • {point}")

    # Header-Zeile nur ausgeben wenn tatsaechlich Daten oder Profil vorhanden
    has_data = bool(body_sections) or profile is not None
    if not has_data:
        return ""

    header_parts: list[str] = []
    if profile:
        name = (profile.get("name") or sender_name or "").strip()
        funktion = (profile.get("funktion") or "").strip()
        if name and funktion:
            header_parts.append(f"\U0001f464 {name} — {funktion}")
        elif name:
            header_parts.append(f"\U0001f464 {name}")
        elif sender_name:
            header_parts.append(f"\U0001f464 {sender_name}")
    elif sender_name and body_sections:
        # sender_name ohne Profil nur als Header wenn es Daten-Sektionen gibt
        header_parts.append(f"\U0001f464 {sender_name}")

    all_sections = header_parts + body_sections
    if not all_sections:
        return ""

    return "\n".join(all_sections)


# ---------------------------------------------------------------------------
# Synthese-Prompt
# ---------------------------------------------------------------------------

_SYNTHESIS_PROMPT = (
    "Du bist Jarvis, ein persoenlicher Butler-Assistent fuer eine Steuerberaterin. "
    "Fasse die folgenden Informationen ueber eine Person in maximal 2-3 Saetzen auf Deutsch zusammen. "
    "Der Text wird vorgelesen (TTS) — keine Listen, keine Sonderzeichen, natuerliche Sprache. "
    "Nenne nur die relevantesten Punkte: offene Aufgaben, anstehende Termine, letzte Kommunikation. "
    "Wenn die Informationen zu duerftig sind, schreibe nichts (leere Antwort). "
    "Beginne direkt mit dem Inhalt, keine Einleitung wie 'Hier sind die Informationen'."
)


async def _synthesize(context_data: dict) -> str:
    """Generiert einen kurzen deutschen Kontext-Text aus den gesammelten Daten.

    Args:
        context_data: Gesammelte Daten aus allen Quellen (profile, mail_rows,
                      calendar_entries, todoist_tasks).

    Returns:
        Synthesierter Text (max. 2-3 Saetze) oder leerer String.
    """
    profile = context_data.get("profile")
    mail_rows = context_data.get("mail_rows", [])
    calendar_entries = context_data.get("calendar_entries", [])
    todoist_tasks = context_data.get("todoist_tasks", [])

    # Pruefe ob irgendetwas vorhanden ist
    has_data = (
        profile is not None
        or bool(mail_rows)
        or bool(calendar_entries)
        or bool(todoist_tasks)
    )
    if not has_data:
        return ""

    # Kontext-Block aufbauen
    parts: list[str] = []

    # Absendername als Anker für Haiku — auch wenn kein Profil gefunden wurde
    sender_name = context_data.get("sender_name", "")
    if sender_name:
        parts.append(f"Person: {sender_name}")

    if profile:
        name = profile.get("name", "")
        funktion = profile.get("funktion", "")
        last_contact = profile.get("last_contact", "")
        if funktion:
            parts.append(f"Funktion: {funktion}")
        if last_contact:
            parts.append(f"Letzter Kontakt: {last_contact}")
        if profile.get("open_points"):
            pts = "; ".join(profile["open_points"][:3])
            parts.append(f"Offene Punkte: {pts}")
        if profile.get("notes"):
            notes = "; ".join(profile["notes"][:2])
            parts.append(f"Notizen: {notes}")

    if mail_rows:
        summaries = [
            r.get("raw_summary") or r.get("content") or ""
            for r in mail_rows[:3]
        ]
        summaries = [s[:120] for s in summaries if s]
        if summaries:
            parts.append("Mail-Wissen: " + " | ".join(summaries))

    if calendar_entries:
        parts.append("Kalender-Termine: " + "; ".join(calendar_entries[:3]))

    if todoist_tasks:
        parts.append("Offene Aufgaben: " + "; ".join(todoist_tasks[:3]))

    if not parts:
        return ""

    user_msg = "\n".join(parts)
    try:
        return await call_llm(_SYNTHESIS_PROMPT, user_msg, max_tokens=150)
    except Exception as exc:
        log.warning(
            "person_context: synthesis LLM call failed: %s: %s",
            type(exc).__name__, exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Oeffentliche API
# ---------------------------------------------------------------------------

async def enrich_mail_with_person_context(
    sender_email: str,
    sender_name: str,
) -> str:
    """Sammelt Kontext-Informationen fuer den Absender aus allen Quellen parallel
    und liefert einen kurzen deutschen Text fuer die Mail-Benachrichtigung.

    Wird nur bei reply_needed=True-Mails aufgerufen. Alle Quellen-Fehler werden
    still uebersprungen. Wenn keine Quelle Daten liefert, wird ein leerer String
    zurueckgegeben (kein Output).

    Args:
        sender_email: Normalisierte Absender-E-Mail-Adresse (darf leer sein).
        sender_name: Anzeigename des Absenders (darf leer sein).

    Returns:
        Synthese-Text (max. 2-3 Saetze) oder leerer String wenn unbekannte Person.
    """
    if not sender_email and not sender_name:
        return ""

    # Alle Quellen parallel abfragen
    loop = asyncio.get_running_loop()

    persons_task = loop.run_in_executor(
        None, _query_persons_db, sender_email, sender_name
    )
    mail_task = loop.run_in_executor(
        None, _query_mail_knowledge, sender_email, sender_name
    )
    calendar_coro = _query_calendar(sender_name)
    todoist_coro = _query_todoist(sender_name)

    results = await asyncio.gather(
        persons_task,
        mail_task,
        calendar_coro,
        todoist_coro,
        return_exceptions=True,
    )

    profile = results[0] if not isinstance(results[0], BaseException) else None
    mail_rows = results[1] if not isinstance(results[1], BaseException) else []
    calendar_entries = results[2] if not isinstance(results[2], BaseException) else []
    todoist_tasks = results[3] if not isinstance(results[3], BaseException) else []

    # Bei Exceptions einzelner Quellen: warnen und leer weiterarbeiten
    for i, label in enumerate(["persons_db", "mail_knowledge", "calendar", "todoist"]):
        if isinstance(results[i], BaseException):
            log.debug(
                "person_context[%s]: source failed gracefully: %s: %s",
                label, type(results[i]).__name__, results[i],
            )

    context_data = {
        "profile": profile,
        "mail_rows": mail_rows,
        "calendar_entries": calendar_entries,
        "todoist_tasks": todoist_tasks,
        "sender_name": sender_name,
    }

    # Visueller Kontext-Block: strukturierter Gesamt-Block aus allen Quellen (#247)
    # Profil, Mail-Verlauf, Kalender und Todoist werden gemeinsam angezeigt.
    # Nur Sektionen mit tatsaechlichen Daten erscheinen.
    # Fuer TTS-Ausgabe bleibt _synthesize() weiterhin verfuegbar
    # (wird bei Bedarf von mail_monitor.py direkt aufgerufen).
    full_context = _format_full_context(context_data)
    if full_context:
        return full_context

    # Fallback auf LLM-Synthese wenn keine strukturierten Daten vorhanden
    return await _synthesize(context_data)
