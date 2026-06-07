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

import asyncio
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
    await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
    resp = await asyncio.wait_for(client.login(account["user"], account["password"]), timeout=30)
    if getattr(resp, "result", None) != "OK":
        raise RuntimeError(f"LOGIN rejected for {account['user']!r}")
    sel = await client.select(account["folder"])
    if getattr(sel, "result", None) != "OK":
        raise RuntimeError(f"SELECT {account['folder']!r} failed")
    return client


async def find_last_mail_from(name_or_addr: str) -> dict | None:
    """Sucht die neueste Mail eines Absenders in allen konfigurierten Konten.

    Durchsucht alle Konten in S.MAIL_MONITOR_ACCOUNTS nach Mails vom
    angegebenen Absender (Name oder E-Mail-Adresse). Gibt die neueste
    gefundene Mail zurueck.

    Hinweis: Verwendet regulaeres SEARCH ohne UID SEARCH (Apple/HILO-
    Kompatibilitaet). Seq-Nummern werden anschliessend via FETCH (UID)
    in UIDs umgewandelt.

    Args:
        name_or_addr: Absender-Name oder E-Mail-Adresse (IMAP FROM-Kriterium).

    Returns:
        Dict mit account, uid, sender, subject, date, message_id — oder None
        wenn kein Konto einen Treffer liefert.
    """
    for acc in S.MAIL_MONITOR_ACCOUNTS:
        if not acc.get("password"):
            log.debug("find_last_mail_from: Konto %r hat kein Passwort — uebersprungen",
                      acc.get("name"))
            continue
        client = None
        try:
            client = await _connect(acc)
            # Regulaeres SEARCH ohne CHARSET (charset=None vermeidet den aioimaplib-
            # Default "utf-8" der "SEARCH CHARSET utf-8 ..." sendet, was Apple/HILO
            # ablehnt). KEIN uid("search", ...) — nicht unterstuetzt auf Apple/HILO.
            typ, data = await client.search("FROM", f'"{name_or_addr}"', charset=None)
            if typ != "OK" or not data or not data[0]:
                continue
            raw_val = (data[0].decode() if isinstance(data[0], (bytes, bytearray))
                       else str(data[0]))
            seq_nums = [s for s in raw_val.split() if s.isdigit()]
            if not seq_nums:
                continue
            # Letztes Element = neueste Seq-Nummer
            last_seq = seq_nums[-1]
            # Seq-Nummer -> UID + ENVELOPE (Header-Felder) via FETCH
            ftyp, fdata = await client.fetch(last_seq, "(UID ENVELOPE)")
            if ftyp != "OK" or not fdata:
                continue
            uid = None
            sender = ""
            subject = ""
            date = ""
            message_id = ""
            for item in fdata:
                if not isinstance(item, (bytes, bytearray)):
                    continue
                txt = item.decode("utf-8", errors="replace")
                # UID extrahieren
                m_uid = re.search(r'\bUID\s+(\d+)', txt)
                if m_uid:
                    uid = int(m_uid.group(1))
                # ENVELOPE: (date subject from sender reply-to to ...)
                # Envelope-Felder via email.header-Dekodierung parsen
                # Betreff aus ENVELOPE
                m_subj = re.search(r'ENVELOPE\s*\(\"([^\"]*?)\"', txt)
                if m_subj:
                    date = m_subj.group(1)
                # Envelope-Parsing ist komplex — Body-Header via BODY.PEEK[HEADER.FIELDS]
                # waere sauberer, aber das verursacht eine zweite Verbindung. Hier
                # nutzen wir die Seq-Nummer noch einmal fuer einen gezielten Header-Fetch.
            if uid is None:
                # Fallback: versuche UID direkt via FETCH BODY.PEEK[HEADER.FIELDS]
                htyp, hdata = await client.fetch(
                    last_seq, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if htyp == "OK" and hdata:
                    for item in hdata:
                        if not isinstance(item, (bytes, bytearray)):
                            continue
                        txt = item.decode("utf-8", errors="replace")
                        m_uid2 = re.search(r'\bUID\s+(\d+)', txt)
                        if m_uid2:
                            uid = int(m_uid2.group(1))
                if uid is None:
                    log.warning("find_last_mail_from[%s]: konnte UID nicht aus FETCH lesen",
                                acc["name"])
                    continue
                # Header-Felder parsen
                for item in hdata:
                    if not isinstance(item, (bytes, bytearray)):
                        continue
                    raw_headers = item
                    try:
                        msg = email.message_from_bytes(raw_headers)
                        sender = (_decode_header(parseaddr(msg.get("From", ""))[0])
                                  or msg.get("From", ""))
                        subject = _decode_header(msg.get("Subject", ""))
                        date = msg.get("Date", "")
                        message_id = msg.get("Message-ID", "")
                    except Exception:
                        pass
            else:
                # Hol Header-Felder fuer die gefundene Seq-Nummer
                htyp, hdata = await client.fetch(
                    last_seq, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if htyp == "OK" and hdata:
                    for item in hdata:
                        if not isinstance(item, (bytes, bytearray)):
                            continue
                        try:
                            msg = email.message_from_bytes(item)
                            sender = (_decode_header(parseaddr(msg.get("From", ""))[0])
                                      or msg.get("From", ""))
                            subject = _decode_header(msg.get("Subject", ""))
                            date = msg.get("Date", "")
                            message_id = msg.get("Message-ID", "")
                        except Exception:
                            pass
                        break

            log.info(
                "find_last_mail_from: Treffer in Konto %r uid=%s from=%r subject=%r",
                acc["name"], uid, sender, subject,
            )
            return {
                "account": acc["name"],
                "uid": uid,
                "sender": sender,
                "subject": subject,
                "date": date,
                "message_id": message_id,
            }
        except Exception as e:
            log.warning("find_last_mail_from[%s]: %s: %s", acc.get("name"), type(e).__name__, e)
            continue
        finally:
            if client is not None:
                try:
                    await client.logout()
                except Exception:
                    pass
    return None


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

TRASH_FOLDER_GUESSES = (
    "Deleted Messages",   # iCloud (English)
    "Gelöschte E-Mails",  # iCloud (German)
    "Trash",
    "Papierkorb",
    "Gelöschte Elemente",
    "Deleted Items",
    "Bin",
    "INBOX.Trash",
    "[Gmail]/Trash",
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


async def save_draft(
    to_addr: str,
    subject: str,
    body: str,
    account_name: str = "",
) -> tuple[bool, str]:
    """Speichere eine neue Mail als Entwurf im konfigurierten Drafts-Ordner.

    Verwendet den ersten konfigurierten IMAP-Account wenn ``account_name``
    leer ist. Der Drafts-Ordner wird aus der Account-Konfiguration gelesen
    (``drafts_folder``-Key, konfigurierbar via config.json); Fallback auf
    ``DRAFTS_FOLDER_GUESSES``.

    Args:
        to_addr: Empfaenger-Adresse.
        subject: Betreff der Mail.
        body: Plaintext-Body.
        account_name: Name des IMAP-Accounts aus MAIL_MONITOR_ACCOUNTS;
            leer = erster Account.

    Returns:
        Tuple (ok, folder_or_error).
    """
    if not S.MAIL_MONITOR_ACCOUNTS:
        return False, "Keine MAIL_MONITOR_ACCOUNTS konfiguriert"

    if account_name:
        acc = _account_by_name(account_name)
        if not acc:
            return False, f"Konto {account_name!r} nicht konfiguriert"
    else:
        acc = S.MAIL_MONITOR_ACCOUNTS[0]

    from_addr = acc.get("user", "")
    msg_bytes = build_reply_message(
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body=body,
    )

    # Bevorzugten Drafts-Ordner aus Account-Config lesen
    preferred = acc.get("drafts_folder", "Drafts")
    candidates = [preferred] + [f for f in DRAFTS_FOLDER_GUESSES if f != preferred]

    client = None
    try:
        client = await _connect(acc)
        last_err = "kein Drafts-Folder gefunden"
        for folder in candidates:
            try:
                resp = await client.append(msg_bytes, mailbox=folder)
                result = getattr(resp, "result", None) or (
                    resp[0] if isinstance(resp, tuple) and resp else None
                )
                if result == "OK":
                    log.info(f"save_draft[{acc['name']}] -> {folder!r}: {subject!r}")
                    return True, folder
                last_err = f"folder={folder!r} result={result}"
            except Exception as exc:
                last_err = f"folder={folder!r} {type(exc).__name__}: {exc}"
                continue
        log.warning(f"save_draft[{acc['name']}] failed: {last_err}")
        return False, last_err
    except Exception as e:
        log.warning(f"save_draft[{acc['name']}]: {type(e).__name__}: {e}")
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


async def _discover_trash_folder(client) -> str | None:
    """Find Trash folder via IMAP LIST, checking \\Trash special-use
    attribute first (RFC 6154), then name patterns. Returns folder name
    or None if not found."""
    try:
        typ, lines = await client.list('""', '"*"')
        if typ != "OK":
            return None
        for line in lines:
            if not isinstance(line, (bytes, bytearray)):
                continue
            decoded = line.decode("utf-8", errors="replace")
            # RFC 6154 special-use attribute takes priority
            if r"\Trash" in decoded:
                m = re.search(r'"[^"]*"\s+"?([^"]+)"?\s*$', decoded)
                if m:
                    return m.group(1).strip('"')
        # Second pass: name heuristics
        for line in lines:
            if not isinstance(line, (bytes, bytearray)):
                continue
            decoded = line.decode("utf-8", errors="replace").lower()
            for pattern in ("trash", "papierkorb", "deleted messages",
                            "deleted items", "gelöscht", "bin"):
                if pattern in decoded:
                    m = re.search(r'"[^"]*"\s+"?([^"]+)"?\s*$',
                                  line.decode("utf-8", errors="replace"))
                    if m:
                        return m.group(1).strip('"')
    except Exception as e:
        log.debug(f"_discover_trash_folder: {type(e).__name__}: {e}")
    return None


async def delete_mail(account_name: str, uid: int) -> tuple[bool, str]:
    """Move a mail to the Trash folder using a single IMAP connection.
    Discovers the trash folder via LIST first, then tries known names.
    Last resort: permanent deletion via \\Deleted + EXPUNGE.
    Returns (success, folder_name_or_error)."""
    acc = _account_by_name(account_name)
    if not acc or not acc["password"]:
        return False, "Konto nicht konfiguriert"
    client = None
    try:
        client = await _connect(acc)
        # Build candidate list: discovered folder first, then known guesses
        discovered = await _discover_trash_folder(client)
        candidates = ([discovered] if discovered else []) + list(TRASH_FOLDER_GUESSES)
        seen: set[str] = set()
        for folder in candidates:
            if folder in seen:
                continue
            seen.add(folder)
            # IMAP requires quoting for folder names that contain spaces
            folder_arg = f'"{folder}"' if " " in folder else folder
            try:
                typ, _ = await client.uid("move", str(uid), folder_arg)
                if typ == "OK":
                    log.info(f"delete_mail[{account_name}] uid={uid} -> {folder!r}")
                    return True, folder
            except Exception:
                pass
            try:
                typ, _ = await client.uid("copy", str(uid), folder_arg)
                if typ == "OK":
                    await client.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
                    await client.expunge()
                    log.info(
                        f"delete_mail[{account_name}] uid={uid} -> {folder!r} (copy+delete)"
                    )
                    return True, folder
            except Exception:
                pass
        # Last resort: permanent deletion
        try:
            await client.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
            await client.expunge()
            log.info(f"delete_mail[{account_name}] uid={uid} permanently deleted")
            return True, "(permanent)"
        except Exception as e:
            log.warning(
                f"delete_mail[{account_name}] uid={uid} permanent delete failed: {e}"
            )
        return False, "Kein Papierkorb-Ordner gefunden"
    except Exception as e:
        log.warning(f"delete_mail[{account_name}] uid={uid}: {type(e).__name__}: {e}")
        return False, f"Fehler: {type(e).__name__}"
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

    smtp_host = acc.get("smtp_host") or acc["host"]
    smtp_port = int(acc.get("smtp_port") or 587)
    log.info(f"forward_mail[{account_name}] SMTP {smtp_host}:{smtp_port} uid={uid} -> {to_addr}")
    try:
        loop = asyncio.get_running_loop()
        def _send():
            ssl_context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
                s.starttls(context=ssl_context)
                s.login(acc["user"], acc["password"])
                s.send_message(fwd)
        await loop.run_in_executor(None, _send)
        log.info(f"forward_mail[{account_name}] uid={uid} -> {to_addr} OK")
        return True
    except Exception as e:
        log.warning(f"forward_mail[{account_name}] SMTP {smtp_host}:{smtp_port} failed: "
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


async def retriage_inbox(
    account_name: str,
    target_folder: str,
    from_domains: tuple[str, ...],
    max_mails: int = 500,
) -> tuple[int, int]:
    """Sucht alle Mails in INBOX deren Absender auf einen der Domains endet,
    markiert sie als gelesen und verschiebt sie in target_folder.
    Returns (moved_count, error_count).
    """
    acc = _account_by_name(account_name)
    if not acc or not acc["password"]:
        log.warning(f"retriage_inbox[{account_name}] Konto nicht gefunden oder kein Passwort")
        return 0, 0
    client = None
    moved = 0
    errors = 0
    log.info(
        f"retriage_inbox START account={account_name!r} target={target_folder!r} "
        f"domains={len(from_domains)} max={max_mails}"
    )
    try:
        client = await _connect(acc)
        # Immer in INBOX suchen, unabhaengig vom account["folder"] Monitor-Setting
        _sel = await client.select("INBOX")
        log.info(f"retriage_inbox[{account_name}] SELECT INBOX -> result={getattr(_sel, 'result', '?')} data={getattr(_sel, 'lines', _sel)!r:.200}")
        if getattr(_sel, "result", None) != "OK":
            raise RuntimeError(f"SELECT INBOX failed for {account_name!r}: {_sel}")
        matched_uids: set[int] = set()
        for domain in from_domains:
            try:
                # Regulaeres SEARCH ohne CHARSET (charset=None vermeidet den aioimaplib-
                # Default "utf-8" der "SEARCH CHARSET utf-8 ..." sendet, was Apple iCloud
                # ablehnt). None als positionales Arg wuerde als Kriterium "None" landen —
                # daher keyword-only. Seq-Nummern → UIDs via anschliessenden FETCH (UID).
                typ, data = await client.search("FROM", f'"{domain}"', charset=None)
                raw_val = ""
                if data and data[0]:
                    raw_val = data[0].decode() if isinstance(data[0], (bytes, bytearray)) else str(data[0])
                before = len(matched_uids)
                if typ == "OK" and raw_val.strip():
                    seq_nums = [s for s in raw_val.split() if s.isdigit()]
                    if seq_nums:
                        # Seq-Nummern → UIDs via FETCH (UID)
                        ftyp, fdata = await client.fetch(",".join(seq_nums), "(UID)")
                        if ftyp == "OK":
                            for item in fdata:
                                txt = item.decode(errors="replace") if isinstance(item, (bytes, bytearray)) else str(item)
                                m = re.search(r'\bUID\s+(\d+)', txt)
                                if m:
                                    matched_uids.add(int(m.group(1)))
                found = len(matched_uids) - before
                if found:
                    log.info(f"retriage_inbox[{account_name}] FROM {domain!r} -> {found} neue UIDs")
                else:
                    log.debug(f"retriage_inbox[{account_name}] FROM {domain!r} -> 0 (typ={typ})")
            except Exception as e:
                log.warning(f"retriage_inbox SEARCH FROM {domain!r}: {e}")
        log.info(f"retriage_inbox[{account_name}] {len(matched_uids)} UIDs gesamt -> verschieben nach {target_folder!r}")
        for uid in list(matched_uids)[:max_mails]:
            try:
                await client.uid("store", str(uid), "+FLAGS", "(\\Seen)")
                # Versuche MOVE (RFC 6851); bei Exception (z.B. Apple iCloud hat kein MOVE)
                # oder non-OK-Response direkt COPY+DELETE als Fallback.
                move_ok = False
                try:
                    typ, _ = await client.uid("move", str(uid), target_folder)
                    move_ok = (typ == "OK")
                except Exception:
                    pass  # kein MOVE-Support, weiter mit COPY-Fallback
                if not move_ok:
                    typ2, copy_data = await client.uid("copy", str(uid), target_folder)
                    if typ2 == "OK":
                        await client.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
                    else:
                        log.warning(f"retriage_inbox uid={uid}: COPY failed ({typ2}): {copy_data}")
                        errors += 1
                        continue
                moved += 1
            except Exception as e:
                log.warning(f"retriage_inbox uid={uid}: {type(e).__name__}: {e}")
                errors += 1
        if moved:
            try:
                await client.expunge()
            except Exception:
                pass
    except Exception as e:
        log.warning(f"retriage_inbox[{account_name}]: {type(e).__name__}: {e}")
    finally:
        if client is not None:
            try:
                await client.logout()
            except Exception:
                pass
    return moved, errors
