"""
Supermarkt-Angebots-Monitor fuer JARVIS.
Prueft woechentlich Angebote bei konfigurierten Maerkten fuer eine gegebene PLZ.

Nutzt httpx (bereits im Projekt) fuer einfache HTTP-Requests.
Kein Playwright — zu schwergewichtig fuer woechentliche Hintergrundfetches.
Jeder Market-Fetch ist individuell abgesichert; bei Fehler wird er still uebersprungen.
Ein JSON-Cache verhindert dass die Shops bei jedem Morgen-Briefing bombardiert werden.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Optional

import httpx

import settings as S

log = S.log

_CACHE_PATH = os.path.expanduser("~/.jarvis_offers_cache.json")
_CACHE_MAX_AGE_HOURS = 6

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[dict]:
    """Laedt den Cache aus ~/.jarvis_offers_cache.json.

    Returns:
        Cache-Dict mit 'timestamp' und 'offers'-Keys, oder None wenn
        der Cache nicht existiert oder beschaedigt ist.
    """
    try:
        if not os.path.exists(_CACHE_PATH):
            return None
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "timestamp" not in data or "offers" not in data:
            return None
        return data
    except Exception as e:
        log.warning(f"offer_monitor: Cache-Ladefehler: {type(e).__name__}: {e}")
        return None


def _save_cache(offers: dict) -> None:
    """Speichert Angebote und aktuellen Timestamp in den Cache.

    Args:
        offers: Dict {item: [market1, market2, ...]} der Treffer.
    """
    try:
        data = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "offers": offers,
        }
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"offer_monitor: Cache-Schreibfehler: {type(e).__name__}: {e}")


def _is_cache_fresh(cache: dict) -> bool:
    """Gibt True zurueck wenn der Cache juenger als _CACHE_MAX_AGE_HOURS ist.

    Args:
        cache: Cache-Dict mit 'timestamp'-Key.

    Returns:
        True wenn der Cache noch gueltig ist.
    """
    try:
        ts = datetime.datetime.fromisoformat(cache["timestamp"])
        age = datetime.datetime.utcnow() - ts
        return age.total_seconds() < _CACHE_MAX_AGE_HOURS * 3600
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Market fetchers
# ---------------------------------------------------------------------------

async def _fetch_rewe(plz: str, client: httpx.AsyncClient) -> list[str]:
    """Ruft Rewe-Angebote ab.

    Nutzt die Rewe-Angebots-Seite und extrahiert Produktbezeichnungen
    aus dem HTML. Gibt eine Liste von Angebotstexten zurueck.

    Args:
        plz: Postleitzahl fuer die Marktsuche.
        client: Bestehender httpx-AsyncClient.

    Returns:
        Liste von Angebotstexten (Produktnamen) bei Rewe.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9",
    }
    url = "https://www.rewe.de/angebote/"
    resp = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    # Produkt-Bezeichnungen im Rewe-HTML stehen meist in <p>-Tags mit
    # spezifischen Klassen-Attributen. Wir extrahieren alle sichtbaren
    # Texte, die kurz genug sind um Produktnamen zu sein.
    items: list[str] = []
    # Muster fuer Produktbezeichnungen in <p>- und <h3>-Tags
    for tag in ("p", "h3", "h4", "span"):
        for match in re.finditer(
            rf"<{tag}[^>]*>([^<]{{3,80}})</{tag}>",
            html,
            re.IGNORECASE,
        ):
            text = match.group(1).strip()
            # Nur alphanumerischen Inhalt mit Laenge 3-80 Zeichen
            if text and 3 <= len(text) <= 80 and not text.startswith("<"):
                items.append(text)
    # Deduplizieren
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    log.info(f"offer_monitor: Rewe {len(result)} Eintraege gefunden")
    return result[:200]  # Obergrenze um Speicher zu schonen


