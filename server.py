"""
Jarvis V2 — FastAPI entry point.

This file is intentionally small: it wires the HTTP / WebSocket
surface together and delegates the actual work to the focused
modules created in M3.1:

  - settings.py     config + secrets + shared httpx/anthropic clients
  - holidays.py     NRW public-holiday math
  - prompt.py       system prompt builder + ACTION-tag parser
  - scheduler.py    weather/tasks refresh + morning-brief background task
  - tts.py          ElevenLabs TTS pipeline (chunking, normalize, retry)
  - actions.py      [ACTION:*] dispatcher to the tool modules
  - conversation.py per-session history with on-disk persistence
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import settings as S
from actions import EMPTY_REPLIES, execute_action
from conversation import (
    append_message,
    conversations,
    get_recent_context_summary,
    load_persistent_history,
)
from prompt import extract_action, get_system_prompt, llm_text, pick_address
import scheduler
from scheduler import (
    birthday_draft_scheduler,
    bring_monitor_scheduler,
    evening_brief_scheduler,
    evening_summary_scheduler,
    meal_plan_reminder_scheduler,
    meal_plan_scheduler,
    memory_reindex_scheduler,
    morning_brief_scheduler,
    proactive_briefs_scheduler,
    refresh_data,
    refresh_morning_brief_data,
    refresh_steuer_recent,
    weekly_outlook_scheduler,
)
from mail_monitor import mail_monitor_main, register_mail_alert_handler
import mail_intelligence
import health_tools
import planner
import session_state
from telegram_bot import telegram_bot_main, send_user_text as telegram_send
from tts import speak

log = S.log


def _guess_proactive_category(text: str) -> str:
    """Return a natural-language category label for the check-in question."""
    lower = text.lower()
    if any(k in lower for k in ("mail", "e-mail", "nachricht von", "schreibt", "absender")):
        return "eine E-Mail-Meldung"
    if any(k in lower for k in ("briefing", "morgen-brief", "morgenbrief", "wochenausblick")):
        return "ein Briefing"
    if any(k in lower for k in ("erinnerung", "termin", "frist", "deadline")):
        return "eine Erinnerung"
    if any(k in lower for k in ("angebot", "angebote")):
        return "eine Angebots-Meldung"
    return "eine Meldung"


async def _broadcast_proactive(text: str) -> None:
    """Push a server-generated message to the UI and Telegram.
    When the web UI is connected, asks before delivering so Catrin can
    decline and receive it on Telegram instead (Issue #148).
    Falls back to Telegram-only when no client is connected.
    Skips entirely during Mac quiet hours (Issue #133)."""
    if S.is_mac_quiet_hours():
        log.info("_broadcast_proactive: mac quiet hours, skipping")
        return
    if not active_clients:
        # No web UI — fall back to Telegram as before.
        log.info("proactive: no clients connected, sending to Telegram")
        await telegram_send(text)
        return
    # Web UI connected — ask first, store for PROACTIVE_DELIVER/DECLINE.
    # telegram_sent=False: PROACTIVE_DECLINE will forward to Telegram.
    target = active_clients[-1]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _show_chrome)
    category = _guess_proactive_category(text)
    S.PENDING_PROACTIVE = {"text": text, "category": category, "telegram_sent": False}
    addr = pick_address()
    question = f"{addr}, haben Sie einen Moment? Ich habe {category} für Sie."
    await speak(question, target, display=question)


async def _mac_alert(text: str) -> None:
    """Mail-monitor alert: ask before delivering when web UI is connected.
    mail_monitor already sent to Telegram, so PROACTIVE_DECLINE must NOT
    re-send (telegram_sent=True). When no client is connected, does nothing."""
    if not active_clients:
        return
    target = active_clients[-1]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _show_chrome)
    category = _guess_proactive_category(text)
    # telegram_sent=True: mail_monitor already forwarded to Telegram.
    S.PENDING_PROACTIVE = {"text": text, "category": category, "telegram_sent": True}
    addr = pick_address()
    question = f"{addr}, haben Sie einen Moment? Ich habe {category} für Sie."
    await speak(question, target, display=question)


