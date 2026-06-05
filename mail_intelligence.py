"""
Mail Intelligence — passiver E-Mail-Wissensmonitor (Issue #161).

Liest alle konfigurierten Postfächer still im Hintergrund und extrahiert
strukturierte Informationen aus relevanten Mails. Kein Telegram, keine
Aktionen — nur stilles Lernen für JARVIS als zweites Gedächtnis.

Ablauf pro Postfach:
1. Neue UIDs ermitteln (max_uid aus State-Datei)
2. Newsletter/Spam-Klassifikation via Claude Haiku (RELEVANT / SKIP)
3. Bei RELEVANT: vollständige Extraktion via Claude Haiku → JSON
4. Ergebnis in SQLite `mail_knowledge` speichern
5. State aktualisieren

Abfrage:
    from mail_intelligence import search_knowledge, get_recent_knowledge
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Optional

import settings as S
from prompt import llm_text

log = S.log

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------

_DB_PATH = os.path.expanduser("~/.jarvis_mail_knowledge.db")


def _state_path(account_name: str) -> str:
    """Pfad zur UID-State-Datei für dieses Konto."""
    safe = "".join(c if c.isalnum() else "_" for c in account_name)
    return os.path.expanduser(f"~/.jarvis_intelligence_seen_{safe}.json")


# ---------------------------------------------------------------------------
# State-Management (max_uid pro Konto)
# ---------------------------------------------------------------------------

def _load_state(account_name: str) -> int:
    """Lädt den zuletzt verarbeiteten UID für das Konto. 0 wenn kein State."""
    p = _state_path(account_name)
    if not os.path.exists(p):
        return 0
    try:
        with open(p) as f:
            return int(json.load(f).get("max_uid", 0))
    except Exception:
        return 0


def _save_state(account_name: str, uid: int) -> None:
    """Speichert den zuletzt verarbeiteten UID für das Konto."""
    try:
        with open(_state_path(account_name), "w") as f:
            json.dump({"max_uid": uid}, f)
    except Exception as e:
        log.warning(
            "mail_intelligence[%s] state save failed: %s: %s",
            account_name, type(e).__name__, e,
        )


# ---------------------------------------------------------------------------
# Datenbank-Setup
# ---------------------------------------------------------------------------

def _init_db() -> None:
    """Erstellt das Schema falls noch nicht vorhanden. Einmalig beim Start aufrufen."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                mail_date TEXT,
                sender TEXT,
                sender_name TEXT,
                subject TEXT,
                category TEXT,
                content TEXT,
                raw_summary TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_knowledge_account "
            "ON mail_knowledge(account)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_knowledge_date "
            "ON mail_knowledge(mail_date)"
        )


# Auf Import-Zeit ausführen: macht search_knowledge/get_recent_knowledge
# unabhängig davon ob mail_intelligence_scheduler() je gestartet wurde.
_init_db()


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Header-Dekodierung + Body-Extraktion
# ---------------------------------------------------------------------------

def _decode_header(raw: Optional[str]) -> str:
    """Dekodiert MIME-encoded E-Mail-Header sicher."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _extract_text_body(msg) -> str:
    """Extrahiert den Klartextinhalt einer E-Mail (text/plain)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    charset = part.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace").strip()
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            charset = msg.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace").strip()
    return ""


def _esc(s: str) -> str:
    """Verdoppelt geschweifte Klammern damit str.format() user-Daten nicht interpoliert."""
    return s.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Claude-Prompts
# ---------------------------------------------------------------------------

_RELEVANCE_PROMPT = """Klassifiziere diese E-Mail in eine Kategorie.
Antworte NUR mit einem Wort: RELEVANT oder SKIP

SKIP wenn: Newsletter, Werbung, automatische Benachrichtigung, Tracking-Mail, \
Bestellbestätigung, System-Notification, Marketing
RELEVANT wenn: persönliche Kommunikation, geschäftliche Anfrage, \
Terminvereinbarung, Entscheidung, Fachinformation, Mandanten-Kommunikation

Von: {sender}
Betreff: {subject}
Text (erste 500 Zeichen): {text_preview}"""


