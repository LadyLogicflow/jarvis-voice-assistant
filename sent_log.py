"""sent_log.py — Persistenter Versand-Log in SQLite (Issue #255).

Verhindert, dass JARVIS nach einem Neustart bereits gemeldete Ereignisse
(Kalender-Alerts, Mail-Benachrichtigungen, Geburtstage, ...) erneut sendet.

Verwendung:
    import sent_log

    if await sent_log.already_sent("calendar_alert", f"{event_id}_{date}"):
        return  # bereits gesendet, überspringen

    await sent_log.mark_sent("calendar_alert", f"{event_id}_{date}")
"""

import os
import datetime
import aiosqlite

_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_sent_log.db")

# Retention in Tagen pro event_type — None = dauerhaft
_RETENTION: dict[str, int | None] = {
    "calendar_alert":    30,
    "mail_catchup":      90,
    "birthday_draft":    None,  # permanent (einmalig pro Person+Jahr)
    "morning_brief":     14,
    "evening_brief":     14,
    "proactive_slot":    14,
    "evening_summary":   14,
}
_DEFAULT_RETENTION = 90  # Tage, wenn event_type nicht in _RETENTION


async def init_db() -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_log (
                event_type TEXT NOT NULL,
                event_key  TEXT NOT NULL,
                sent_at    TEXT NOT NULL,
                PRIMARY KEY (event_type, event_key)
            )
        """)
        await db.commit()


async def already_sent(event_type: str, event_key: str) -> bool:
    try:
        async with aiosqlite.connect(_DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM sent_log WHERE event_type=? AND event_key=? LIMIT 1",
                (event_type, event_key),
            ) as cur:
                return await cur.fetchone() is not None
    except Exception:
        return False  # Im Zweifel: lieber senden als stumm bleiben


async def mark_sent(event_type: str, event_key: str) -> None:
    try:
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        async with aiosqlite.connect(_DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO sent_log (event_type, event_key, sent_at) VALUES (?,?,?)",
                (event_type, event_key, now),
            )
            await db.commit()
    except Exception:
        pass  # Best-effort — Fehler nicht fatal


async def prune_old_entries() -> int:
    """Löscht abgelaufene Einträge. Gibt Anzahl gelöschter Zeilen zurück."""
    deleted = 0
    try:
        now = datetime.datetime.utcnow()
        async with aiosqlite.connect(_DB_PATH) as db:
            for event_type, days in _RETENTION.items():
                if days is None:
                    continue
                cutoff = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                cur = await db.execute(
                    "DELETE FROM sent_log WHERE event_type=? AND sent_at<?",
                    (event_type, cutoff),
                )
                deleted += cur.rowcount
            # Default-Retention für nicht explizit gelistete Typen
            cutoff_default = (
                now - datetime.timedelta(days=_DEFAULT_RETENTION)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = await db.execute(
                "DELETE FROM sent_log WHERE event_type NOT IN ({}) AND sent_at<?".format(
                    ",".join("?" * len(_RETENTION))
                ),
                (*_RETENTION.keys(), cutoff_default),
            )
            deleted += cur.rowcount
            await db.commit()
    except Exception:
        pass
    return deleted