async def broadcast_to_all_sessions(text: str) -> None:
    """Send a text-only status message to all connected WebSocket clients
    (display only, no TTS). Used by the planner for silent notifications."""
    for ws in list(active_clients):
        try:
            import json as _json
            await ws.send_text(_json.dumps({"type": "status", "text": text}))
        except Exception:
            pass


async def _extract_and_save_promises(user_text: str) -> None:
    """Hintergrund-Task: extrahiert offene Vorhaben aus user_text und
    speichert sie. Wird nur aufgerufen wenn has_obligation_markers() True ist.
    Fehler werden geloggt aber nicht weitergereicht."""
    try:
        import promise_tracker as _pt
        promises = await _pt.extract_promises(user_text)
        for promise in promises:
            await _pt.save_promise(promise, source="conversation")
    except Exception as e:
        log.warning(f"_extract_and_save_promises failed: {type(e).__name__}: {e}")


@asynccontextmanager
async def _lifespan(_app):  # type: ignore[no-untyped-def]  # AsyncGenerator
    """Startup: prime weather/tasks + spawn the morning-brief task and
    the proactive-briefs task. Shutdown: cancel both and close the
    shared httpx client."""
    # Prüfe PyMuPDF; fehlt es, wird es automatisch nachinstalliert.
    try:
        import fitz  # noqa: F401
    except ImportError:
        log.warning("STARTUP: PyMuPDF fehlt — starte Auto-Install …")
        try:
            import subprocess, sys as _sys
            _loop = asyncio.get_event_loop()
            _r = await _loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [_sys.executable, "-m", "pip", "install", "pymupdf"],
                    capture_output=True, text=True, timeout=300,
                ),
            )
            if _r.returncode == 0:
                log.info("STARTUP: PyMuPDF erfolgreich installiert.")
            else:
                log.error("STARTUP: PyMuPDF-Install fehlgeschlagen: %s", _r.stderr[-300:])
        except Exception as _e:
            log.error("STARTUP: PyMuPDF-Auto-Install Fehler: %s", _e)
    await refresh_data()
    session_state.load_all()
    import meal_plan as _mp
    _mp.load_meal_plan()
    scheduler.register_proactive_handler(_broadcast_proactive)
    register_mail_alert_handler(_mac_alert)
    # Startet Reindex beim Boot (im Hintergrund, blockiert nicht)
    import memory_search
    task_reindex = asyncio.create_task(memory_search.reindex_all())
    task_brief = asyncio.create_task(morning_brief_scheduler())
    task_evening = asyncio.create_task(evening_brief_scheduler())
    task_proactive = asyncio.create_task(proactive_briefs_scheduler())
    task_telegram = asyncio.create_task(telegram_bot_main())
    task_mail = asyncio.create_task(mail_monitor_main())
    task_weekly = asyncio.create_task(weekly_outlook_scheduler())
    task_memory = asyncio.create_task(memory_reindex_scheduler())
    task_planner = asyncio.create_task(planner.planner_loop())
    task_meal_plan = asyncio.create_task(meal_plan_scheduler())
    task_meal_reminder = asyncio.create_task(meal_plan_reminder_scheduler())
    task_birthday_draft = asyncio.create_task(birthday_draft_scheduler())
    task_evening_summary = asyncio.create_task(evening_summary_scheduler())
    log.info(f"Steuerrecht-Scheduler gestartet (taeglich um {S.MORNING_HOUR}:00 Uhr)")
    log.info(f"Abschluss-Ritual aktiv (taeglich um {S.EVENING_HOUR}:00 Uhr)")
    log.info(f"Proaktive Briefs aktiv: {S.PROACTIVE_BRIEFS_TIMES}")
    log.info("Wochenausblick aktiv (Sonntag 18:00)")
    log.info("Memory-Reindex-Scheduler aktiv (täglich 03:00 Uhr + Startup)")
    log.info("Task-Planer aktiv (stündlich, Mo–Fr 17–19 Uhr)")
    log.info("Speiseplanung aktiv (Donnerstag 09:00 + taeglich 17:30 Rezept-Reminder)")
    log.info("Geburtstags-Entwurf-Scheduler aktiv (Freitag 08:00)")
    log.info("Abendzusammenfassung aktiv (taeglich 20:30 Uhr)")
    # Bring!-Monitor (Issue #123): nur starten wenn Zugangsdaten konfiguriert
    task_bring: asyncio.Task | None = None
    if S.BRING_EMAIL and S.BRING_PASSWORD:
        task_bring = asyncio.create_task(bring_monitor_scheduler())
        log.info("Bring!-Monitor aktiv (alle 15 Minuten)")
    else:
        log.info("Bring!-Monitor inaktiv (BRING_EMAIL/PASSWORD nicht konfiguriert)")
    # Mail-Intelligence (Issue #161): passiver Wissensmonitor
    task_mail_intelligence: asyncio.Task | None = None
    if S.MAIL_INTELLIGENCE_ENABLED and S.MAIL_MONITOR_ACCOUNTS:
        task_mail_intelligence = asyncio.create_task(
            mail_intelligence.mail_intelligence_scheduler()
        )
        log.info(
            "Mail-Intelligence aktiv (%d Konto(en), Intervall: %ds)",
            len(S.MAIL_MONITOR_ACCOUNTS), S.MAIL_INTELLIGENCE_INTERVAL,
        )
    else:
        log.info(
            "Mail-Intelligence inaktiv "
            "(MAIL_INTELLIGENCE_ENABLED=%s, Konten=%d)",
            S.MAIL_INTELLIGENCE_ENABLED, len(S.MAIL_MONITOR_ACCOUNTS),
        )
    try:
        yield
    finally:
        tasks_to_cancel = [task_reindex, task_brief, task_evening, task_proactive,
                           task_telegram, task_mail, task_weekly, task_memory,
                           task_planner, task_meal_plan, task_meal_reminder,
                           task_birthday_draft, task_evening_summary]
        if task_bring is not None:
            tasks_to_cancel.append(task_bring)
        if task_mail_intelligence is not None:
            tasks_to_cancel.append(task_mail_intelligence)
        for t in tasks_to_cancel:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await S.ai.close()
        await S.http.aclose()


