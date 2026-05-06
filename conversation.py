"""
Conversation history with on-disk persistence.

Single user, single rolling history (M6.2): when Jarvis is restarted
the previous context seeds the next session. Disabled when
`persist_conversations: false` in config.json.
"""

from __future__ import annotations

import json
import os
import threading

import settings as S

log = S.log

# Per-WebSocket-session conversation. Each entry is {"role": "...", "content": "..."}.
conversations: dict[str, list] = {}
MAX_CONVERSATION_HISTORY = 50

# Module-level lock: serialises concurrent save_persistent_history calls from
# different browser tabs so they read-modify-write the shared history file
# without clobbering each other (Issue #62).
_history_lock = threading.Lock()


def load_persistent_history() -> list:
    """Read the rolling history from disk (best-effort).

    Returns the entries stored under the 'default' session key so that a
    brand-new session is seeded with the combined history of previous ones.
    """
    if not S.PERSIST_HISTORY or not os.path.exists(S.HISTORY_PATH):
        return []
    try:
        with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Multi-session format: return the 'default' slot as seed.
            default = data.get("default", [])
            if isinstance(default, list):
                return default[-MAX_CONVERSATION_HISTORY:]
        elif isinstance(data, list):
            # Legacy single-list format: return as-is.
            return data[-MAX_CONVERSATION_HISTORY:]
    except Exception as e:
        log.warning(f"load_persistent_history failed: {type(e).__name__}: {e}")
    return []


def save_persistent_history(session_id: str, history: list) -> None:
    """Best-effort write to disk; never crashes the request path.

    Reads the current on-disk state first so that multiple simultaneous
    browser-tab sessions do not overwrite each other's history (Issue #62).
    The file is a JSON object keyed by session_id; writes are serialised via
    a module-level threading.Lock so a concurrent .tmp → replace race is
    impossible within the same process.
    """
    if not S.PERSIST_HISTORY:
        return
    try:
        with _history_lock:
            # Read current on-disk data.
            existing: dict = {}
            if os.path.exists(S.HISTORY_PATH):
                try:
                    with open(S.HISTORY_PATH, "r", encoding="utf-8") as f:
                        on_disk = json.load(f)
                    if isinstance(on_disk, dict):
                        existing = on_disk
                    elif isinstance(on_disk, list):
                        # Migrate legacy single-list format: put it under 'default'.
                        existing = {"default": on_disk}
                except Exception:
                    pass  # start fresh if file is corrupt
            # Update only our session's slice.
            existing[session_id] = history[-MAX_CONVERSATION_HISTORY:]
            # Keep a 'default' slot that always holds the most recent session
            # so load_persistent_history() can seed new tabs quickly.
            existing["default"] = history[-MAX_CONVERSATION_HISTORY:]
            tmp = S.HISTORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
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
    save_persistent_history(session_id, conv)
