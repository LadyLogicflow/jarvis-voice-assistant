"""
Tests fuer das Qwen-LLM-Backend (Issue #249).

Prueft:
- call_qwen() gibt Antwort-Text zurueck wenn Qwen erreichbar
- call_qwen() faellt auf Haiku zurueck bei Timeout
- call_qwen() faellt direkt auf Haiku zurueck wenn S.qwen = None
- call_qwen() faellt auf Haiku zurueck bei beliebiger Exception
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Sicherstellen dass die echten Module geladen werden, nicht Stubs.
# test_full_context_block.py und test_person_context_format.py installieren
# einen Stub-`prompt` in sys.modules. Diesen hier durch das echte Modul
# ersetzen damit call_qwen() verfuegbar ist.
# ---------------------------------------------------------------------------

def _ensure_real_modules() -> None:
    """Laedt settings und prompt aus /workspace/jarvis, ersetzt ggf. Stubs."""
    jarvis_root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    if jarvis_root not in sys.path:
        sys.path.insert(0, jarvis_root)
    # Stub-Erkennung: hat das Modul keinen richtigen __file__-Pfad, ist es ein Stub.
    for mod_name in ("settings", "prompt"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            origin = getattr(mod, "__file__", None) or ""
            if "jarvis" not in origin:
                # Stub entfernen so dass das echte Modul neu importiert wird
                del sys.modules[mod_name]
    import settings  # noqa: F401
    import prompt    # noqa: F401


_ensure_real_modules()


def _run(coro):
    """Fuehrt eine Coroutine synchron aus."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_openai_response(text: str):
    """Baut eine OpenAI-kompatible Chat-Completion-Response."""
    choice = MagicMock()
    choice.message = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_anthropic_response(text: str):
    """Baut eine Anthropic-kompatible Messages-Response."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


class TestCallQwenSuccess:
    """Qwen antwortet korrekt — Text wird direkt zurueckgegeben."""

    def test_call_qwen_success(self):
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat = MagicMock()
        mock_qwen_client.chat.completions = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response("Qwen-Antwort")
        )

        original_qwen = S.qwen
        try:
            S.qwen = mock_qwen_client
            result = _run(prompt.call_qwen("System-Prompt", "User-Frage", max_tokens=100))
        finally:
            S.qwen = original_qwen

        assert result == "Qwen-Antwort"
        mock_qwen_client.chat.completions.create.assert_awaited_once()

    def test_call_qwen_strips_whitespace(self):
        """Fuehrende/abschliessende Leerzeichen werden entfernt."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response("  Text mit Leerzeichen  ")
        )

        original_qwen = S.qwen
        try:
            S.qwen = mock_qwen_client
            result = _run(prompt.call_qwen("sys", "user"))
        finally:
            S.qwen = original_qwen

        assert result == "Text mit Leerzeichen"

    def test_call_qwen_empty_response(self):
        """Wenn Qwen None als content zurueckgibt, wird leerer String zurueckgegeben."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response(None)
        )

        original_qwen = S.qwen
        try:
            S.qwen = mock_qwen_client
            result = _run(prompt.call_qwen("sys", "user"))
        finally:
            S.qwen = original_qwen

        assert result == ""


class TestCallQwenTimeoutFallback:
    """Timeout -> Fallback auf Claude Haiku."""

    def test_call_qwen_timeout_fallback(self):
        import asyncio
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            side_effect=asyncio.TimeoutError("Timeout")
        )
        mock_haiku_resp = _make_anthropic_response("Haiku-Fallback-Text")

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = mock_qwen_client
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(return_value=mock_haiku_resp)
            S.ai = mock_ai

            result = _run(prompt.call_qwen("System", "User", max_tokens=200))
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert result == "Haiku-Fallback-Text"
        mock_ai.messages.create.assert_awaited_once()

    def test_call_qwen_connection_error_fallback(self):
        """Verbindungsfehler -> Fallback auf Haiku."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            side_effect=ConnectionError("Connection refused")
        )
        mock_haiku_resp = _make_anthropic_response("Haiku bei Verbindungsfehler")

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = mock_qwen_client
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(return_value=mock_haiku_resp)
            S.ai = mock_ai

            result = _run(prompt.call_qwen("sys", "user"))
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert result == "Haiku bei Verbindungsfehler"


class TestCallQwenDisabled:
    """S.qwen = None -> direkt Haiku, kein Versuch Qwen zu erreichen."""

    def test_call_qwen_disabled_uses_haiku_directly(self):
        import settings as S
        import prompt

        mock_haiku_resp = _make_anthropic_response("Haiku direkt")

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = None
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(return_value=mock_haiku_resp)
            S.ai = mock_ai

            result = _run(prompt.call_qwen("sys", "user", max_tokens=150))
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert result == "Haiku direkt"
        mock_ai.messages.create.assert_awaited_once()

    def test_call_qwen_disabled_passes_max_tokens(self):
        """max_tokens-Parameter wird korrekt an Haiku weitergegeben."""
        import settings as S
        import prompt

        mock_haiku_resp = _make_anthropic_response("OK")

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = None
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(return_value=mock_haiku_resp)
            S.ai = mock_ai

            _run(prompt.call_qwen("sys", "user", max_tokens=99))
            call_kwargs = mock_ai.messages.create.call_args
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert call_kwargs.kwargs.get("max_tokens") == 99


class TestCallQwenExceptionFallback:
    """Beliebige Exception von Qwen -> Fallback auf Haiku."""

    def test_call_qwen_exception_fallback(self):
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("Unerwarteter Fehler")
        )
        mock_haiku_resp = _make_anthropic_response("Haiku nach Exception")

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = mock_qwen_client
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(return_value=mock_haiku_resp)
            S.ai = mock_ai

            result = _run(prompt.call_qwen("sys", "user"))
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert result == "Haiku nach Exception"

    def test_haiku_fallback_also_fails_returns_empty_string(self):
        """Wenn auch Haiku fehlschlaegt, wird leerer String zurueckgegeben."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("Qwen down")
        )

        original_qwen = S.qwen
        original_ai = S.ai
        try:
            S.qwen = mock_qwen_client
            mock_ai = MagicMock()
            mock_ai.messages.create = AsyncMock(
                side_effect=Exception("Haiku auch down")
            )
            S.ai = mock_ai

            result = _run(prompt.call_qwen("sys", "user"))
        finally:
            S.qwen = original_qwen
            S.ai = original_ai

        assert result == ""
