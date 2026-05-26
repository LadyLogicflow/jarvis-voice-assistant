"""
Proaktives Gedaechtnis — Promise Tracker (Issue #117).

Speichert "offene Vorhaben" aus Gespraechen (z.B. "Ich muss noch X machen")
in einer SQLite-Tabelle und fragt nach 1-2 Tagen nach ob sie erledigt sind.

API:
  has_obligation_markers(text) -> bool
      Schnell-Check: enthaelt der Text moegliche Vorhaben-Trigger?

  extract_promises(text) -> list[str]
      Laedt Claude Haiku, um offene Vorhaben aus einem Gespraechstext zu
      extrahieren. Nur aufrufen wenn has_obligation_markers() True ist.

  save_promise(text, source) -> None
      Speichert ein Vorhaben in der DB.

  get_open_promises(max_age_days) -> list[dict]
      Gibt offene (nicht erledigte) Vorhaben zurueck, die juenger als
      max_age_days sind.

  mark_promise_done(promise_id) -> None
      Markiert ein Vorhaben als erledigt.
"""

from __future__ import annotations

import datetime
import os
import re

import aiosqlite

import settings as S

log = S.log

# DB liegt neben der bestehenden History-Datei im Projekt-Root.
_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_promises.db")

# Schnell-Filter: nur wenn mindestens eines dieser Woerter im Text steht,
# lohnt sich der teurere Claude-Aufruf.
_OBLIGATION_PATTERN = re.compile(
    r"\b(muss|soll|sollte|wollte|werde|vergiss\s+nicht|merk\s+dir|"
    r"erinnere\s+mich|habe\s+noch|noch\s+zu\s+erledigen|noch\s+offen)\b",
    re.IGNORECASE,
)


