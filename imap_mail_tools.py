"""
Jarvis — Cross-platform Mail backend over IMAP.

Used as an alternative to mail_tools.py (which talks to macOS Mail.app
via AppleScript). Switch by setting `mail_backend: "imap"` in
config.json plus the matching .env secrets.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
from email.utils import parseaddr

log = logging.getLogger("jarvis.imap")


def _decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 mail header (encoded UTF-8 / Quoted-Printable)
    into a plain string."""
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


def get_unread_mails_imap(
    host: str,
    user: str,
    password: str,
    *,
    port: int = 993,
    use_ssl: bool = True,
    folder: str = "INBOX",
    max_count: int = 5,
) -> str:
    """Fetch up to `max_count` unread mails from an IMAP folder.

    Returns the same text shape as `mail_tools.get_unread_mails` so the
    caller doesn't care which backend ran:
    - "KEINE_MAILS" when the inbox has no unread messages
    - Otherwise: "Ungelesen insgesamt: N\\n\\n---\\nVon: ...\\n..."
    """
    try:
        cls = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
        with cls(host, port) as M:
            M.login(user, password)
            M.select(folder, readonly=True)
            typ, data = M.search(None, "UNSEEN")
            if typ != "OK":
                return f"Fehler beim IMAP-Search: {typ}"
            ids = data[0].split()
            total = len(ids)
            if total == 0:
                return "KEINE_MAILS"

            # Walk newest first.
            picked = list(reversed(ids))[:max_count]
            lines = [f"Ungelesen insgesamt: {total}", ""]
            for mid in picked:
                typ, msg_data = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_headers = msg_data[0][1]
                msg = email.message_from_bytes(raw_headers)
                from_addr = parseaddr(msg.get("From", ""))[0] or msg.get("From", "")
                subject = _decode_header(msg.get("Subject"))
                date = msg.get("Date", "")
                lines.append("---")
                lines.append(f"Von: {_decode_header(from_addr)}")
                lines.append(f"Betreff: {subject}")
                lines.append(f"Empfangen: {date}")
            return "\n".join(lines)
    except imaplib.IMAP4.error as e:
        log.warning(f"IMAP login/protocol error: {e}")
        return f"IMAP-Fehler: {e}"
    except Exception as e:
        log.warning(f"IMAP backend failed: {type(e).__name__}: {e}")
        return f"Fehler: {e}"
