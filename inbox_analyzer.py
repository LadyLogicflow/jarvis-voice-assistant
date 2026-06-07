"""
Inbox-Analyse (Issue #206): scannt die letzten N Tage aller IMAP-Konten
(nur Header), clustert nach Absender-Domain und generiert Regelvorschlaege.

Kein Body-Download — es werden ausschliesslich Header-Felder gelesen.
"""

from __future__ import annotations

import asyncio
import datetime
import email as _email_module
import json
import os
from email.utils import parseaddr

import settings as S

log = S.log

_RULES_PATH = os.path.join(os.path.dirname(__file__), "mail_triage_rules.json")

# Bekannte Zielordner fuer LLM-Prompt
_KNOWN_FOLDERS = [
    "Werbung", "Newsletter", "DHL", "Amazon", "Reise", "Bank",
    "Rechnung", "Behoerden", "Social", "Gelesen_automatisch", "Junk",
]

# Timeout pro IMAP-Konto in Sekunden
_IMAP_TIMEOUT = 60


async def scan_inbox_headers(days: int = 90) -> list[dict]:
    """Scannt alle konfigurierten IMAP-Konten, liest nur From/Subject/Date/List-Unsubscribe Header.

    Args:
        days: Anzahl der Tage rueckwirkend, die gescannt werden sollen.

    Returns:
        Liste von dicts: {account, from_addr, domain, subject, has_list_unsubscribe}
        Kein Body-Download.
    """
    try:
        import aioimaplib as _aioimaplib  # type: ignore
    except ImportError:
        log.warning("inbox_analyzer: aioimaplib nicht verfuegbar")
        return []

    accounts = S.MAIL_MONITOR_ACCOUNTS
    if not accounts:
        log.info("inbox_analyzer: keine MAIL_MONITOR_ACCOUNTS konfiguriert")
        return []

    # Eigene Domains ermitteln (nicht zaehlen)
    own_domains: set[str] = set()
    for acc in accounts:
        user = acc.get("user", "")
        if "@" in user:
            own_domains.add(user.split("@", 1)[1].lower())

    since_date = (datetime.date.today() - datetime.timedelta(days=days))
    # IMAP SINCE-Format: DD-Mon-YYYY (z.B. 08-Mar-2026)
    _MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    since_str = f"{since_date.day:02d}-{_MONTH_ABBR[since_date.month - 1]}-{since_date.year}"

    all_headers: list[dict] = []
    tasks = [
        _scan_account(acc, since_str, own_domains, _aioimaplib)
        for acc in accounts
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            acc_name = accounts[i].get("name", "?")
            log.warning(f"inbox_analyzer: Konto {acc_name!r} Fehler: "
                        f"{type(result).__name__}: {result}")
        else:
            all_headers.extend(result)

    log.info(f"inbox_analyzer: {len(all_headers)} Headers von "
             f"{len(accounts)} Konto(en) gelesen")
    return all_headers


async def _scan_account(
    account: dict,
    since_str: str,
    own_domains: set[str],
    aioimaplib_module,
) -> list[dict]:
    """Scannt ein einzelnes IMAP-Konto und gibt Header-Dicts zurueck."""
    name = account["name"]
    folder = account.get("folder", "INBOX")
    cls = aioimaplib_module.IMAP4_SSL if account.get("ssl", True) else aioimaplib_module.IMAP4
    client = cls(host=account["host"], port=account["port"], timeout=_IMAP_TIMEOUT)

    try:
        await asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
        login_resp = await asyncio.wait_for(
            client.login(account["user"], account["password"]), timeout=30
        )
        if getattr(login_resp, "result", None) != "OK":
            raise RuntimeError(f"LOGIN abgelehnt fuer {account['user']!r}")

        select_resp = await client.select(folder)
        if getattr(select_resp, "result", None) != "OK":
            raise RuntimeError(f"SELECT {folder!r} fehlgeschlagen")

        # Reguläres SEARCH (kein UID SEARCH) — Apple- und HILO-IMAP
        # unterstützen UID nur für FETCH/COPY/STORE, nicht für SEARCH.
        search_resp = await asyncio.wait_for(
            client.search(f"SINCE {since_str}"), timeout=_IMAP_TIMEOUT
        )
        if getattr(search_resp, "result", None) != "OK":
            return []

        seq_raw = b" ".join(
            line for line in (search_resp.lines or [])
            if isinstance(line, (bytes, bytearray))
        )
        seq_list = [s.strip() for s in seq_raw.split() if s.strip()]
        if not seq_list:
            log.info(f"inbox_analyzer[{name}]: keine Mails seit {since_str}")
            return []

        log.info(f"inbox_analyzer[{name}]: {len(seq_list)} Mails seit {since_str}")

        headers: list[dict] = []
        # In Batches von 50 fetchen (Sequence Numbers, kein uid-Prefix)
        batch_size = 50
        for i in range(0, len(seq_list), batch_size):
            batch = seq_list[i:i + batch_size]
            seq_set = b",".join(batch).decode()
            try:
                typ, data = await asyncio.wait_for(
                    client.fetch(
                        seq_set,
                        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE)])"
                    ),
                    timeout=_IMAP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning(f"inbox_analyzer[{name}]: Fetch-Timeout bei Batch {i}")
                continue
            except Exception as e:
                log.warning(f"inbox_analyzer[{name}]: Fetch-Fehler: {e}")
                continue

            if typ != "OK" or not data:
                continue

            # aioimaplib liefert bytes und bytearray items
            byte_items = [b for b in data if isinstance(b, (bytes, bytearray))]
            for raw in byte_items:
                if len(raw) < 5:
                    continue
                try:
                    msg = _email_module.message_from_bytes(raw)
                except Exception:
                    continue

                from_raw = str(msg.get("From", "") or "")
                if not from_raw:
                    continue

                _, from_addr = parseaddr(from_raw)
                from_addr = (from_addr or "").lower().strip()
                if not from_addr or "@" not in from_addr:
                    continue

                domain = from_addr.split("@", 1)[1]
                if domain in own_domains:
                    continue

                subject = str(msg.get("Subject", "") or "").strip()
                has_unsub = bool(msg.get("List-Unsubscribe"))

                headers.append({
                    "account": name,
                    "from_addr": from_addr,
                    "domain": domain,
                    "subject": subject,
                    "has_list_unsubscribe": has_unsub,
                })

        return headers

    finally:
        try:
            await asyncio.wait_for(client.logout(), timeout=10)
        except Exception:
            pass


