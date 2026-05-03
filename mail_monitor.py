"""
IMAP IDLE Mail-Monitor (issue #48).

Hangs an IDLE connection on the configured INBOX and pushes only
relevant new mails to Telegram. "Relevant" is decided by Claude Haiku:
each new mail is classified into {werbung, info, handlungsbedarf}, and
only the categories listed in `S.MAIL_MONITOR_FORWARD` (default
['handlungsbedarf']) are forwarded.

Quiet hours (S.is_quiet_hours) suppress pushes — mails are still
classified and remembered as 'seen' so they don't show up at 7 AM.
"""

from __future__ import annotations

import asyncio
import datetime
import email
import email.header
import json
import os
from email.utils import parseaddr

import settings as S
import telegram_bot

log = S.log

# UID-tracker: only mails with UID > this are new since startup.
_max_seen_uid = 0
_state_path = os.path.join(os.path.dirname(__file__), ".jarvis_mail_seen.json")


def _load_state() -> int:
    if not os.path.exists(_state_path):
        return 0
    try:
        with open(_state_path) as f:
            return int(json.load(f).get("max_seen_uid", 0))
    except Exception:
        return 0


def _save_state(uid: int) -> None:
    try:
        with open(_state_path, "w") as f:
            json.dump({"max_seen_uid": uid}, f)
    except Exception as e:
        log.warning(f"mail_monitor: state save failed: {type(e).__name__}: {e}")


def _decode_header(raw: str | None) -> str:
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
    "Du bist ein E-Mail-Klassifikator. Antworte mit GENAU EINEM Wort, "
    "aus dieser Liste: werbung, info, handlungsbedarf.\n"
    "- werbung = Newsletter, Marketing, Werbeangebote, no-reply Marketing-Mails\n"
    "- info = Statusmeldungen, automatische Notifications die kein Handeln erfordern "
    "(Versandbestaetigungen, OAuth-Logins, etc.)\n"
    "- handlungsbedarf = Persoenliche Antworten erwartet, Termine, Fristen, Mandantensachen, "
    "wichtige Mitteilungen.\n"
    "Antworte NUR mit dem einen Wort, kein Satz, keine Erklaerung."
)


async def _classify(sender: str, subject: str, body_preview: str) -> str:
    """Returns one of: werbung | info | handlungsbedarf | unknown."""
    user_msg = f"Von: {sender}\nBetreff: {subject}\n\n{body_preview[:1500]}"
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=_CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        cat = resp.content[0].text.strip().lower()
        # Strip any punctuation; pick the first known token.
        for token in cat.replace(",", " ").replace(".", " ").split():
            if token in ("werbung", "info", "handlungsbedarf"):
                return token
        log.info(f"mail_monitor: classifier returned {cat!r}, defaulting to 'info'")
        return "info"
    except Exception as e:
        log.warning(f"mail_monitor classify failed: {type(e).__name__}: {e}")
        return "unknown"


def _format_for_telegram(sender: str, subject: str, category: str) -> str:
    icon = {
        "handlungsbedarf": "🔴",
        "info": "🟡",
        "werbung": "⚪",
    }.get(category, "✉️")
    return f"{icon} *Neue Mail* ({category})\nVon: {sender}\nBetreff: {subject}"


