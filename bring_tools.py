"""
Bring!-Integration fuer JARVIS (Issue #123).

Kapselt die gesamte Kommunikation mit der Bring! REST API:
  - Login + Token-Caching (Refresh bei 401 oder Ablauf nach 1 Stunde)
  - Listen abrufen
  - Artikel hinzufuegen
  - Angebotsabgleich gegen S.WEEKLY_OFFERS

Alle Funktionen degradieren gracefully wenn BRING_EMAIL / BRING_PASSWORD
nicht konfiguriert sind — kein Crash, nur leere Rueckgabewerte und Logs.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

import httpx

import settings as S

log = S.log

# ---------------------------------------------------------------------------
# Bring! API-Endpunkte
# ---------------------------------------------------------------------------
_API_BASE = "https://api.getbring.com/rest/v2"
_AUTH_URL = f"{_API_BASE}/bringauth"
_USER_LISTS_URL_TPL = f"{_API_BASE}/bringusers/{{uuid}}/lists"
_LIST_ITEMS_URL_TPL = f"{_API_BASE}/bringlists/{{list_uuid}}"
_API_TIMEOUT = 10
_BRING_HEADERS = {
    "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH",
    "X-BRING-CLIENT-SOURCE": "webApp",
    "X-BRING-COUNTRY": "DE",
}

# ---------------------------------------------------------------------------
# Modulebene Token-Cache
# ---------------------------------------------------------------------------
_bring_uuid: str = ""
_bring_token: str = ""
_bring_token_expiry: Optional[datetime.datetime] = None
_bring_default_list_uuid: str = ""


def _token_valid() -> bool:
    """True wenn Token gesetzt und noch nicht abgelaufen."""
    if not _bring_token or not _bring_token_expiry:
        return False
    return datetime.datetime.utcnow() < _bring_token_expiry


async def bring_login() -> tuple[str, str]:
    """Meldet sich bei der Bring! API an.

    Nutzt den gecachten Token falls noch gueltig, sonst neuer Login.
    Cached uuid + token + List-UUID fuer nachfolgende Calls.

    Returns:
        (uuid, access_token) — beide leer bei Fehler.
    """
    global _bring_uuid, _bring_token, _bring_token_expiry, _bring_default_list_uuid

    if not S.BRING_EMAIL or not S.BRING_PASSWORD:
        return "", ""

    if _token_valid() and _bring_uuid and _bring_default_list_uuid:
        return _bring_uuid, _bring_token

    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.post(
                _AUTH_URL,
                data={"email": S.BRING_EMAIL, "password": S.BRING_PASSWORD},
                headers=_BRING_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()

        _bring_uuid = data.get("uuid", "")
        _bring_token = data.get("access_token", "")
        # Tokens leben ~1 Stunde; wir refreshen 5 Minuten frueher
        _bring_token_expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=55)

        # Standard-Listen abrufen und erste Liste cachen
        if _bring_uuid and _bring_token:
            _bring_default_list_uuid = await _fetch_first_list_uuid(
                _bring_uuid, _bring_token
            )
            # In settings-Cache schreiben damit andere Module darauf zugreifen
            S.BRING_LIST_UUID_CACHE = _bring_default_list_uuid
            log.info(
                f"bring_login: OK — uuid={_bring_uuid!r}, "
                f"list_uuid={_bring_default_list_uuid!r}"
            )
        return _bring_uuid, _bring_token

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            log.warning("bring_login: ungueltige Zugangsdaten (401) — Bring! deaktiviert")
        else:
            log.warning(f"bring_login: HTTP {e.response.status_code}: {e}")
        _bring_uuid = _bring_token = _bring_default_list_uuid = ""
        return "", ""
    except Exception as e:
        log.warning(f"bring_login: {type(e).__name__}: {e}")
        _bring_uuid = _bring_token = _bring_default_list_uuid = ""
        return "", ""


async def _fetch_first_list_uuid(uuid: str, token: str) -> str:
    """Ruft die Listen des Nutzers ab und gibt die UUID der ersten zurueck."""
    try:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.get(
                _USER_LISTS_URL_TPL.format(uuid=uuid),
                headers={**_BRING_HEADERS, "Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        lists = data.get("lists", [])
        if lists:
            return lists[0].get("listUuid", "")
    except Exception as e:
        log.warning(f"_fetch_first_list_uuid: {type(e).__name__}: {e}")
    return ""


async def _ensure_logged_in() -> bool:
    """Sicherstellt dass ein gueltiges Login existiert.

    Returns:
        True wenn eingeloggt und bereit, False bei Konfigurationsfehler.
    """
    if not S.BRING_EMAIL or not S.BRING_PASSWORD:
        return False
    uuid, token = await bring_login()
    return bool(uuid and token)


async def bring_get_items(list_uuid: str | None = None) -> list[str]:
    """Gibt die aktuellen Einkaufsartikel der Bring!-Liste zurueck.

    Args:
        list_uuid: Optionale Listen-UUID. Wenn None, wird die
            gecachte Standard-Liste verwendet.

    Returns:
        Liste von Artikel-Namen (purchase-Bucket), leer bei Fehler oder
        wenn keine Eintraege vorhanden sind.
    """
    if not await _ensure_logged_in():
        return []

    target_list = list_uuid or _bring_default_list_uuid or S.BRING_LIST_UUID
    if not target_list:
        log.warning("bring_get_items: keine Listen-UUID verfuegbar")
        return []

    async def _do_fetch(tok: str) -> list[str]:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.get(
                _LIST_ITEMS_URL_TPL.format(list_uuid=target_list),
                headers={"Authorization": f"Bearer {tok}"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [item.get("name", "") for item in data.get("purchase", []) if item.get("name")]

    try:
        items = await _do_fetch(_bring_token)
        log.info(f"bring_get_items: {len(items)} Artikel")
        return items
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            # Token abgelaufen — neu einloggen und einmal retry
            global _bring_token_expiry
            _bring_token_expiry = None
            if await _ensure_logged_in():
                try:
                    return await _do_fetch(_bring_token)
                except Exception as retry_e:
                    log.warning(f"bring_get_items retry: {type(retry_e).__name__}: {retry_e}")
        else:
            log.warning(f"bring_get_items: HTTP {e.response.status_code}: {e}")
    except Exception as e:
        log.warning(f"bring_get_items: {type(e).__name__}: {e}")
    return []


async def bring_add_item(item: str, list_uuid: str | None = None) -> bool:
    """Fuegt einen Artikel zur Bring!-Einkaufsliste hinzu.

    Args:
        item: Artikel-Name.
        list_uuid: Optionale Listen-UUID.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    if not await _ensure_logged_in():
        return False

    target_list = list_uuid or _bring_default_list_uuid or S.BRING_LIST_UUID
    if not target_list or not item.strip():
        return False

    async def _do_add(tok: str) -> bool:
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.put(
                _LIST_ITEMS_URL_TPL.format(list_uuid=target_list),
                data={"purchase": item.strip(), "recently": ""},
                headers={"Authorization": f"Bearer {tok}"},
            )
            resp.raise_for_status()
        return True

    try:
        result = await _do_add(_bring_token)
        log.info(f"bring_add_item: '{item}' hinzugefuegt")
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            global _bring_token_expiry
            _bring_token_expiry = None
            if await _ensure_logged_in():
                try:
                    return await _do_add(_bring_token)
                except Exception as retry_e:
                    log.warning(f"bring_add_item retry: {type(retry_e).__name__}: {retry_e}")
        else:
            log.warning(f"bring_add_item: HTTP {e.response.status_code}: {e}")
    except Exception as e:
        log.warning(f"bring_add_item: {type(e).__name__}: {e}")
    return False


