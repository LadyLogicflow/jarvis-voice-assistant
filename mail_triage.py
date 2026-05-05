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
    "@dhl.com", "@dhl.de", "@hermesworld.com", "@my.hermesworld.com",
    "@dpd.com", "@dpd.de", "@ups.com", "@ankunft.dpd",
    "@amazon-logistics.de", "@versand.amazon.de",
)
_TRAVEL_FROM_DOMAINS = (
    "@bahn.de", "@deutschebahn.com", "@lufthansa.com", "@booking.com",
    "@airbnb.com", "@flixbus.com", "@flixtrain.com", "@rail-online.de",
)


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


def route(
    sender: str,
    subject: str,
    category: str,
    msg=None,
) -> dict:
    """Decide what to do with an incoming mail. Returns one of:
      {"action": "none"}                       -> proceed normal flow
      {"action": "mark_read"}
      {"action": "move", "folder": "..."}
      {"action": "forward", "to": "...",
       "and_then": "move"|"mark_read",
       "folder": "..."}                        -> after forwarding, also do this
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
                # Fallback path
                fb = rule.get("fallback_action", "mark_read")
                if fb == "move":
                    return {"action": "move", "folder": rule.get("fallback_folder", "Junk")}
                return {"action": "mark_read"}
            return {
                "action": "forward",
                "to": rule.get("to", ""),
                "and_then": "move" if rule.get("after_forward_move_to") else "mark_read",
                "folder": rule.get("after_forward_move_to", ""),
            }
        if action == "move":
            return {"action": "move", "folder": rule.get("folder", "Junk")}
        if action == "mark_read":
            return {"action": "mark_read"}

    # 2) Heuristics (vor dem Werbung-Klassifikator damit Bounce/Paket nicht
    #    in den Werbung-Folder wandern)
    heur = rules.get("heuristics", {})
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()

    if heur.get("bounce_to_junk", False):
        if any(p in sender_l for p in _BOUNCE_FROM_PATTERNS) or \
           any(p in subject_l for p in _BOUNCE_SUBJECT_PATTERNS):
            return {"action": "move", "folder": "Junk"}

    pkg_folder = heur.get("package_to_dhl_folder")
    if pkg_folder and any(d in sender_l for d in _PACKAGE_FROM_DOMAINS):
        return {"action": "move", "folder": pkg_folder}

    travel_folder = heur.get("travel_to_reise_folder")
    if travel_folder and any(d in sender_l for d in _TRAVEL_FROM_DOMAINS):
        return {"action": "move", "folder": travel_folder}

    # 3) Newsletter heuristic — needs the parsed msg for List-Unsubscribe
    if msg is not None:
        newsletter_folder = heur.get("newsletter_to_werbung_folder")
        if (newsletter_folder
                and msg.get("List-Unsubscribe")
                and category == "werbung"):
            return {"action": "move", "folder": newsletter_folder}

    # 4) Generic werbung action from the rules file
    if category == "werbung":
        action = rules.get("werbung_action")
        if action == "move":
            return {"action": "move", "folder": rules.get("werbung_folder", "Junk")}
        if action == "mark_read":
            return {"action": "mark_read"}

    # 5) Nothing matched — normal flow
    return {"action": "none"}
