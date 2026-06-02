"""
Follow-up-Tracker.

Speichert gesendete Mails bei denen eine Antwort erwartet wird.
Aufloest automatisch wenn eine Antwort eingeht (via In-Reply-To).
Ausstehende Follow-ups erscheinen im Morgen-Briefing.

Persistenz: JSON in .jarvis_followups.json (gitignored).
"""
from __future__ import annotations

import datetime
import json
import os

import settings as S

log = S.log

_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_followups.json")
_followups: dict[str, dict] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(_DB_PATH):
        return
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _followups.update(data)
        log.info(f"followup_tracker: {len(_followups)} Eintraege geladen")
    except Exception as e:
        log.warning(f"followup_tracker._load failed: {type(e).__name__}: {e}")


def _save() -> None:
    try:
        tmp = _DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_followups, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DB_PATH)
    except Exception as e:
        log.warning(f"followup_tracker._save failed: {type(e).__name__}: {e}")


def save_followup(
    *,
    message_id: str,
    account: str,
    to_email: str,
    to_name: str,
    subject: str,
    sent_date: str,
) -> None:
    """Speichert eine gesendete Mail als ausstehenden Follow-up."""
    _load()
    mid = message_id.strip()
    if not mid or mid in _followups:
        return
    _followups[mid] = {
        "account": account,
        "to_email": to_email,
        "to_name": to_name or to_email,
        "subject": subject,
        "sent_date": sent_date[:10] if sent_date else datetime.date.today().isoformat(),
        "status": "pending",
    }
    _save()
    import activity_log as _al
    _al.log_action("followup_saved")
    log.info(f"followup_tracker: '{subject}' -> {to_email} als ausstehend gespeichert")


def resolve_followup(message_id: str) -> bool:
    """Markiert einen Follow-up als erledigt (Antwort eingegangen).

    Returns True wenn ein pending Eintrag gefunden und aufgeloest wurde.
    """
    _load()
    mid = message_id.strip() if message_id else ""
    entry = _followups.get(mid)
    if entry and entry.get("status") == "pending":
        entry["status"] = "resolved"
        entry["resolved_date"] = datetime.date.today().isoformat()
        _save()
        log.info(
            f"followup_tracker: '{entry.get('subject')}' beantwortet — aufgeloest"
        )
        import activity_log as _al
        _al.log_action("followup_resolved")
        return True
    return False


def get_pending_followups(max_age_days: int = 7) -> list[dict]:
    """Gibt ausstehende Follow-ups zurueck die nicht aelter als max_age_days sind."""
    _load()
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
    result = []
    for mid, entry in _followups.items():
        if entry.get("status") != "pending":
            continue
        sent = entry.get("sent_date", "")
        if sent and sent < cutoff:
            continue
        result.append({"message_id": mid, **entry})
    result.sort(key=lambda x: x.get("sent_date", ""))
    return result


def format_followups_block(max_age_days: int = 7) -> str:
    """Formatierter Text-Block fuer das Briefing."""
    pending = get_pending_followups(max_age_days=max_age_days)
    if not pending:
        return ""
    lines = ["Ausstehende Antworten:"]
    for entry in pending:
        name = entry.get("to_name") or entry.get("to_email", "?")
        subj = entry.get("subject", "?")
        sent = entry.get("sent_date", "")
        lines.append(f"• {name}: {subj} (gesendet {sent})")
    return "\n".join(lines)


def prune_old(max_age_days: int = 14) -> int:
    """Entfernt erledigte Eintraege die aelter als max_age_days sind."""
    _load()
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
    to_delete = [
        mid for mid, e in _followups.items()
        if e.get("status") == "resolved"
        and e.get("resolved_date", "9999") < cutoff
    ]
    for mid in to_delete:
        del _followups[mid]
    if to_delete:
        _save()
        log.info(f"followup_tracker: {len(to_delete)} alte Eintraege bereinigt")
    return len(to_delete)