async def _ensure_db() -> None:
    """Legt die Tabelle an falls sie noch nicht existiert."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promises (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                text     TEXT    NOT NULL,
                source   TEXT    NOT NULL DEFAULT 'conversation',
                created  TEXT    NOT NULL,
                done     INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()


def has_obligation_markers(text: str) -> bool:
    """Schnell-Check ohne LLM-Aufruf. True wenn der Text moeglicherweise
    ein offenes Vorhaben enthaelt."""
    return bool(_OBLIGATION_PATTERN.search(text or ""))


async def extract_promises(text: str) -> list[str]:
    """Ruft Claude Haiku auf, um offene Vorhaben aus text zu extrahieren.

    Gibt eine (moeglicherweise leere) Liste von Vorhaben-Strings zurueck.
    Empfehlung: nur aufrufen wenn has_obligation_markers() True ist.
    """
    if not text or not text.strip():
        return []
    sys_prompt = (
        "Du bist ein Extraktor fuer persoenliche Vorhaben. "
        "Aus dem folgenden deutschen Gespraechstext extrahiere AUSSCHLIESSLICH "
        "konkrete, unerledigte Vorhaben der sprechenden Person "
        "(Phrasen wie 'Ich muss noch X', 'Ich wollte noch Y', "
        "'Vergiss nicht Z', 'Ich muss X noch erledigen'). "
        "Ignoriere: Aufgaben die bereits erledigt klingen, Bitten an andere "
        "Personen, allgemeine Ueberlegungen ohne Handlungscharakter. "
        "ANTWORTE NUR mit einer Zeile pro Vorhaben. "
        "Keine Nummerierung, keine Aufzaehlungszeichen, keine Erklaerungen. "
        "Wenn kein konkretes Vorhaben erkannt wird: antworte mit 'KEINE'."
    )
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=sys_prompt,
            messages=[{"role": "user", "content": text[:2000]}],
        )
        raw = ""
        if resp and resp.content:
            raw = (resp.content[0].text or "").strip()
        if not raw or raw.upper() == "KEINE":
            return []
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        # Filtere Zeilen die nur "KEINE" oder aehnlich lauten
        return [l for l in lines if l.upper() not in ("KEINE", "NONE", "-")]
    except Exception as e:
        log.warning(f"extract_promises failed: {type(e).__name__}: {e}")
        return []


async def save_promise(text: str, source: str = "conversation") -> None:
    """Speichert ein Vorhaben in der DB wenn es kein Duplikat ist.

    Duplikat-Pruefung: identischer Text in den letzten 24h -> nicht erneut
    speichern.
    """
    text = text.strip()
    if not text:
        return
    await _ensure_db()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    async with aiosqlite.connect(_DB_PATH) as db:
        # Duplikat-Pruefung
        async with db.execute(
            "SELECT id FROM promises WHERE text = ? AND created > ? AND done = 0",
            (text, cutoff),
        ) as cur:
            row = await cur.fetchone()
        if row:
            log.debug(f"promise_tracker: Duplikat uebersprungen: {text[:60]}")
            return
        await db.execute(
            "INSERT INTO promises (text, source, created) VALUES (?, ?, ?)",
            (text, source, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        await db.commit()
    log.info(f"promise_tracker: gespeichert: {text[:80]}")


async def get_open_promises(max_age_days: int = 3) -> list[dict]:
    """Liefert offene (unerledigte) Vorhaben die juenger als max_age_days sind.

    Jeder Eintrag ist ein dict mit: id, text, source, created, age_label.
    age_label ist z.B. 'gestern' oder 'vor 2 Tagen'.
    """
    await _ensure_db()
    cutoff = (
        datetime.datetime.now() - datetime.timedelta(days=max_age_days)
    ).isoformat(timespec="seconds")
    now = datetime.datetime.now()
    rows: list[dict] = []
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, source, created FROM promises "
            "WHERE done = 0 AND created > ? "
            "ORDER BY created ASC",
            (cutoff,),
        ) as cur:
            async for row in cur:
                promise_id, text, source, created_str = row
                try:
                    created_dt = datetime.datetime.fromisoformat(created_str)
                    delta = now - created_dt
                    days = delta.days
                    if days == 0:
                        age_label = "heute"
                    elif days == 1:
                        age_label = "gestern"
                    else:
                        age_label = f"vor {days} Tagen"
                except Exception:
                    age_label = "kuerzlich"
                rows.append(
                    {
                        "id": promise_id,
                        "text": text,
                        "source": source,
                        "created": created_str,
                        "age_label": age_label,
                    }
                )
    return rows


async def mark_promise_done(promise_id: int) -> None:
    """Markiert ein Vorhaben als erledigt."""
    await _ensure_db()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE promises SET done = 1 WHERE id = ?", (promise_id,)
        )
        await db.commit()
    log.info(f"promise_tracker: promise #{promise_id} als erledigt markiert")


_FOLLOWUP_DATE_FILE = os.path.expanduser("~/.jarvis_promise_followup_date")


async def get_oldest_overdue_promise(min_age_days: int = 2) -> dict | None:
    """Gibt das aelteste offene Vorhaben zurueck das mindestens min_age_days alt ist.

    Liefert None wenn kein solches Vorhaben existiert.
    Jeder Eintrag enthaelt: id, text, source, created, age_label.
    """
    await _ensure_db()
    cutoff = (
        datetime.datetime.now() - datetime.timedelta(days=min_age_days)
    ).isoformat(timespec="seconds")
    now = datetime.datetime.now()
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            "SELECT id, text, source, created FROM promises "
            "WHERE done = 0 AND created <= ? "
            "ORDER BY created ASC "
            "LIMIT 1",
            (cutoff,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    promise_id, text, source, created_str = row
    try:
        created_dt = datetime.datetime.fromisoformat(created_str)
        delta = now - created_dt
        days = delta.days
        if days == 0:
            age_label = "heute"
        elif days == 1:
            age_label = "gestern"
        else:
            age_label = f"vor {days} Tagen"
    except Exception:
        age_label = "kuerzlich"
    return {
        "id": promise_id,
        "text": text,
        "source": source,
        "created": created_str,
        "age_label": age_label,
    }


async def was_followup_sent_today() -> bool:
    """Prueft ob heute bereits eine Versprechen-Nachfrage gesendet wurde.

    Liest das Datum aus ~/.jarvis_promise_followup_date (ueberlebt Server-Neustarts).
    """
    try:
        with open(_FOLLOWUP_DATE_FILE, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        return stored == datetime.date.today().isoformat()
    except FileNotFoundError:
        return False
    except Exception as e:
        log.warning(f"was_followup_sent_today: Lesefehler: {type(e).__name__}: {e}")
        return False


async def mark_followup_sent_today() -> None:
    """Speichert das heutige Datum als 'Nachfrage gesendet'.

    Schreibt in ~/.jarvis_promise_followup_date.
    """
    try:
        with open(_FOLLOWUP_DATE_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
        log.info("promise_tracker: Nachfrage-Datum gespeichert")
    except Exception as e:
        log.warning(f"mark_followup_sent_today: Schreibfehler: {type(e).__name__}: {e}")


async def format_promises_block(max_age_days: int = 3) -> str:
    """Formatiert offene Vorhaben als Briefing-Block.

    Liefert leeren String wenn keine offenen Vorhaben vorhanden.
    Beispiel-Output:
      'Offene Vorhaben: Steuerbescheid pruefen (gestern), Rueckruf Mueller (vor zwei Tagen)'
    """
    promises = await get_open_promises(max_age_days=max_age_days)
    if not promises:
        return ""
    items = [f"{p['text']} ({p['age_label']})" for p in promises]
    return "Offene Vorhaben: " + ", ".join(items)
