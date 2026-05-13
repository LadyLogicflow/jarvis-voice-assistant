"""
JARVIS Task Planner (Issue #98)

Hourly background loop that syncs Todoist tasks to Google Calendar:
- Callback tasks ("Rückruf" etc.) → Mail draft with booking link
- Known task types (Steuererklärung etc.) → fixed-duration calendar block
- Unknown tasks → flagged for Catrin to provide duration

Scheduling window: Mon–Fri 17:00–19:00 Europe/Berlin.
Notifications only when something actually happened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, date

import pytz

log = logging.getLogger("jarvis.planner")

_TZ = pytz.timezone("Europe/Berlin")
_DB_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_planner.json")
_BOOKING_LINK = "https://hiloneuss.simplybook.it/v2/#book/service/4/count/1/provider/5/"

# ── Duration rules (keyword → minutes) ────────────────────────────────────────
_DURATION_RULES: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\best\s*202[0-9]\b", re.I), 90),
    (re.compile(r"steuererklärung|steuererklaerung|einkommensteuer", re.I), 90),
    (re.compile(r"\besg\b|\bnachhaltigkeitsbericht\b", re.I), 60),
    (re.compile(r"\blohnsteuer\b|\bjahresausgleich\b", re.I), 60),
]
_DEFAULT_CALLBACK_MIN = 15  # unused for calendar, kept for reference

# ── Callback detection ─────────────────────────────────────────────────────────
_CALLBACK_PATTERN = re.compile(
    r"\b(rückruf|rueckruf|zurückrufen|zurueckrufen|anrufen|telefonisch|tel\.?\s*rückruf)\b",
    re.I,
)


def _detect_type(title: str) -> tuple[str, int]:
    """Returns (type, duration_min). type is 'callback', 'calendar', or 'unknown'."""
    if _CALLBACK_PATTERN.search(title):
        return ("callback", 0)
    for pattern, minutes in _DURATION_RULES:
        if pattern.search(title):
            return ("calendar", minutes)
    return ("unknown", 0)


def _extract_name(title: str) -> str:
    """Best-effort: extract a person name from a callback task title."""
    cleaned = _CALLBACK_PATTERN.sub("", title).strip(" :,-")
    # Remove common leading words
    cleaned = re.sub(r"^(an|bei|für|fuer|von|wegen)\s+", "", cleaned, flags=re.I)
    return cleaned.strip() or title.strip()


# ── Planner DB ─────────────────────────────────────────────────────────────────
def _load_db() -> dict:
    if not os.path.exists(_DB_PATH):
        return {}
    try:
        with open(_DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("planner: db load failed: %s", e)
        return {}


def _save_db(db: dict) -> None:
    try:
        tmp = _DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DB_PATH)
    except Exception as e:
        log.warning("planner: db save failed: %s", e)


# ── Slot finder ────────────────────────────────────────────────────────────────
async def _find_free_slot(duration_min: int, start_from: datetime | None = None) -> datetime | None:
    """Find next free Mo–Fr 17:00–19:00 slot for duration_min minutes.
    start_from defaults to now (Berlin time)."""
    import google_calendar_tools as cal

    now = start_from or datetime.now(_TZ)
    candidate = now.replace(second=0, microsecond=0)

    _max_iters = 14 * 20  # hard cap: 14 days × 20 slots/day max
    for _iter in range(_max_iters):
        # Skip weekends
        if candidate.weekday() >= 5:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
            continue
        # Move to 17:00 if before window
        if candidate.hour < 17:
            candidate = candidate.replace(hour=17, minute=0)
        # Skip past window
        if candidate.hour >= 19 or (candidate.hour == 18 and
                candidate.minute + duration_min > 60):
            candidate = (candidate + timedelta(days=1)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
            continue

        slot_end = candidate + timedelta(minutes=duration_min)
        if slot_end.hour > 19 or (slot_end.hour == 19 and slot_end.minute > 0):
            candidate = (candidate + timedelta(days=1)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
            continue

        # Fetch existing events for this day's window
        day_start = candidate.replace(hour=17, minute=0, second=0, microsecond=0)
        day_end = candidate.replace(hour=19, minute=0, second=0, microsecond=0)
        events = await cal.get_events_raw(day_start, day_end)

        # Check for overlap
        conflict = False
        for ev in events:
            ev_start_str = ev["start"].get("dateTime", "")
            ev_end_str = ev["end"].get("dateTime", "")
            if not ev_start_str or not ev_end_str:
                continue
            ev_s = datetime.fromisoformat(ev_start_str.replace("Z", "+00:00")).astimezone(_TZ)
            ev_e = datetime.fromisoformat(ev_end_str.replace("Z", "+00:00")).astimezone(_TZ)
            # Overlap if slot_start < ev_end AND slot_end > ev_start
            if candidate < ev_e and slot_end > ev_s:
                # Move candidate to end of conflicting event, rounded up to 15 min
                new_candidate = ev_e.replace(second=0, microsecond=0)
                mins = new_candidate.minute
                new_candidate = new_candidate.replace(
                    minute=((mins + 14) // 15) * 15 % 60,
                    hour=new_candidate.hour + ((mins + 14) // 15) * 15 // 60,
                )
                # Safety: always advance by at least 15 minutes to prevent oscillation
                if new_candidate <= candidate:
                    new_candidate = candidate + timedelta(minutes=15)
                candidate = new_candidate
                conflict = True
                break

        if not conflict:
            return candidate

    return None  # no slot found in 2 weeks


# ── Contact email lookup ───────────────────────────────────────────────────────
async def _lookup_email(name: str) -> str:
    """Try to find an email address for a person name. Returns "" if not found."""
    if not name:
        return ""
    try:
        import contacts
        import persons_db
        q = name.lower()
        # 1. persons_db (Catrin's stored profiles)
        for prof in persons_db.all_profiles():
            if q in prof.name.lower():
                if prof.primary_email:
                    return prof.primary_email
        # 2. Apple Contacts
        hits = await contacts.find_contacts_by_name(name)
        if hits:
            for c in hits:
                if c.emails:
                    return c.emails[0]
    except Exception as e:
        log.warning("planner: email lookup failed for %r: %s", name, e)
    return ""


# ── Main sync ──────────────────────────────────────────────────────────────────
async def _sync_once() -> list[str]:
    """Run one planning cycle. Returns list of notification lines (empty = silent)."""
    import settings as S
    import todoist_tools
    import google_calendar_tools as cal
    import mail_tools

    if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
        return []

    # Fetch all open Todoist tasks (raw dicts)
    raw = await todoist_tools._fetch_all_tasks(S.TODOIST_TOKEN)
    if isinstance(raw, str):
        log.warning("planner: todoist fetch failed: %s", raw)
        return []

    my_id = await todoist_tools._my_id(S.TODOIST_TOKEN)

    mine = [
        t for t in raw
        if not t.get("checked")
        and not t.get("is_deleted")
        and (not my_id or str(t.get("creator_id", "")) == my_id or str(t.get("assignee_id", "")) == my_id)
    ]
    open_ids = {str(t["id"]) for t in mine}
    task_by_id = {str(t["id"]): t for t in mine}

    db = _load_db()
    notifications: list[str] = []

    # ── Clean up completed tasks ──────────────────────────────────────────────
    for task_id in list(db.keys()):
        if task_id not in open_ids:
            entry = db.pop(task_id)
            event_id = entry.get("event_id", "")
            if event_id and entry.get("type") == "calendar":
                await cal.delete_event(event_id)
                log.info("planner: removed completed task %s, deleted event %s", task_id, event_id)
            else:
                log.info("planner: removed completed task %s (type=%s)", task_id, entry.get("type"))

    # ── Schedule new tasks ────────────────────────────────────────────────────
    needs_duration: list[str] = []

    for task_id in open_ids:
        if task_id in db:
            continue  # already handled

        task = task_by_id[task_id]
        title = task.get("content", "").strip()
        if not title:
            continue

        task_type, duration_min = _detect_type(title)

        if task_type == "callback":
            name = _extract_name(title)
            email = await _lookup_email(name)
            recipient = email or ""
            placeholder = "" if email else "\n\n[E-Mail-Adresse bitte ergänzen]"
            body = (
                f"Sehr geehrte Damen und Herren,\n\n"
                f"für ein Telefonat bitten wir Sie, einen Termin über folgenden Link zu buchen:\n"
                f"{_BOOKING_LINK}\n\n"
                f"Mit freundlichen Grüßen\n"
                f"Lohnsteuerhilfeverein HILO e.V., Beratungsstelle Neuss"
                f"{placeholder}"
            )
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: mail_tools.create_draft(
                    to=recipient,
                    subject="Telefontermin Lohnsteuerhilfeverein HILO",
                    body=body,
                ),
            )
            db[task_id] = {"type": "callback", "event_id": "", "title": title}
            if email:
                notifications.append(f"✉ Entwurf erstellt: Rückruf {name} ({email})")
            else:
                notifications.append(f"✉ Entwurf erstellt: Rückruf {name} — E-Mail-Adresse nicht gefunden, bitte ergänzen")
            log.info("planner: callback draft for task %s (%s): %s", task_id, title, result)

        elif task_type == "calendar":
            slot = await _find_free_slot(duration_min)
            if slot is None:
                notifications.append(f"⚠ Kein freier Slot gefunden für: {title}")
                continue
            try:
                event_id = await cal.create_event_at(title, slot, duration_min)
                db[task_id] = {
                    "type": "calendar",
                    "event_id": event_id,
                    "title": title,
                    "scheduled_at": slot.isoformat(),
                    "duration_min": duration_min,
                }
                day_str = slot.strftime("%a %d.%m. %H:%M")
                notifications.append(f"📅 Eingeplant: {title} — {day_str} Uhr ({duration_min} Min)")
                log.info("planner: scheduled task %s at %s event=%s", task_id, slot, event_id)
            except Exception as e:
                log.warning("planner: create_event_at failed for %s: %s", task_id, e)
                notifications.append(f"⚠ Kalender-Fehler für: {title} ({e})")

        else:  # unknown
            needs_duration.append(title)
            db[task_id] = {"type": "pending_duration", "event_id": "", "title": title}

    if needs_duration:
        tasks_str = ", ".join(f'"{t}"' for t in needs_duration)
        notifications.append(f"❓ Zeitbedarf unbekannt — bitte angeben für: {tasks_str}")

    _save_db(db)
    return notifications


# ── Notification helper ────────────────────────────────────────────────────────
async def _notify(lines: list[str]) -> None:
    """Send planning summary via WebSocket broadcast + Telegram text."""
    if not lines:
        return
    msg = "Planung aktualisiert:\n" + "\n".join(lines)

    try:
        import session_state
        import server as _srv
        await _srv.broadcast_to_all_sessions(msg)
    except Exception as e:
        log.warning("planner: ws broadcast failed: %s", e)

    try:
        import telegram_bot
        await telegram_bot.send_user_text(msg)
    except Exception as e:
        log.warning("planner: telegram send failed: %s", e)


# ── Background loop ────────────────────────────────────────────────────────────
async def planner_loop() -> None:
    """Long-running asyncio task. Spawned by server.lifespan."""
    log.info("planner: loop started")
    while True:
        try:
            notifications = await _sync_once()
            await _notify(notifications)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("planner: sync_once failed: %s", e)
        await asyncio.sleep(3600)


# ── Manual trigger ─────────────────────────────────────────────────────────────
async def plan_now() -> str:
    """Trigger an immediate sync. Returns a summary string."""
    try:
        notifications = await _sync_once()
        if not notifications:
            return "Nichts Neues — alles bereits eingeplant."
        return "Planung aktualisiert:\n" + "\n".join(notifications)
    except Exception as e:
        return f"Planer-Fehler: {e}"
