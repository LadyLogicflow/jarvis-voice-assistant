"""
Strukturierter Session-State neben dem reinen Chat-Verlauf.

Conversation-History (conversation.py) speichert die letzten 50 Chat-
Nachrichten. Dieses Modul ergaenzt das um typisierten State, den
Aktionen und der System-Prompt-Builder brauchen, um konversationale
Multi-Turn-Workflows zu fahren (z.B. Mail-Decision-Tree in Issue #49).

Pro Session werden gehalten:
- active_mail: die zuletzt erwaehnte Mail (Decision-Tree-Anker)
- pending_draft: Mail-Antwort-Entwurf waehrend der Iteration
- recent_mails: die letzten N forwarded Mails (Referenzierung wie
  "die zweite Mail")

Persistenz: ein JSON-File pro session_id, atomar geschrieben, beim
Boot des Servers gelesen. Crashed nie das Request-Path-Handling.

Issue #59 (Milestone Gedaechtnis).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

import settings as S

log = S.log


# Wieviele Mails wir pro Session vorhalten fuer Referenzierungen
# wie "die dritte Mail" oder "die Mail von Max".
RECENT_MAILS_CAP = 5


@dataclass
class MailRef:
    """Identifiziert eine Mail eindeutig + traegt die Header-Felder
    die der Decision-Tree braucht."""
    account: str
    uid: int
    sender: str
    subject: str
    date: str = ""
    message_id: str = ""
    references: str = ""


@dataclass
class PendingDraft:
    """Ein in Iteration befindlicher Antwort-Entwurf. Wird ueberschrieben
    bei DRAFT_REVISE, geleert bei APPROVE oder CANCEL."""
    account: str
    to: str
    subject: str
    body: str
    in_reply_to: str = ""
    references: str = ""


@dataclass
class PendingCalendar:
    """Vorgeschlagener Kalender-Eintrag aus einer Mail-Einladung.
    Catrin bestaetigt mit 'eintragen' (-> Google Calendar) oder
    lehnt mit 'ablehnen' ab. Bezieht sich auf die aktive Mail."""
    summary: str
    dtstart: str = ""           # ICS rohes DTSTART
    dtend: str = ""             # ICS rohes DTEND
    when_human: str = ""        # bereits formatiert: "7. Mai 2026 um 14:00"
    location: str = ""
    organizer: str = ""


@dataclass
class PendingPersonAction:
    """Vorgeschlagenes Update der Personen-DB / Apple Kontakte.
    kind: 'new_person' | 'email_drift' | 'phone_drift' | 'call_choice'.
    Catrin sagt 'ja' / 'nein' im Decision-Tree."""
    kind: str
    contact_id: str = ""        # bei email/phone-drift gesetzt
    name: str = ""
    new_email: str = ""
    new_phone: str = ""
    extra_phones: list[str] = field(default_factory=list)
    # Bei kind='new_person' optional von Claude geratene Felder:
    anrede: str = ""
    funktion: str = ""
    organization: str = ""


@dataclass
class SessionState:
    """Alles was eine Session an strukturiertem State haelt. Erweiterbar
    fuer kuenftige Workflows (active_call, pending_appointment, ...)."""
    active_mail: Optional[MailRef] = None
    pending_draft: Optional[PendingDraft] = None
    pending_calendar: Optional[PendingCalendar] = None
    pending_person: Optional[PendingPersonAction] = None
    recent_mails: list[MailRef] = field(default_factory=list)


# In-memory store: session_id -> SessionState. Beim Server-Boot mit den
# persistierten Werten gefuellt (siehe load_all).
_states: dict[str, SessionState] = {}

# Set der aktuell per WebSocket verbundenen Session-IDs.
# Wird von server.py via register_session / deregister_session gepflegt.
# broadcast_active_mail benutzt dieses Set um alte/inaktive Sessions zu
# uebergehen (Fix Issue #89).
_active_sessions: set[str] = set()


def _state_dir() -> str:
    """Verzeichnis fuer die per-session JSON-Dateien. Liegt im
    Workspace-Root als .jarvis_session_state/."""
    d = os.path.join(os.path.dirname(__file__), ".jarvis_session_state")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_session(session_id: str) -> str:
    """Filename-safe variant of the session id (defensive)."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)


def _state_path(session_id: str) -> str:
    return os.path.join(_state_dir(), f"{_safe_session(session_id)}.json")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _serialize(state: SessionState) -> dict:
    return {
        "active_mail": asdict(state.active_mail) if state.active_mail else None,
        "pending_draft": asdict(state.pending_draft) if state.pending_draft else None,
        "pending_calendar": asdict(state.pending_calendar) if state.pending_calendar else None,
        "pending_person": asdict(state.pending_person) if state.pending_person else None,
        "recent_mails": [asdict(m) for m in state.recent_mails],
    }


def _deserialize(raw: dict) -> SessionState:
    am = raw.get("active_mail")
    pd = raw.get("pending_draft")
    pc = raw.get("pending_calendar")
    pp = raw.get("pending_person")
    rm = raw.get("recent_mails") or []
    return SessionState(
        active_mail=MailRef(**{k: v for k, v in am.items() if k in MailRef.__dataclass_fields__}) if am else None,
        pending_draft=PendingDraft(**{k: v for k, v in pd.items() if k in PendingDraft.__dataclass_fields__}) if pd else None,
        pending_calendar=PendingCalendar(**{k: v for k, v in pc.items() if k in PendingCalendar.__dataclass_fields__}) if pc else None,
        pending_person=PendingPersonAction(**{k: v for k, v in pp.items() if k in PendingPersonAction.__dataclass_fields__}) if pp else None,
        recent_mails=[MailRef(**{k: v for k, v in m.items() if k in MailRef.__dataclass_fields__}) for m in rm if isinstance(m, dict)],
    )


