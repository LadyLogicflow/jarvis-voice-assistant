"""
Mail-Auto-Triage-Engine (Issue #49).

Liest mail_triage_rules.json bei jeder neuen Mail frisch ein, prueft
Sender-Regeln + Klassifikator-Action + Heuristiken in dieser
Reihenfolge, gibt ein dict mit der zu fuehrenden Aktion zurueck:

  {"action": "move", "folder": "..."}
  {"action": "mark_read"}
  {"action": "forward", "to": "...", "and_then": "move"|"mark_read", "folder": "..."}
  {"action": "none"}  -> normaler Pfad (Telegram/Mac-Push)

Heuristiken:
- Bounce-Mails (Mailer-Daemon) -> Junk
- Paket-Mails (DHL/Hermes/DPD/UPS/Amazon-Logistics) -> DHL-Folder
- Reise-Bestaetigungen (Bahn/Lufthansa/Booking/Airbnb/Flixbus) -> Reise-Folder
- Newsletter (List-Unsubscribe-Header + werbung) -> Werbung-Folder
- Einkauf-Mails (Bestellbestaetigungen, Versand, PayPal) -> INBOX.Einkauf (Issue #230)
"""

from __future__ import annotations

import json
import os
import re
from email.utils import parseaddr

import settings as S

log = S.log


_RULES_PATH = os.path.join(os.path.dirname(__file__), "mail_triage_rules.json")


def _load_rules() -> dict:
    """Read fresh from disk on every call so Catrin can edit live."""
    if not os.path.exists(_RULES_PATH):
        return {}
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"mail_triage: rules-file failed to parse: "
                    f"{type(e).__name__}: {e}")
        return {}


# Hard-coded heuristics — domain/keyword sets. Conservative, can be
# overridden / extended via the rules file.
_BOUNCE_FROM_PATTERNS = (
    "mailer-daemon@", "postmaster@", "mail-daemon@",
)
_BOUNCE_SUBJECT_PATTERNS = (
    "undelivered mail", "delivery status notification", "mail delivery failed",
    "returned mail", "delivery failure", "undeliverable",
)
_PACKAGE_FROM_DOMAINS = (
    "@dhl.com", "@dhl.de", "@dhl-paket.de",
    "@post.dhl.com", "@notifications.dhl.com",
    "@hermesworld.com", "@my.hermesworld.com",
    "@dpd.com", "@dpd.de", "@ups.com", "@ankunft.dpd",
    "@amazon-logistics.de", "@versand.amazon.de",
    "@gls-group.eu",
)
_TRAVEL_FROM_DOMAINS = (
    "@bahn.de", "@deutschebahn.com", "@lufthansa.com", "@booking.com",
    "@airbnb.com", "@flixbus.com", "@flixtrain.com", "@rail-online.de",
)

_AMAZON_FROM_DOMAINS = (
    "@amazon.de", "@amazon.com", "@marketplace.amazon.de",
    "@email.amazon.de", "@gc.email.amazon.de", "@review.amazon.de",
)

# ---------------------------------------------------------------------------
# Einkauf heuristics (Issue #230)
# ---------------------------------------------------------------------------
# Known online shops and shipping/payment providers.  Only order-related
# mails from these domains should land in INBOX.Einkauf -- marketing mails
# from the same domain are filtered by the subject-keyword check below.
# Exception: PayPal always routes to Einkauf regardless of subject.
_EINKAUF_DOMAINS = (
    "paypal.de", "paypal.com",
    "amazon.de", "amazon.com", "amazon.co.uk",
    "zalando.de", "otto.de", "aboutyou.de", "ebay.de", "ebay.com",
    "myhermes.de", "dpd.de", "gls-pakete.de",
)