# ---------------------------------------------------------------------------
# IMAP IDLE loop
# ---------------------------------------------------------------------------
async def _process_new_uids(client, uids: list[int]) -> None:
    """Fetch + classify + maybe-forward each given UID."""
    global _max_seen_uid
    for uid in sorted(uids):
        if uid <= _max_seen_uid:
            continue
        try:
            typ, data = await client.uid("fetch", str(uid).encode(),
                                          "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] "
                                          "BODY.PEEK[TEXT]<0.2000>)")
            if typ != "OK" or not data:
                continue
            # data is a list of bytes; combine them into something we can
            # parse with email.message_from_bytes.
            raw_blocks = [b for b in data if isinstance(b, (bytes, bytearray)) and len(b) > 10]
            if not raw_blocks:
                continue
            raw = b"\n".join(raw_blocks)
            msg = email.message_from_bytes(raw)
            sender = _decode_header(parseaddr(msg.get("From", ""))[0]) or msg.get("From", "")
            subject = _decode_header(msg.get("Subject"))
            body_preview = msg.get_payload(decode=False) or ""
            if isinstance(body_preview, list):
                body_preview = ""
            body_preview = str(body_preview)

            category = await _classify(sender, subject, body_preview)
            log.info(f"mail_monitor uid={uid} sender={sender!r} subject={subject!r} "
                     f"-> {category}")

            if category in S.MAIL_MONITOR_FORWARD:
                if S.is_quiet_hours():
                    log.info(f"mail_monitor uid={uid}: quiet hours, suppressed")
                else:
                    text = _format_for_telegram(sender, subject, category)
                    await telegram_bot.send_user_text(text)
        except Exception as e:
            log.warning(f"mail_monitor uid={uid}: {type(e).__name__}: {e}")
        finally:
            _max_seen_uid = max(_max_seen_uid, uid)
            _save_state(_max_seen_uid)


async def _idle_session(aioimaplib_module) -> None:
    """One IMAP login + IDLE cycle. Returns when the connection drops."""
    global _max_seen_uid
    cls = aioimaplib_module.IMAP4_SSL if S.IMAP_SSL else aioimaplib_module.IMAP4
    client = cls(host=S.IMAP_HOST, port=S.IMAP_PORT, timeout=60)
    await client.wait_hello_from_server()
    await client.login(S.IMAP_USER, S.IMAP_PASSWORD)
    await client.select(S.MAIL_MONITOR_FOLDER)

    # Initialize: take whatever is currently the max UID as our baseline.
    typ, data = await client.uid("search", b"ALL")
    if typ == "OK" and data and data[0]:
        all_uids = [int(x) for x in data[0].split() if x.isdigit()]
        if all_uids:
            current_max = max(all_uids)
            if _max_seen_uid == 0:
                _max_seen_uid = current_max
                _save_state(_max_seen_uid)
                log.info(f"mail_monitor baseline UID = {_max_seen_uid}")
            elif current_max > _max_seen_uid:
                # Mails arrived while we were down — process them now.
                new_uids = [u for u in all_uids if u > _max_seen_uid]
                log.info(f"mail_monitor catching up on {len(new_uids)} mail(s)")
                await _process_new_uids(client, new_uids)

    log.info("mail_monitor: IDLE-Loop aktiv")
    while True:
        idle_task = await client.idle_start(timeout=29 * 60)  # IDLE max 29min per RFC
        msg = await client.wait_server_push()
        client.idle_done()
        try:
            await asyncio.wait_for(idle_task, timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass

        if msg and any(b"EXISTS" in m for m in msg if isinstance(m, (bytes, bytearray))):
            typ, data = await client.uid("search", b"ALL")
            if typ == "OK" and data and data[0]:
                all_uids = [int(x) for x in data[0].split() if x.isdigit()]
                new_uids = [u for u in all_uids if u > _max_seen_uid]
                if new_uids:
                    await _process_new_uids(client, new_uids)


async def mail_monitor_main() -> None:
    """Long-running task: connect to IMAP, IDLE forever, reconnect on
    drop. Skips silently if the feature is disabled or IMAP isn't
    configured."""
    global _max_seen_uid
    if not S.MAIL_MONITOR_ENABLED:
        log.info("mail_monitor disabled (mail_monitor_enabled=false)")
        return
    if not (S.IMAP_HOST and S.IMAP_USER and S.IMAP_PASSWORD):
        log.warning("mail_monitor enabled but IMAP credentials incomplete — skipping")
        return
    try:
        import aioimaplib
    except ImportError:
        log.warning("aioimaplib not installed — mail_monitor disabled")
        return

    _max_seen_uid = _load_state()
    log.info(f"mail_monitor starting (host={S.IMAP_HOST}, "
             f"forward_categories={S.MAIL_MONITOR_FORWARD}, "
             f"start_uid={_max_seen_uid})")

    while True:
        try:
            await _idle_session(aioimaplib)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"mail_monitor session crashed: {type(e).__name__}: {e}; "
                        f"reconnect in 30s")
            await asyncio.sleep(30)
