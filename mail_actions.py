"""
Mail-Aktionen fuer den Decision-Tree (Issue #49).

Stellt kurze IMAP-Operationen bereit, die unabhaengig vom langlebigen
mail_monitor.IDLE-Loop laufen — pro Aufruf eine eigene Connection,
die nach Ende sofort geschlossen wird. Vermeidet das Teilen von State
mit dem Polling-Loop.

Aktionen in dieser Stufe:
- read_mail_body(account, uid)       -> Body-Text + Header-Felder
- mark_mail_read(account, uid)       -> setzt IMAP \\Seen-Flag
"""

from __future__ import annotations

import email
import email.header
import re
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
        subject = _decode_header(msg.get("Subject"))
        date = msg.get("Date", "")
        body = _extract_text_from_email(msg)
        # Truncate (TTS-friendly).
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rsplit(" ", 1)[0] + " ..."
        return {
            "sender": sender,
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
        typ, data = await client.uid("store", str(uid), "+FLAGS", "\\Seen")
        if typ != "OK":
            log.warning(f"mark_mail_read[{account_name}] uid={uid}: "
                        f"STORE failed typ={typ} data={data!r}")
            return False
        log.info(f"mark_mail_read[{account_name}] uid={uid}: marked \\Seen")
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
