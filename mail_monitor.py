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
from email.utils import parseaddr

import settings as S
import telegram_bot

log = S.log


# UID-tracker per account. {account_name: max_seen_uid}
_max_seen: dict[str, int] = {}


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
    user_msg = f"Von: {sender}\nBetreff: {subject}\n\n{body_preview[:1500]}"
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=_CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        cat = resp.content[0].text.strip().lower()
        for token in cat.replace(",", " ").replace(".", " ").split():
            if token in ("werbung", "info", "handlungsbedarf"):
                return token
        log.info(f"mail_monitor: classifier returned {cat!r}, defaulting to 'info'")
        return "info"
    except Exception as e:
        log.warning(f"mail_monitor classify failed: {type(e).__name__}: {e}")
        return "unknown"


def _format_for_telegram(account_name: str, sender: str, subject: str, category: str) -> str:
    icon = {
        "handlungsbedarf": "🔴",
        "info": "🟡",
        "werbung": "⚪",
    }.get(category, "✉️")
    return (
        f"{icon} Neue Mail [{account_name}] ({category})\n"
        f"Von: {sender}\nBetreff: {subject}"
    )


# ---------------------------------------------------------------------------
# Per-account IDLE session
# ---------------------------------------------------------------------------
async def _process_new_uids(account: dict, client, uids: list[int]) -> None:
    name = account["name"]
    for uid in sorted(uids):
        if uid <= _max_seen.get(name, 0):
            continue
        try:
            typ, data = await client.uid(
                "fetch", str(uid).encode(),
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT]<0.2000>)"
            )
            if typ != "OK" or not data:
                continue
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
            log.info(f"mail_monitor[{name}] uid={uid} sender={sender!r} "
                     f"subject={subject!r} -> {category}")

            if category in S.MAIL_MONITOR_FORWARD:
                if S.is_quiet_hours():
                    log.info(f"mail_monitor[{name}] uid={uid}: quiet hours, suppressed")
                else:
                    text = _format_for_telegram(name, sender, subject, category)
                    await telegram_bot.send_user_text(text)
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


async def _uids_above(client, low_uid: int) -> list[int]:
    """Return UIDs > low_uid via UID FETCH (Apple iCloud safe)."""
    if low_uid < 0:
        low_uid = 0
    typ, data = await client.uid("fetch", f"{low_uid + 1}:*".encode(), "(UID)")
    if typ != "OK" or not data:
        return []
    uids: list[int] = []
    for item in data:
        if isinstance(item, (bytes, bytearray)):
            for m in re.finditer(rb"UID (\d+)", item):
                u = int(m.group(1))
                if u > low_uid:
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
    await client.wait_hello_from_server()

    login_resp = await client.login(account["user"], account["password"])
    if getattr(login_resp, "result", None) != "OK":
        raise RuntimeError(
            f"LOGIN rejected for user={account['user']!r}: {_resp_summary(login_resp)}"
        )
    log.info(f"mail_monitor[{name}] login ok")

    # Re-read CAPABILITY: many servers (Apple iCloud especially) only
    # advertise IDLE after authentication. Without this refresh,
    # client.idle_start() raises Abort('server has not IDLE capability').
    try:
        await client.capability()
        caps_iter = getattr(client.protocol, "capabilities", None) or []
        caps_str = " ".join(str(c) for c in caps_iter) or "(none)"
        log.info(f"mail_monitor[{name}] post-login capabilities: {caps_str}")
    except Exception as e:
        log.info(f"mail_monitor[{name}] capability refresh skipped: "
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
        new_uids = await _uids_above(client, _max_seen[name])
        if new_uids:
            log.info(f"mail_monitor[{name}] catching up on {len(new_uids)} mail(s)")
            await _process_new_uids(account, client, new_uids)

    # Some servers (Apple iCloud especially) reject IDLE if it follows
    # SELECT too tightly. A NOOP between gives the server a beat to
    # settle the SELECT state.
    try:
        await client.noop()
    except Exception as e:
        log.info(f"mail_monitor[{name}] noop ignored: {type(e).__name__}: {e}")

    log.info(f"mail_monitor[{name}]: IDLE-Loop aktiv")
    while True:
        idle_task = await client.idle_start(timeout=29 * 60)
        msg = await client.wait_server_push()
        client.idle_done()
        try:
            await asyncio.wait_for(idle_task, timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass

        if msg and any(b"EXISTS" in m for m in msg if isinstance(m, (bytes, bytearray))):
            new_uids = await _uids_above(client, _max_seen.get(name, 0))
            if new_uids:
                await _process_new_uids(account, client, new_uids)


async def _account_loop(account: dict, aioimaplib_module) -> None:
    """Keep the IDLE session alive for one account, reconnecting on
    crash with 30 s back-off."""
    name = account["name"]
    while True:
        try:
            await _idle_session(account, aioimaplib_module)
        except asyncio.CancelledError:
            raise
        except Exception as e:
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
    tasks = [asyncio.create_task(_account_loop(acc, aioimaplib)) for acc in valid]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