app = FastAPI(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Side-channel HTTP endpoints used by the launch-session.sh / Shortcut.
# Optionally protected by JARVIS_AUTH_TOKEN (M1.5).
# ---------------------------------------------------------------------------
def _hide_chrome() -> None:
    script = 'tell application "System Events" to set visible of process "Google Chrome" to false'
    subprocess.Popen(["osascript", "-e", script])


def _show_chrome() -> None:
    script = 'tell application "Google Chrome" to activate'
    subprocess.Popen(["osascript", "-e", script])


def require_jarvis_token(x_jarvis_token: str | None = Header(default=None)) -> None:
    """No-op when JARVIS_AUTH_TOKEN is unset, otherwise rejects requests
    without a matching `X-Jarvis-Token` header."""
    if not S.JARVIS_AUTH_TOKEN:
        return
    if x_jarvis_token != S.JARVIS_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Jarvis-Token")


@app.get("/hide", dependencies=[Depends(require_jarvis_token)])
async def hide_endpoint() -> dict:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _hide_chrome)
    return {"ok": True}


@app.get("/show", dependencies=[Depends(require_jarvis_token)])
async def show_endpoint() -> dict:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _show_chrome)
    return {"ok": True}


# Active WebSocket connections; debounce state for /activate.
active_clients: list = []
_inflight_tasks: dict[str, asyncio.Task] = {}
_screen_futures: dict[str, asyncio.Future] = {}
_last_activate_time: float = 0.0
_last_greeting_time: float = 0.0


