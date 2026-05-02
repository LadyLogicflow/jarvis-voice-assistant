"""Shared pytest fixtures.

Most server.py imports require a populated config.json + .env. The
fixtures here patch the environment / filesystem just enough so that
isolated unit tests of pure functions can import the module.

Tests of pure helpers (`get_easter`, `_split_text`, `_is_safe_url`, ...)
can use the lightweight `import_pure_funcs` fixture; tests touching IO
can use the heavier `mock_httpx` / `mock_anthropic` fixtures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Make the project root importable so tests can `import server`, etc.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _stub_env_and_config(monkeypatch):
    """Provide minimal env vars + ensure a config.json exists so
    `import server` doesn't crash on a fresh checkout. The values are
    safe to share across tests; individual tests that need real config
    can override fields after import."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs-key")
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
    monkeypatch.delenv("PICOVOICE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_TOKEN", raising=False)

    # CI / fresh checkouts don't have a config.json (it's gitignored).
    # Write a minimal stub if missing — won't overwrite a developer's real
    # config.json.
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        cfg_path.write_text(
            '{"user_name":"TestUser","city":"Hamburg","workspace_path":"/tmp"}',
            encoding="utf-8",
        )


@pytest.fixture
def mock_httpx(monkeypatch):
    """Drop-in async client whose `.get`, `.post` return a 200 JSON.
    Use respx for richer scenarios; this is the cheap path."""
    response = MagicMock()
    response.status_code = 200
    response.text = "{}"
    response.content = b"{}"
    response.json = lambda: {}
    response.raise_for_status = lambda: None

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.aclose = AsyncMock(return_value=None)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_anthropic():
    """Anthropic AsyncAnthropic stub. `.messages.create` returns a
    response object whose `.content[0].text` is `'OK'`."""
    msg = MagicMock()
    msg.content = [MagicMock(text="OK")]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=msg)
    return client
