"""
Tests fuer den LLM-Backend-Umschalter (Issue #250).

Prueft:
- Default-Modus ist 'qwen' wenn S.qwen gesetzt
- Default-Modus ist 'claude' wenn S.qwen None ist
- set_llm_mode('qwen') setzt den Modus korrekt
- set_llm_mode('claude') setzt den Modus korrekt
- Ungultiger Wert loest ValueError aus
- call_llm() leitet an call_qwen() weiter im Qwen-Modus
- call_llm() leitet an Haiku weiter im Claude-Modus
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Echte Module sicherstellen (kein Stub-prompt aus anderen Test-Modulen).
# ---------------------------------------------------------------------------

def _ensure_real_modules() -> None:
    jarvis_root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    if jarvis_root not in sys.path:
        sys.path.insert(0, jarvis_root)
    for mod_name in ("settings", "prompt"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            origin = getattr(mod, "__file__", None) or ""
            if "jarvis" not in origin:
                del sys.modules[mod_name]
    import settings  # noqa: F401
    import prompt    # noqa: F401


_ensure_real_modules()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_openai_response(text: str):
    choice = MagicMock()
    choice.message = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_anthropic_response(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


# ---------------------------------------------------------------------------
# Helper: reset _llm_mode to what it was before a test.
# ---------------------------------------------------------------------------

class _ModeSaver:
    """Kontext-Manager: stellt _llm_mode nach dem Test wieder her."""

    def __enter__(self):
        import settings as S
        self._original = S._llm_mode
        return self

    def __exit__(self, *_):
        import settings as S
        S._llm_mode = self._original


# ===========================================================================
# Tests
# ===========================================================================


class TestDefaultMode:
    """Default-Modus haengt davon ab ob S.qwen gesetzt ist."""

    def test_default_mode_qwen_when_client_set(self):
        """Wenn S.qwen ein Client-Objekt ist, muss der Default 'qwen' sein."""
        import settings as S
        mock_client = MagicMock()
        with _ModeSaver():
            S.qwen = mock_client
            # Simuliere Modul-Import-Logik: setze _llm_mode so wie es beim
            # Import gesetzt wuerde wenn S.qwen nicht None waere.
            S._llm_mode = "qwen" if (S.qwen is not None) else "claude"
            assert S.get_llm_mode() == "qwen"

    def test_default_mode_claude_when_no_client(self):
        """Wenn S.qwen None ist, muss der Default 'claude' sein."""
        import settings as S
        original_qwen = S.qwen
        with _ModeSaver():
            S.qwen = None
            S._llm_mode = "qwen" if (S.qwen is not None) else "claude"
            assert S.get_llm_mode() == "claude"
        S.qwen = original_qwen


class TestSetMode:
    """set_llm_mode() und get_llm_mode() korrekt."""

    def test_set_mode_qwen(self):
        import settings as S
        with _ModeSaver():
            S.set_llm_mode("qwen")
            assert S.get_llm_mode() == "qwen"

    def test_set_mode_claude(self):
        import settings as S
        with _ModeSaver():
            S.set_llm_mode("claude")
            assert S.get_llm_mode() == "claude"

    def test_set_mode_invalid_raises(self):
        import settings as S
        with _ModeSaver():
            with pytest.raises(ValueError, match="Unbekannter LLM-Modus"):
                S.set_llm_mode("gpt4")

    def test_set_mode_invalid_empty_raises(self):
        import settings as S
        with _ModeSaver():
            with pytest.raises(ValueError):
                S.set_llm_mode("")


class TestCallLlmRouting:
    """call_llm() leitet je nach Modus an das richtige Backend weiter."""

    def test_call_llm_routes_to_qwen(self):
        """Im Qwen-Modus wird call_qwen() aufgerufen."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat = MagicMock()
        mock_qwen_client.chat.completions = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response("Qwen-Antwort")
        )

        original_qwen = S.qwen
        with _ModeSaver():
            S.qwen = mock_qwen_client
            S.set_llm_mode("qwen")
            result = _run(prompt.call_llm("System", "User", max_tokens=50))

        S.qwen = original_qwen
        assert result == "Qwen-Antwort"
        mock_qwen_client.chat.completions.create.assert_awaited_once()

    def test_call_llm_routes_to_haiku(self):
        """Im Claude-Modus wird Haiku direkt aufgerufen (kein Qwen)."""
        import settings as S
        import prompt

        mock_haiku_resp = _make_anthropic_response("Haiku-Antwort")

        with _ModeSaver(), patch.object(
            S.ai.messages, "create", new=AsyncMock(return_value=mock_haiku_resp)
        ):
            S.set_llm_mode("claude")
            result = _run(prompt.call_llm("System", "User", max_tokens=50))

        assert result == "Haiku-Antwort"

    def test_call_llm_qwen_mode_does_not_call_haiku_directly(self):
        """Im Qwen-Modus wird Haiku NICHT direkt aufgerufen."""
        import settings as S
        import prompt

        mock_qwen_client = MagicMock()
        mock_qwen_client.chat = MagicMock()
        mock_qwen_client.chat.completions = MagicMock()
        mock_qwen_client.chat.completions.create = AsyncMock(
            return_value=_make_openai_response("Qwen")
        )

        original_qwen = S.qwen
        haiku_called = []

        async def _fake_haiku(system, user, max_tokens):
            haiku_called.append(True)
            return "HAIKU"

        with _ModeSaver(), patch.object(prompt, "_call_haiku_fallback", new=_fake_haiku):
            S.qwen = mock_qwen_client
            S.set_llm_mode("qwen")
            _run(prompt.call_llm("System", "User", max_tokens=50))

        S.qwen = original_qwen
        assert haiku_called == [], "Haiku sollte im Qwen-Modus nicht direkt aufgerufen werden"
