"""Tests for the main application module."""
from src.main import main


def test_main_runs_without_error():
    """Main function should execute without raising exceptions."""
    # Basic smoke test
    main()