# German + English keywords that indicate a transactional order/shipping/
# payment mail (not marketing).  Strong enough to match regardless of sender
# domain -- marketing newsletters never say "Bestellbest\u00e4tigung" in the subject.
_EINKAUF_SUBJECT_KEYWORDS = (
    "bestellbest\u00e4tigung", "bestellbestaetigung",
    "versandbest\u00e4tigung", "versandbestaetigung",
    "deine bestellung", "ihre bestellung",
    "wurde versandt", "ist unterwegs",
    "abholbereit",
    "zahlungsbest\u00e4tigung", "zahlungsbestaetigung",
    "zahlung erhalten", "zahlungseingang",
    "payment confirmed", "order confirmation",
    "has been shipped", "your order",
)

# PayPal sender domains -- all mails from these senders are einkauf.
_PAYPAL_DOMAINS = ("paypal.de", "paypal.com")


def _is_einkauf_mail(sender_email: str, subject: str) -> bool:
    """Return True when the mail is a transactional order/shipping/payment mail.

    Fast path -- no LLM needed.

    Match order (first match wins):
    1. PayPal sender domain → always True (every PayPal mail is a payment).
    2. Subject contains a strong transactional keyword → True, regardless of
       sender domain.  This avoids maintaining an exhaustive shop-domain list:
       "Bestellbestätigung", "abholbereit", "wurde versandt" etc. never appear
       in marketing subjects, only in real order/shipping/pickup mails.
    3. Known shop domain but no keyword → False (Amazon/Zalando marketing).

    Amazon marketing mails (e.g. "Angebot", "Deal", "Sale") are NOT matched
    because their subjects contain none of the order keywords -- they fall
    through to the normal Werbung/Amazon path.
    """
    s_lower = (sender_email or "").lower()
    subj_lower = (subject or "").lower()

    # PayPal: always einkauf.
    if any(domain in s_lower for domain in _PAYPAL_DOMAINS):
        return True

    # Strong transactional keyword in subject → einkauf from any sender.
    if any(kw in subj_lower for kw in _EINKAUF_SUBJECT_KEYWORDS):
        return True

    return False


TRIAGE_RULES = [
    {
        "name": "Pakete",
        "domains": _PACKAGE_FROM_DOMAINS,
        "folder": "DHL",
    },
    {
        "name": "Amazon",
        "domains": _AMAZON_FROM_DOMAINS,
        "folder": "Amazon",
    },
]


def _has_attachment(msg) -> bool:
    """Cheap detection of any non-text attachment in a parsed Message."""
    if msg is None or not msg.is_multipart():
        return False
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            return True
        ctype = part.get_content_type()
        if ctype and not ctype.startswith("text/") and not ctype.startswith("multipart/"):
            return True
    return False


def _matches_rule(rule: dict, sender: str, subject: str) -> bool:
    """Check if a sender/subject matches the rule's matcher fields."""
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()
    # from_contains: substring match on sender's email
    fc = rule.get("from_contains")
    if fc and fc.lower() in sender_l:
        return True
    sc = rule.get("subject_contains")
    if sc and sc.lower() in subject_l:
        return True
    return False


def _apply_folder_override(result: dict, account: str, rules: dict) -> dict:
    """Remap folder names per-account (e.g. iCloud uses 'Junk' not 'INBOX.Spam')."""
    folder = result.get("folder")
    if not folder or not account:
        return result
    overrides = rules.get("account_folder_map", {}).get(account, {})
    if folder in overrides:
        result = dict(result)
        result["folder"] = overrides[folder]
    return result