async def _fetch_lidl(plz: str, client: httpx.AsyncClient) -> list[str]:
    """Ruft Lidl-Angebote ab.

    Args:
        plz: Postleitzahl (wird fuer Lidl nicht benoetigt, aber Signatur konsistent).
        client: Bestehender httpx-AsyncClient.

    Returns:
        Liste von Angebotstexten bei Lidl.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9",
    }
    url = "https://www.lidl.de/de/angebote"
    resp = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    items: list[str] = []
    for tag in ("h3", "h4", "p", "span"):
        for match in re.finditer(
            rf"<{tag}[^>]*>([^<]{{3,80}})</{tag}>",
            html,
            re.IGNORECASE,
        ):
            text = match.group(1).strip()
            if text and 3 <= len(text) <= 80:
                items.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    log.info(f"offer_monitor: Lidl {len(result)} Eintraege gefunden")
    return result[:200]


async def _fetch_aldi_sued(plz: str, client: httpx.AsyncClient) -> list[str]:
    """Ruft Aldi-Sued-Angebote ab.

    Args:
        plz: Postleitzahl (wird nicht direkt benutzt).
        client: Bestehender httpx-AsyncClient.

    Returns:
        Liste von Angebotstexten bei Aldi Sued.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9",
    }
    url = "https://www.aldi-sued.de/de/angebote.html"
    resp = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    items: list[str] = []
    for tag in ("h3", "h4", "p", "span"):
        for match in re.finditer(
            rf"<{tag}[^>]*>([^<]{{3,80}})</{tag}>",
            html,
            re.IGNORECASE,
        ):
            text = match.group(1).strip()
            if text and 3 <= len(text) <= 80:
                items.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    log.info(f"offer_monitor: Aldi Sued {len(result)} Eintraege gefunden")
    return result[:200]


