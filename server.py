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
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import settings as S
from actions import EMPTY_REPLIES, execute_action
from conversation import (
    append_message,
    conversations,
    load_persistent_history,
)
from prompt import extract_action, get_system_prompt, llm_text, pick_address
import scheduler
from scheduler import (
    memory_reindex_scheduler,
    morning_brief_scheduler,
    proactive_briefs_scheduler,
    refresh_data,
    refresh_morning_brief_data,
    refresh_steuer_recent,
    weekly_outlook_scheduler,
)
from mail_monitor import mail_monitor_main, register_mail_alert_handler
import planner
import session_state
from telegram_bot import telegram_bot_main
from tts import speak

log = S.log


async def _broadcast_proactive(text: str) -> None:
    """Push a server-generated message to the most recent client.
    Brings Chrome to the foreground first so the user sees + hears it."""
    if not active_clients:
        log.info("proactive: no clients connected, skipping")
        return
    target = active_clients[-1]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _show_chrome)
    await speak(text, target, display=text)


async def broadcast_to_all_sessions(text: str) -> None:
    """Send a text-only status message to all connected WebSocket clients
    (display only, no TTS). Used by the planner for silent notifications."""
    for ws in list(active_clients):
        try:
            import json as _json
            await ws.send_text(_json.dumps({"type": "status", "text": text}))
        except Exception:
            pass


@asynccontextmanager
async def _lifespan(_app):  # type: ignore[no-untyped-def]  # AsyncGenerator
    """Startup: prime weather/tasks + spawn the morning-brief task and
    the proactive-briefs task. Shutdown: cancel both and close the
    shared httpx client."""
    await refresh_data()
    session_state.load_all()
    scheduler.register_proactive_handler(_broadcast_proactive)
    register_mail_alert_handler(_broadcast_proactive)
    # Startet Reindex beim Boot (im Hintergrund, blockiert nicht)
    import memory_search
    task_reindex = asyncio.create_task(memory_search.reindex_all())
    task_brief = asyncio.create_task(morning_brief_scheduler())
    task_proactive = asyncio.create_task(proactive_briefs_scheduler())
    task_telegram = asyncio.create_task(telegram_bot_main())
    task_mail = asyncio.create_task(mail_monitor_main())
    task_weekly = asyncio.create_task(weekly_outlook_scheduler())
    task_memory = asyncio.create_task(memory_reindex_scheduler())
    task_planner = asyncio.create_task(planner.planner_loop())
    log.info(f"Steuerrecht-Scheduler gestartet (taeglich um {S.MORNING_HOUR}:00 Uhr)")
    log.info(f"Proaktive Briefs aktiv: {S.PROACTIVE_BRIEFS_TIMES}")
    log.info("Wochenausblick aktiv (Sonntag 18:00)")
    log.info("Memory-Reindex-Scheduler aktiv (täglich 03:00 Uhr + Startup)")
    log.info("Task-Planer aktiv (stündlich, Mo–Fr 17–19 Uhr)")
    try:
        yield
    finally:
        for t in (task_reindex, task_brief, task_proactive, task_telegram, task_mail,
                  task_weekly, task_memory, task_planner):
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


def require_jarvis_token(x_jarvis_token: Optional[str] = Header(default=None)) -> None:
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
_last_activate_time: float = 0.0
_last_greeting_time: float = 0.0


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
        # Seed a brand-new session with the persisted history (M6.2).
        conversations[session_id] = load_persistent_history()

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
    history = conversations[session_id][-16:]

    response = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
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
    elif action["type"] == "MAIL":
        await speak("Ich werfe einen Blick in Ihren Posteingang, Madam.", ws,
                    display="Ich werfe einen Blick in Ihren Posteingang, Madam.")

    try:
        action_result = await execute_action(action)
        log.info(f"Result: {str(action_result)[:200]}")
    except Exception as e:
        log.warning(f"Action error: {e}")
        action_result = f"Fehler: {e}"

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
                model="claude-haiku-4-5-20251001",
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
        "READ_MAIL", "SUMMARIZE_MAIL",
        "DRAFT_REPLY", "DRAFT_REVISE", "DRAFT_APPROVE", "DRAFT_CANCEL",
        "MAIL_TO_TASK", "MARK_MAIL_READ",
        "MEMORIZE",
        "ACCEPT_CALENDAR_INVITE", "DECLINE_CALENDAR_INVITE",
        "ACCEPT_PERSON_ACTION", "DECLINE_PERSON_ACTION",
        "WEEKLY_OUTLOOK", "CONTACTS_INFO", "LOOKUP_CONTACT",
        "PLAN_NOW",
    ):
        await append_message(session_id, "assistant", action_result)
        await speak(action_result, ws, display=action_result)
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
    else:
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. Fasse die folgenden "
            f"Informationen KURZ auf Deutsch zusammen — maximal 2-3 Saetze. "
            f"Ton: trocken, knapp, leicht sarkastisch, Butler-Stil. Sprich {addr} an. "
            f"KEINE Begruessung. KEINE Tags in eckigen Klammern. KEINE ACTION-Tags."
        )
    summary_resp = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=summary_system,
        messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
    )
    summary, _ = extract_action(llm_text(summary_resp))
    # Defense: if the LLM ended mid-sentence, trim back to the last
    # complete sentence so the user doesn't hear truncated content.
    summary = scheduler.trim_to_complete_sentences(summary)
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

            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            # Cancel still-running prior message so the user can interrupt
            # by simply talking again.
            _cancel_inflight("new message arrived")

            log.info(f"You:    {user_text}")
            task = asyncio.create_task(process_message(session_id, user_text, ws))
            _inflight_tasks[session_id] = task
            try:
                await task
            except asyncio.CancelledError:
                log.info("process_message was cancelled")

    except Exception as e:
        log.info(f"Client disconnected: {type(e).__name__}")
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


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn
    log.info("=" * 50)
    log.info("J.A.R.V.I.S. V2 Server")
    log.info(f"http://{S.SERVER_HOST}:{S.SERVER_PORT}")
    log.info("=" * 50)
    uvicorn.run(app, host=S.SERVER_HOST, port=S.SERVER_PORT)
