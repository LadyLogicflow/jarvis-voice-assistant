"""
Picnic-Integration fuer JARVIS.

Kapselt Login, Produktsuche und Warenkorb-Verwaltung via python-picnic-api.
Die Library ist synchron — alle Aufrufe laufen in asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import settings as S

log = S.log

_COUNTRY = "DE"
_client: object | None = None  # PicnicAPI instance, lazy-init


def _get_client():
    """Gibt gecachten PicnicAPI-Client zurueck oder erstellt einen neuen."""
    global _client
    if _client is not None:
        return _client
    from python_picnic_api import PicnicAPI
    _client = PicnicAPI(
        username=S.PICNIC_EMAIL,
        password=S.PICNIC_PASSWORD,
        country_code=_COUNTRY,
    )
    return _client


def _flatten_search_results(results: list) -> list[dict]:
    """Extrahiert Produkte aus der verschachtelten Picnic-Suchantwort."""
    products: list[dict] = []
    for section in results:
        items = section.get("items", [])
        for item in items:
            if item.get("type") in ("SINGLE_ARTICLE", "PRODUCT", "ITEM"):
                products.append(item)
            # Manche Ergebnisse haben weitere verschachtelte items
            for sub in item.get("items", []):
                if sub.get("type") in ("SINGLE_ARTICLE", "PRODUCT", "ITEM"):
                    products.append(sub)
    return products


def _search_best_match(term: str) -> Optional[tuple[str, str, int]]:
    """Sucht synchron nach einem Artikel, gibt (id, name, preis_cents) oder None zurueck."""
    try:
        client = _get_client()
        results = client.search(term)
        if not results:
            return None
        products = _flatten_search_results(results)
        if not products:
            return None
        best = products[0]
        return best["id"], best.get("name", term), best.get("price", 0)
    except Exception as e:
        log.warning(f"picnic search '{term}': {type(e).__name__}: {e}")
        return None


def _add_to_cart_sync(product_id: str) -> bool:
    """Legt einen Artikel synchron in den Warenkorb."""
    try:
        _get_client().add_product(product_id, count=1)
        return True
    except Exception as e:
        log.warning(f"picnic add_product {product_id}: {type(e).__name__}: {e}")
        return False


def _get_cart_sync() -> list[dict]:
    """Gibt den aktuellen Warenkorb synchron zurueck."""
    try:
        cart = _get_client().get_cart()
        return cart.get("items", []) if isinstance(cart, dict) else []
    except Exception as e:
        log.warning(f"picnic get_cart: {type(e).__name__}: {e}")
        return []


async def picnic_add_items(
    items: list[str],
) -> tuple[list[str], list[str]]:
    """
    Sucht jeden Artikel auf Picnic und legt ihn in den Warenkorb.

    Gibt (gefunden, nicht_gefunden) zurueck.
    """
    added: list[str] = []
    not_found: list[str] = []

    for item in items:
        match = await asyncio.to_thread(_search_best_match, item)
        if match is None:
            not_found.append(item)
            continue
        product_id, product_name, _ = match
        ok = await asyncio.to_thread(_add_to_cart_sync, product_id)
        if ok:
            added.append(product_name)
            log.info(f"Picnic: '{item}' → '{product_name}' in Warenkorb")
        else:
            not_found.append(item)

    return added, not_found


async def picnic_get_cart_summary() -> str:
    """Gibt den aktuellen Warenkorb als lesbaren Text zurueck."""
    items = await asyncio.to_thread(_get_cart_sync)
    if not items:
        return "Der Picnic-Warenkorb ist leer."
    names = [i.get("name", "?") for i in items]
    return f"Im Picnic-Warenkorb: {', '.join(names)}."
