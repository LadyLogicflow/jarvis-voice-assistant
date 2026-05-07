"""
Mail-Aktionen fuer den Decision-Tree (Issue #49).

Stellt kurze IMAP-Operationen bereit, die unabhaengig vom langlebigen
mail_monitor.IDLE-Loop laufen — pro Aufruf eine eigene Connection,
die nach Ende sofort geschlossen wird. Vermeidet das Teilen von State
mit dem Polling-Loop.

Aktionen:
- read_mail_body(account, uid)        -> Body-Text + Header-Felder
- mark_mail_read(account, uid)        -> setzt IMAP \\Seen-Flag
- build_reply_message(...)            -> RFC822-Bytes fuer IMAP APPEND
- append_to_drafts(account, bytes)    -> Entwurf in Drafts-Ordner ablegen
"""

from __future__ import annotations

import email
import email.header
import email.utils
import re
from email.message import EmailMessage
from email.utils import parseaddr

import settings as S

log = S.log


# Maximal-Laenge fuer den vorgelesenen Body. Apple liest TTS sonst
# minutenlang vor.
MAX_BODY_CHARS = 1500


def _account_by_name(name: str) -> dict | None:
    """Find the account dict (with password) in MAIL_MONITOR_ACCOUNTS."""
    for acc in S.MAIL_MONITOR_ACCOUNTS:
        if acc.get("name") == name:
            return acc
    return None


def _decode_header(raw):
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


def _html_to_text(html: str) -> str:
    """Best-effort: strip HTML tags + collapse whitespace. We don't want
    a heavy dependency for one feature."""
    no_script = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
    no_style = re.sub(r"<style[\s\S]*?</style>", "", no_script, flags=re.I)
    no_tags = re.sub(r"<[^>]+>", " ", no_style)
    # decode HTML entities (very basic — handles &amp; &lt; &gt; &quot; &nbsp;)
    txt = (no_tags.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">")
                  .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def _extract_text_from_email(msg: email.message.Message) -> str:
    """Pick the best text representation. Prefer text/plain, fall back
    to a stripped text/html."""
    if msg.is_multipart():
        plain_parts = []
        html_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    plain_parts.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    plain_parts.append(payload.decode("utf-8", errors="replace"))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    html_parts.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    html_parts.append(payload.decode("utf-8", errors="replace"))
        if plain_parts:
            return "\n".join(plain_parts).strip()
        if html_parts:
            return _html_to_text("\n".join(html_parts))
        return ""
    # Single-part: payload may already be a str or bytes
    payload = msg.get_payload(decode=True) or b""
    if isinstance(payload, bytes):
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
    else:
        text = str(payload)
    if msg.get_content_type() == "text/html":
        return _html_to_text(text)
    return text.strip()


async def _connect(account: dict):
    """Open a fresh IMAP connection + login + select folder. Returns a
    ready-to-use aioimaplib client."""
    import aioimaplib  # local import; only needed for these actions
    cls = aioimaplib.IMAP4_SSL if account["ssl"] else aioimaplib.IMAP4
    client = cls(host=account["host"], port=account["port"], timeout=30)
    await client.wait_hello_from_server()
    resp = await client.login(account["user"], account["password"])
    if getattr(resp, "result", None) != "OK":
        raise RuntimeError(f"LOGIN rejected for {account['user']!r}")
    sel = await client.select(account["folder"])
    if getattr(sel, "result", None) != "OK":
        raise RuntimeError(f"SELECT {account['folder']!r} failed")
    return client


