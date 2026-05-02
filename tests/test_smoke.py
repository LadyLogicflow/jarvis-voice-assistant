"""Smoke test: the test infrastructure itself works."""

from __future__ import annotations


def test_pytest_runs():
    assert 1 + 1 == 2


def test_repo_layout_visible():
    """Project root is on sys.path so we can import the actual modules."""
    import importlib.util

    for name in ("browser_tools", "screen_capture", "notes_tools"):
        spec = importlib.util.find_spec(name)
        assert spec is not None, f"module {name!r} should be importable"