_EXTRACTION_PROMPT = """Extrahiere strukturierte Informationen aus dieser E-Mail.
Antworte NUR mit einem JSON-Objekt.

{{
  "raw_summary": "<1-2 Sätze Zusammenfassung>",
  "items": [
    {{
      "category": "<deadline|decision|person|action|fact>",
      "content": "<extrahierte Information>"
    }}
  ]
}}

Von: {sender_name} <{sender}>
Datum: {mail_date}
Betreff: {subject}
Text: {text}"""


async def _classify_relevance(
    sender: str,
    subject: str,
    text_preview: str,
) -> bool:
    """Gibt True zurück wenn die Mail RELEVANT ist (nicht SKIP).

    Args:
        sender: Absender-Adresse oder Anzeigename.
        subject: Betreff der Mail.
        text_preview: Erste ~500 Zeichen des Klartexts.

    Returns:
        True wenn RELEVANT, False wenn SKIP oder Fehler.
    """
    prompt = _RELEVANCE_PROMPT.format(
        sender=_esc(sender),
        subject=_esc(subject),
        text_preview=_esc(text_preview[:500]),
    )
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        result = llm_text(resp).strip().upper()
        return result.startswith("RELEVANT")
    except Exception as e:
        log.warning(
            "mail_intelligence: relevance classify failed: %s: %s",
            type(e).__name__, e,
        )
        return False


async def _extract_knowledge(
    sender: str,
    sender_name: str,
    mail_date: str,
    subject: str,
    text: str,
) -> Optional[dict]:
    """Extrahiert strukturiertes Wissen aus einer E-Mail via Claude Haiku.

    Args:
        sender: E-Mail-Adresse des Absenders.
        sender_name: Anzeigename des Absenders.
        mail_date: Datum der Mail (aus dem Date-Header).
        subject: Betreff.
        text: Vollständiger Klartext der Mail.

    Returns:
        Dict mit 'raw_summary' und 'items' Liste, oder None bei Fehler.
    """
    prompt = _EXTRACTION_PROMPT.format(
        sender_name=_esc(sender_name),
        sender=_esc(sender),
        mail_date=_esc(mail_date),
        subject=_esc(subject),
        text=_esc(text[:3000]),
    )
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = llm_text(resp).strip()
        # Code-Fences entfernen falls vorhanden
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.debug(
            "mail_intelligence: extraction JSON parse failed: %s — raw: %r",
            e, raw[:200] if "raw" in dir() else "?",
        )
        return None
    except Exception as e:
        log.warning(
            "mail_intelligence: extraction failed: %s: %s",
            type(e).__name__, e,
        )
        return None


# ---------------------------------------------------------------------------
# Datenbankoperationen
# ---------------------------------------------------------------------------