async def read_mail_body(account_name: str, uid: int) -> dict:
    """Hol Body + Header der Mail. Gibt ein Dict zurueck mit
    sender/subject/date/text — text ist auf MAX_BODY_CHARS gekuerzt.
    Bei Fehler: dict mit 'error'-Key."""
    acc = _account_by_name(account_name)
    if not acc:
        return {"error": f"Konto {account_name!r} nicht konfiguriert"}
    if not acc["password"]:
        return {"error": f"Anmeldung fuer {account_name!r} fehlt in .env"}

    client = None
    try:
        client = await _connect(acc)
        # Same FETCH form that proved to work on Apple iCloud:
        # plain str UID, no extra parens around the atom.
        typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[]")
        if typ != "OK" or not data:
            return {"error": f"FETCH lieferte nichts (typ={typ})"}
        byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
        if not byte_items:
            return {"error": "keine bytes in FETCH-Antwort"}
        raw = max(byte_items, key=len)
        msg = email.message_from_bytes(raw)
        sender = _decode_header(parseaddr(msg.get("From", ""))[0]) or msg.get("From", "")
        # reply_to: the address Jarvis should direct replies to.
        # RFC 2822: Reply-To has precedence over From for replies.
        raw_reply_to = msg.get("Reply-To", "")
        reply_to = parseaddr(raw_reply_to)[1].strip() if raw_reply_to else ""
        subject = _decode_header(msg.get("Subject"))
        date = msg.get("Date", "")
        body = _extract_text_from_email(msg)
        # Truncate (TTS-friendly).
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rsplit(" ", 1)[0] + " ..."
        return {
            "sender": sender,
            "reply_to": reply_to,
            "subject": subject,
            "date": date,
            "text": body,
        }
    except Exception as e:
        log.warning(f"read_mail_body[{account_name}] uid={uid}: "
                    f"{type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Drafts: build RFC822 + IMAP APPEND
# ---------------------------------------------------------------------------
DRAFTS_FOLDER_GUESSES = (
    "Drafts",
    "Entwürfe",  # Entwürfe
    "Entwuerfe",
    "INBOX.Drafts",
    "INBOX.Entwürfe",
)


def build_reply_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> bytes:
    """Construct an RFC822 reply ready for IMAP APPEND.

    Adds Re: prefix if missing, In-Reply-To + References headers so the
    reply threads correctly in Apple Mail / Outlook / etc.
    """
    msg = EmailMessage()
    msg["From"] = from_addr or ""
    msg["To"] = to_addr or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg["Subject"] = subject or "Re:"
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = f"{references} {in_reply_to}"
        else:
            msg["References"] = in_reply_to
    elif references:
        msg["References"] = references
    msg.set_content(body or "")
    return msg.as_bytes()


async def append_to_drafts(account_name: str, msg_bytes: bytes) -> tuple[bool, str]:
    """Try to APPEND a message to the Drafts folder. Tries common
    folder names (Drafts / Entwürfe / INBOX.Drafts...) until one
    accepts the APPEND. Returns (ok, folder_or_error)."""
    acc = _account_by_name(account_name)
    if not acc:
        return False, f"Konto {account_name!r} nicht konfiguriert"
    if not acc["password"]:
        return False, f"Anmeldung fuer {account_name!r} fehlt in .env"

    client = None
    try:
        client = await _connect(acc)
        last_err = "kein Drafts-Folder gefunden"
        for folder in DRAFTS_FOLDER_GUESSES:
            try:
                resp = await client.append(msg_bytes, mailbox=folder)
                # aioimaplib's append returns Response(result, lines)
                result = getattr(resp, "result", None) or (
                    resp[0] if isinstance(resp, tuple) and resp else None
                )
                if result == "OK":
                    log.info(f"append_to_drafts[{account_name}] -> {folder!r}")
                    return True, folder
                last_err = f"folder={folder!r} typ={result}"
            except Exception as e:
                last_err = f"folder={folder!r} {type(e).__name__}: {e}"
                continue
        log.warning(f"append_to_drafts[{account_name}] failed: {last_err}")
        return False, last_err
    except Exception as e:
        log.warning(f"append_to_drafts[{account_name}]: {type(e).__name__}: {e}")
        return False, f"{type(e).__name__}: {e}"
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass


def extract_calendar_invite(msg) -> dict | None:
    """Wenn die Mail einen Kalender-Termin enthaelt (text/calendar oder
    .ics-Anhang), gib ein dict mit den wichtigen Feldern zurueck.

    Felder: summary, dtstart, dtend, location, description, organizer.
    Datums-Werte als rohes ICS-Format (z.B. '20260507T140000' oder
    '20260507T120000Z') — der Aufrufer formatiert weiter."""
    candidates: list[bytes] = []
    if msg is None:
        return None
    if not msg.is_multipart():
        if msg.get_content_type() == "text/calendar":
            payload = msg.get_payload(decode=True)
            if payload:
                candidates.append(payload)
    else:
        for part in msg.walk():
            if part.get_content_type() == "text/calendar":
                payload = part.get_payload(decode=True)
                if payload:
                    candidates.append(payload)
                continue
            filename = (part.get_filename() or "").lower()
            if filename.endswith(".ics"):
                payload = part.get_payload(decode=True)
                if payload:
                    candidates.append(payload)
    if not candidates:
        return None
    # Use the first found ICS payload
    raw = candidates[0]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    # RFC 5545 line-folding: lines starting with space/tab continue
    # the previous logical line.
    folded: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and folded:
            folded[-1] += line[1:]
        else:
            folded.append(line)
    info: dict = {}
    for line in folded:
        if ":" not in line:
            continue
        key_part, val = line.split(":", 1)
        key = key_part.split(";")[0].upper()
        if key in ("DTSTART", "DTEND", "SUMMARY", "LOCATION",
                   "DESCRIPTION", "ORGANIZER"):
            info[key.lower()] = val.strip()
    if not (info.get("summary") or info.get("dtstart")):
        return None
    return info


def format_calendar_when(ics_dt: str) -> str:
    """ICS DTSTART -> menschenlesbar wie '7. Mai 2026 um 14:00'."""
    import datetime
    if not ics_dt:
        return ""
    s = ics_dt.rstrip("Z")
    fmt_candidates = ["%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"]
    for fmt in fmt_candidates:
        try:
            dt = datetime.datetime.strptime(s, fmt)
            months = ["Januar", "Februar", "Maerz", "April", "Mai", "Juni",
                      "Juli", "August", "September", "Oktober", "November", "Dezember"]
            if "T" in s:
                return f"{dt.day}. {months[dt.month-1]} {dt.year} um {dt.strftime('%H:%M')}"
            return f"{dt.day}. {months[dt.month-1]} {dt.year}"
        except ValueError:
            continue
    return ics_dt


async def move_mail(account_name: str, uid: int, target_folder: str) -> bool:
    """Move a UID to a different folder via IMAP UID MOVE (RFC 6851).
    Falls back to UID COPY + UID STORE +FLAGS \\Deleted + EXPUNGE on
    servers without MOVE. Returns True on success.

    Apple iCloud supports UID MOVE so the fallback is rarely hit."""
    acc = _account_by_name(account_name)
    if not acc or not acc["password"]:
        return False
    client = None
    try:
        client = await _connect(acc)
        # Try MOVE first
        try:
            typ, data = await client.uid("move", str(uid), target_folder)
            if typ == "OK":
                log.info(f"move_mail[{account_name}] uid={uid} -> {target_folder!r}")
                return True
            log.info(f"move_mail[{account_name}] uid={uid} MOVE returned {typ}, "
                     f"trying COPY+DELETE fallback")
        except Exception as e:
            log.info(f"move_mail[{account_name}] MOVE not supported ({e}), "
                     f"trying COPY+DELETE fallback")
        # Fallback: COPY + STORE \\Deleted + EXPUNGE
        typ, data = await client.uid("copy", str(uid), target_folder)
        if typ != "OK":
            log.warning(f"move_mail[{account_name}] uid={uid} COPY failed: typ={typ}")
            return False
        await client.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        await client.expunge()
        log.info(f"move_mail[{account_name}] uid={uid} -> {target_folder!r} (via copy+delete)")
        return True
    except Exception as e:
        log.warning(f"move_mail[{account_name}] uid={uid}: {type(e).__name__}: {e}")
        return False
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass


async def forward_mail(account_name: str, uid: int, to_addr: str) -> bool:
    """Forward a mail (with all attachments) to a different address by
    fetching the original RFC822, wrapping in a forward header, and
    sending via SMTP. Returns True on success.

    Note: this actually sends — the only place in the code where
    Jarvis emits an outgoing message without explicit Catrin-approval.
    Used by the Hellomed-getmyinvoices auto-forward rule."""
    import smtplib
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    acc = _account_by_name(account_name)
    if not acc or not acc["password"]:
        return False
    client = None
    try:
        client = await _connect(acc)
        typ, data = await client.uid("fetch", str(uid), "BODY.PEEK[]")
        if typ != "OK" or not data:
            log.warning(f"forward_mail[{account_name}] uid={uid} fetch typ={typ}")
            return False
        byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
        if not byte_items:
            return False
        original_bytes = max(byte_items, key=len)
    except Exception as e:
        log.warning(f"forward_mail[{account_name}] fetch failed: "
                    f"{type(e).__name__}: {e}")
        return False
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass

    # Build forward message
    original = email.message_from_bytes(original_bytes)
    fwd = MIMEMultipart()
    fwd["From"] = acc["user"]
    fwd["To"] = to_addr
    orig_subj = _decode_header(original.get("Subject", ""))
    fwd["Subject"] = (orig_subj if orig_subj.lower().startswith("fwd:")
                      else f"Fwd: {orig_subj}")
    fwd["Date"] = email.utils.formatdate(localtime=True)
    fwd["Message-ID"] = email.utils.make_msgid()
    fwd_body = (
        f"Automatische Weiterleitung durch Jarvis.\n"
        f"---------- Original ----------\n"
        f"From: {original.get('From', '')}\n"
        f"Date: {original.get('Date', '')}\n"
        f"Subject: {orig_subj}\n"
    )
    fwd.attach(MIMEText(fwd_body, "plain", "utf-8"))
    # Attach the original as message/rfc822 — Apple Mail / Outlook
    # render this as a forwarded mail with all attachments preserved.
    from email.mime.message import MIMEMessage
    fwd.attach(MIMEMessage(original))

    # Send via SMTP. Server is the same hostname as IMAP for most providers.
    smtp_host = acc.get("smtp_host", acc["host"])
    smtp_port = int(acc.get("smtp_port", 587))
    try:
        loop = __import__("asyncio").get_event_loop()
        def _send():
            ssl_context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
                s.starttls(context=ssl_context)
                s.login(acc["user"], acc["password"])
                s.send_message(fwd)
        await loop.run_in_executor(None, _send)
        log.info(f"forward_mail[{account_name}] uid={uid} -> {to_addr}")
        return True
    except Exception as e:
        log.warning(f"forward_mail[{account_name}] SMTP failed: "
                    f"{type(e).__name__}: {e}")
        return False


async def mark_mail_read(account_name: str, uid: int) -> bool:
    """Set the \\Seen flag on a UID via UID STORE +FLAGS. Returns True
    on success, False on any failure (logged)."""
    acc = _account_by_name(account_name)
    if not acc or not acc["password"]:
        log.warning(f"mark_mail_read[{account_name}]: account not configured")
        return False
    client = None
    try:
        client = await _connect(acc)
        # Apple iCloud rejects the bare flag form. Standard IMAP form
        # is "(\Seen)" — parens around the flag list.
        typ, data = await client.uid("store", str(uid), "+FLAGS", "(\\Seen)")
        log.info(f"mark_mail_read[{account_name}] uid={uid}: STORE typ={typ}")
        if typ != "OK":
            log.warning(f"mark_mail_read[{account_name}] uid={uid}: "
                        f"STORE failed typ={typ} data={data!r}")
            return False
        return True
    except Exception as e:
        log.warning(f"mark_mail_read[{account_name}] uid={uid}: "
                    f"{type(e).__name__}: {e}")
        return False
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass
