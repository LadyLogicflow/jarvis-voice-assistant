"""
weather_tools.py — Echtzeit-Wetterdaten für Neuss via Open-Meteo.

Kein API-Key erforderlich. Ergebnis wird 30 Minuten gecacht.
Async-Funktion `get_weather_neuss()` gibt einen deutschen Kurztext zurück,
z.B. "18 Grad, teilweise bewölkt". Bei Fehler wird "" zurückgegeben.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL = 1800  # 30 Minuten

_WMO_CODES: dict[int, str] = {
    0: "Sonnenschein",
    1: "überwiegend klar",
    2: "teilweise bewölkt",
    3: "bewölkt",
    45: "neblig",
    48: "gefrierender Nebel",
    51: "leichter Nieselregen",
    53: "Nieselregen",
    55: "starker Nieselregen",
    61: "leichter Regen",
    63: "Regen",
    65: "starker Regen",
    71: "leichter Schneefall",
    73: "Schneefall",
    75: "starker Schneefall",
    80: "Regenschauer",
    81: "Schauer",
    82: "starke Schauer",
    95: "Gewitter",
    96: "Gewitter mit Hagel",
    99: "starkes Gewitter mit Hagel",
}


async def get_weather_neuss() -> str:
    """Aktuelle Wetterlage für Neuss (Open-Meteo, kostenlos, kein API-Key).

    Returns:
        Formatierter Kurztext, z.B. "18 Grad, teilweise bewölkt".
        Leerer String bei Fehler oder Timeout.
    """
    now = time.monotonic()
    if _CACHE.get("ts", 0) + _CACHE_TTL > now:
        return _CACHE.get("text", "")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 51.2036,
                    "longitude": 6.6879,
                    "current": "temperature_2m,weathercode",
                    "timezone": "Europe/Berlin",
                },
            )
            r.raise_for_status()
            cur = r.json()["current"]
            temp = round(cur["temperature_2m"])
            desc = _WMO_CODES.get(int(cur["weathercode"]), "wechselhaft")
            text = f"{temp} Grad, {desc}"
            _CACHE["ts"] = now
            _CACHE["text"] = text
            return text
    except Exception as e:
        log.warning("weather_tools.get_weather_neuss: %s", e)
        return ""
