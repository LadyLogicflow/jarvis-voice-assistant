"""
Speiseplanung fuer JARVIS (Issue #125).

Generiert wöchentliche Menuepläne inklusive vollstaendiger Rezepte via
Claude. Plan laeuft Samstag bis Freitag der Folgewoche.

Constraints (fest):
- Ausgewogen, kalorienarm, geeignet fuer Typ-2-Diabetiker (kein Zucker,
  wenig einfache Kohlenhydrate)
- Kochzeit maximal 1 Stunde
- Montag und Donnerstag: 3 Personen (Abendessen zu dritt)
- Ausnahme: wenn Catrin laut Google Calendar einen Abendtermin hat -> 3 Pers.
- Sonst: S.MEAL_PLAN_SERVINGS_DEFAULT (Standard: 4)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Any

import settings as S

log = logging.getLogger("jarvis.meal_plan")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _next_saturday() -> datetime.date:
    """Gibt das Datum des naechsten Samstags zurueck (immer in der Zukunft,
    auch wenn heute bereits Samstag ist)."""
    today = datetime.date.today()
    days_ahead = (5 - today.weekday()) % 7  # Samstag = weekday 5
    if days_ahead == 0:
        days_ahead = 7  # Naechste Woche wenn heute schon Samstag
    return today + datetime.timedelta(days=days_ahead)


def _week_dates() -> list[datetime.date]:
    """Liefert die 7 Tagesdaten Samstag bis Freitag der naechsten Woche."""
    saturday = _next_saturday()
    return [saturday + datetime.timedelta(days=i) for i in range(7)]


async def get_servings_for_date(date: datetime.date) -> int:
    """Personenanzahl fuer ein bestimmtes Datum bestimmen.

    Logik:
    - Montag (weekday=0) und Donnerstag (weekday=3): immer 3 Personen
    - Google Calendar: wenn Catrin einen Abendtermin hat -> 3 Personen
    - Sonst: S.MEAL_PLAN_SERVINGS_DEFAULT (Standard: 4)

    Args:
        date: Das Datum fuer das die Personenanzahl ermittelt werden soll.

    Returns:
        Anzahl der Personen als int.
    """
    # Montag und Donnerstag sind immer 3 Personen (regelgemaess)
    if date.weekday() in (0, 3):  # 0=Montag, 3=Donnerstag
        return 3

    # Google Calendar: Abendtermine pruefen
    try:
        import google_calendar_tools
        events_text = await google_calendar_tools.get_events(days=14, max_results=50)
        if events_text and events_text != "KEINE_TERMINE":
            date_short = date.strftime("%d.%m.")
            for line in events_text.splitlines():
                if not line.startswith("•"):
                    continue
                if date_short not in line:
                    continue
                # Abendtermin: Uhrzeit >= 17:00 Uhr
                import re
                m = re.search(r"(\d{2}):(\d{2})", line)
                if m:
                    event_hour = int(m.group(1))
                    if event_hour >= 17:
                        log.info(
                            f"get_servings_for_date {date}: Abendtermin gefunden -> 3 Pers."
                        )
                        return 3
    except Exception as e:
        log.warning(
            f"get_servings_for_date: Kalender-Abruf fehlgeschlagen: "
            f"{type(e).__name__}: {e}"
        )

    return S.MEAL_PLAN_SERVINGS_DEFAULT


async def generate_meal_plan() -> dict:
    """Generiert einen 7-tägigen Speisenplan (Samstag bis Freitag) via Claude.

    Erstellt den Plan fuer die naechste Woche, berücksichtigt Personenzahl
    pro Tag via Google Calendar und speichert das Ergebnis in
    S.MEAL_PLAN_WEEK.

    Returns:
        dict mit Datums-String als Schluessel und Tages-Dict als Wert:
        {
            "2026-05-30": {
                "dish": "Lachs mit Spinat",
                "recipe": "...",
                "servings": 4,
                "ingredients": ["300g Lachs", "200g Spinat", ...]
            },
            ...
        }
    """
    dates = _week_dates()

    # Personenzahl pro Tag parallel abfragen
    servings_list = await asyncio.gather(
        *[get_servings_for_date(d) for d in dates]
    )
    days_info = [
        {
            "date": d.isoformat(),
            "weekday_de": _weekday_de(d.weekday()),
            "servings": s,
        }
        for d, s in zip(dates, servings_list)
    ]

    diabetes_hint = (
        "WICHTIG: Alle Gerichte muessen fuer Typ-2-Diabetiker geeignet sein: "
        "kein Zucker, wenig einfache Kohlenhydrate (kein Weissmehl, kein "
        "weisser Reis, kein normaler Pasta). Vollkornprodukte, Hülsenfrüchte "
        "und viel Gemüse bevorzugen. Keine sueßen Saucen oder Desserts."
        if S.MEAL_PLAN_DIABETES_MODE
        else ""
    )

    days_block = "\n".join(
        f"- {d['date']} ({d['weekday_de']}): {d['servings']} Personen"
        for d in days_info
    )

    system_prompt = (
        "Du bist Jarvis, der britisch-hoefliche KI-Butler. Du planst den "
        "woechentlichen Speisenplan fuer Catrin.\n\n"
        f"{diabetes_hint}\n\n"
        "Weitere Anforderungen:\n"
        "- Maximale Kochzeit: 60 Minuten pro Gericht\n"
        "- Abwechslungsreich: kein Gericht zweimal in einer Woche\n"
        "- Mischung aus Fleisch (1-2x), Fisch (1-2x), vegetarisch (Rest)\n"
        "- Saisonale Zutaten bevorzugen\n"
        "- Einfache, alltagstaugliche Gerichte\n\n"
        "Antworte AUSSCHLIESSLICH mit einem gueltigen JSON-Objekt. "
        "Kein Text davor oder dahinter. Format:\n"
        "{\n"
        '  "DATUM": {\n'
        '    "dish": "Gerichtsname",\n'
        '    "recipe": "Schritt-fuer-Schritt Rezept als ein langer Text",\n'
        '    "servings": <Personenanzahl>,\n'
        '    "ingredients": ["Zutat 1 mit Menge", "Zutat 2 mit Menge", ...],\n'
        '    "cook_time_minutes": <Ganzzahl>\n'
        "  },\n"
        "  ...\n"
        "}\n"
        "DATUM ist immer im Format YYYY-MM-DD."
    )

    user_msg = (
        f"Erstelle einen Speisenplan fuer die folgende Woche:\n\n{days_block}\n\n"
        f"Bitte generiere fuer jeden Tag ein vollstaendiges Abendessen mit "
        f"Rezept und Zutaten (angepasst an die jeweilige Personenanzahl)."
    )

    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip() if resp and resp.content else ""
    except Exception as e:
        log.warning(f"generate_meal_plan: Claude-Aufruf fehlgeschlagen: "
                    f"{type(e).__name__}: {e}")
        return {}

    # JSON-Block aus der Antwort extrahieren
    plan_data: dict[str, Any] = {}
    try:
        # Robuste Extraktion: falls Claude doch Markdown-Fences mitliefert
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            )
        plan_data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(
            f"generate_meal_plan: JSON-Parse fehlgeschlagen: "
            f"{type(e).__name__}: {e}\nRaw: {raw[:300]}"
        )
        return {}

    # Plan validieren + Servings aus unserer Berechnung eintragen
    # (Claude koennte andere Werte nehmen)
    servings_by_date = {d["date"]: d["servings"] for d in days_info}
    result: dict[str, Any] = {}
    for date_str, entry in plan_data.items():
        if not isinstance(entry, dict):
            continue
        result[date_str] = {
            "dish": str(entry.get("dish", "")),
            "recipe": str(entry.get("recipe", "")),
            "servings": servings_by_date.get(date_str, S.MEAL_PLAN_SERVINGS_DEFAULT),
            "ingredients": [str(i) for i in entry.get("ingredients", [])],
            "cook_time_minutes": int(entry.get("cook_time_minutes", 45)),
        }

    S.MEAL_PLAN_WEEK = result
    log.info(
        f"generate_meal_plan: Plan fuer {len(result)} Tage generiert "
        f"({list(result.keys())[0] if result else 'leer'} .. "
        f"{list(result.keys())[-1] if result else 'leer'})"
    )
    return result


async def get_today_recipe() -> str:
    """Holt das formatierte Rezept fuer das heutige Gericht.

    Returns:
        Formatierten Text mit Gericht + Rezept + Zutaten, oder leeren
        String wenn kein Plan vorhanden ist.
    """
    today_str = datetime.date.today().isoformat()
    entry = S.MEAL_PLAN_WEEK.get(today_str)
    if not entry:
        return ""

    dish = entry.get("dish", "")
    recipe = entry.get("recipe", "")
    servings = entry.get("servings", S.MEAL_PLAN_SERVINGS_DEFAULT)
    ingredients = entry.get("ingredients", [])
    cook_time = entry.get("cook_time_minutes", 45)

    parts = [
        f"Heutiges Abendessen ({_weekday_de(datetime.date.today().weekday())}): "
        f"{dish} fuer {servings} Personen.",
        f"Kochzeit: ca. {cook_time} Minuten.",
    ]

    if ingredients:
        parts.append("Zutaten:\n" + "\n".join(f"- {i}" for i in ingredients))

    if recipe:
        parts.append(f"Zubereitung:\n{recipe}")

    return "\n\n".join(parts)


async def get_ingredients_for_week() -> list[str]:
    """Aggregiert alle Zutaten der Woche aus S.MEAL_PLAN_WEEK.

    Gibt eine bereinigte, deduplizierte Liste zurueck die direkt an
    bring_tools.bring_add_items() uebergeben werden kann.

    Returns:
        Sortierte Liste aller Zutaten (ohne Duplikate).
    """
    all_ingredients: list[str] = []
    seen: set[str] = set()

    for entry in S.MEAL_PLAN_WEEK.values():
        for ing in entry.get("ingredients", []):
            cleaned = ing.strip()
            if not cleaned:
                continue
            # Deduplizierung auf normalisierten Schluessel (lowercase, ohne Mengenangabe)
            key = _normalize_ingredient(cleaned)
            if key not in seen:
                seen.add(key)
                all_ingredients.append(cleaned)

    return sorted(all_ingredients)


def _normalize_ingredient(ingredient: str) -> str:
    """Normalisiert eine Zutat fuer Deduplizierung (Mengen entfernen)."""
    import re
    # Mengenangaben entfernen: "300g", "2 EL", "1/2 Tasse" etc.
    normalized = re.sub(
        r"^\d+[\d,.]*\s*(g|kg|ml|l|EL|TL|Tasse|Stk|Stück|Prise|Bund|Scheibe[n]?)\s*",
        "",
        ingredient,
        flags=re.IGNORECASE,
    )
    return normalized.strip().lower()


def format_meal_plan_telegram() -> str:
    """Formatiert den aktuellen Wochenplan fuer den Telegram-Versand.

    Returns:
        Formatierten Text fuer Telegram mit Plan fuer alle 7 Tage,
        oder Fehlermeldung wenn kein Plan vorhanden.
    """
    if not S.MEAL_PLAN_WEEK:
        return "Kein Speisenplan verfuegbar."

    lines = ["Wochenplan Abendessen:\n"]
    for date_str in sorted(S.MEAL_PLAN_WEEK.keys()):
        entry = S.MEAL_PLAN_WEEK[date_str]
        try:
            d = datetime.date.fromisoformat(date_str)
            day_label = f"{_weekday_de(d.weekday())}, {d.strftime('%d.%m.')}"
        except ValueError:
            day_label = date_str
        dish = entry.get("dish", "")
        servings = entry.get("servings", S.MEAL_PLAN_SERVINGS_DEFAULT)
        cook_time = entry.get("cook_time_minutes", 45)
        lines.append(
            f"{day_label}: {dish} "
            f"({servings} Pers., ca. {cook_time} Min.)"
        )

    lines.append(
        "\nSende [ACTION:SPEISEPLAN_SWAP] Tag|Neues Gericht zum Tauschen "
        "oder [ACTION:EINKAUF_FREIGEBEN] zum Übertragen auf die Einkaufsliste."
    )
    return "\n".join(lines)


def _weekday_de(weekday: int) -> str:
    """Wochentag-Name auf Deutsch (0=Montag .. 6=Sonntag)."""
    names = [
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ]
    return names[weekday] if 0 <= weekday <= 6 else str(weekday)
