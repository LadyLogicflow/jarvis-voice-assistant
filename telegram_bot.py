"""
Telegram-Bot — zweiter Kommunikationskanal neben der Web-UI.

Empfaengt Sprach- oder Textnachrichten von Catrins Telegram-Bot,
transkribiert Voice-Notes mit faster-whisper (lokal), schickt den Text
durch denselben Claude+Action-Flow wie die Web-UI und antwortet als
Voice-Note (TTS via ElevenLabs).

Ruhezeit (default 21:00-07:00) blockiert Antworten — der Bot meldet
einen kurzen "schlafe noch"-Satz statt zu eskalieren.

Issue #47.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import os
import tempfile
from typing import Optional

from actions import EMPTY_REPLIES, execute_action
import settings as S
from prompt import extract_action, get_system_prompt, pick_address
from tts import _split_text, _tts_one, normalize_for_tts

log = S.log


# ---------------------------------------------------------------------------
# Whisper transcription (lazy-loaded; first call takes a few seconds while
# the model loads, every call after is fast).
# ---------------------------------------------------------------------------
_whisper_model = None


def _load_whisper():
    """Lazy import + load. Doing it at module-import time would block
    server startup for ~5 seconds on first run."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    from faster_whisper import WhisperModel  # type: ignore
    log.info(f"Loading faster-whisper model={S.WHISPER_MODEL!r} (one-time)")
    _whisper_model = WhisperModel(
        S.WHISPER_MODEL,
        device="cpu",
        compute_type="int8",  # smallest memory + decent speed on Apple Silicon
    )
    return _whisper_model


async def _transcribe(audio_bytes: bytes) -> str:
    """Transcribe an OGG/MP3/WAV blob via faster-whisper (German)."""
    loop = asyncio.get_event_loop()

    def _do():
        model = _load_whisper()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            segments, _info = model.transcribe(
                tmp_path,
                language="de",
                beam_size=5,
                vad_filter=True,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return await loop.run_in_executor(None, _do)


# Quiet hours helper now lives in settings.is_quiet_hours so the IMAP
# mail monitor (issue #48) can share it.
is_quiet_hours = S.is_quiet_hours


# Actions that DON'T make sense over Telegram (need the Mac browser /
# screen / Mail.app). Their tags are stripped and we tell the user.
_TELEGRAM_BAD_ACTIONS = {"OPEN", "SCREEN"}


async def _summarize_action(action_type: str, action_result: str) -> str:
    """Ask Claude to condense the raw tool output into 2-3 spoken
    sentences — same shape as server.process_message does for the
    WebSocket flow."""
    addr = pick_address()
    no_greeting = (
        "KEINE Begruessung wie 'Guten Tag' oder 'Guten Morgen' — "
        "der Text folgt schon einer Eroeffnung."
    )
    if action_type == "MAIL":
        sys_prompt = (
            f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
            f"Gib eine KURZE ueberblickende Info zu den ungelesenen E-Mails — "
            f"maximal 2 Saetze. Nenne nur die Anzahl, wer geschrieben hat und "
            f"ob etwas Dringendes dabei ist. Sprich {addr} an. "
            f"{no_greeting} KEINE Tags in eckigen Klammern."
        )
    elif action_type == "NEWS":
        sys_prompt = (
            f"Du bist Jarvis. Fasse die Nachrichten in maximal 2-3 praegnanten "
            f"Saetzen zusammen. Sprich {addr} an. "
            f"{no_greeting} KEINE Tags in eckigen Klammern."
        )
    else:
        sys_prompt = (
            f"Du bist Jarvis. Fasse die folgenden Informationen KURZ auf "
            f"Deutsch zusammen, maximal 2-3 Saetze, im Jarvis-Stil. "
            f"Sprich {addr} an. {no_greeting} KEINE Tags in eckigen Klammern. "
            f"KEINE ACTION-Tags."
        )
    resp = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=sys_prompt,
        messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
    )
    summary, _ = extract_action(resp.content[0].text)
    return summary