def _save(session_id: str) -> None:
    state = _states.get(session_id)
    if state is None:
        return
    serialized = _serialize(state)  # fast, no I/O — do on calling thread

    def _write() -> None:
        try:
            path = _state_path(session_id)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(serialized, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            log.warning(f"session_state save failed for {session_id}: "
                        f"{type(e).__name__}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _write)
    except RuntimeError:
        _write()


def load_all() -> None:
    """Lese alle persistierten Session-States vom Disk in den Memory-
    Store. Aufzurufen einmal beim Server-Boot."""
    d = _state_dir()
    if not os.path.isdir(d):
        return
    loaded = 0
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        session_id = fn[:-5]
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                raw = json.load(f)
            _states[session_id] = _deserialize(raw)
            loaded += 1
        except Exception as e:
            log.warning(f"session_state load failed for {fn}: "
                        f"{type(e).__name__}: {e}")
    if loaded:
        log.info(f"session_state: {loaded} session(s) restored from disk")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get(session_id: str) -> SessionState:
    """Hol oder erstelle den State fuer eine Session."""
    if session_id not in _states:
        _states[session_id] = SessionState()
    return _states[session_id]


def all_sessions() -> list[str]:
    """Liste alle bekannten session_ids — fuer Broadcast-Szenarien
    (z.B. 'active_mail in jeder verbundenen Session setzen wenn neue
    Mail reinkommt')."""
    return list(_states.keys())


def set_active_mail(session_id: str, mail: MailRef) -> None:
    """Setzt die aktive Mail + haengt sie hinten an recent_mails (mit Cap)."""
    state = get(session_id)
    state.active_mail = mail
    # Dedup: wenn diese Mail schon in recent_mails ist (gleiche
    # account+uid), zuerst raus.
    state.recent_mails = [
        m for m in state.recent_mails
        if not (m.account == mail.account and m.uid == mail.uid)
    ]
    state.recent_mails.append(mail)
    if len(state.recent_mails) > RECENT_MAILS_CAP:
        state.recent_mails = state.recent_mails[-RECENT_MAILS_CAP:]
    _save(session_id)


def clear_active_mail(session_id: str) -> None:
    """Markiert: keine Mail steht mehr zur Debatte (z.B. nach
    Bearbeitung oder Mark-As-Read)."""
    state = get(session_id)
    state.active_mail = None
    _save(session_id)


def set_pending_draft(session_id: str, draft: PendingDraft) -> None:
    state = get(session_id)
    state.pending_draft = draft
    _save(session_id)


def clear_pending_draft(session_id: str) -> None:
    state = get(session_id)
    state.pending_draft = None
    _save(session_id)


def set_pending_calendar(session_id: str, cal: PendingCalendar) -> None:
    state = get(session_id)
    state.pending_calendar = cal
    _save(session_id)


def clear_pending_calendar(session_id: str) -> None:
    state = get(session_id)
    state.pending_calendar = None
    _save(session_id)


def set_pending_person(session_id: str, action: PendingPersonAction) -> None:
    state = get(session_id)
    state.pending_person = action
    _save(session_id)


def clear_pending_person(session_id: str) -> None:
    state = get(session_id)
    state.pending_person = None
    _save(session_id)


def find_recent_mail(session_id: str, query: str) -> Optional[MailRef]:
    """Sucht in den letzten Mails einen Treffer per Sender- oder
    Subject-Substring. Case-insensitive. Fuer Befehle wie 'die Mail
    von Max' oder 'die Frist-Mail'."""
    if not query:
        return None
    q = query.lower().strip()
    state = get(session_id)
    for mail in reversed(state.recent_mails):
        if q in mail.sender.lower() or q in mail.subject.lower():
            return mail
    return None


def register_session(session_id: str) -> None:
    """Markiert eine Session als aktiv (WebSocket verbunden).
    Muss von server.py beim WebSocket-Accept aufgerufen werden."""
    _active_sessions.add(session_id)


def deregister_session(session_id: str) -> None:
    """Entfernt eine Session aus dem Aktiv-Set (WebSocket getrennt).
    Muss von server.py beim Disconnect aufgerufen werden."""
    _active_sessions.discard(session_id)


def broadcast_active_mail(mail: MailRef) -> None:
    """Setzt active_mail nur in Sessions mit aktiver WebSocket-Verbindung
    plus dem festen 'default'-Slot.

    Vor Issue #89 wurden ALLE persistierten Sessions (auch alte/inaktive
    Browser-Tabs) beschrieben. Nach einem Server-Neustart wurden dadurch
    alle durch load_all() wiederhergestellten Sessions mit der zuletzt
    eingegangenen Mail aufgeweckt.

    Fix: Nur Sessions in _active_sessions (registriert via
    register_session beim WebSocket-Accept, entfernt via
    deregister_session beim Disconnect) erhalten das Update.

    'default' bleibt als Fallback-Slot erhalten, damit Actions die
    active_mail lesen (READ_MAIL, MARK_MAIL_READ, prompt.py) immer
    einen gueltigen Anker haben, auch wenn noch kein Browser-Client
    verbunden ist."""
    sessions = set(_active_sessions)
    sessions.add("default")
    for sid in sessions:
        set_active_mail(sid, mail)