def cluster_by_domain(headers: list[dict]) -> list[dict]:
    """Gruppiert nach Absender-Domain, zaehlt Haeufigkeit, erkennt Newsletter-Flag.

    Args:
        headers: Liste von Header-Dicts aus scan_inbox_headers().

    Returns:
        Sortierte Liste: [{domain, count, newsletter_ratio, accounts, sample_subjects}]
    """
    from collections import defaultdict

    domain_map: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "unsub_count": 0,
        "accounts": set(),
        "sample_subjects": [],
    })

    for h in headers:
        d = h["domain"]
        entry = domain_map[d]
        entry["count"] += 1
        if h["has_list_unsubscribe"]:
            entry["unsub_count"] += 1
        entry["accounts"].add(h["account"])
        if len(entry["sample_subjects"]) < 3 and h.get("subject"):
            entry["sample_subjects"].append(h["subject"])

    clusters = []
    for domain, entry in domain_map.items():
        count = entry["count"]
        unsub_count = entry["unsub_count"]
        newsletter_ratio = unsub_count / count if count > 0 else 0.0
        clusters.append({
            "domain": domain,
            "count": count,
            "newsletter_ratio": newsletter_ratio,
            "accounts": sorted(entry["accounts"]),
            "sample_subjects": entry["sample_subjects"],
        })

    clusters.sort(key=lambda x: x["count"], reverse=True)
    return clusters