def _store_knowledge(
    account: str,
    mail_date: str,
    sender: str,
    sender_name: str,
    subject: str,
    extracted: dict,
) -> None:
    """Speichert extrahierte Wissenselemente in der Datenbank.

    Args:
        account: Name des Postfachs.
        mail_date: Datum der Mail.
        sender: E-Mail-Adresse des Absenders.
        sender_name: Anzeigename des Absenders.
        subject: Betreff.
        extracted: Ausgabe von _extract_knowledge().
    """
    raw_summary = extracted.get("raw_summary", "")
    items = extracted.get("items", [])
    if not items and not raw_summary:
        return
    # Wenn keine Items: raw_summary als einzelnes fact-Item speichern
    if not items and raw_summary:
        items = [{"category": "fact", "content": raw_summary}]

    # RFC 2822 → ISO-Datum normalisieren damit Datums-Vergleiche funktionieren
    try:
        mail_date = email.utils.parsedate_to_datetime(mail_date).strftime("%Y-%m-%d")
    except Exception:
        pass  # unbekanntes Format: Rohwert behalten

    created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            for item in items:
                conn.execute(
                    """
                    INSERT INTO mail_knowledge
                        (account, mail_date, sender, sender_name, subject,
                         category, content, raw_summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account,
                        mail_date,
                        sender,
                        sender_name,
                        subject,
                        item.get("category", "fact"),
                        item.get("content", ""),
                        raw_summary,
                        created_at,
                    ),
                )
        log.debug(
            "mail_intelligence[%s]: %d Einträge gespeichert (Betreff: %r)",
            account, len(items), subject,
        )
    except Exception as e:
        log.warning(
            "mail_intelligence[%s]: DB store failed: %s: %s",
            account, type(e).__name__, e,
        )


# ---------------------------------------------------------------------------
# Abfrage-API (für actions.py)
# ---------------------------------------------------------------------------

def search_knowledge(query: str, limit: int = 10) -> list[dict]:
    """Durchsucht die mail_knowledge Tabelle nach einem Stichwort.

    Sucht in content, subject, sender_name und raw_summary.
    Gibt Ergebnisse nach mail_date absteigend sortiert zurück.

    Args:
        query: Suchbegriff (case-insensitiv).
        limit: Maximale Anzahl Ergebnisse.

    Returns:
        Liste von Row-Dicts mit allen Spalten.
    """
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            pattern = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM mail_knowledge
                WHERE content LIKE ?
                   OR subject LIKE ?
                   OR sender_name LIKE ?
                   OR sender LIKE ?
                   OR raw_summary LIKE ?
                ORDER BY mail_date DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(
            "mail_intelligence: search_knowledge failed: %s: %s",
            type(e).__name__, e,
        )
        return []


def get_mail_context_block(days: int = 1) -> str:
    """Erzeugt einen kompakten Block für den Systemprompt (Issue #162).

    Fasst die wichtigsten Wissenselemente der letzten N Tage zusammen.
    Priorisiert: Fristen > Entscheidungen > Aufgaben > Personen > Fakten.
    Gecappt auf 1500 Zeichen damit das Kontext-Budget nicht gesprengt wird.

    Args:
        days: Lookback-Fenster in Tagen (Default: 1 = letzte 24h).

    Returns:
        Formatierter Deutsch-Text-Block mit Quellenangaben,
        oder kurzer Leer-Hinweis wenn nichts vorliegt.
    """
    _EMPTY = "\nMail-Wissen (letzte 24h): Keine neuen Informationen aus den Postfächern."
    try:
        return _build_mail_context_block(days=days, empty=_EMPTY)
    except Exception as e:
        log.warning("mail_intelligence: get_mail_context_block failed: %s: %s", type(e).__name__, e)
        return _EMPTY


def _build_mail_context_block(days: int, empty: str) -> str:
    rows = get_recent_knowledge(days=days, limit=20)

    if not rows:
        return empty

    _CAT_ORDER = ["deadline", "decision", "action", "person", "fact"]
    _CAT_LABEL = {
        "deadline": "Frist",
        "decision": "Entscheidung",
        "action": "Aufgabe",
        "person": "Person",
        "fact": "Info",
    }

    by_cat: dict[str, list[dict]] = {c: [] for c in _CAT_ORDER}
    for row in rows:
        cat = row.get("category", "fact")
        if cat not in by_cat:
            cat = "fact"
        by_cat[cat].append(row)

    lines: list[str] = []
    for cat in _CAT_ORDER:
        for row in by_cat[cat]:
            label = _CAT_LABEL.get(cat, "Info")
            sender = (row.get("sender_name") or row.get("sender") or "?")
            date = (row.get("mail_date") or "")[:10]
            content = (row.get("content") or "").strip()
            account = row.get("account", "")

            entry = f"[{label}] {sender}"
            meta_parts = []
            if account:
                meta_parts.append(account)
            if date:
                meta_parts.append(date)
            if meta_parts:
                entry += " (" + ", ".join(meta_parts) + ")"
            if content:
                c = content[:100] + ("…" if len(content) > 100 else "")
                entry += f": {c}"
            lines.append(entry)

    if not lines:
        return empty

    block = "\nMail-Wissen (letzte 24h):\n" + "\n".join(lines[:10])
    if len(block) > 1500:
        block = block[:1497] + "…"
    return block


def get_recent_knowledge(days: int = 7, limit: int = 20) -> list[dict]:
    """Liefert Wissenselemente der letzten N Tage.

    Args:
        days: Anzahl Tage zurück (Default: 7).
        limit: Maximale Anzahl Ergebnisse.

    Returns:
        Liste von Row-Dicts mit allen Spalten, nach mail_date absteigend.
    """
    try:
        # Filter on mail_date so results reflect when the email was sent,
        # not when JARVIS processed it (avoids backlog mismatch after restart).
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM mail_knowledge
                WHERE mail_date >= ?
                ORDER BY mail_date DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(
            "mail_intelligence: get_recent_knowledge failed: %s: %s",
            type(e).__name__, e,
        )
        return []


# ---------------------------------------------------------------------------
# IMAP-Poll: einzelnes Konto
# ---------------------------------------------------------------------------

async def _poll_account_once(account: dict) -> None:
    """Einmaliger Poll eines Postfachs: verbinden, neue UIDs verarbeiten.

    Überspringt Mails die 'jarvis' im Betreff haben — diese werden von
    mail_monitor.py verarbeitet.

    Args:
        account: Normalisiertes Konto-Dict aus settings.MAIL_MONITOR_ACCOUNTS.
    """
    import aioimaplib

    name = account["name"]
    max_seen = _load_state(name)

    cls = aioimaplib.IMAP4_SSL if account["ssl"] else aioimaplib.IMAP4
    client = cls(host=account["host"], port=account["port"], timeout=60)

    try:
        await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
        login_resp = await asyncio.wait_for(
            client.login(account["user"], account["password"]), timeout=30
        )
        if getattr(login_resp, "result", None) != "OK":
            log.warning("mail_intelligence[%s]: login failed", name)
            return

        select_resp = await client.select(account["folder"])
        if getattr(select_resp, "result", None) != "OK":
            log.warning(
                "mail_intelligence[%s]: SELECT %r failed",
                name, account["folder"],
            )
            return

        # Server-Maximum ermitteln
        server_max = await _baseline_uid(client)
        if server_max <= max_seen:
            log.debug(
                "mail_intelligence[%s]: keine neuen UIDs (server_max=%d, seen=%d)",
                name, server_max, max_seen,
            )
            return

        new_uids = await _uids_in_range(client, max_seen, server_max)
        if not new_uids:
            new_uids = list(range(max_seen + 1, server_max + 1))

        log.info(
            "mail_intelligence[%s]: %d neue Mail(s) zu verarbeiten",
            name, len(new_uids),
        )

        new_max = max_seen
        for uid in sorted(new_uids):
            try:
                await _process_uid(account, client, uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug(
                    "mail_intelligence[%s] uid=%d: Fehler: %s: %s",
                    name, uid, type(e).__name__, e,
                )
            new_max = max(new_max, uid)

        _save_state(name, new_max)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning(
            "mail_intelligence[%s]: poll failed: %s: %s",
            name, type(e).__name__, e,
        )
    finally:
        try:
            await client.logout()
        except Exception:
            pass


async def _baseline_uid(client) -> int:
    """Ermittelt die höchste UID im aktuell selektierten Ordner.

    Args:
        client: Verbundener aioimaplib-Client mit selektiertem Ordner.

    Returns:
        Höchste UID als int, 0 bei Fehler.
    """
    try:
        typ, data = await client.uid("search", None, "ALL")
        if typ != "OK" or not data:
            return 0
        uids_raw = b" ".join(b for b in data if isinstance(b, (bytes, bytearray)))
        uids = [int(u) for u in uids_raw.split() if u.isdigit()]
        return max(uids) if uids else 0
    except Exception as e:
        log.debug("mail_intelligence: _baseline_uid failed: %s: %s", type(e).__name__, e)
        return 0


async def _uids_in_range(client, min_uid: int, max_uid: int) -> list[int]:
    """Ermittelt alle UIDs im Bereich (min_uid, max_uid].

    Args:
        client: Verbundener aioimaplib-Client mit selektiertem Ordner.
        min_uid: Untere Grenze (exklusiv).
        max_uid: Obere Grenze (inklusiv).

    Returns:
        Sortierte Liste von UIDs.
    """
    try:
        search_range = f"{min_uid + 1}:{max_uid}"
        typ, data = await client.uid("search", None, f"UID {search_range}")
        if typ != "OK" or not data:
            return []
        uids_raw = b" ".join(b for b in data if isinstance(b, (bytes, bytearray)))
        return sorted(int(u) for u in uids_raw.split() if u.isdigit())
    except Exception:
        return []


async def _process_uid(account: dict, client, uid: int) -> None:
    """Verarbeitet eine einzelne Mail-UID: klassifizieren und ggf. extrahieren.

    Mails mit 'jarvis' im Betreff werden übersprungen (zuständig: mail_monitor.py).

    Args:
        account: Normalisiertes Konto-Dict.
        client: Verbundener aioimaplib-Client (INBOX selektiert).
        uid: IMAP UID der zu verarbeitenden Mail.
    """
    name = account["name"]

    # Header holen
    typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[HEADER]")
    if typ != "OK" or not data:
        return
    byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
    if not byte_items:
        return

    msg_header = email.message_from_bytes(max(byte_items, key=len))
    from_parsed = parseaddr(msg_header.get("From", ""))
    sender_name = _decode_header(from_parsed[0]) or ""
    sender = (from_parsed[1] or "").lower()
    subject = _decode_header(msg_header.get("Subject", ""))
    mail_date = msg_header.get("Date", "")

    if not sender and not subject:
        return

    # Jarvis-Trigger-Mails überspringen — die hat mail_monitor.py
    if "jarvis" in subject.lower():
        log.debug(
            "mail_intelligence[%s] uid=%d: jarvis-trigger, übersprungen",
            name, uid,
        )
        return

    # Vollständigen Text holen für Klassifikation + Extraktion
    text = ""
    try:
        typ2, data2 = await client.uid("fetch", str(uid), "BODY.PEEK[]")
        if typ2 == "OK" and data2:
            byte_items2 = [b for b in data2 if isinstance(b, (bytes, bytearray))]
            if byte_items2:
                full_msg = email.message_from_bytes(max(byte_items2, key=len))
                text = _extract_text_body(full_msg)
    except Exception as e:
        log.debug(
            "mail_intelligence[%s] uid=%d: body fetch failed: %s: %s",
            name, uid, type(e).__name__, e,
        )

    display_sender = sender_name or sender
    text_preview = text[:500]

    # Stufe 1: Newsletter/Spam-Filter
    relevant = await _classify_relevance(display_sender, subject, text_preview)
    if not relevant:
        log.debug(
            "mail_intelligence[%s] uid=%d: SKIP (Betreff: %r)",
            name, uid, subject,
        )
        return

    # Stufe 2: Vollständige Extraktion
    extracted = await _extract_knowledge(
        sender=sender,
        sender_name=sender_name,
        mail_date=mail_date,
        subject=subject,
        text=text,
    )
    if not extracted:
        log.debug(
            "mail_intelligence[%s] uid=%d: Extraktion leer, übersprungen (Betreff: %r)",
            name, uid, subject,
        )
        return

    # Stufe 3: Speichern
    _store_knowledge(
        account=name,
        mail_date=mail_date,
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        extracted=extracted,
    )
    log.info(
        "mail_intelligence[%s] uid=%d: gespeichert (Betreff: %r, Items: %d)",
        name, uid, subject, len(extracted.get("items", [])),
    )


# ---------------------------------------------------------------------------
# Scheduler-Hauptschleife
# ---------------------------------------------------------------------------

async def mail_intelligence_scheduler() -> None:
    """Hintergrund-Task: pollt alle Postfächer alle MAIL_INTELLIGENCE_INTERVAL Sekunden.

    Startet nach einem kurzen Initialverzug damit der IMAP-Monitor sich zuerst
    verbinden kann. Fehler werden geloggt und übersprungen — der Server wird
    nie gecrasht.
    """
    accounts = S.MAIL_MONITOR_ACCOUNTS
    if not accounts:
        log.info("mail_intelligence: keine Konten konfiguriert, Scheduler inaktiv")
        return

    interval = S.MAIL_INTELLIGENCE_INTERVAL
    log.info(
        "mail_intelligence: Scheduler gestartet (%d Konto(en), Intervall: %ds)",
        len(accounts), interval,
    )

    # Kurzer Initialverzug: 60 Sekunden nach Serverstart
    await asyncio.sleep(60)

    while True:
        for account in accounts:
            try:
                await _poll_account_once(account)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "mail_intelligence[%s]: unerwarteter Fehler: %s: %s",
                    account.get("name", "?"), type(e).__name__, e,
                )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