async def _fetch_edeka(plz: str, client: httpx.AsyncClient) -> list[str]:
    """Ruft Edeka-Angebote ab.

    Args:
        plz: Postleitzahl (wird nicht direkt benutzt).
        client: Bestehender httpx-AsyncClient.

    Returns:
        Liste von Angebotstexten bei Edeka.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9",
    }
    url = "https://www.edeka.de/angebote/"
    resp = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    items: list[str] = []
    for tag in ("h3", "h4", "p", "span"):
        for match in re.finditer(
            rf"<{tag}[^>]*>([^<]{{3,80}})</{tag}>",
            html,
            re.IGNORECASE,
        ):
            text = match.group(1).strip()
            if text and 3 <= len(text) <= 80:
                items.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    log.info(f"offer_monitor: Edeka {len(result)} Eintraege gefunden")
    return result[:200]


async def _fetch_trinkgut(plz: str, client: httpx.AsyncClient) -> list[str]:
    """Ruft Trinkgut-Angebote ab.

    Args:
        plz: Postleitzahl (wird nicht direkt benutzt).
        client: Bestehender httpx-AsyncClient.

    Returns:
        Liste von Angebotstexten bei Trinkgut.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9",
    }
    url = "https://www.trinkgut.de/angebote"
    resp = await client.get(url, headers=headers, timeout=10, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    items: list[str] = []
    for tag in ("h3", "h4", "p", "span"):
        for match in re.finditer(
            rf"<{tag}[^>]*>([^<]{{3,80}})</{tag}>",
            html,
            re.IGNORECASE,
        ):
            text = match.group(1).strip()
            if text and 3 <= len(text) <= 80:
                items.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    log.info(f"offer_monitor: Trinkgut {len(result)} Eintraege gefunden")
    return result[:200]


# ---------------------------------------------------------------------------
# Market-Dispatcher
# ---------------------------------------------------------------------------

_MARKET_FETCHERS = {
    "Rewe": _fetch_rewe,
    "Lidl": _fetch_lidl,
    "Aldi": _fetch_aldi_sued,
    "Edeka": _fetch_edeka,
    "Trinkgut": _fetch_trinkgut,
}


async def fetch_offers_for_market(market: str, plz: str) -> list[str]:
    """Ruft Angebote fuer einen einzelnen Markt ab.

    Jeder Fehler wird still protokolliert — der Aufrufer entscheidet
    ob ein leeres Ergebnis akzeptabel ist.

    Args:
        market: Marktname ('Rewe', 'Lidl', 'Aldi', 'Edeka', 'Trinkgut').
        plz: Postleitzahl fuer lokale Marktsuche.

    Returns:
        Liste von Angebotstexten fuer diesen Markt. Leer bei Fehler.
    """
    fetcher = _MARKET_FETCHERS.get(market)
    if not fetcher:
        log.warning(f"offer_monitor: unbekannter Markt '{market}'")
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return await fetcher(plz, client)
    except Exception as e:
        log.warning(
            f"offer_monitor: {market} fetch fehlgeschlagen "
            f"({type(e).__name__}: {e}) — uebersprungen"
        )
        return []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _item_in_offers(item: str, offers: list[str]) -> bool:
    """Prueft ob ein Watchlist-Item in der Angebotsliste vorkommt.

    Einfacher Case-insensitive Teilstring-Vergleich.

    Args:
        item: Suchbegriff aus der Watchlist.
        offers: Liste von Angebots-Strings des Markts.

    Returns:
        True wenn item (case-insensitiv) in mindestens einem Angebot vorkommt.
    """
    item_lower = item.lower()
    return any(item_lower in offer.lower() for offer in offers)


async def get_matching_offers(
    watchlist: list[str],
    plz: str,
    force_refresh: bool = False,
) -> dict[str, list[str]]:
    """Gibt Watchlist-Treffer je Markt zurueck, nutzt Cache wenn moeglich.

    Wenn der Cache juenger als _CACHE_MAX_AGE_HOURS ist, wird er direkt
    zurueckgegeben. Andernfalls werden alle Maerkte frisch abgerufen.

    Bei Fehler aller Maerkte wird ein leeres Dict zurueckgegeben —
    das System degradiert graceful ohne Exception.

    Args:
        watchlist: Liste von Suchbegriffen, z.B. ["Coca Cola", "Wasser"].
        plz: Postleitzahl fuer Marktsuche.
        force_refresh: Ignoriert den Cache und laed neu.

    Returns:
        Dict {item: [markt1, markt2, ...]} fuer alle gefundenen Treffer.
        Nur Items mit mindestens einem Treffer sind enthalten.
    """
    if not watchlist or not plz:
        return {}

    # Cache pruefen
    if not force_refresh:
        cache = _load_cache()
        if cache and _is_cache_fresh(cache):
            log.info("offer_monitor: Cache ist frisch — kein neuer Fetch")
            return cache.get("offers", {})

    # Alle Maerkte parallel abrufen
    import asyncio
    market_names = list(_MARKET_FETCHERS.keys())
    results = await asyncio.gather(
        *[fetch_offers_for_market(m, plz) for m in market_names],
        return_exceptions=True,
    )

    # Ergebnisse auswerten
    market_offers: dict[str, list[str]] = {}
    all_empty = True
    for market, result in zip(market_names, results):
        if isinstance(result, Exception):
            log.warning(f"offer_monitor: {market} Exception: {result}")
            market_offers[market] = []
        elif isinstance(result, list) and result:
            market_offers[market] = result
            all_empty = False
        else:
            market_offers[market] = []

    if all_empty:
        log.warning("offer_monitor: Alle Maerkte haben leere Ergebnisse geliefert")
        _save_cache({})
        return {}

    # Watchlist gegen Angebote matchen
    matches: dict[str, list[str]] = {}
    for item in watchlist:
        found_in: list[str] = []
        for market, offers in market_offers.items():
            if _item_in_offers(item, offers):
                found_in.append(market)
        if found_in:
            matches[item] = found_in

    log.info(f"offer_monitor: {len(matches)} Treffer fuer Watchlist {watchlist}")
    _save_cache(matches)
    return matches


async def format_offers_block(watchlist: list[str], plz: str) -> str:
    """Formatiert Angebots-Treffer als lesbaren Text fuer das Briefing.

    Beispiel-Output:
        Diese Woche im Angebot:
        Coca Cola: Rewe, Lidl
        Wasser: Edeka

    Wenn keine Treffer: leerer String zurueck.

    Args:
        watchlist: Liste von Suchbegriffen.
        plz: Postleitzahl fuer Marktsuche.

    Returns:
        Formatierter Text oder leerer String wenn keine Treffer.
    """
    if not watchlist or not plz:
        return ""
    matches = await get_matching_offers(watchlist, plz)
    if not matches:
        return ""
    lines = ["Diese Woche im Angebot:"]
    for item, markets in matches.items():
        lines.append(f"{item}: {', '.join(markets)}")
    return "\n".join(lines)
