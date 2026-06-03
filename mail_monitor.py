"""
IMAP IDLE Mail-Monitor (issue #48).

Hangs an IDLE connection on each configured account's INBOX (Catrin
runs Apple + HILO in parallel) and pushes only relevant new mails to
Telegram. "Relevant" is decided by Claude Haiku: each new mail is
classified into {werbung, info, handlungsbedarf}, and only the
categories listed in `S.MAIL_MONITOR_FORWARD` are forwarded.

Quiet hours (S.is_quiet_hours) suppress pushes — mails are still
classified and remembered as 'seen' so they don't show up at 7 AM.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import json
import os
import re
import sys
from email.utils import parseaddr

# Apple Contacts (osascript) only works on macOS.
_MACOS = sys.platform == "darwin"

import contact_sync
import mail_actions
import mail_triage
from prompt import llm_text
import session_state
import settings as S
import telegram_bot

log = S.log


# UID-tracker per account. {account_name: max_seen_uid}
_max_seen: dict[str, int] = {}

# UID-tracker for the Sent folder (separate state file per account).
_max_seen_sent: dict[str, int] = {}

# Sent-folder poll interval: 15 minutes. Fresh connect per poll.
_SENT_POLL_INTERVAL = 15 * 60

# ---------------------------------------------------------------------------
# Jarvis-Trigger: pending-delete queue (Issue #159)
# Mails with 'jarvis' in subject are queued for deletion 24h after arrival.
# Format: {(account_name, uid): received_timestamp_float}
# ---------------------------------------------------------------------------
import time as _time

_JARVIS_DELETE_AFTER = 24 * 60 * 60  # 24 hours in seconds
_pending_jarvis_delete: dict[tuple[str, int], float] = {}


def _sent_state_path(account_name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in account_name)
    return os.path.join(os.path.dirname(__file__), f".jarvis_mail_sent_{safe}.json")


def _load_sent_state(account_name: str) -> int:
    p = _sent_state_path(account_name)
    if not os.path.exists(p):
        return 0
    try:
        with open(p) as f:
            return int(json.load(f).get("max_seen_uid", 0))
    except Exception:
        return 0


def _save_sent_state(account_name: str, uid: int) -> None:
    try:
        with open(_sent_state_path(account_name), "w") as f:
            json.dump({"max_seen_uid": uid}, f)
    except Exception as e:
        log.warning(f"mail_monitor[{account_name}] sent state save failed: "
                    f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Passive learning (Issue #102)
# ---------------------------------------------------------------------------

def _learn_from_mail(
    account: str,
    uid: int,
    sender: str,
    sender_email: str,
    subject: str,
    msg,
) -> None:
    """Update persons_db and memory index from a handlungsbedarf mail.

    Called for every handlungsbedarf mail that reaches the notification
    path (triage action == "none").  Failures are logged at DEBUG level
    and never propagate to the caller.

    Args:
        account:      IMAP account name.
        uid:          IMAP UID of the mail.
        sender:       Display name of the sender.
        sender_email: Normalised sender e-mail address (may be empty).
        subject:      Decoded mail subject.
        msg:          email.message.Message object (headers already parsed).
    """
    import datetime as _dt

    # Extract a short plain-text snippet from the mail body for richer notes.
    body_snippet = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    raw = part.get_payload(decode=True)
                    if raw:
                        charset = part.get_content_charset() or "utf-8"
                        body_snippet = raw.decode(charset, errors="replace").strip()
                        break
        else:
            raw = msg.get_payload(decode=True)
            if raw:
                charset = msg.get_content_charset() or "utf-8"
                body_snippet = raw.decode(charset, errors="replace").strip()
        # Keep only first 200 chars; strip quoted reply lines ("> ...")
        lines = [l for l in body_snippet.splitlines() if l and not l.startswith(">")]
        body_snippet = " ".join(lines)[:200].strip()
    except Exception:
        body_snippet = ""

    # persons_db: update last_contact + auto-save mail note for known senders.
    if sender_email:
        try:
            import persons_db
            profile = persons_db.find_by_email(sender_email)
            if profile is not None:
                today = _dt.date.today().isoformat()
                note_text = f"{today}: Mail empfangen — {subject}"
                if body_snippet:
                    note_text += f" | {body_snippet[:150]}"
                if note_text not in profile.notes:
                    profile.notes.append(note_text)
                profile.last_contact = today
                persons_db.upsert(profile)
                log.debug(
                    "mail_monitor[%s] learn: saved mail note for %s (%s)",
                    account, profile.name, sender_email,
                )
        except Exception as exc:
            log.debug(
                "mail_monitor[%s] learn: persons_db update failed: %s: %s",
                account, type(exc).__name__, exc,
            )

    # memory_search: index sender + subject + body snippet for richer recall.
    try:
        import memory_search
        date_str = msg.get("Date", "")
        display = sender or sender_email or "Unbekannt"
        text = f"Mail von {display}: {subject}"
        if body_snippet:
            text += f" — {body_snippet[:200]}"
        doc_id = memory_search.make_doc_id(
            "mail", f"{account}:{uid}:{sender_email}:{subject}"
        )
        memory_search.index_text(
            text=text,
            source="mail",
            doc_id=doc_id,
            metadata={
                "type": "mail",
                "account": account,
                "uid": str(uid),
                "sender": sender_email,
                "date": date_str,
            },
        )
        log.debug(
            "mail_monitor[%s] learn: indexed mail uid=%s in memory_search",
            account, uid,
        )
    except Exception as exc:
        log.debug(
            "mail_monitor[%s] learn: memory_search index failed: %s: %s",
            account, type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Jarvis-Trigger helpers (Issue #159)
# ---------------------------------------------------------------------------

def _is_jarvis_trigger(subject: str) -> bool:
    """Return True when the subject contains 'jarvis' (case-insensitive).

    Args:
        subject: Decoded mail subject string.

    Returns:
        True if the subject contains the keyword 'jarvis'.
    """
    return "jarvis" in subject.lower()


def _extract_attachments(msg) -> list[dict]:
    """Extract all attachments from an email.message.Message.

    Returns a list of dicts with keys:
        filename (str), content_type (str), data (bytes)

    Only parts with a Content-Disposition of 'attachment' (or inline parts
    that carry an explicit filename) are returned.

    Args:
        msg: email.message.Message object (fully fetched with BODY[]).

    Returns:
        List of attachment dicts. Empty list if none found.
    """
    attachments: list[dict] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        filename_raw = part.get_filename()
        if not filename_raw and "attachment" not in disp:
            continue
        filename = email.header.decode_header(filename_raw or "")[0]
        if isinstance(filename[0], bytes):
            try:
                fname = filename[0].decode(filename[1] or "utf-8", errors="replace")
            except LookupError:
                fname = filename[0].decode("utf-8", errors="replace")
        else:
            fname = str(filename[0]) if filename[0] else "attachment"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append({
            "filename": fname,
            "content_type": part.get_content_type(),
            "data": payload,
        })
    return attachments


async def _handle_jarvis_trigger(
    account: dict,
    client,
    uid: int,
    subject: str,
    sender: str,
) -> None:
    """Process a mail that has 'jarvis' in the subject (Issue #159).

    Fetches the full mail body + attachments, routes PDFs through the
    analyze_pdf_stub, sends a Telegram confirmation, and queues the mail
    for deletion after 24 h.

    Args:
        account: Normalised account dict from settings.
        client:  Connected aioimaplib client (INBOX already selected).
        uid:     IMAP UID of the trigger mail.
        subject: Decoded subject string.
        sender:  Display name / address of the sender.
    """
    import pdf_tools
    name = account["name"]
    log.info("mail_monitor[%s] uid=%s: jarvis-trigger erkannt (Betreff: %r)",
             name, uid, subject)

    # Fetch the full message so we can inspect attachments.
    msg_full = None
    try:
        typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[]")
        if typ == "OK" and data:
            byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
            if byte_items:
                msg_full = email.message_from_bytes(max(byte_items, key=len))
    except Exception as e:
        log.warning("mail_monitor[%s] uid=%s: jarvis-trigger full-fetch failed: %s: %s",
                    name, uid, type(e).__name__, e)

    confirmation_parts: list[str] = []

    if msg_full is not None:
        attachments = _extract_attachments(msg_full)
        if attachments:
            for att in attachments:
                ctype = att["content_type"]
                fname = att["filename"]
                if ctype == "application/pdf" or fname.lower().endswith(".pdf"):
                    try:
                        dest = pdf_tools.save_pdf(att["data"], fname)
                        pdf_tools.analyze_pdf_stub(dest)
                        confirmation_parts.append(
                            f"PDF '{fname}' empfangen und zur Analyse vorgemerkt."
                        )
                    except Exception as e:
                        log.warning(
                            "mail_monitor[%s] uid=%s: PDF-Speicherung fehlgeschlagen: "
                            "%s: %s", name, uid, type(e).__name__, e
                        )
                        confirmation_parts.append(
                            f"PDF '{fname}' empfangen (Speicherung fehlgeschlagen: {e})."
                        )
                else:
                    log.info(
                        "mail_monitor[%s] uid=%s: Anhang übersprungen (kein PDF): %s (%s)",
                        name, uid, fname, ctype,
                    )
        else:
            log.info("mail_monitor[%s] uid=%s: jarvis-trigger ohne Anhang", name, uid)

    if not confirmation_parts:
        confirmation_parts.append("Jarvis-Befehl empfangen.")

    confirmation = (
        f"Jarvis-Trigger via E-Mail (Betreff: {subject!r}):\n"
        + "\n".join(f"• {p}" for p in confirmation_parts)
    )
    try:
        await telegram_bot.send_user_text(confirmation)
    except Exception as e:
        log.warning("mail_monitor[%s] uid=%s: Telegram-Bestätigung fehlgeschlagen: %s: %s",
                    name, uid, type(e).__name__, e)

    # Queue for deletion after 24 h.
    _pending_jarvis_delete[(name, uid)] = _time.time()
    log.info("mail_monitor[%s] uid=%s: zur Löschung vorgemerkt (in 24h)", name, uid)


async def _flush_jarvis_deletes(accounts: list[dict]) -> None:
    """Delete queued jarvis-trigger mails whose 24 h window has expired.

    Called once per poll cycle from the main IDLE loop. Uses a fresh IMAP
    connection so it doesn't interfere with the persistent polling session.

    Args:
        accounts: List of normalised account dicts (from settings).
    """
    if not _pending_jarvis_delete:
        return
    now = _time.time()
    due = [
        (acc_name, uid)
        for (acc_name, uid), ts in list(_pending_jarvis_delete.items())
        if now - ts >= _JARVIS_DELETE_AFTER
    ]
    if not due:
        return

    # Group by account for efficiency.
    by_account: dict[str, list[int]] = {}
    for acc_name, uid in due:
        by_account.setdefault(acc_name, []).append(uid)

    acc_map = {a["name"]: a for a in accounts}

    for acc_name, uids in by_account.items():
        acc = acc_map.get(acc_name)
        if not acc:
            log.warning("mail_monitor: jarvis-delete: Konto %r nicht gefunden", acc_name)
            continue
        try:
            import aioimaplib
            cls = aioimaplib.IMAP4_SSL if acc["ssl"] else aioimaplib.IMAP4
            client = cls(host=acc["host"], port=acc["port"], timeout=30)
            await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
            resp = await asyncio.wait_for(
                client.login(acc["user"], acc["password"]), timeout=30
            )
            if getattr(resp, "result", None) != "OK":
                log.warning("mail_monitor[%s] jarvis-delete: login failed", acc_name)
                continue
            await client.select(acc["folder"])
            for uid in uids:
                try:
                    await client.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
                    log.info("mail_monitor[%s] uid=%s: jarvis-trigger mail als gelöscht markiert",
                             acc_name, uid)
                except Exception as e:
                    log.warning(
                        "mail_monitor[%s] uid=%s: STORE \\Deleted fehlgeschlagen: %s: %s",
                        acc_name, uid, type(e).__name__, e,
                    )
            try:
                await client.expunge()
            except Exception as e:
                log.warning("mail_monitor[%s] EXPUNGE fehlgeschlagen: %s: %s",
                            acc_name, type(e).__name__, e)
            await client.logout()
        except Exception as e:
            log.warning("mail_monitor[%s] jarvis-delete connection failed: %s: %s",
                        acc_name, type(e).__name__, e)
        else:
            for uid in uids:
                _pending_jarvis_delete.pop((acc_name, uid), None)


def _state_path(account_name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in account_name)
    return os.path.join(os.path.dirname(__file__), f".jarvis_mail_seen_{safe}.json")


def _load_state(account_name: str) -> int:
    p = _state_path(account_name)
    if not os.path.exists(p):
        return 0
    try:
        with open(p) as f:
            return int(json.load(f).get("max_seen_uid", 0))
    except Exception:
        return 0


def _save_state(account_name: str, uid: int) -> None:
    try:
        with open(_state_path(account_name), "w") as f:
            json.dump({"max_seen_uid": uid}, f)
    except Exception as e:
        log.warning(f"mail_monitor[{account_name}]: state save failed: "
                    f"{type(e).__name__}: {e}")


def _decode_header(raw: Optional[str]) -> str:
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


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
_CLASSIFIER_PROMPT = (
    "Du bist ein E-Mail-Klassifikator. Antworte mit JSON: "
    "{\"category\": \"...\", \"reply_needed\": true/false}\n"
    "category-Werte:\n"
    "- werbung = Newsletter, Marketing, Werbeangebote, no-reply Marketing-Mails\n"
    "- info = automatische Statusmeldungen die kein Handeln erfordern "
    "(Versandbestaetigungen, OAuth-Logins, etc.)\n"
    "- handlungsbedarf = Fristen, Termine, wichtige Mitteilungen. "
    "IMMER handlungsbedarf: ELSTER, Finanzamt, Behoerden, Gerichte, "
    "Steuerberaterkammer, IHK, Rentenversicherung, Krankenkassen, DATEV-Nachrichten.\n"
    "reply_needed = true NUR wenn der Absender offensichtlich eine persoenliche "
    "Antwort erwartet (direkte Fragen, Mandantenanfragen, persoenliche Nachrichten). "
    "false bei automatischen Mails oder Einwegkommunikation.\n"
    "Antworte NUR mit dem JSON-Objekt, nichts anderes."
)


async def _classify(sender: str, subject: str, body_preview: str) -> tuple[str, bool]:
    """Returns (category, reply_needed)."""
    import json as _json
    user_msg = f"Von: {sender}\nBetreff: {subject}\n\n{body_preview[:1500]}"
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            system=_CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = llm_text(resp).strip()
        # Strip markdown code fences the LLM occasionally wraps around JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        try:
            data = _json.loads(raw)
            cat = str(data.get("category", "info")).strip().lower()
            if cat not in ("werbung", "info", "handlungsbedarf"):
                cat = "info"
            return cat, bool(data.get("reply_needed", False))
        except _json.JSONDecodeError:
            # Fallback: alte Einzel-Wort-Antwort tolerieren
            for token in raw.lower().replace(",", " ").replace(".", " ").split():
                if token in ("werbung", "info", "handlungsbedarf"):
                    return token, False
        log.info(f"mail_monitor: classifier returned {raw!r}, defaulting to 'info'")
        return "info", False
    except Exception as e:
        log.warning(f"mail_monitor classify failed: {type(e).__name__}: {e}")
        return "unknown", False


_SUMMARY_PROMPT = (
    "Du bist Jarvis. Fasse diese E-Mail in 1-2 knappen deutschen Saetzen zusammen. "
    "Nenne den Kerninhalt und falls vorhanden die gewuenschte Aktion. "
    "Kein 'Die E-Mail handelt von...', direkt zum Punkt. Keine Anrede, kein Schluss."
)


async def _summarize_body(sender: str, subject: str, body: str) -> str:
    """Return a 1-2 sentence German summary of the mail body, or '' on failure."""
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system=_SUMMARY_PROMPT,
            messages=[{"role": "user", "content":
                        f"Von: {sender}\nBetreff: {subject}\n\n{body[:2000]}"}],
        )
        return llm_text(resp).strip()
    except Exception as e:
        log.warning(f"mail_monitor: summarize failed: {type(e).__name__}: {e}")
        return ""


def _format_for_telegram(
    account_name: str, sender: str, subject: str,
    category: str, summary: str = "", reply_needed: bool = False,
    prior_context: str = "",
) -> str:
    icon = {
        "handlungsbedarf": "🔴",
        "info": "🟡",
        "werbung": "⚪",
    }.get(category, "✉️")
    base = (
        f"{icon} Neue Mail [{account_name}]\n"
        f"Von: {sender}\nBetreff: {subject}"
    )
    if prior_context:
        base += f"\n📋 {prior_context}"
    if summary:
        base += f"\n\n{summary}"
    if reply_needed:
        base += "\n\n↩️ Antwort erwartet — Entwurf vorbereiten?"
    elif category == "handlungsbedarf":
        base += "\n\n→ Aufgabe anlegen?"
    elif category == "info":
        base += "\n\n→ Mails vom Absender zukünftig immer als gelesen markieren?"
    return base


def _format_for_voice(sender: str, subject: str, reply_needed: bool = False) -> str:
    """Natural-language sentence for Jarvis to speak via the Mac UI."""
    base = f"Eine neue, dringende E-Mail von {sender} mit dem Betreff: {subject}."
    if reply_needed:
        base += " Eine Antwort scheint erwartet zu werden — soll ich einen Entwurf vorbereiten?"
    return base


# ---------------------------------------------------------------------------
# Sent-folder classifier + poll (Follow-up Tracker)
# ---------------------------------------------------------------------------
_SENT_CLASSIFIER_PROMPT = (
    "Du bist ein E-Mail-Assistent. Analysiere diese gesendete E-Mail. "
    "Antworte mit JSON: {\"reply_expected\": true/false}\n"
    "reply_expected = true wenn die Mail:\n"
    "- Eine direkte Frage stellt die eine Antwort benoetigt\n"
    "- Eine Anfrage, Bitte oder Auftrag enthaelt (Dokument anfordern, Termin vorschlagen, Genehmigung bitten)\n"
    "- Ausdruecklich eine Rueckmeldung erbittet ('bitte geben Sie mir Bescheid', 'ich bitte um Rueckmeldung')\n"
    "reply_expected = false bei: abschliessenden Antworten, FYI-Mails, "
    "Weiterleitungen, Bestaetigungen, automatischen Mails.\n"
    "Antworte NUR mit dem JSON-Objekt, nichts anderes."
)


async def _classify_outgoing(to_addr: str, subject: str, body_preview: str) -> bool:
    """Returns True wenn eine Antwort auf diese gesendete Mail erwartet wird."""
    import json as _json
    user_msg = f"An: {to_addr}\nBetreff: {subject}\n\n{body_preview[:1500]}"
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            system=_SENT_CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = llm_text(resp).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        data = _json.loads(raw)
        return bool(data.get("reply_expected", False))
    except Exception as e:
        log.debug(f"mail_monitor: _classify_outgoing failed: {type(e).__name__}: {e}")
        return False


async def _poll_sent_folder_once(account: dict, aioimaplib_module) -> None:
    """Single poll of the Sent folder: connect, check for new UIDs, disconnect.

    For each new sent mail, classifies whether a reply is expected and if so
    saves it to followup_tracker. State is persisted in a separate file per
    account so we never re-scan old mails.
    """
    import followup_tracker as _ft
    name = account["name"]
    sent_folder = account.get("sent_folder", "Sent")

    cls = aioimaplib_module.IMAP4_SSL if account["ssl"] else aioimaplib_module.IMAP4
    client = cls(host=account["host"], port=account["port"], timeout=60)
    try:
        await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
        login_resp = await asyncio.wait_for(
            client.login(account["user"], account["password"]), timeout=30
        )
        if getattr(login_resp, "result", None) != "OK":
            log.warning(f"mail_monitor[{name}] sent: login failed")
            return

        select_resp = await client.select(sent_folder)
        if getattr(select_resp, "result", None) != "OK":
            log.debug(
                f"mail_monitor[{name}] sent: SELECT {sent_folder!r} failed — "
                f"kein Gesendete-Ordner mit diesem Namen. "
                f"'sent_folder' in config.json anpassen."
            )
            return

        server_max = await _baseline_uid(client, sent_folder)
        our_max = _max_seen_sent.get(name, 0)
        if server_max <= our_max:
            return

        new_uids = await _uids_in_range(client, our_max, server_max)
        if not new_uids:
            new_uids = list(range(our_max + 1, server_max + 1))

        log.info(f"mail_monitor[{name}] sent: {len(new_uids)} neue gesendete Mail(s)")

        for uid in sorted(new_uids):
            try:
                typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[HEADER]")
                if typ != "OK" or not data:
                    continue
                byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
                if not byte_items:
                    continue
                raw = max(byte_items, key=len)
                msg = email.message_from_bytes(raw)

                to_raw = _decode_header(msg.get("To", ""))
                to_parsed = parseaddr(to_raw)
                to_name = _decode_header(to_parsed[0]) if to_parsed[0] else ""
                to_email = (to_parsed[1] or "").lower()
                subject = _decode_header(msg.get("Subject", ""))
                message_id = msg.get("Message-ID", "").strip()
                date_str = msg.get("Date", "")

                if not message_id or not subject:
                    continue

                # Try to fetch body preview for better classification.
                # Apple iCloud rejects partial-range syntax — failure is expected.
                body_preview = ""
                try:
                    typ2, data2 = await client.uid(
                        "fetch", str(uid), "BODY.PEEK[TEXT]<0.2000>"
                    )
                    if typ2 == "OK" and data2:
                        byte_items2 = [
                            b for b in data2 if isinstance(b, (bytes, bytearray))
                        ]
                        if byte_items2:
                            body_preview = max(byte_items2, key=len).decode(
                                errors="replace"
                            )[:2000]
                except Exception:
                    pass

                reply_expected = await _classify_outgoing(
                    to_raw or to_email, subject, body_preview
                )
                if reply_expected:
                    _ft.save_followup(
                        message_id=message_id,
                        account=name,
                        to_email=to_email,
                        to_name=to_name,
                        subject=subject,
                        sent_date=date_str,
                    )
            except Exception as e:
                log.debug(
                    f"mail_monitor[{name}] sent uid={uid}: {type(e).__name__}: {e}"
                )
            finally:
                _max_seen_sent[name] = max(_max_seen_sent.get(name, 0), uid)
                _save_sent_state(name, _max_seen_sent[name])
    except Exception as e:
        log.warning(
            f"mail_monitor[{name}] sent poll failed: {type(e).__name__}: {e}"
        )
    finally:
        try:
            await client.logout()
        except Exception:
            pass


async def _sent_account_loop(account: dict, aioimaplib_module) -> None:
    """Long-running task: poll the Sent folder every _SENT_POLL_INTERVAL seconds.

    Uses a fresh IMAP connection per poll rather than a persistent IDLE session —
    the infrequent interval makes connect/disconnect overhead negligible and keeps
    the logic simpler.
    """
    name = account["name"]
    _max_seen_sent[name] = _load_sent_state(name)
    # Short initial delay so the INBOX connection settles first.
    await asyncio.sleep(30)
    while True:
        try:
            await _poll_sent_folder_once(account, aioimaplib_module)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(
                f"mail_monitor[{name}] sent_loop: {type(e).__name__}: {e}"
            )
        await asyncio.sleep(_SENT_POLL_INTERVAL)


# Callback the server registers to push spoken alerts to the Web-UI.
# Stays None when the server hasn't wired it up — Telegram still works.
_mail_alert_handler = None


def register_mail_alert_handler(fn) -> None:
    """server.py registers its broadcaster here so mail_monitor stays
    decoupled from the WebSocket layer. Mail-monitor calls the handler
    whenever a handlungsbedarf-mail arrives during waking hours; the
    handler decides whether to actually speak (e.g. only when a client
    is connected)."""
    global _mail_alert_handler
    _mail_alert_handler = fn


# ---------------------------------------------------------------------------
# Per-account IDLE session
# ---------------------------------------------------------------------------
async def _process_new_uids(account: dict, client, uids: list[int]) -> None:
    name = account["name"]
    for uid in sorted(uids):
        if uid <= _max_seen.get(name, 0):
            continue
        try:
            # Most portable FETCH form: BODY.PEEK[HEADER]. Apple iCloud
            # rejects the previous attempts:
            #  - (BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] ...) —
            #    nested parens, aioimaplib quoting bug
            #  - (RFC822.HEADER BODY.PEEK[TEXT]<0.2000>) — Apple BAD
            #    Parse Error (didn't accept either RFC822.HEADER or the
            #    partial-range syntax)
            # BODY.PEEK[HEADER] is the standard form every server takes.
            # We skip the body-preview FETCH entirely; subject + sender
            # is enough for the classifier.
            typ, data = await client.uid(
                "fetch", str(uid), "BODY.PEEK[HEADER]"
            )
            log.info(f"mail_monitor[{name}] fetch uid={uid}: typ={typ} "
                     f"data_len={len(data) if data else 0}")
            if typ != "OK" or not data:
                log.warning(f"mail_monitor[{name}] fetch uid={uid} empty: "
                            f"typ={typ!r} data={data!r}")
                continue
            # The header content is the LARGEST bytes entry in the
            # response. Joining all of them (previous approach) gave
            # email.message_from_bytes a salad of FETCH-protocol framing
            # + header bytes, so it returned empty From/Subject.
            byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
            if not byte_items:
                preview = " | ".join(repr(d)[:120] for d in data)
                log.warning(f"mail_monitor[{name}] fetch uid={uid} no bytes "
                            f"in response; raw={preview}")
                continue
            raw = max(byte_items, key=len)
            msg = email.message_from_bytes(raw)
            from_parsed = parseaddr(msg.get("From", ""))
            sender = _decode_header(from_parsed[0]) or msg.get("From", "")
            sender_email = (from_parsed[1] or "").lower()
            subject = _decode_header(msg.get("Subject"))
            if not sender and not subject:
                # Parser came back empty — this is a system/protocol message
                # (e.g. IMAP server notification, DSN with no envelope).
                # Skip it entirely — forwarding it produces a useless
                # "no subject, no content" notification.
                preview = raw[:300].decode(errors="replace")
                log.warning(f"mail_monitor[{name}] uid={uid} empty headers, skipping; "
                            f"raw[0:300]={preview!r}")
                continue

            # Auto-resolve follow-ups: if this incoming mail is a reply to a
            # tracked sent mail, mark it as answered in followup_tracker.
            try:
                import followup_tracker as _ft
                in_reply_to = msg.get("In-Reply-To", "").strip()
                references = msg.get("References", "").strip()
                mids_to_check = ([in_reply_to] if in_reply_to else []) + references.split()
                for _mid in mids_to_check:
                    _mid = _mid.strip()
                    if _mid and _ft.resolve_followup(_mid):
                        log.info(
                            f"mail_monitor[{name}] uid={uid}: "
                            f"follow-up aufgeloest fuer message-id {_mid!r}"
                        )
                        break
            except Exception as _exc:
                log.debug(
                    f"mail_monitor[{name}] uid={uid}: followup resolve: {_exc}"
                )

            # Jarvis-Trigger (Issue #159): Betreff enthält 'jarvis' (case-insensitive)
            # → separater Verarbeitungspfad, normale Klassifikation überspringen.
            if _is_jarvis_trigger(subject):
                try:
                    await _handle_jarvis_trigger(account, client, uid, subject, sender)
                except Exception as e:
                    log.warning(
                        f"mail_monitor[{name}] uid={uid}: jarvis-trigger failed: "
                        f"{type(e).__name__}: {e}"
                    )
                continue

            # We only fetch BODY.PEEK[HEADER] (Apple-strict-parser-friendly),
            # so the classifier runs on sender + subject only. Empty body
            # preview by design.
            category, reply_needed = await _classify(sender, subject, "")
            log.info(f"mail_monitor[{name}] uid={uid} sender={sender!r} "
                     f"subject={subject!r} -> {category} reply_needed={reply_needed}")

            # Personen-Drift-Detection (Issue #55) — nur fuer
            # forward-eligible Mails, sonst flutet Werbung den Kontakt-
            # Vorschlag. Wenn Drift erkannt: spezielle Voice-Note +
            # pending_person_action setzen, normalen Push ueberspringen.
            # On non-macOS (Raspberry Pi) Apple Contacts is inaccessible,
            # so drift detection is skipped to avoid spurious "Soll ich
            # anlegen?" questions for contacts that already exist.
            if category in S.MAIL_MONITOR_FORWARD and _MACOS:
                try:
                    drift = await contact_sync.check_mail_for_drift(
                        msg, sender_email, sender,
                    )
                except Exception as e:
                    log.warning(f"mail_monitor[{name}] drift check failed: "
                                f"{type(e).__name__}: {e}")
                    drift = None
                if drift:
                    if drift["kind"] == "new_person":
                        # Lass Claude Anrede / Funktion / Organisation aus
                        # der Mail raten — Catrin bestaetigt dann ein
                        # vollstaendigeres Profil.
                        try:
                            details = await contact_sync.extract_person_details(
                                msg, sender_email, sender,
                            )
                        except Exception as e:
                            log.warning(f"mail_monitor[{name}] extract_person_details failed: "
                                        f"{type(e).__name__}: {e}")
                            details = {
                                "name": drift["name"], "email": drift["email"],
                                "anrede": "", "funktion": "", "organization": "",
                            }
                        pp = session_state.PendingPersonAction(
                            kind="new_person",
                            name=details["name"] or drift["name"],
                            new_email=drift["email"],
                            extra_phones=drift.get("phones", []),
                            anrede=details.get("anrede", ""),
                            funktion=details.get("funktion", ""),
                            organization=details.get("organization", ""),
                        )
                        # Voice-Note mit den Detail-Feldern
                        detail_bits: list[str] = []
                        if details.get("funktion"):
                            detail_bits.append(details["funktion"])
                        if details.get("organization") and details["organization"] not in (
                            details.get("funktion", "")
                        ):
                            detail_bits.append(details["organization"])
                        detail_str = ", ".join(detail_bits)
                        spoken = (
                            f"{pp.name}"
                            + (f" ({detail_str})" if detail_str else "")
                            + " ist mir noch nicht in den Kontakten. "
                            f"Soll ich anlegen?"
                        )
                    elif drift["kind"] == "email_drift":
                        c = drift["contact"]
                        old = (drift.get("old_emails") or [""])[0]
                        pp = session_state.PendingPersonAction(
                            kind="email_drift",
                            contact_id=c.id,
                            name=c.name,
                            new_email=drift["new_email"],
                        )
                        spoken = (
                            f"{c.name} schreibt jetzt von {drift['new_email']}"
                            + (f" — bisher {old}" if old else "")
                            + ". Soll ich die Adresse aktualisieren?"
                        )
                    elif drift["kind"] == "phone_drift":
                        c = drift["contact"]
                        pp = session_state.PendingPersonAction(
                            kind="phone_drift",
                            contact_id=c.id,
                            name=c.name,
                            new_phone=drift["new_phone"],
                        )
                        spoken = (
                            f"{c.name} hat in der Signatur eine Nummer die ich "
                            f"nicht kenne: {drift['new_phone']}. "
                            f"Soll ich die im Kontakt ergaenzen?"
                        )
                    else:
                        pp = None
                        spoken = ""
                    if pp:
                        session_state.set_pending_person("default", pp)
                        _mail_ref_drift = session_state.MailRef(
                            account=name, uid=uid, sender=sender, subject=subject,
                            date=msg.get("Date", ""),
                            message_id=msg.get("Message-ID", ""),
                            references=msg.get("References", ""),
                        )
                        session_state.broadcast_active_mail(_mail_ref_drift)
                        if not S.is_quiet_hours():
                            await telegram_bot.send_user_voice(
                                spoken, caption=spoken, mail_ref=_mail_ref_drift
                            )
                        if not S.is_mac_quiet_hours() and _mail_alert_handler is not None:
                            try:
                                await _mail_alert_handler(spoken)
                            except Exception as e:
                                log.warning(f"mail_monitor[{name}] mac alert (drift) failed: "
                                            f"{type(e).__name__}: {e}")
                        log.info(f"mail_monitor[{name}] uid={uid}: drift {drift['kind']} pending")
                        # Issue #116: Mail-Inhalt zusaetzlich zur Kontaktfrage zeigen
                        try:
                            body_data = await mail_actions.read_mail_body(name, uid)
                            if body_data.get("text"):
                                body_summary = await _summarize_body(
                                    sender, subject, body_data["text"]
                                )
                                if body_summary:
                                    summary_msg = (
                                        f"📧 {sender} | {subject}\n{body_summary}"
                                    )
                                    await telegram_bot.send_user_text(summary_msg)
                        except Exception as e:
                            log.warning(
                                f"mail_monitor[{name}] drift body summary failed: "
                                f"{type(e).__name__}: {e}"
                            )
                        continue

            # Kalender-Einladung erkannt? (Stage 5)
            ics_invite = mail_actions.extract_calendar_invite(msg)
            if ics_invite:
                when_human = mail_actions.format_calendar_when(ics_invite.get("dtstart", ""))
                log.info(f"mail_monitor[{name}] uid={uid}: calendar invite "
                         f"summary={ics_invite.get('summary')!r} when={when_human}")
                # active_mail + pending_calendar setzen, ohne Auto-Triage,
                # damit Catrin entscheidet.
                _mail_ref_cal = session_state.MailRef(
                    account=name, uid=uid, sender=sender, subject=subject,
                    date=msg.get("Date", ""),
                    message_id=msg.get("Message-ID", ""),
                    references=msg.get("References", ""),
                )
                session_state.broadcast_active_mail(_mail_ref_cal)
                session_state.set_pending_calendar(
                    "default",
                    session_state.PendingCalendar(
                        summary=ics_invite.get("summary", subject),
                        dtstart=ics_invite.get("dtstart", ""),
                        dtend=ics_invite.get("dtend", ""),
                        when_human=when_human,
                        location=ics_invite.get("location", ""),
                        organizer=ics_invite.get("organizer", ""),
                    ),
                )
                tg_quiet = S.is_quiet_hours()
                mac_quiet = S.is_mac_quiet_hours()
                spoken = (
                    f"Eine Termin-Einladung von {sender}, "
                    f"{ics_invite.get('summary', subject)}"
                    + (f", am {when_human}" if when_human else "")
                    + ". Soll ich den Termin eintragen?"
                )
                caption = (
                    f"\U0001F4C5 Termin-Einladung [{name}]\n"
                    f"Von: {sender}\nBetreff: {subject}\n"
                    f"Termin: {when_human or ics_invite.get('dtstart', '?')}"
                )
                if tg_quiet:
                    log.info(f"mail_monitor[{name}] uid={uid}: telegram quiet hours, suppressed (calendar)")
                else:
                    await telegram_bot.send_user_voice(spoken, caption=caption, mail_ref=_mail_ref_cal)
                if mac_quiet:
                    log.info(f"mail_monitor[{name}] uid={uid}: mac quiet hours, suppressed (calendar)")
                elif _mail_alert_handler is not None:
                    try:
                        await _mail_alert_handler(spoken)
                    except Exception as e:
                        log.warning(f"mail_monitor[{name}] mac alert (cal) failed: "
                                    f"{type(e).__name__}: {e}")
                continue

            # Auto-Triage zuerst pruefen — Sender-Regeln, Heuristiken
            # (Bounce/Paket/Reise/Newsletter), und werbung_action.
            # Voller From-Header ("Name <email@domain>") uebergeben damit
            # from_contains auf Domain matchen kann, nicht nur Anzeigenamen.
            triage_sender = msg.get("From", "") or sender
            try:
                triage = mail_triage.route(triage_sender, subject, category, msg=msg, account=name)
            except Exception as e:
                log.warning(f"mail_monitor[{name}] uid={uid}: triage failed: {type(e).__name__}: {e}")
                triage = {"action": "none"}
            if triage["action"] != "none":
                log.info(f"mail_monitor[{name}] uid={uid}: triage -> {triage}")
                import activity_log as _al
                import datetime as _dt_triage
                _al.log_action("mail_triage")
                _triage_label = {
                    "mark_read": "als gelesen markiert",
                    "move": f"verschoben nach {triage.get('folder', 'Junk')}",
                    "forward": f"weitergeleitet an {triage.get('to', '?')}",
                }.get(triage["action"], triage["action"])
                _al.log_action(
                    "mail_processed",
                    f"{_dt_triage.datetime.now().strftime('%H:%M')} | {sender} | {subject} | {_triage_label}",
                )
                if triage["action"] == "mark_read":
                    await mail_actions.mark_mail_read(name, uid)
                elif triage["action"] == "move":
                    folder = triage.get("folder", "Junk")
                    await mail_actions.mark_mail_read(name, uid)
                    await mail_actions.move_mail(name, uid, folder)
                elif triage["action"] == "forward":
                    to_addr = triage.get("to", "")
                    if to_addr:
                        ok = await mail_actions.forward_mail(name, uid, to_addr)
                        log.info(f"mail_monitor[{name}] uid={uid}: forward -> {to_addr}: {ok}")
                        # After forwarding: also archive
                        and_then = triage.get("and_then", "mark_read")
                        if and_then == "move":
                            await mail_actions.move_mail(name, uid, triage.get("folder", "Junk"))
                        else:
                            await mail_actions.mark_mail_read(name, uid)
                # Triage handled it — skip the normal forward/notify path
                continue

            if category in S.MAIL_MONITOR_FORWARD:
                # Prior-context aus persons_db VOR dem Lernen abfragen,
                # damit nur echte fruehere Eintraege (nicht der aktuelle) angezeigt werden.
                prior_context = ""
                if sender_email:
                    try:
                        import persons_db as _pdb
                        _profile = _pdb.find_by_email(sender_email)
                        if _profile and _profile.notes:
                            # Letzten 2 Eintraege (ohne den heute gerade ankommenden)
                            today_prefix = __import__("datetime").date.today().isoformat()
                            old_notes = [n for n in _profile.notes if not n.startswith(today_prefix)]
                            if old_notes:
                                prior_context = "Bekannt: " + " | ".join(old_notes[-2:])
                    except Exception:
                        pass

                # Passive learning (Issue #102): index sender + subject so
                # future draft replies have better context.
                try:
                    _learn_from_mail(name, uid, sender, sender_email, subject, msg)
                except Exception as e:
                    log.debug(f"mail_monitor[{name}] learn failed: {type(e).__name__}: {e}")

                # Egal ob's geforwarded wird oder nicht: in den Session-
                # State, damit Catrin gleich darauf referenzieren kann
                # ("vorlesen", "antworten", "Aufgabe daraus").
                _mail_ref = session_state.MailRef(
                    account=name, uid=uid, sender=sender, subject=subject,
                    date=msg.get("Date", ""),
                    message_id=msg.get("Message-ID", ""),
                    references=msg.get("References", ""),
                    reply_needed=reply_needed,
                )
                session_state.broadcast_active_mail(_mail_ref)
                tg_quiet = S.is_quiet_hours()
                mac_quiet = S.is_mac_quiet_hours()
                spoken = _format_for_voice(sender, subject, reply_needed=reply_needed)
                # Fetch body + summarize so Catrin can decide Mail/Aufgabe/Absender
                summary = ""
                try:
                    body_data = await mail_actions.read_mail_body(name, uid)
                    if "text" in body_data and body_data["text"]:
                        summary = await _summarize_body(sender, subject, body_data["text"])
                except Exception as e:
                    log.warning(f"mail_monitor[{name}] summary fetch failed: {type(e).__name__}: {e}")
                caption = _format_for_telegram(name, sender, subject, category,
                                               summary=summary, reply_needed=reply_needed,
                                               prior_context=prior_context)
                # Telegram: voice-note + caption, sofern nicht in
                # Telegram-Quiet-Hours.
                import activity_log as _al_notify
                import datetime as _dt_notify
                _al_notify.log_action(
                    "mail_processed",
                    f"{_dt_notify.datetime.now().strftime('%H:%M')} | {sender} | {subject} | gemeldet",
                )
                if tg_quiet:
                    log.info(f"mail_monitor[{name}] uid={uid}: telegram quiet hours, suppressed")
                else:
                    await telegram_bot.send_user_voice(spoken, caption=caption, mail_ref=_mail_ref)
                # Mac-Ansage zusaetzlich, sofern nicht in Mac-Quiet-
                # Hours UND eine Web-UI verbunden ist (der Handler
                # prueft das selbst).
                if mac_quiet:
                    log.info(f"mail_monitor[{name}] uid={uid}: mac quiet hours, suppressed")
                elif _mail_alert_handler is not None:
                    try:
                        await _mail_alert_handler(spoken)
                    except Exception as e:
                        log.warning(f"mail_monitor[{name}] mac alert failed: "
                                    f"{type(e).__name__}: {e}")
        except asyncio.CancelledError:
            # Server shutdown mid-processing: the finally block below
            # persists the current UID before we re-raise so asyncio can
            # cleanly terminate the task without double-processing on the
            # next server start (Issue #78).
            raise
        except Exception as e:
            log.warning(f"mail_monitor[{name}] uid={uid}: {type(e).__name__}: {e}")
        finally:
            _max_seen[name] = max(_max_seen.get(name, 0), uid)
            _save_state(name, _max_seen[name])


async def _baseline_uid(client, folder: str) -> int:
    """Highest currently-assigned UID via STATUS UIDNEXT.

    UID SEARCH ALL would be the obvious choice, but Apple iCloud rejects
    UID SEARCH (only allows COPY/FETCH/EXPUNGE/STORE). STATUS UIDNEXT
    works on every IMAP server.
    """
    typ, data = await client.status(folder, "(UIDNEXT)")
    if typ != "OK" or not data:
        return 0
    joined = b" ".join(d for d in data if isinstance(d, (bytes, bytearray)))
    m = re.search(rb"UIDNEXT (\d+)", joined)
    return (int(m.group(1)) - 1) if m else 0


async def _uids_in_range(client, low_uid: int, high_uid: int) -> list[int]:
    """Return UIDs in (low_uid, high_uid] via UID FETCH with explicit
    bounds. Avoids the '*' wildcard which Apple iCloud sometimes
    handles unexpectedly."""
    if high_uid <= low_uid:
        return []
    typ, data = await client.uid(
        "fetch", f"{low_uid + 1}:{high_uid}", "UID"
    )
    if typ != "OK" or not data:
        return []
    uids: list[int] = []
    for item in data:
        if isinstance(item, (bytes, bytearray)):
            for m in re.finditer(rb"UID (\d+)", item):
                u = int(m.group(1))
                if low_uid < u <= high_uid:
                    uids.append(u)
    return sorted(set(uids))


def _resp_summary(resp) -> str:
    """Stringify aioimaplib Response (or (typ, lines) tuple) for logging."""
    try:
        result = getattr(resp, "result", None) or (resp[0] if resp else "?")
        lines = getattr(resp, "lines", None) or (resp[1] if resp and len(resp) > 1 else [])
        text = " ".join(
            line.decode(errors="replace") if isinstance(line, (bytes, bytearray)) else str(line)
            for line in (lines or [])
        )
        return f"{result} {text}".strip()
    except Exception:
        return repr(resp)


async def _idle_session(account: dict, aioimaplib_module) -> None:
    """One IMAP login + IDLE cycle for one account. Returns when the
    connection drops."""
    name = account["name"]
    cls = aioimaplib_module.IMAP4_SSL if account["ssl"] else aioimaplib_module.IMAP4
    client = cls(host=account["host"], port=account["port"], timeout=60)
    log.info(f"mail_monitor[{name}] connecting to {account['host']}:{account['port']}…")
    try:
        await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
    except asyncio.TimeoutError:
        raise RuntimeError(f"IMAP greeting timeout after 30s ({account['host']}:{account['port']})")

    try:
        login_resp = await asyncio.wait_for(
            client.login(account["user"], account["password"]), timeout=30
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"LOGIN timeout after 30s for user={account['user']!r}")
    if getattr(login_resp, "result", None) != "OK":
        raise RuntimeError(
            f"LOGIN rejected for user={account['user']!r}: {_resp_summary(login_resp)}"
        )
    log.info(f"mail_monitor[{name}] login ok")

    # aioimaplib caches the pre-login CAPABILITY list. Apple iCloud
    # (among others) only advertises IDLE *after* authentication, so
    # idle_start() raises Abort('server has not IDLE capability') even
    # though the server fully supports IDLE.
    #
    # Workaround: read the cached caps for diagnostics, then force-inject
    # IDLE. aioimaplib's check is only against the cached set; if IDLE
    # is in there, the actual IDLE command goes out and the server
    # accepts it.
    caps_obj = getattr(client.protocol, "capabilities", None)
    caps_str = " ".join(sorted(str(c) for c in (caps_obj or []))) or "(none)"
    log.info(f"mail_monitor[{name}] cached capabilities: {caps_str}")
    if caps_obj is not None and "IDLE" not in caps_obj:
        injected = False
        for adder in ("add", "append"):
            fn = getattr(caps_obj, adder, None)
            if callable(fn):
                try:
                    fn("IDLE")
                    injected = True
                    break
                except (TypeError, AttributeError):
                    pass
        if injected:
            log.info(f"mail_monitor[{name}] forced IDLE into capabilities")
        else:
            log.warning(f"mail_monitor[{name}] could not inject IDLE "
                        f"(caps type={type(caps_obj).__name__})")

    # One-time diagnostic: log all available folders + their UIDNEXT.
    # Catrin's iCloud test mails increment INBOX UIDNEXT but UID FETCH
    # returns nothing for those UIDs — heuristic says the mails landed
    # in a different mailbox. The folder list will tell us where.
    try:
        list_resp = await client.list('""', "*")
        if getattr(list_resp, "result", None) == "OK":
            folders: list[str] = []
            for line in list_resp.lines or []:
                if isinstance(line, (bytes, bytearray)):
                    text = line.decode(errors="replace")
                    # Folder name is the last quoted string on the line.
                    quoted = re.findall(r'"([^"]+)"', text)
                    if quoted:
                        folders.append(quoted[-1])
            log.info(f"mail_monitor[{name}] folders ({len(folders)}): "
                     f"{', '.join(folders)}")
    except Exception as e:
        log.info(f"mail_monitor[{name}] folder LIST skipped: "
                 f"{type(e).__name__}: {e}")

    select_resp = await client.select(account["folder"])
    if getattr(select_resp, "result", None) != "OK":
        raise RuntimeError(
            f"SELECT {account['folder']!r} failed: {_resp_summary(select_resp)}"
        )

    if _max_seen.get(name, 0) == 0:
        baseline = await _baseline_uid(client, account["folder"])
        _max_seen[name] = baseline
        _save_state(name, baseline)
        log.info(f"mail_monitor[{name}] baseline UID = {baseline}")
    else:
        server_max = await _baseline_uid(client, account["folder"])
        if server_max > _max_seen[name]:
            new_uids = await _uids_in_range(client, _max_seen[name], server_max)
            if not new_uids:
                new_uids = list(range(_max_seen[name] + 1, server_max + 1))
            log.info(f"mail_monitor[{name}] catching up on {len(new_uids)} mail(s)")
            await _process_new_uids(account, client, new_uids)

    # Some servers (Apple iCloud especially) reject IDLE if it follows
    # SELECT too tightly. A NOOP between gives the server a beat to
    # settle the SELECT state.
    try:
        await client.noop()
    except Exception as e:
        log.info(f"mail_monitor[{name}] noop ignored: {type(e).__name__}: {e}")

    # Apple iCloud accepts IDLE but never sends EXISTS pushes — they use
    # the proprietary XAPPLEPUSHSERVICE protocol which aioimaplib doesn't
    # speak. Fall back to active polling: cheap (one UID FETCH every
    # 60 s), works on every server, max latency 60 s.
    poll_interval = 60
    log.info(f"mail_monitor[{name}]: polling-Loop aktiv (interval={poll_interval}s)")
    poll_count = 0
    while True:
        await asyncio.sleep(poll_interval)
        poll_count += 1

        # Jarvis-Trigger (Issue #159): prüfe ob vorgemerkte Mails
        # gelöscht werden sollen (24h-Frist abgelaufen).
        try:
            await _flush_jarvis_deletes([account])
        except Exception as _e:
            log.debug("mail_monitor[%s] flush_jarvis_deletes: %s: %s",
                      name, type(_e).__name__, _e)

        # STATUS UIDNEXT is the authoritative "highest UID assigned".
        # Cheaper than UID FETCH and tells us if there's anything new.
        server_max = await _baseline_uid(client, account["folder"])
        our_max = _max_seen.get(name, 0)
        if server_max > our_max:
            log.info(f"mail_monitor[{name}] poll #{poll_count}: server_max="
                     f"{server_max} > our_max={our_max}, fetching")
            new_uids = await _uids_in_range(client, our_max, server_max)
            if not new_uids:
                # UID FETCH returned nothing despite UIDNEXT signaling
                # new mail. Process the explicit range as fallback.
                log.warning(f"mail_monitor[{name}] UID FETCH returned no UIDs "
                            f"despite UIDNEXT={server_max + 1}; using explicit range")
                new_uids = list(range(our_max + 1, server_max + 1))
            await _process_new_uids(account, client, new_uids)
        else:
            await client.noop()


async def _account_loop(account: dict, aioimaplib_module) -> None:
    """Keep the IDLE session alive for one account, reconnecting on
    crash with back-off. Auth failures use a long back-off (30 min) to
    avoid triggering IP bans from repeated rapid login attempts."""
    name = account["name"]
    while True:
        try:
            await _idle_session(account, aioimaplib_module)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = str(e)
            is_auth = "LOGIN rejected" in err or "AUTHENTICATIONFAILED" in err
            is_timeout = (isinstance(e, (asyncio.TimeoutError, TimeoutError))
                          or "TimeoutError" in type(e).__name__
                          or "timeout" in err.lower())
            if is_auth:
                log.warning(f"mail_monitor[{name}] auth failed: {e}; "
                            f"reconnect in 30min (avoid IP ban)")
                await asyncio.sleep(1800)
            elif is_timeout:
                log.warning(f"mail_monitor[{name}] connection timeout (server unresponsive / IP throttle?); "
                            f"reconnect in 10min")
                await asyncio.sleep(600)
            else:
                log.warning(f"mail_monitor[{name}] session crashed: "
                            f"{type(e).__name__}: {e}; reconnect in 30s")
                await asyncio.sleep(30)


async def mail_monitor_main() -> None:
    """Long-running task: open one IDLE connection per configured
    account in S.MAIL_MONITOR_ACCOUNTS, monitor forever."""
    if not S.MAIL_MONITOR_ENABLED:
        log.info("mail_monitor disabled (mail_monitor_enabled=false)")
        return
    if not S.MAIL_MONITOR_ACCOUNTS:
        log.warning("mail_monitor enabled but no mail_monitor_accounts configured")
        return
    try:
        import aioimaplib
    except ImportError:
        log.warning("aioimaplib not installed — mail_monitor disabled")
        return

    valid: list[dict] = []
    for acc in S.MAIL_MONITOR_ACCOUNTS:
        if not (acc["host"] and acc["user"] and acc["password"]):
            log.warning(f"mail_monitor[{acc['name']}]: incomplete config "
                        f"(missing host/user/password env {acc['env_key']}) — skipping")
            continue
        _max_seen[acc["name"]] = _load_state(acc["name"])
        valid.append(acc)
        log.info(f"mail_monitor[{acc['name']}] starting (host={acc['host']}, "
                 f"start_uid={_max_seen[acc['name']]})")

    if not valid:
        log.warning("mail_monitor: no usable accounts after config check")
        return

    log.info(f"mail_monitor active for {len(valid)} account(s); "
             f"forward_categories={S.MAIL_MONITOR_FORWARD}")
    inbox_tasks = [asyncio.create_task(_account_loop(acc, aioimaplib)) for acc in valid]
    sent_tasks = [
        asyncio.create_task(_sent_account_loop(acc, aioimaplib)) for acc in valid
    ]
    all_tasks = inbox_tasks + sent_tasks
    try:
        await asyncio.gather(*all_tasks)
    finally:
        for t in all_tasks:
            t.cancel()