async def _ask_claude(user_text: str) -> str:
    """Mirror server.process_message: LLM call, optional action, optional
    summarization. Returns the final spoken text."""
    response = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=get_system_prompt(),
        messages=[{"role": "user", "content": user_text}],
    )
    reply = response.content[0].text
    spoken_text, action = extract_action(reply)
    log.info(f"Telegram LLM: spoken='{spoken_text[:80]}' action={action}")

    # No action — just speak the LLM's text.
    if not action:
        return spoken_text or reply

    a_type = action["type"]

    if a_type in _TELEGRAM_BAD_ACTIONS:
        # OPEN / SCREEN don't help when she's on the phone.
        return (
            f"{spoken_text} Diese Aktion ({a_type}) macht ueber Telegram keinen "
            f"Sinn, {pick_address()} — versuch's am Mac."
        ).strip()

    try:
        action_result = await execute_action(action)
        log.info(f"Telegram action result: '{str(action_result)[:120]}'")
    except Exception as e:
        log.warning(f"Telegram action failed: {type(e).__name__}: {e}")
        return f"{spoken_text} Die Aktion ist fehlgeschlagen, {pick_address()}."

    # Empty-result sentinels.
    if isinstance(action_result, str) and action_result in EMPTY_REPLIES:
        return EMPTY_REPLIES[action_result]

    # Actions whose result is already user-facing text. Same passthrough
    # set as server.process_message — never re-summarize these.
    if a_type in (
        "STEUERNEWS", "ADDTASK", "DONETASK", "ADDCAL", "NOTE",
        "READ_MAIL", "SUMMARIZE_MAIL",
        "DRAFT_REPLY", "DRAFT_REVISE", "DRAFT_APPROVE", "DRAFT_CANCEL",
        "MAIL_TO_TASK", "MARK_MAIL_READ",
    ):
        return action_result

    # The rest go through a summarization pass like the WebSocket flow.
    summary = await _summarize_action(a_type, action_result)
    return f"{spoken_text} {summary}".strip() if spoken_text else summary


async def _tts_full(text: str) -> bytes:
    """Concatenate TTS chunks into one audio blob suitable for
    Telegram's voice-note attachment."""
    text = normalize_for_tts(text)
    if not text:
        return b""
    out = bytearray()
    for chunk in _split_text(text):
        audio = await _tts_one(chunk)
        if audio:
            out += audio
    return bytes(out)


# ---------------------------------------------------------------------------
# python-telegram-bot handlers.
# ---------------------------------------------------------------------------
def _is_authorized(update) -> bool:
    """Filter: only respond to Catrin's own chat id (when configured)."""
    if not S.TELEGRAM_CHAT_ID:
        return True  # no filter -> open
    return str(update.effective_chat.id) == str(S.TELEGRAM_CHAT_ID)


async def _handle_message(update, context, *, source_text: str | None = None) -> None:
    """Common handler for both voice and text messages."""
    if not _is_authorized(update):
        log.warning(
            f"Telegram: ignoring message from unauthorized chat id "
            f"{update.effective_chat.id}"
        )
        return
    if is_quiet_hours():
        await update.message.reply_text(
            f"Schlafenszeit, {pick_address()}. Ich melde mich morgen ab "
            f"{S.TELEGRAM_QUIET_END} Uhr wieder."
        )
        return

    try:
        if source_text is None:
            voice = update.message.voice or update.message.audio
            if voice is None:
                await update.message.reply_text("Bitte als Sprachnachricht oder Text.")
                return
            tg_file = await voice.get_file()
            audio_bytes = bytes(await tg_file.download_as_bytearray())
            log.info(f"Telegram voice {len(audio_bytes)} bytes — transcribing")
            user_text = await _transcribe(audio_bytes)
            log.info(f"Telegram transcript: '{user_text[:120]}'")
            if not user_text:
                await update.message.reply_text("Ich konnte nichts verstehen.")
                return
        else:
            user_text = source_text
            log.info(f"Telegram text: '{user_text[:120]}'")

        reply_text = await _ask_claude(user_text)
        log.info(f"Telegram reply: '{reply_text[:120]}'")

        audio = await _tts_full(reply_text)
        if audio:
            await update.message.reply_voice(voice=io.BytesIO(audio), caption=None)
        else:
            await update.message.reply_text(reply_text)
    except Exception as e:
        log.warning(f"Telegram handler error: {type(e).__name__}: {e}")
        try:
            await update.message.reply_text(f"Fehler: {e}")
        except Exception:
            pass


