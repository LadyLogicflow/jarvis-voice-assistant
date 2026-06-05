"""
Conversation history with on-disk persistence.

Single user, single rolling history (M6.2): when Jarvis is restarted
the previous context seeds the next session. Disabled when
`persist_conversations: false` in config.json.

M6.4: 3-day rolling window with timestamps. get_recent_context_summary()
builds a compact day-by-day digest for proactive system-prompt injection.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import time as _time

import settings as S

log = S.log

# Per-WebSocket-session conversation. Each entry is
# {"role": "...", "content": "...", "ts": float}.
conversations: dict[str, list] = {}

MAX_CONVERSATION_HISTORY = 200   # hard cap per session
MAX_CONVERSATION_DAYS = 3        # rolling window for disk storage + context summary

# Module-level asyncio lock: serialises concurrent save_persistent_history calls
# from different browser tabs so they read-modify-write the shared history file
# without clobbering each other (Issue #62, #81).
_history_lock: asyncio.Lock | None = None


def _get_history_lock() -> asyncio.Lock:
    global _history_lock
    if _history_lock is None:
        _history_lock = asyncio.Lock()
    return _history_lock


def _cutoff_ts(days: int = MAX_CONVERSATION_DAYS) -> float:
    return _time.time() - days * 86400


def load_persistent_history() -> list:
    """Read the rolling history from disk (best-effort).

    Returns the entries stored under the 'default' session key so that a
    brand-new session is seeded with the combined history of previous ones.
    Only messages from the last MAX_CONVERSATION_DAYS days are returned.
    """
    if not S.PERSIST_HISTORY or not os.path.exists(S.HISTORY_PATH):
        return []
    try:
        with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        msgs: list = []
        if isinstance(data, dict):
            default = data.get("default", [])
            if isinstance(default, list):
                msgs = default
        elif isinstance(data, list):
            msgs = data
        # Apply time filter — keep messages without ts (legacy) and recent ones.
        cutoff = _cutoff_ts()
        filtered = [
            m for m in msgs
            if not isinstance(m.get("ts"), (int, float)) or m["ts"] >= cutoff
        ]
        return filtered[-MAX_CONVERSATION_HISTORY:]
    except Exception as e:
        log.warning(f"load_persistent_history failed: {type(e).__name__}: {e}")
    return []


def get_recent_context_summary(days: int = MAX_CONVERSATION_DAYS) -> str:
    """Return a compact day-by-day digest of the last N days' user messages.

    Only messages with a 'ts' field (written since M6.4) are included.
    Reads the 'default' (web-UI) session slot from disk so it works even
    before the first WebSocket session of the day has started.

    Returns an empty string if nothing useful is available.
    """
    if not S.PERSIST_HISTORY or not os.path.exists(S.HISTORY_PATH):
        return ""
    try:
        with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        msgs: list = []
        if isinstance(data, dict):
            default = data.get("default", [])
            if isinstance(default, list):
                msgs = default
        elif isinstance(data, list):
            msgs = data
        cutoff = _cutoff_ts(days)
        recent_user = [
            m for m in msgs
            if isinstance(m, dict)
            and m.get("role") == "user"
            and isinstance(m.get("ts"), (int, float))
            and m["ts"] >= cutoff
        ]
        if not recent_user:
            return ""
        recent_user.sort(key=lambda m: m["ts"])
        by_day: dict[str, list[str]] = {}
        for m in recent_user:
            day_label = _dt.datetime.fromtimestamp(m["ts"]).strftime("%d.%m.")
            snippet = m["content"][:100].replace("\n", " ").strip()
            if snippet:
                by_day.setdefault(day_label, []).append(snippet)
        if not by_day:
            return ""
        lines = ["Gesprächsverlauf letzte {} Tage (zur proaktiven Nutzung):".format(days)]
        for day in sorted(by_day.keys()):
            snippets = by_day[day][:5]  # max 5 snippets per day
            lines.append("[{}] {}".format(day, " | ".join(snippets)))
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"get_recent_context_summary failed: {type(e).__name__}: {e}")
        return ""


def _write_history_sync(session_id: str, history: list) -> None:
    """Synchronous file I/O for persisting conversation history.

    Reads the current on-disk state first so that multiple simultaneous
    browser-tab sessions do not overwrite each other's history (Issue #62).
    Uses an atomic tempfile + os.replace pattern to prevent partial writes.
    Drops messages older than MAX_CONVERSATION_DAYS to bound file growth.
    """
    existing: dict = {}
    if os.path.exists(S.HISTORY_PATH):
        try:
            with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            if isinstance(on_disk, dict):
                existing = on_disk
            elif isinstance(on_disk, list):
                existing = {"default": on_disk}
        except Exception:
            pass
    # Drop messages older than MAX_CONVERSATION_DAYS, then cap by count.
    cutoff = _cutoff_ts()
    to_store = [
        m for m in history
        if not isinstance(m.get("ts"), (int, float)) or m["ts"] >= cutoff
    ]
    to_store = to_store[-MAX_CONVERSATION_HISTORY:]
    existing[session_id] = to_store
    # Keep a 'default' slot for web UI sessions only.
    if not session_id.lstrip("-").isdigit():
        existing["default"] = to_store
    tmp = S.HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False)
    os.replace(tmp, S.HISTORY_PATH)


async def save_persistent_history(session_id: str, history: list) -> None:
    """Best-effort async write to disk; never crashes the request path."""
    if not S.PERSIST_HISTORY:
        return
    try:
        async with _get_history_lock():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _write_history_sync, session_id, history
            )
    except Exception as e:
        log.warning(f"save_persistent_history failed: {type(e).__name__}: {e}")


async def append_message(session_id: str, role: str, content: str) -> None:
    """Append a message to a conversation, cap the list length, and
    persist to disk when persistence is enabled."""
    conv = conversations.setdefault(session_id, [])
    conv.append({"role": role, "content": content, "ts": _time.time()})
    if content:
        try:
            import memory_search as _ms
            import logging as _logging
            _doc_id = _ms.make_doc_id(f"conversation_{role}", content)
            loop = asyncio.get_running_loop()

            def _index_and_log() -> None:
                try:
                    _ms.index_text(content, "conversation", _doc_id, {"role": role})
                except Exception as exc:
                    _logging.getLogger(__name__).warning(
                        "conversation memory indexing failed: %s", exc, exc_info=True
                    )

            asyncio.ensure_future(
                asyncio.get_event_loop().run_in_executor(None, _index_and_log)
            )
        except Exception:
            pass
    if len(conv) > MAX_CONVERSATION_HISTORY:
        del conv[: len(conv) - MAX_CONVERSATION_HISTORY]
    await save_persistent_history(session_id, conv)