def filter_already_covered(clusters: list[dict]) -> list[dict]:
    """Filtert Domains raus die bereits abgedeckt sind oder zu selten vorkommen.

    Filtert:
    - Domains aus mail_triage._PACKAGE_FROM_DOMAINS, _AMAZON_FROM_DOMAINS,
      _TRAVEL_FROM_DOMAINS (bereits durch Heuristiken abgedeckt)
    - Domains aus mail_triage_rules.json (bereits konfiguriert)
    - Domains mit count < 3

    Args:
        clusters: Liste aus cluster_by_domain().

    Returns:
        Gefilterte, bereits sortierte Liste.
    """
    import mail_triage as _triage

    # Bereits durch Heuristiken abgedeckte Domain-Suffixe zusammenfuehren
    covered_suffixes: set[str] = set()
    for d in (
        _triage._PACKAGE_FROM_DOMAINS
        + _triage._AMAZON_FROM_DOMAINS
        + _triage._TRAVEL_FROM_DOMAINS
    ):
        # Format ist "@domain.tld" -> "domain.tld"
        covered_suffixes.add(d.lstrip("@").lower())

    # Bereits in rules.json konfigurierte from_contains-Werte
    covered_from_contains: set[str] = set()
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            rules_data = json.load(f)
        for rule in rules_data.get("rules", []):
            fc = rule.get("from_contains", "").lower()
            if fc:
                covered_from_contains.add(fc.lstrip("@"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    result = []
    for c in clusters:
        domain = c["domain"].lower()

        # count-Schwelle
        if c["count"] < 3:
            continue

        # Heuristik-Domains
        if domain in covered_suffixes:
            continue
        if any(domain.endswith("." + s) or domain == s for s in covered_suffixes):
            continue

        # Bereits konfigurierte Regeln
        if domain in covered_from_contains:
            continue

        result.append(c)

    return result


async def generate_suggestions(
    clusters: list[dict],
    top_n: int = 20,
) -> list[dict]:
    """LLM (Haiku) generiert fuer Top-N Domains je einen Regelvorschlag.

    Args:
        clusters: Gefilterte, sortierte Cluster-Liste.
        top_n: Maximale Anzahl der Domains, die dem LLM uebergeben werden.

    Returns:
        Liste von dicts: {domain, count, action, folder, reason, display_text}
        Bei LLM-Fehler: Fallback ohne reason.
    """
    if not clusters:
        return []

    top = clusters[:top_n]
    domain_lines = []
    for c in top:
        newsletter_hint = " (Newsletter/Unsubscribe)" if c["newsletter_ratio"] > 0.5 else ""
        domain_lines.append(
            f"{c['domain']} ({c['count']} Mails{newsletter_hint})"
        )
    domains_text = "\n".join(domain_lines)

    folders_str = ", ".join(_KNOWN_FOLDERS)
    system_prompt = (
        "Du bist Jarvis. Antworte NUR mit einem JSON-Array, kein Prolog, kein Epilog."
    )
    user_prompt = (
        f"Analysiere diese E-Mail-Absender-Domains und schlage je eine Triage-Regel vor.\n"
        f"Verfuegbare Zielordner: {folders_str}\n"
        f"Format pro Eintrag: "
        f'[{{"domain": "...", "action": "move"|"mark_read", "folder": "...", "reason": "1 Satz DE"}}]\n'
        f"Bei action='mark_read': folder weglassen oder leer lassen.\n"
        f"Domains:\n{domains_text}"
    )

    suggestions: list[dict] = []
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL if hasattr(S, "HAIKU_MODEL") else "claude-haiku-4-5",
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = ""
        if resp and resp.content:
            raw_text = resp.content[0].text or ""

        # JSON aus der Antwort extrahieren
        import re as _re
        json_match = _re.search(r"\[.*\]", raw_text, _re.DOTALL)
        if json_match:
            llm_items = json.loads(json_match.group(0))
        else:
            llm_items = json.loads(raw_text)

        # Ergebnis mit count anreichern
        count_map = {c["domain"]: c["count"] for c in top}
        for item in llm_items:
            if not isinstance(item, dict) or "domain" not in item:
                continue
            domain = item.get("domain", "")
            action = item.get("action", "move")
            folder = item.get("folder", "")
            reason = item.get("reason", "")
            count = count_map.get(domain, 0)

            display_parts = [f"{domain} ({count} Mails) -> "]
            if action == "move" and folder:
                display_parts.append(f"Ordner: {folder}")
            else:
                display_parts.append("als gelesen markieren")
            if reason:
                display_parts.append(f" — {reason}")

            suggestions.append({
                "domain": domain,
                "count": count,
                "action": action,
                "folder": folder if action == "move" else "",
                "reason": reason,
                "display_text": "".join(display_parts),
            })

    except Exception as e:
        log.warning(f"inbox_analyzer: LLM-Fehler, Fallback ohne reason: "
                    f"{type(e).__name__}: {e}")
        # Fallback: Domains ohne LLM-Reason ausgeben
        for c in top:
            domain = c["domain"]
            action = "move" if c["newsletter_ratio"] > 0.5 else "mark_read"
            folder = "Newsletter" if c["newsletter_ratio"] > 0.5 else ""
            suggestions.append({
                "domain": domain,
                "count": c["count"],
                "action": action,
                "folder": folder,
                "reason": "",
                "display_text": (
                    f"{domain} ({c['count']} Mails) -> "
                    + (f"Ordner: {folder}" if folder else "als gelesen markieren")
                ),
            })

    return suggestions


async def run_analysis(days: int = 90) -> list[dict]:
    """Kompletter Durchlauf: scan -> cluster -> filter -> suggest.

    Args:
        days: Anzahl der Tage fuer den Scan.

    Returns:
        Vorschlags-Liste aus generate_suggestions().
    """
    log.info(f"inbox_analyzer: Starte Analyse fuer {days} Tage...")
    headers = await scan_inbox_headers(days=days)
    if not headers:
        log.info("inbox_analyzer: Keine Headers gefunden, Analyse abgebrochen")
        return []

    clusters = cluster_by_domain(headers)
    log.info(f"inbox_analyzer: {len(clusters)} Domains geclustert")

    filtered = filter_already_covered(clusters)
    log.info(f"inbox_analyzer: {len(filtered)} Domains nach Filter verbleiben")

    if not filtered:
        return []

    suggestions = await generate_suggestions(filtered)
    log.info(f"inbox_analyzer: {len(suggestions)} Regelvorschlaege generiert")
    return suggestions
