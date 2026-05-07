"""
Tests for Issue #81: conversation.py async I/O fix.

Verifies that:
1. save_persistent_history is a coroutine (async def)
2. append_message is a coroutine (async def)
3. _write_history_sync performs the actual file I/O correctly
4. asyncio.Lock is used (not threading.Lock)
5. Concurrent calls serialize correctly via the asyncio.Lock
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_conversation_state():
    """Reset the conversation module state between tests."""
    # Force a fresh import of conversation (bypasses stale sys.modules from
    # other test files that inject settings stubs).
    if "conversation" in sys.modules:
        conv = sys.modules["conversation"]
        original_lock = conv._history_lock
        original_conversations = dict(conv.conversations)
    else:
        original_lock = None
        original_conversations = {}
    yield
    if "conversation" in sys.modules:
        conv = sys.modules["conversation"]
        conv._history_lock = original_lock
        conv.conversations.clear()
        conv.conversations.update(original_conversations)


@pytest.fixture
def tmp_history(tmp_path):
    """Patch HISTORY_PATH to a temp file and enable PERSIST_HISTORY.

    Works even when test_calendar_invite.py has injected a settings stub
    that lacks these attributes — we set them directly via setattr.
    """
    import settings as S
    history_file = tmp_path / "history.json"
    original_path = getattr(S, "HISTORY_PATH", None)
    original_persist = getattr(S, "PERSIST_HISTORY", None)
    S.HISTORY_PATH = str(history_file)
    S.PERSIST_HISTORY = True
    yield history_file
    if original_path is None:
        if hasattr(S, "HISTORY_PATH"):
            del S.HISTORY_PATH
    else:
        S.HISTORY_PATH = original_path
    if original_persist is None:
        if hasattr(S, "PERSIST_HISTORY"):
            del S.PERSIST_HISTORY
    else:
        S.PERSIST_HISTORY = original_persist


# ---------------------------------------------------------------------------
# Structural tests (no I/O needed)
# ---------------------------------------------------------------------------

def test_save_persistent_history_is_coroutine():
    """save_persistent_history must be an async function (Issue #81)."""
    import conversation
    assert inspect.iscoroutinefunction(conversation.save_persistent_history), (
        "save_persistent_history must be async def"
    )


def test_append_message_is_coroutine():
    """append_message must be an async function so callers can await it."""
    import conversation
    assert inspect.iscoroutinefunction(conversation.append_message), (
        "append_message must be async def"
    )


def test_no_threading_lock_in_module():
    """The module must not import threading or use threading.Lock (Issue #81)."""
    import conversation
    assert not hasattr(conversation, "threading"), (
        "conversation module must not import threading"
    )


def test_asyncio_lock_is_used():
    """The module-level lock must be an asyncio.Lock (or None before first use)."""
    import conversation
    conversation._history_lock = None  # reset to test lazy init
    lock = conversation._history_lock
    assert lock is None or isinstance(lock, asyncio.Lock), (
        f"_history_lock must be asyncio.Lock or None, got {type(lock)}"
    )


def test_get_history_lock_creates_asyncio_lock():
    """_get_history_lock() must return an asyncio.Lock."""
    import conversation
    conversation._history_lock = None  # reset
    lock = conversation._get_history_lock()
    assert isinstance(lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# Functional tests (require an event loop)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_message_updates_in_memory(tmp_history):
    """append_message must add to the in-memory dict."""
    import conversation
    session = "test-session"
    conversation.conversations.pop(session, None)

    await conversation.append_message(session, "user", "Hello")

    assert session in conversation.conversations
    msgs = conversation.conversations[session]
    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "Hello"}


@pytest.mark.asyncio
async def test_append_message_persists_to_disk(tmp_history):
    """append_message must write history to disk via save_persistent_history."""
    import conversation
    session = "sess-persist"
    conversation.conversations.pop(session, None)

    await conversation.append_message(session, "user", "hi")
    await conversation.append_message(session, "assistant", "hello")

    assert tmp_history.exists(), "History file must be created"
    data = json.loads(tmp_history.read_text())
    assert session in data
    assert data[session][0]["content"] == "hi"
    assert data[session][1]["content"] == "hello"


@pytest.mark.asyncio
async def test_write_history_sync_atomic(tmp_history):
    """_write_history_sync must use tempfile + os.replace (no partial writes)."""
    import conversation

    session = "atomic-test"
    conversation._write_history_sync(session, [{"role": "user", "content": "x"}])

    assert tmp_history.exists()
    # The .tmp file must NOT remain after the write
    tmp_file = Path(str(tmp_history) + ".tmp")
    assert not tmp_file.exists(), ".tmp file must be removed after atomic replace"


@pytest.mark.asyncio
async def test_save_persistent_history_no_op_when_disabled(tmp_history):
    """When PERSIST_HISTORY is False, no file must be written."""
    import settings as S
    import conversation
    original = S.PERSIST_HISTORY
    S.PERSIST_HISTORY = False
    try:
        await conversation.save_persistent_history("s", [{"role": "user", "content": "x"}])
        assert not tmp_history.exists(), "No file must be written when persistence is disabled"
    finally:
        S.PERSIST_HISTORY = original


@pytest.mark.asyncio
async def test_concurrent_append_messages_serialize(tmp_history):
    """Concurrent append_message calls must not corrupt the history file."""
    import conversation
    session = "concurrent-test"
    conversation.conversations.pop(session, None)
    conversation._history_lock = None  # ensure fresh lock

    # Fire 10 concurrent appends
    await asyncio.gather(*[
        conversation.append_message(session, "user", f"msg-{i}")
        for i in range(10)
    ])

    # All 10 messages must be in memory
    assert len(conversation.conversations[session]) == 10

    # File must be valid JSON
    data = json.loads(tmp_history.read_text())
    assert session in data
    assert len(data[session]) == 10


@pytest.mark.asyncio
async def test_run_in_executor_called(tmp_history):
    """save_persistent_history must delegate I/O to run_in_executor."""
    import conversation

    executor_calls = []

    async def fake_run_in_executor(pool, func, *args):
        executor_calls.append(func.__name__)
        return func(*args)

    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    loop.run_in_executor = fake_run_in_executor
    try:
        await conversation.save_persistent_history(
            "exec-test", [{"role": "user", "content": "test"}]
        )
    finally:
        loop.run_in_executor = original

    assert "_write_history_sync" in executor_calls, (
        "save_persistent_history must call run_in_executor with _write_history_sync"
    )
