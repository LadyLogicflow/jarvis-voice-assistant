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
from prompt import llm_text

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
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=150,
            system=_SYNTHESIS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        return llm_text(resp).strip()
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

    return await _synthesize(context_data)
