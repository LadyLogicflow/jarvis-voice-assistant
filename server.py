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
from prompt import extract_action, get_system_prompt
import scheduler
from scheduler import (
    morning_brief_scheduler,
    proactive_briefs_scheduler,
    refresh_data,
    refresh_morning_brief_data,
    refresh_steuer_recent,
)
from tts import speak

log = S.log


async def _broadcast_proactive(text: str) -> None:
    """Push a server-generated message to the most recent client.
    Brings Chrome to the foreground first so the user sees + hears it."""
    if not active_clients:
        log.info("proactive: no clients connected, skipping")
        return
    target = active_clients[-1]
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _show_chrome)
    await speak(text, target, display=text)


@asynccontextmanager
async def _lifespan(_app):  # type: ignore[no-untyped-def]  # AsyncGenerator
    """Startup: prime weather/tasks + spawn the morning-brief task and
    the proactive-briefs task. Shutdown: cancel both and close the
    shared httpx client."""
    await refresh_data()
    scheduler.register_proactive_handler(_broadcast_proactive)
    task_brief = asyncio.create_task(morning_brief_scheduler())
    task_proactive = asyncio.create_task(proactive_briefs_scheduler())
    log.info(f"Steuerrecht-Scheduler gestartet (taeglich um {S.MORNING_HOUR}:00 Uhr)")
    log.info(f"Proaktive Briefs aktiv: {S.PROACTIVE_BRIEFS_TIMES}")
    try:
        yield
    finally:
        for t in (task_brief, task_proactive):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
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
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _hide_chrome)
    return {"ok": True}


@app.get("/show", dependencies=[Depends(require_jarvis_token)])
async def show_endpoint() -> dict:
    loop = asyncio.get_event_loop()
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

    append_message(session_id, "user", user_text)
    history = conversations[session_id][-16:]

    response = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=get_system_prompt(),
        messages=history,
    )
    reply = response.content[0].text
    log.info(f"LLM raw: {reply[:200]}")
    spoken_text, action = extract_action(reply)

    if spoken_text:
        log.info(f"Jarvis: {spoken_text[:80]}")
        append_message(session_id, "assistant", spoken_text)
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
            append_message(session_id, "assistant", action_result)
            await speak(action_result, ws, display=action_result)
        return

    if isinstance(action_result, str) and action_result in EMPTY_REPLIES:
        msg = EMPTY_REPLIES[action_result]
        append_message(session_id, "assistant", msg)
        await speak(msg, ws, display=msg)
        return

    if not action_result or "fehlgeschlagen" in action_result:
        summary = f"Das hat leider nicht funktioniert, {S.USER_ADDRESS}."
        append_message(session_id, "assistant", summary)
        await speak(summary, ws, display=summary)
        return

    # These actions return text that's already user-friendly — pass through.
    if action["type"] in ("STEUERNEWS", "ADDTASK", "DONETASK", "ADDCAL", "NOTE"):
        append_message(session_id, "assistant", action_result)
        await speak(action_result, ws, display=action_result)
        return

    # Otherwise: ask Claude to summarize the raw tool output.
    if action["type"] == "MAIL":
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Gib eine KURZE ueberblickende Info zu den ungelesenen E-Mails — maximal 2 Saetze. "
            f"Lies KEINE einzelnen Mails vor. Nenne nur die Anzahl, wer geschrieben hat und ob etwas Dringendes dabei ist. "
            f"Ton: trocken, knapp, Butler-Stil. Sprich {S.USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
        )
    elif action["type"] == "NEWS":
        summary_system = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Fasse die Nachrichtenlage in maximal 2-3 praegnanten Saetzen zusammen — wie ein Butler der die Zeitung ueberflogen hat. "
            f"Nenne nur die 2-3 wichtigsten Themen, kein Auflisten einzelner Meldungen. "
            f"Ton: trocken, informiert, kein Journalistendeutsch. Sprich {S.USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
        )
    else:
        summary_system = (
            f"Du bist Jarvis. Fasse die folgenden Informationen KURZ auf Deutsch zusammen, "
            f"maximal 2-3 Saetze, im Jarvis-Stil. Sprich den Nutzer als {S.USER_ADDRESS} an. "
            f"KEINE Tags in eckigen Klammern. KEINE ACTION-Tags."
        )
    summary_resp = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=summary_system,
        messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
    )
    summary, _ = extract_action(summary_resp.content[0].text)
    append_message(session_id, "assistant", summary)
    await speak(summary, ws, display=summary)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session_id = str(id(ws))
    # Drop stale connections (prevents multi-wake).
    active_clients.clear()
    active_clients.append(ws)
    log.info("Client connected (Liste bereinigt)")

    async def keepalive():
        while True:
            await asyncio.sleep(15)
            try:
                await ws.send_json({"type": "ping"})
            except Exception:
                break

    asyncio.create_task(keepalive())

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

    except (WebSocketDisconnect, RuntimeError, Exception) as e:
        log.info(f"Client disconnected: {type(e).__name__}")
        _cancel_inflight("client disconnected")
        _inflight_tasks.pop(session_id, None)
        conversations.pop(session_id, None)
        if ws in active_clients:
            active_clients.remove(ws)


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
    log.info(f"http://localhost:{S.SERVER_PORT}")
    log.info("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=S.SERVER_PORT)
