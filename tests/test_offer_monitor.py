"""Tests fuer offer_monitor.py — Angebote-Monitor (Issue #122).

Prueft den Cache-Mechanismus, das Matching-Logik und das graceful
Fallback-Verhalten wenn Maerkte nicht erreichbar sind.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fresh_cache(offers: dict) -> dict:
    """Erstellt einen frischen Cache-Dict (Timestamp: jetzt)."""
    return {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "offers": offers,
    }


def _make_stale_cache(offers: dict) -> dict:
    """Erstellt einen veralteten Cache-Dict (Timestamp: vor 8 Stunden)."""
    old_ts = datetime.datetime.utcnow() - datetime.timedelta(hours=8)
    return {
        "timestamp": old_ts.isoformat(),
        "offers": offers,
    }


# ---------------------------------------------------------------------------
# Cache-Tests
# ---------------------------------------------------------------------------

class TestCacheHelpers:
    def test_is_cache_fresh_with_new_cache(self):
        import offer_monitor
        cache = _make_fresh_cache({"Coca Cola": ["Rewe"]})
        assert offer_monitor._is_cache_fresh(cache) is True

    def test_is_cache_fresh_with_stale_cache(self):
        import offer_monitor
        cache = _make_stale_cache({"Coca Cola": ["Rewe"]})
        assert offer_monitor._is_cache_fresh(cache) is False

    def test_is_cache_fresh_with_broken_timestamp(self):
        import offer_monitor
        cache = {"timestamp": "INVALID", "offers": {}}
        assert offer_monitor._is_cache_fresh(cache) is False

    def test_load_cache_returns_none_when_missing(self, tmp_path, monkeypatch):
        import offer_monitor
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", str(tmp_path / "nope.json"))
        assert offer_monitor._load_cache() is None

    def test_save_and_load_cache(self, tmp_path, monkeypatch):
        import offer_monitor
        cache_file = str(tmp_path / "cache.json")
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        expected = {"Coca Cola": ["Rewe", "Lidl"]}
        offer_monitor._save_cache(expected)
        loaded = offer_monitor._load_cache()
        assert loaded is not None
        assert loaded["offers"] == expected

    def test_save_cache_handles_write_error(self, monkeypatch):
        import offer_monitor
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", "/nonexistent/path/cache.json")
        # Darf keine Exception werfen
        offer_monitor._save_cache({"item": ["market"]})


# ---------------------------------------------------------------------------
# Matching-Tests
# ---------------------------------------------------------------------------

class TestItemMatching:
    def test_exact_match(self):
        import offer_monitor
        assert offer_monitor._item_in_offers("Coca Cola", ["Coca Cola 1,5l", "Pepsi"]) is True

    def test_partial_match(self):
        import offer_monitor
        assert offer_monitor._item_in_offers("Cola", ["Coca Cola Flasche"]) is True

    def test_case_insensitive_match(self):
        import offer_monitor
        assert offer_monitor._item_in_offers("coca cola", ["COCA COLA 1L"]) is True

    def test_no_match(self):
        import offer_monitor
        assert offer_monitor._item_in_offers("Bier", ["Wein", "Saft"]) is False

    def test_empty_offers(self):
        import offer_monitor
        assert offer_monitor._item_in_offers("Cola", []) is False


# ---------------------------------------------------------------------------
# get_matching_offers — Cache-Hit
# ---------------------------------------------------------------------------

class TestGetMatchingOffersCache:
    @pytest.mark.asyncio
    async def test_returns_fresh_cache_without_fetching(self, tmp_path, monkeypatch):
        import offer_monitor
        cache_data = _make_fresh_cache({"Coca Cola": ["Rewe"]})
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        # fetch_offers_for_market sollte NICHT aufgerufen werden
        fetch_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(offer_monitor, "fetch_offers_for_market", fetch_mock)
        result = await offer_monitor.get_matching_offers(["Coca Cola"], "41466")
        assert result == {"Coca Cola": ["Rewe"]}
        fetch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self, tmp_path, monkeypatch):
        import offer_monitor
        cache_data = _make_fresh_cache({"Coca Cola": ["Rewe"]})
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        # Maerkte liefern keine Ergebnisse → leeres Dict
        fetch_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(offer_monitor, "fetch_offers_for_market", fetch_mock)
        result = await offer_monitor.get_matching_offers(
            ["Coca Cola"], "41466", force_refresh=True
        )
        assert result == {}
        assert fetch_mock.called


# ---------------------------------------------------------------------------
# get_matching_offers — Leere Eingaben
# ---------------------------------------------------------------------------

class TestGetMatchingOffersEmptyInputs:
    @pytest.mark.asyncio
    async def test_empty_watchlist_returns_empty(self):
        import offer_monitor
        result = await offer_monitor.get_matching_offers([], "41466")
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_plz_returns_empty(self):
        import offer_monitor
        result = await offer_monitor.get_matching_offers(["Coca Cola"], "")
        assert result == {}


# ---------------------------------------------------------------------------
# get_matching_offers — Alle Maerkte schlagen fehl
# ---------------------------------------------------------------------------

class TestGetMatchingOffersFallback:
    @pytest.mark.asyncio
    async def test_all_markets_fail_returns_empty(self, tmp_path, monkeypatch):
        import offer_monitor
        # Cache ist veraltet
        cache_data = _make_stale_cache({})
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        # Alle Maerkte liefern leere Listen
        fetch_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(offer_monitor, "fetch_offers_for_market", fetch_mock)
        result = await offer_monitor.get_matching_offers(["Coca Cola"], "41466")
        assert result == {}


# ---------------------------------------------------------------------------
# format_offers_block
# ---------------------------------------------------------------------------

class TestFormatOffersBlock:
    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_watchlist(self):
        import offer_monitor
        result = await offer_monitor.format_offers_block([], "41466")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_plz(self):
        import offer_monitor
        result = await offer_monitor.format_offers_block(["Coca Cola"], "")
        assert result == ""

    @pytest.mark.asyncio
    async def test_formats_matches_correctly(self, tmp_path, monkeypatch):
        import offer_monitor
        matches = {"Coca Cola": ["Rewe", "Lidl"], "Wasser": ["Edeka"]}
        cache_data = _make_fresh_cache(matches)
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        result = await offer_monitor.format_offers_block(["Coca Cola", "Wasser"], "41466")
        assert "Diese Woche im Angebot" in result
        assert "Coca Cola" in result
        assert "Rewe" in result
        assert "Wasser" in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self, tmp_path, monkeypatch):
        import offer_monitor
        cache_data = _make_fresh_cache({})
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        monkeypatch.setattr(offer_monitor, "_CACHE_PATH", cache_file)
        result = await offer_monitor.format_offers_block(["Bier", "Wein"], "41466")
        assert result == ""
