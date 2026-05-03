"""
Conversation history with on-disk persistence.

Single user, single rolling history (M6.2): when Jarvis is restarted
the previous context seeds the next session. Disabled when
`persist_conversations: false` in config.json.
"""

from __future__ import annotations

import json
import os

import settings as S

log = S.log

# Per-WebSocket-session conversation. Each entry is {"role": "...", "content": "..."}.
conversations: dict[str, list] = {}
MAX_CONVERSATION_HISTORY = 50


def load_persistent_history() -> list:
    """Read the rolling history from disk (best-effort)."""
    if not S.PERSIST_HISTORY or not os.path.exists(S.HISTORY_PATH):
        return []
    try:
        with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data[-MAX_CONVERSATION_HISTORY:]
    except Exception as e:
        log.warning(f"load_persistent_history failed: {type(e).__name__}: {e}")
    return []


def save_persistent_history(history: list) -> None:
    """Best-effort write to disk; never crashes the request path."""
    if not S.PERSIST_HISTORY:
        return
    try:
        tmp = S.HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history[-MAX_CONVERSATION_HISTORY:], f, ensure_ascii=False)
        os.replace(tmp, S.HISTORY_PATH)
    except Exception as e:
        log.warning(f"save_persistent_history failed: {type(e).__name__}: {e}")


def append_message(session_id: str, role: str, content: str) -> None:
    """Append a message to a conversation, cap the list length, and
    persist to disk when persistence is enabled."""
    conv = conversations.setdefault(session_id, [])
    conv.append({"role": role, "content": content})
    if len(conv) > MAX_CONVERSATION_HISTORY:
        del conv[: len(conv) - MAX_CONVERSATION_HISTORY]
    save_persistent_history(conv)