@app.post("/health")
async def health_webhook(request: Request) -> dict:
    """Empfaengt Gesundheitsdaten von Health Auto Export (iOS).
    Kein Auth-Token noetig — Endpunkt ist nur im Heimnetz erreichbar.
    Speichert die geparsten Daten in S.HEALTH_INFO."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    # Log raw metric names so we can debug field-name mismatches.
    raw = payload.get("data", payload)
    metric_names = [m.get("name", "?") for m in raw.get("metrics", [])]
    log.info(f"/health: empfangene Metriken: {metric_names}")
    parsed = health_tools.parse_health_export(payload)
    # Rotate: gestrige Werte sichern bevor wir ueberschreiben.
    if S.HEALTH_INFO and S.HEALTH_INFO.get("date") != parsed.get("date"):
        S.HEALTH_INFO_PREV = S.HEALTH_INFO
    S.HEALTH_INFO = parsed
    log.info(f"/health: Daten empfangen — Schlaf {parsed.get('sleep_h')}h, "
             f"Bewegung {parsed.get('move_kcal')} kcal, "
             f"Training {parsed.get('exercise_min')} min")
    return {"ok": True, "date": parsed.get("date")}


@app.post("/maintenance/update", dependencies=[Depends(require_jarvis_token)])
async def maintenance_update() -> dict:
    """Git pull + pip install -r requirements.txt + background restart.
    Geschuetzt durch JARVIS_AUTH_TOKEN. Nur im Heimnetz / Tailscale erreichbar.
    """
    import subprocess, sys as _sys, asyncio as _aio, os as _os
    project_dir = _os.path.dirname(_os.path.abspath(__file__))
    results: dict[str, str] = {}

    # 1) git pull
    try:
        r = subprocess.run(["git", "-C", project_dir, "pull"], capture_output=True, text=True, timeout=60)
        results["git_pull"] = r.stdout.strip() or r.stderr.strip() or "ok"
    except Exception as e:
        results["git_pull"] = f"error: {e}"

    # 2) pip install -r requirements.txt
    try:
        r2 = subprocess.run(
            [_sys.executable, "-m", "pip", "install", "-r", f"{project_dir}/requirements.txt"],
            capture_output=True, text=True, timeout=300,
        )
        lines = [l for l in r2.stdout.splitlines() if l.strip()]
        results["pip_install"] = lines[-1] if lines else (r2.stderr[-200:] or "ok")
    except Exception as e:
        results["pip_install"] = f"error: {e}"

    # 3) background restart (3 s Delay damit die Response noch rausgeht)
    async def _restart():
        await _aio.sleep(3)
        subprocess.Popen(["sudo", "systemctl", "restart", "jarvis"])
    _aio.create_task(_restart())

    results["restart"] = "geplant in 3 s"
    return results


@app.get("/health/status")
async def health_status() -> dict:
    """Zeigt die zuletzt empfangenen Health-Daten (nur im Heimnetz erreichbar)."""
    return {
        "current": S.HEALTH_INFO or None,
        "previous": S.HEALTH_INFO_PREV or None,
    }


@app.get("/activate", dependencies=[Depends(require_jarvis_token)])
async def activate_endpoint() -> dict:
    """Wake-up endpoint called by the clap-trigger / keyboard shortcut.
    Debounced to at most once per ACTIVATE_COOLDOWN seconds; sends the
    wake signal only to the most recently connected client."""
    global _last_activate_time
    now = time.time()
    if now - _last_activate_time < S.ACTIVATE_COOLDOWN:
        remaining = int(S.ACTIVATE_COOLDOWN - (now - _last_activate_time))
        log.info(f"/activate ignoriert (Cooldown noch {remaining}s)")
        return {"ok": False, "reason": f"cooldown {remaining}s"}
    _last_activate_time = now
    if not active_clients:
        log.info("/activate: kein Client verbunden")
        return {"ok": False, "reason": "no clients"}
    target = active_clients[-1]
    log.info(f"Wake-Signal an letzten Client ({len(active_clients)} gesamt)")
    try:
        await target.send_json({"type": "wake"})
    except Exception:
        active_clients.remove(target)
        return {"ok": False, "reason": "client send failed"}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Core conversation loop.
# ---------------------------------------------------------------------------
async def process_message(session_id: str, user_text: str, ws: WebSocket) -> None:
    """One user turn end-to-end: refresh data on activate, call Claude,
    speak the reply, optionally execute and summarize an ACTION."""
    global _last_greeting_time

    if session_id not in conversations:
        # Seed a brand-new session with the persisted history (M6.2/M6.4).
        conversations[session_id] = load_persistent_history()
        # Build 3-day context digest and inject into system prompt (M6.4).
        S.RECENT_CONTEXT = get_recent_context_summary(days=3)

    # Speiseplan-Wunsch-Abfrage: Antwort abfangen bevor Claude sie verarbeitet.
    if S.MEAL_PLAN_AWAITING_WISHES:
        async with S.MEAL_PLAN_WISHES_LOCK:
            S.MEAL_PLAN_WISHES = user_text[:500]  # Laenge begrenzen
            S.MEAL_PLAN_AWAITING_WISHES = False
            S.MEAL_PLAN_WISHES_EVENT.set()
        log.info(f"Web: Speisewunsch empfangen: '{user_text[:80]}'")
        reply = (
            "Danke — ich notiere das als Wunsch fuer den Speiseplan "
            "und generiere gleich."
        )
        await speak(reply, ws, display=reply)
        return

    if "activate" in user_text.lower():
        now = time.time()
        if now - _last_greeting_time < S.GREETING_COOLDOWN:
            log.info(f"Doppelbegrüßung blockiert (Cooldown {S.GREETING_COOLDOWN}s)")
            return
        _last_greeting_time = now
        await refresh_data()
        await refresh_steuer_recent()
        # Before MORNING_BRIEF_UNTIL_HOUR also fetch tasks/calendar/politik
        # so the system prompt has everything for the full briefing.
        import datetime as _dt
        if _dt.datetime.now().hour < S.MORNING_BRIEF_UNTIL_HOUR:
            await refresh_morning_brief_data()

    await append_message(session_id, "user", user_text)
    # Strip internal fields (ts, ...) — Anthropic API akzeptiert nur role+content.
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in conversations[session_id][-40:]
    ]

    # Emotionale Kalibrierung (Issue #118): Stress-Level nach jeder Nachricht aktualisieren.
    session_state.update_stress_level(session_id, len(user_text), time.time())

    # Promise-Extraktion (Issue #117): im Hintergrund, blockiert nicht.
    # Nur wenn der Text Verpflichtungs-Marker enthaelt (schneller Regex-Check).
    import promise_tracker as _pt
    if _pt.has_obligation_markers(user_text):
        asyncio.create_task(_extract_and_save_promises(user_text))

    response = await S.ai.messages.create(
        model=S.HAIKU_MODEL,
        max_tokens=1024,
        system=get_system_prompt(),
        messages=history,
    )
    reply = llm_text(response)
    log.info(f"LLM raw: {reply[:200]}")
    spoken_text, action = extract_action(reply)

    # RECALL: hold spoken_text until we know if there are results.
    # The LLM often generates a negative pre-text ("I don't know...") before
    # triggering RECALL, which would then contradict the actual results.
    _hold_spoken = action is not None and action["type"] == "RECALL"

    if spoken_text and not _hold_spoken:
        log.info(f"Jarvis: {spoken_text[:80]}")
        await append_message(session_id, "assistant", spoken_text)
        if not await speak(spoken_text, ws, display=spoken_text):
            return  # WebSocket lost, abort

    if not action:
        return

    log.info(f"Action: {action['type']} -> {action['payload'][:100]}")

    # Two action types speak a status line *before* their result lands.
    if action["type"] == "SCREEN":
        await speak("Lassen Sie mich einen Blick auf Ihren Bildschirm werfen.", ws,
                    display="Lassen Sie mich einen Blick auf Ihren Bildschirm werfen.")
        # Ask the browser to capture a frame; wait up to 30 s for screen_data.
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        _screen_futures[session_id] = fut
        try:
            await ws.send_json({"type": "screen_request"})
            image_b64 = await asyncio.wait_for(asyncio.shield(fut), timeout=30.0)
            if image_b64:
                action["image_b64"] = image_b64
        except asyncio.TimeoutError:
            log.warning("screen_request: no response from browser after 30 s")
        finally:
            _screen_futures.pop(session_id, None)
    elif action["type"] == "MAIL":
        await speak("Ich werfe einen Blick in Ihren Posteingang, Madam.", ws,
                    display="Ich werfe einen Blick in Ihren Posteingang, Madam.")

    try:
        action_result = await execute_action(action)
        log.info(f"Result: {str(action_result)[:200]}")
    except Exception as e:
        log.warning(f"Action error: {e}")
        action_result = f"Fehler: {e}"
    _card_html = S.PENDING_CARD_HTML
    S.PENDING_CARD_HTML = ""

    if action["type"] == "OPEN":
        # OPEN normally stays silent; speak only when the URL was rejected.
        if isinstance(action_result, str) and action_result.startswith("Diese URL"):
            await append_message(session_id, "assistant", action_result)
            await speak(action_result, ws, display=action_result)
        return

    if isinstance(action_result, str) and action_result in EMPTY_REPLIES:
        msg = EMPTY_REPLIES[action_result]
        await append_message(session_id, "assistant", msg)
        await speak(msg, ws, display=msg)
        return

    if not action_result or "fehlgeschlagen" in action_result:
        summary = f"Das hat leider nicht funktioniert, {pick_address()}."
        await append_message(session_id, "assistant", summary)
        await speak(summary, ws, display=summary)
        return

    # Fire-and-forget actions: if the LLM already pre-announced the action
    # (spoken_text non-empty), suppress the success confirmation — only
    # speak when there's a question ("?") or an error in the result.
    # This prevents the double-announcement pattern Catrin reported:
    # "Ich merke mir das." + "Notiert in den Notizen." = two identical beats.
    _SILENT_ON_SUCCESS = {
        "MEMORIZE", "ADDTASK", "DONETASK", "ADDCAL", "NOTE",
        "MARK_MAIL_READ", "MAIL_TO_TASK",
        "DRAFT_APPROVE", "DRAFT_CANCEL",
        "ACCEPT_CALENDAR_INVITE", "DECLINE_CALENDAR_INVITE",
        "ACCEPT_PERSON_ACTION", "DECLINE_PERSON_ACTION",
        "PROMISE_DONE",
        "BRING_ADD", "EINKAUF_FREIGEBEN",
    }
    if action["type"] in _SILENT_ON_SUCCESS and spoken_text:
        if "?" not in action_result and "fehlgeschlagen" not in action_result:
            return  # LLM pre-announced; success needs no repetition
    # RECALL: synthesize raw notes into natural butler speech.
    # spoken_text was held above so no contradiction is possible.
    if action["type"] == "RECALL":
        no_results = action_result.startswith("Ich finde nichts") or \
                     action_result.startswith("Wonach soll ich")
        if no_results:
            # Use the LLM's own "I don't know" phrasing if available, else
            # fall back to the action message.
            msg = spoken_text if spoken_text else action_result
            await append_message(session_id, "assistant", msg)
            await speak(msg, ws, display=msg)
        else:
            addr = pick_address()
            synth_resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
                max_tokens=350,
                system=(
                    f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
                    f"Fasse die folgenden Gedaechtnis-Eintraege in 2-3 natuerlichen Saetzen "
                    f"zusammen. Keine Aufzaehlung, keine 'Notiz:'-Prefixe, keine eckigen "
                    f"Klammern, keine Kategorien. Nur die relevanten Fakten in fliesender "
                    f"Sprache. Ton: trocken, praezise, Butler-Stil. Sprich {addr} an. "
                    f"Keine Begrueszung, kein 'Guten Tag'."
                ),
                messages=[{"role": "user", "content": action_result}],
            )
            summary, _ = extract_action(llm_text(synth_resp))
            summary = scheduler.trim_to_complete_sentences(summary)
            await append_message(session_id, "assistant", summary)
            await speak(summary, ws, display=summary)
        return

    # These actions return text that's already user-friendly — pass
    # through verbatim. Forcing them through the summary pipeline below
    # would (a) mangle long content like full mail bodies, (b) clip via
    # max_tokens, (c) waste an LLM round-trip.
    if action["type"] in (
        "STEUERNEWS", "ADDTASK", "DONETASK", "ADDCAL", "NOTE",
        "MAIL_LOG", "READ_MAIL", "SUMMARIZE_MAIL",
        "DRAFT_REPLY", "DRAFT_REVISE", "DRAFT_APPROVE", "DRAFT_CANCEL",
        "MAIL_TO_TASK", "MARK_MAIL_READ", "DELETE_MAIL", "REMEMBER_SENDER",
        "MEMORIZE", "PROMISE_DONE",
        "ACCEPT_CALENDAR_INVITE", "DECLINE_CALENDAR_INVITE",
        "ACCEPT_PERSON_ACTION", "DECLINE_PERSON_ACTION",
        "WEEKLY_OUTLOOK", "CONTACTS_INFO", "LOOKUP_CONTACT",
        "PLAN_NOW", "IMPORT_MAIL_HISTORY",
        "PROACTIVE_DELIVER", "PROACTIVE_DECLINE",
        "SPEISEPLAN", "SPEISEPLAN_SHOW", "SPEISEPLAN_PREF",
    ):
        await append_message(session_id, "assistant", action_result)
        await speak(action_result, ws, display=action_result, card_html=_card_html)
        return

    # Otherwise: ask Claude to summarize the raw tool output.
    addr = pick_address()
    if action["type"] == "MAIL":
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Gib eine KURZE ueberblickende Info zu den ungelesenen E-Mails — maximal 2 Saetze. "
            f"Lies KEINE einzelnen Mails vor. Nenne nur die Anzahl, wer geschrieben hat und ob etwas Dringendes dabei ist. "
            f"Ton: trocken, knapp, Butler-Stil. Sprich {addr} an. "
            f"KEINE Begruessung wie 'Guten Tag' oder 'Guten Morgen' — der Text folgt schon "
            f"einer Eroeffnung. KEINE Tags in eckigen Klammern."
        )
    elif action["type"] == "NEWS":
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Aus den folgenden Schlagzeilen waehle die DREI WICHTIGSTEN — "
            f"Mix aus Politik und Wirtschaft. "
            f"Format STRENG: genau 3 Nachrichten, je 1 vollstaendiger "
            f"aussagefaehiger Satz (Subjekt+Praedikat+Objekt, mit Punkt). "
            f"KEINE Aufzaehlung mit Bullet, keine Nummerierung. "
            f"Wenn eine Headline klar positiv/konstruktiv ist (Erfolg, "
            f"Fortschritt, Loesung), haenge sie als 4. vollstaendigen Satz "
            f"an. Wenn nichts Positives dabei: bei 3 Saetzen aufhoeren. "
            f"WICHTIG: jeden Satz mit Punkt beenden, keinen Satz abbrechen. "
            f"Sprich {addr} an. KEINE Begruessung. KEINE Tags."
        )
    elif action["type"] == "WEATHER":
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Formuliere den folgenden Wetterbericht auf Deutsch in natuerlichen Saetzen — "
            f"keine Aufzaehlungen, keine Tabellen. "
            f"Nutze deutsche Wetterbegriffe: sonnig, bewoelkt, Schauer, Gewitter, Nieselregen, usw. "
            f"Temperaturen als 'zwischen X und Y Grad'. Regenwahrscheinlichkeit nur erwaehnen "
            f"wenn sie ueber 30 Prozent liegt. "
            f"Umfang: 2-3 fliesende Saetze. Ton: trocken, knapp, Butler-Stil. "
            f"Sprich {addr} an. KEINE Begruessung. KEINE Tags."
        )
    elif action["type"] in ("SEARCH", "BROWSE"):
        # Research prompt: comprehensive enough that follow-up questions work.
        # Raw result also stored in conversation history below so the LLM can
        # reference specifics in subsequent turns.
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler und Recherche-Assistent. "
            f"Fasse die folgenden Recherche-Ergebnisse auf Deutsch zusammen. "
            f"Ziel: {addr} soll nach dieser Zusammenfassung Folgefragen stellen koennen. "
            f"Strukturiere die Antwort so: Was ist das Thema? Wer oder was steckt dahinter? "
            f"Welche Kernfakten sind relevant? Falls vorhanden: Gruender, Produkt, Standort, Preis. "
            f"Umfang: 4-6 vollstaendige Saetze, fliessend, keine Aufzaehlungen. "
            f"Ton: trocken, praezise, Butler-Stil. KEINE Begruessung. KEINE Tags."
        )
    else:
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. Fasse die folgenden "
            f"Informationen KURZ auf Deutsch zusammen — maximal 2-3 Saetze. "
            f"Ton: trocken, knapp, leicht sarkastisch, Butler-Stil. Sprich {addr} an. "
            f"KEINE Begruessung. KEINE Tags in eckigen Klammern. KEINE ACTION-Tags."
        )
    # Research actions get more tokens; others stay at 400.
    summary_max_tokens = 700 if action["type"] in ("SEARCH", "BROWSE") else 400
    summary_resp = await S.ai.messages.create(
        model=S.HAIKU_MODEL,
        max_tokens=summary_max_tokens,
        system=summary_system,
        messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
    )
    summary, _ = extract_action(llm_text(summary_resp))
    # Defense: if the LLM ended mid-sentence, trim back to the last
    # complete sentence so the user doesn't hear truncated content.
    summary = scheduler.trim_to_complete_sentences(summary)
    # Store raw search content in history so follow-up questions have
    # access to the full source material, not just the spoken summary.
    if action["type"] in ("SEARCH", "BROWSE"):
        await append_message(session_id, "user", f"[Recherche-Ergebnis]\n{action_result}")
    await append_message(session_id, "assistant", summary)
    await speak(summary, ws, display=summary)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session_id = str(id(ws))
    # Drop stale connections (prevents multi-wake).
    active_clients.clear()
    active_clients.append(ws)
    # Issue #89: Nur aktive WebSocket-Sessions in broadcast_active_mail
    # beschreiben. Register hier, deregister beim Disconnect.
    session_state.register_session(session_id)
    log.info("Client connected (Liste bereinigt)")

    async def keepalive():
        while True:
            await asyncio.sleep(15)
            try:
                await ws.send_json({"type": "ping"})
            except Exception:
                break

    # Issue #96: Referenz speichern, damit wir den Task beim Disconnect
    # explizit cancellen koennen und kein "Geister-Task" weiterlaeuft.
    ka_task = asyncio.create_task(keepalive())

    def _cancel_inflight(reason: str) -> bool:
        task = _inflight_tasks.get(session_id)
        if task and not task.done():
            log.info(f"cancel inflight ({reason})")
            task.cancel()
            return True
        return False

    def _on_pm_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.exception("process_message error", exc_info=t.exception())

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            if msg_type == "pong":
                continue
            if msg_type == "cancel":
                if _cancel_inflight("client requested cancel"):
                    try:
                        await ws.send_json({"type": "cancelled"})
                    except Exception:
                        pass
                continue
            if msg_type == "screen_data":
                future = _screen_futures.get(session_id)
                if future and not future.done():
                    future.set_result(data.get("image", ""))
                continue

            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            # Cancel still-running prior message so the user can interrupt.
            _cancel_inflight("new message arrived")

            log.info(f"You:    {user_text}")
            task = asyncio.create_task(process_message(session_id, user_text, ws))
            _inflight_tasks[session_id] = task
            task.add_done_callback(_on_pm_done)
            # Not awaited: the loop stays live so screen_data / cancel messages
            # can arrive while process_message is running.

    except Exception as e:
        log.exception(f"Client disconnected: {type(e).__name__}")
        _cancel_inflight("client disconnected")
        _inflight_tasks.pop(session_id, None)
        conversations.pop(session_id, None)
        if ws in active_clients:
            active_clients.remove(ws)
        session_state.deregister_session(session_id)
    finally:
        ka_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ka_task


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")),
    name="static",
)


_STATIC_VER = str(int(time.time()))


@app.get("/")
async def serve_index():
    filepath = os.path.join(os.path.dirname(__file__), "frontend", "index.html")
    with open(filepath, encoding="utf-8") as f:
        html = f.read()
    html = html.replace('/static/style.css"', f'/static/style.css?v={_STATIC_VER}"')
    html = html.replace('/static/main.js"', f'/static/main.js?v={_STATIC_VER}"')
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    ssl_on = bool(S.SERVER_SSL_CERT and S.SERVER_SSL_KEY)
    proto = "https" if ssl_on else "http"
    log.info("=" * 50)
    log.info("J.A.R.V.I.S. V2 Server")
    log.info(f"{proto}://{S.SERVER_HOST}:{S.SERVER_PORT}")
    log.info("=" * 50)
    uvicorn.run(
        app,
        host=S.SERVER_HOST,
        port=S.SERVER_PORT,
        ssl_certfile=S.SERVER_SSL_CERT or None,
        ssl_keyfile=S.SERVER_SSL_KEY or None,
    )