async def _voice_handler(update, context) -> None:
    await _handle_message(update, context, source_text=None)


async def _text_handler(update, context) -> None:
    await _handle_message(update, context, source_text=update.message.text or "")


# Reference to the running Application; set by telegram_bot_main once
# the bot is up. Other modules (mail_monitor) use it via send_user_text.
_app = None


async def send_user_text(text: str) -> bool:
    """Push a text message to Catrin's Telegram chat from anywhere in
    the server. Returns True on success, False if not configured /
    bot not running / send failed. Quiet-hours aware."""
    if not S.TELEGRAM_BOT_TOKEN or not S.TELEGRAM_CHAT_ID:
        return False
    if _app is None:
        log.warning("send_user_text: bot not yet running")
        return False
    if S.is_quiet_hours():
        log.info(f"send_user_text suppressed by quiet hours: {text[:60]!r}")
        return False
    try:
        await _app.bot.send_message(chat_id=S.TELEGRAM_CHAT_ID, text=text)
        return True
    except Exception as e:
        log.warning(f"send_user_text failed: {type(e).__name__}: {e}")
        return False


async def send_user_voice(spoken_text: str, caption: str | None = None) -> bool:
    """Push a spoken voice-note (ElevenLabs TTS) plus optional text
    caption to Catrin's Telegram chat. Falls back to text if the TTS
    pipeline returns nothing. Quiet-hours aware."""
    if not S.TELEGRAM_BOT_TOKEN or not S.TELEGRAM_CHAT_ID:
        return False
    if _app is None:
        log.warning("send_user_voice: bot not yet running")
        return False
    if S.is_quiet_hours():
        log.info(f"send_user_voice suppressed by quiet hours: {spoken_text[:60]!r}")
        return False
    try:
        audio = await _tts_full(spoken_text)
        if not audio:
            log.info("send_user_voice: TTS empty, falling back to text")
            return await send_user_text(caption or spoken_text)
        await _app.bot.send_voice(
            chat_id=S.TELEGRAM_CHAT_ID,
            voice=io.BytesIO(audio),
            caption=caption,
        )
        return True
    except Exception as e:
        log.warning(f"send_user_voice failed: {type(e).__name__}: {e}")
        return False


async def telegram_bot_main() -> None:
    """Long-running task: starts the Telegram bot loop. Spawned by
    server.lifespan when TELEGRAM_BOT_TOKEN is configured."""
    global _app
    if not S.TELEGRAM_BOT_TOKEN:
        log.info("Telegram bot disabled (no TELEGRAM_BOT_TOKEN in env)")
        return
    try:
        from telegram import Update
        from telegram.ext import (
            ApplicationBuilder,
            MessageHandler,
            filters,
        )
    except ImportError:
        log.warning("python-telegram-bot not installed — Telegram bot disabled")
        return

    log.info(
        f"Telegram bot starting (chat filter: "
        f"{S.TELEGRAM_CHAT_ID or 'open'} | quiet: "
        f"{S.TELEGRAM_QUIET_START}-{S.TELEGRAM_QUIET_END})"
    )
    _app = ApplicationBuilder().token(S.TELEGRAM_BOT_TOKEN).build()
    _app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _voice_handler))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text_handler))

    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try:
        # Block until cancelled by the lifespan teardown.
        while True:
            await asyncio.sleep(3600)
    finally:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        _app = None