def route(
    sender: str,
    subject: str,
    category: str,
    msg=None,
    account: str = "",
) -> dict:
    """Decide what to do with an incoming mail. Returns one of:
      {"action": "none"}                       -> proceed normal flow
      {"action": "mark_read"}
      {"action": "move", "folder": "..."}
      {"action": "forward", "to": "...",
       "and_then": "move"|"mark_read",
       "folder": "..."}                        -> after forwarding, also do this
      {"action": "einkauf", "folder": "..."}   -> silently archive to Einkauf (Issue #230)
    """
    rules = _load_rules()

    # 1) Explicit sender / subject rules win first
    for rule in rules.get("rules", []):
        if not _matches_rule(rule, sender, subject):
            continue
        action = rule.get("action", "none")
        if action == "forward":
            with_attach_only = rule.get("forward_only_with_attachment", False)
            if with_attach_only and not _has_attachment(msg):
                fb = rule.get("fallback_action", "mark_read")
                if fb == "move":
                    return _apply_folder_override(
                        {"action": "move", "folder": rule.get("fallback_folder", "Junk")},
                        account, rules,
                    )
                return {"action": "mark_read"}
            return _apply_folder_override({
                "action": "forward",
                "to": rule.get("to", ""),
                "and_then": "move" if rule.get("after_forward_move_to") else "mark_read",
                "folder": rule.get("after_forward_move_to", ""),
            }, account, rules)
        if action == "move":
            return _apply_folder_override({
                "action": "move",
                "folder": rule.get("folder", "Junk"),
                "also_mark_read": rule.get("also_mark_read", False),
            }, account, rules)
        if action == "mark_read":
            return {"action": "mark_read"}

    # 2) Einkauf check (Issue #230) -- BEFORE LLM and BEFORE Amazon heuristic.
    #    Bestellbestaetigungen, Versandmeldungen und PayPal-Zahlungen werden
    #    still nach INBOX.Einkauf verschoben, ohne Telegram/WebUI-Push.
    #    Note: invoice detection in mail_monitor runs before triage is called,
    #    so PDFs are already forwarded to getmyinvoices at this point.
    _sender_email_raw = parseaddr(sender or "")[1].lower()
    if _is_einkauf_mail(_sender_email_raw, subject):
        _einkauf_folder = rules.get("einkauf_folder", "INBOX.Einkauf")
        return _apply_folder_override(
            {"action": "einkauf", "folder": _einkauf_folder},
            account, rules,
        )

    # 3) Heuristics (vor dem Werbung-Klassifikator damit Bounce/Paket nicht
    #    in den Werbung-Folder wandern)
    heur = rules.get("heuristics", {})
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()

    if heur.get("bounce_to_junk", False):
        if any(p in sender_l for p in _BOUNCE_FROM_PATTERNS) or \
           any(p in subject_l for p in _BOUNCE_SUBJECT_PATTERNS):
            return _apply_folder_override(
                {"action": "move", "folder": heur.get("bounce_folder", "INBOX.Spam")},
                account, rules,
            )

    pkg_folder = heur.get("package_to_dhl_folder", "DHL")
    if pkg_folder and any(d in sender_l for d in _PACKAGE_FROM_DOMAINS):
        return _apply_folder_override(
            {"action": "move", "folder": pkg_folder}, account, rules
        )

    travel_folder = heur.get("travel_to_reise_folder", "Reise")
    if travel_folder and any(d in sender_l for d in _TRAVEL_FROM_DOMAINS):
        return _apply_folder_override(
            {"action": "move", "folder": travel_folder}, account, rules
        )

    amazon_folder = heur.get("amazon_to_amazon_folder", "Amazon")
    if amazon_folder and any(d in sender_l for d in _AMAZON_FROM_DOMAINS):
        return _apply_folder_override(
            {"action": "move_with_summary", "folder": amazon_folder}, account, rules
        )

    # 4) Newsletter heuristic -- List-Unsubscribe header alone is definitive;
    #    LLM category is not required (newsletters often misclassified as "info")
    if msg is not None:
        newsletter_folder = heur.get("newsletter_to_werbung_folder", "Werbung")
        if newsletter_folder and msg.get("List-Unsubscribe"):
            return _apply_folder_override(
                {"action": "move", "folder": newsletter_folder}, account, rules
            )

    # 5) Generic werbung action from the rules file
    if category == "werbung":
        action = rules.get("werbung_action", "move")  # default: move (Issue #224)
        if action == "move":
            return _apply_folder_override(
                {"action": "move", "folder": rules.get("werbung_folder", "Werbung")},
                account, rules,
            )
        if action == "mark_read":
            return {"action": "mark_read"}

    # 6) Nothing matched -- normal flow
    return {"action": "none"}