async def bring_add_items(items: list[str], list_uuid: str | None = None) -> int:
    """Fuegt mehrere Artikel zur Bring!-Liste hinzu.

    Args:
        items: Liste von Artikel-Namen.
        list_uuid: Optionale Listen-UUID.

    Returns:
        Anzahl der erfolgreich hinzugefuegten Artikel.
    """
    count = 0
    for item in items:
        if item.strip():
            if await bring_add_item(item, list_uuid):
                count += 1
    return count


async def bring_check_offers(items: list[str]) -> str:
    """Prueft eine Liste von Artikel-Namen gegen die aktuellen Wochenangebote.

    Fuehrt einfaches Substring-Matching gegen S.WEEKLY_OFFERS durch.
    S.WEEKLY_OFFERS ist der von offer_monitor.format_offers_block()
    gelieferte Formatierungsstring.

    Args:
        items: Liste von Artikel-Namen aus der Bring!-Liste.

    Returns:
        Formatierter String wie "Milch: Angebot diese Woche, Joghurt: Angebot"
        oder leerer String wenn keine Treffer oder WEEKLY_OFFERS leer.
    """
    if not S.WEEKLY_OFFERS or not items:
        return ""

    offers_lower = S.WEEKLY_OFFERS.lower()
    hits: list[str] = []

    for item in items:
        if not item.strip():
            continue
        # Substring-Match: Artikelname muss im Angebots-Block vorkommen
        if item.strip().lower() in offers_lower:
            hits.append(item.strip())

    if not hits:
        return ""

    return "Angebote diese Woche: " + ", ".join(hits)
