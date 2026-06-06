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
import os
import re
from typing import Any

import settings as S

log = logging.getLogger("jarvis.meal_plan")

MEAL_PLAN_CACHE_PATH = os.path.join(os.path.dirname(__file__), "meal_plan_cache.json")


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


def _week_dates(start_today: bool = False) -> list[datetime.date]:
    """Liefert die Tagesdaten fuer die Speiseplanung.

    start_today=True (on-demand): heute bis diesen Freitag (Mo–Fr).
    start_today=False (Scheduler): naechster Samstag bis Freitag (7 Tage).
    """
    today = datetime.date.today()
    if start_today and today.weekday() < 5:  # Mon–Fri
        days_to_friday = 4 - today.weekday()
        # On Friday days_to_friday == 0 → single-day plan (only today). Intentional.
        return [today + datetime.timedelta(days=i) for i in range(days_to_friday + 1)]
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


def _season_produce() -> str:
    """Saisonales Gemüse/Obst für den aktuellen Monat (Deutschland)."""
    month = datetime.date.today().month
    produce = {
        1:  "Wurzelgemüse (Karotten, Pastinaken), Grünkohl, Lauch, Äpfel",
        2:  "Feldsalat, Lauch, Rotkohl, Äpfel, Birnen",
        3:  "Spinat, Feldsalat, Lauch, frühe Radieschen",
        4:  "Spinat, Rucola, Radieschen, frühe Erdbeeren",
        5:  "Spargel, Erdbeeren, Radieschen, Spinat, Pak Choi, Rhabarber",
        6:  "Erbsen, Kohlrabi, Zucchini, Erdbeeren, Kirschen, junger Spinat",
        7:  "Tomaten, Gurken, Paprika, Zucchini, Himbeeren, Johannisbeeren",
        8:  "Tomaten, Auberginen, Mais, Paprika, Pflaumen, Melonen",
        9:  "Kürbis, Wirsing, Äpfel, Birnen, Weintrauben, Fenchel",
        10: "Kürbis, Rotkohl, Wirsing, Äpfel, Birnen, Rote Bete",
        11: "Grünkohl, Rotkohl, Rosenkohl, Kohlrabi, Äpfel",
        12: "Grünkohl, Rotkohl, Rosenkohl, Lauch, Äpfel",
    }
    return produce.get(month, "")


def _weather_hint() -> str:
    """Wetter-basierter Hinweis für die Speiseplanung."""
    if not S.WEATHER_INFO:
        return ""
    try:
        temp = int(S.WEATHER_INFO.get("temp", 0))
        desc = S.WEATHER_INFO.get("description", "").lower()
        warm_keywords = ("sunny", "clear", "sonnig", "heiter", "klar", "warm")
        is_warm_sunny = temp >= 22 and any(kw in desc for kw in warm_keywords)
        if is_warm_sunny:
            return (
                f"Das Wetter ist warm und sonnig (aktuell {temp}°C). "
                "Bevorzuge leichte Sommerkost: Grill-Gerichte, Salate, "
                "kalte Küche, frische Sommersalate. Weniger Schmorgerichte "
                "oder schwere Eintöpfe."
            )
        if temp <= 10:
            return (
                f"Das Wetter ist kühl (aktuell {temp}°C). "
                "Wärmende Gerichte sind willkommen: Suppen, Eintöpfe, "
                "Aufläufe, herzhafte Pfannengerichte."
            )
    except (ValueError, TypeError):
        pass
    return ""


def _offers_hint() -> str:
    """Angebots-Kontext aus S.WEEKLY_OFFERS für den Plan-Prompt."""
    if not S.WEEKLY_OFFERS:
        return ""
    return (
        "Diese Woche im Angebot (bitte diese Zutaten bevorzugt einplanen):\n"
        + S.WEEKLY_OFFERS
    )


def _preferred_market() -> str:
    """Ermittelt bevorzugten Markt (Lidl/Rewe) anhand der Angebotsanzahl."""
    if not S.WEEKLY_OFFERS:
        return ""
    text = S.WEEKLY_OFFERS.lower()
    lidl_count = text.count("lidl")
    rewe_count = text.count("rewe")
    if lidl_count > rewe_count:
        return f"Lidl (diese Woche {lidl_count} relevante Angebote)"
    if rewe_count > 0:
        return f"Rewe (diese Woche {rewe_count} relevante Angebote)"
    return ""


async def generate_meal_plan(start_today: bool = False, wishes: str = "",
                             explicit_dates: list | None = None) -> dict:
    """Generiert einen Speisenplan via Claude und persistiert ihn.

    start_today=True: von heute bis diesen Freitag (on-demand).
    start_today=False: naechster Samstag bis Freitag (Donnerstag-Scheduler).
    wishes: optionale Sonderwuensche der Nutzerin, fliessen in den Prompt ein.

    Returns:
        dict mit Datums-String als Schluessel und Tages-Dict als Wert.
    """
    dates = explicit_dates if explicit_dates is not None else _week_dates(start_today=start_today)

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

    import meal_prefs as _mprefs
    season = _season_produce()
    weather = _weather_hint()
    offers = _offers_hint()
    avoid_h = _mprefs.avoid_hint()
    fish_h = _mprefs.fish_hint()
    wishes_hint = (
        f"BESONDERE WUENSCHE DIESE WOCHE (bitte unbedingt beruecksichtigen):\n{wishes}"
        if wishes.strip() else ""
    )

    context_blocks = "\n\n".join(
        block for block in [diabetes_hint, avoid_h, fish_h, weather, offers, wishes_hint]
        if block
    )

    days_block = "\n".join(
        f"- {d['date']} ({d['weekday_de']}): {d['servings']} Personen"
        for d in days_info
    )

    system_prompt = (
        "Du bist Jarvis, der britisch-hoefliche KI-Butler. Du planst den "
        "woechentlichen Speisenplan fuer Catrin.\n\n"
        f"{context_blocks}\n\n"
        "Weitere Anforderungen:\n"
        "- Maximale Kochzeit: 60 Minuten pro Gericht\n"
        "- Abwechslungsreich: kein Gericht zweimal in einer Woche\n"
        "- Mischung aus Fleisch (1-2x), Fisch (1-2x), vegetarisch (Rest)\n"
        + (f"- Saisonales Gemüse/Obst bevorzugen, aktuell verfuegbar: {season}\n"
           if season else "- Saisonale Zutaten bevorzugen\n")
        + "- Einfache, alltagstaugliche Gerichte\n"
        "- Bevorzuge Thermomix-kompatible Gerichte; formuliere die Zubereitung "
        "mit Thermomix-Schritten (Temperatur in °C, Stufe, Minuten) wo sinnvoll\n"
        "- Lass dich von Rezepten auf HelloFresh.de inspirieren — frische, "
        "ausgewogene Alltagsküche in diesem Stil\n\n"
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
        f"Erstelle einen Speisenplan fuer {len(dates)} Tag(e):\n\n{days_block}\n\n"
        f"Bitte generiere fuer jeden Tag ein vollstaendiges Abendessen mit "
        f"Rezept und Zutaten (angepasst an die jeweilige Personenanzahl)."
    )

    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
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

    # Naehrwerte (kcal, Kohlenhydrate) pro Gericht via Haiku schaetzen
    for date_str, entry in result.items():
        dish = entry.get("dish", "")
        ingredients = entry.get("ingredients", [])
        if not dish:
            continue
        try:
            nut_resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
                max_tokens=100,
                system=(
                    "Schätze den Kaloriengehalt und Kohlenhydratgehalt pro Portion "
                    "fuer das genannte Gericht auf Basis der Zutaten. "
                    "Antworte NUR mit gueltigem JSON: "
                    '{"kcal_per_serving": <int>, "carbs_g_per_serving": <int>}'
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Gericht: {dish}\n"
                        f"Zutaten: {', '.join(ingredients[:10])}"
                    ),
                }],
            )
            nut_raw = nut_resp.content[0].text.strip() if nut_resp and nut_resp.content else "{}"
            if nut_raw.startswith("```"):
                nut_raw = "\n".join(
                    l for l in nut_raw.splitlines() if not l.strip().startswith("```")
                )
            nut_data = json.loads(nut_raw)
            entry["kcal_estimate"] = int(nut_data.get("kcal_per_serving", 0))
            entry["carbs_estimate"] = int(nut_data.get("carbs_g_per_serving", 0))
        except Exception as nut_exc:
            log.debug(
                "generate_meal_plan: Naehrwert-Schaetzung fehlgeschlagen fuer %r: %s",
                dish, nut_exc,
            )
            entry["kcal_estimate"] = 0
            entry["carbs_estimate"] = 0

    # Bring!-Sync: Wocheneinkauf automatisch uebertragen wenn konfiguriert
    if S.BRING_EMAIL and S.BRING_PASSWORD:
        try:
            import bring_tools
            all_ingredients: list[str] = []
            seen_norm: set[str] = set()
            for entry in result.values():
                for ing in entry.get("ingredients", []):
                    cleaned = ing.strip()
                    if not cleaned:
                        continue
                    key = _normalize_ingredient(cleaned)
                    if key not in seen_norm:
                        seen_norm.add(key)
                        all_ingredients.append(cleaned)
            if all_ingredients:
                count = await bring_tools.bring_add_items(all_ingredients)
                log.info(
                    "generate_meal_plan: %d Zutaten an Bring! uebertragen", count
                )
        except Exception as bring_exc:
            log.warning(
                "generate_meal_plan: Bring!-Sync fehlgeschlagen: %s: %s",
                type(bring_exc).__name__, bring_exc,
            )

    # Clear before update so stale entries from prior generations are not kept.
    S.MEAL_PLAN_WEEK.clear()
    S.MEAL_PLAN_WEEK.update(result)
    log.info(
        f"generate_meal_plan: Plan fuer {len(result)} Tage generiert "
        f"({list(result.keys())[0] if result else 'leer'} .. "
        f"{list(result.keys())[-1] if result else 'leer'})"
    )
    save_meal_plan()
    return result


def save_meal_plan() -> None:
    """Persistiert S.MEAL_PLAN_WEEK atomar als JSON-Datei (temp + os.replace).

    Schreibt zusaetzlich den aktuellen ISO-Wochen-String unter dem
    Schluessel ``"generated_week"`` in den Cache, damit nach einem
    Neustart erkannt werden kann, ob der Plan bereits dieser Woche
    gehoert (Issue #179).
    """
    import tempfile
    today = datetime.date.today()
    iso_year, iso_week, _ = today.isocalendar()
    generated_week = f"{iso_year}-W{iso_week:02d}"
    payload = dict(S.MEAL_PLAN_WEEK)
    payload["generated_week"] = generated_week
    try:
        dir_ = os.path.dirname(MEAL_PLAN_CACHE_PATH)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp"
        ) as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp_path = f.name
        os.replace(tmp_path, MEAL_PLAN_CACHE_PATH)
        log.info(
            f"save_meal_plan: {len(S.MEAL_PLAN_WEEK)} Eintraege gespeichert "
            f"(generated_week={generated_week})"
        )
    except Exception as e:
        log.warning(f"save_meal_plan: {type(e).__name__}: {e}")


def load_meal_plan() -> None:
    """Laedt den persistierten Speisenplan beim Serverstart.

    Der Schluessel ``"generated_week"`` wird aus dem Cache gelesen und
    in S.MEAL_PLAN_GENERATED_WEEK gespeichert, aber NICHT in
    S.MEAL_PLAN_WEEK eingetragen (Issue #179).
    """
    if not os.path.exists(MEAL_PLAN_CACHE_PATH):
        return
    try:
        with open(MEAL_PLAN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Metadaten-Schluessel herausloesen, bevor wir die Eintraege laden.
            generated_week = data.pop("generated_week", "")
            S.MEAL_PLAN_GENERATED_WEEK = generated_week
            # Clear first so stale in-memory state is fully replaced by the cache.
            S.MEAL_PLAN_WEEK.clear()
            S.MEAL_PLAN_WEEK.update(data)
            log.info(
                f"load_meal_plan: {len(data)} Eintraege geladen "
                f"(generated_week={generated_week!r})"
            )
    except Exception as e:
        log.warning(f"load_meal_plan: {type(e).__name__}: {e}")


def get_generated_week() -> str:
    """Gibt den ISO-Wochen-String des gespeicherten Speiseplans zurueck.

    Beispiel: ``"2026-W23"``. Leerer String wenn kein Plan gespeichert
    wurde oder der Cache kein ``generated_week``-Feld enthaelt.

    Dient als Dedup-Guard in ``meal_plan_scheduler`` (Issue #179).

    Returns:
        ISO-Wochen-String oder leerer String.
    """
    return getattr(S, "MEAL_PLAN_GENERATED_WEEK", "")


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


def format_meal_plan_tts() -> str:
    """Kurze TTS-freundliche Übersicht: 'Montag: Gericht, Dienstag: Gericht, ...'"""
    if not S.MEAL_PLAN_WEEK:
        return "Es gibt noch keinen Speisenplan."
    parts = []
    for date_str in sorted(S.MEAL_PLAN_WEEK.keys()):
        entry = S.MEAL_PLAN_WEEK[date_str]
        try:
            d = datetime.date.fromisoformat(date_str)
            day = _weekday_de(d.weekday())
        except ValueError:
            day = date_str
        parts.append(f"{day}: {entry.get('dish', '')}")
    return ", ".join(parts) + "."


def build_meal_plan_card_html() -> str:
    """Erstellt eine HTML-Kachel mit dem aktuellen Speiseplan für das Web-Frontend."""
    if not S.MEAL_PLAN_WEEK:
        return ""
    dates = sorted(S.MEAL_PLAN_WEEK.keys())
    first = datetime.date.fromisoformat(dates[0])
    last = datetime.date.fromisoformat(dates[-1])
    kw = first.isocalendar()[1]
    today_str = datetime.date.today().isoformat()

    header = f"Speiseplan KW {kw} &nbsp;·&nbsp; {first.strftime('%d.%m.')}–{last.strftime('%d.%m.%Y')}"
    rows_html = ""
    for date_str in dates:
        e = S.MEAL_PLAN_WEEK[date_str]
        try:
            d = datetime.date.fromisoformat(date_str)
            day_label = f"{_weekday_de(d.weekday())}, {d.strftime('%d.%m.')}"
        except ValueError:
            day_label = date_str
        dish = e.get("dish", "")
        servings = e.get("servings", S.MEAL_PLAN_SERVINGS_DEFAULT)
        cook_time = e.get("cook_time_minutes", 45)
        is_today = date_str == today_str
        row_style = " mp-today" if is_today else ""
        rows_html += (
            f'<div class="mp-row{row_style}">'
            f'<span class="mp-day">{day_label}</span>'
            f'<span class="mp-dish">{dish}</span>'
            f'<span class="mp-meta">{servings}&nbsp;Pers.&nbsp;·&nbsp;{cook_time}&nbsp;Min.</span>'
            f'</div>'
        )

    return (
        f'<div class="mp-card">'
        f'<div class="mp-header">{header}</div>'
        f'{rows_html}'
        f'</div>'
    )


async def categorize_ingredients(ingredients: list[str]) -> dict[str, list[str]]:
    """Kategorisiert Zutaten via Haiku in fuenf Einkaufsgruppen.

    Args:
        ingredients: Liste von Zutatennamen (ggf. mit Mengenangaben).

    Returns:
        Dict mit Kategorie als Schluessel und Liste von Zutaten als Wert.
        Kategorien: "Obst & Gemüse", "Fleisch & Wurst",
        "Milcherzeugnisse", "Trockenwaren & Gewürze", "Sonstiges".
        Bei Fehler landen alle Zutaten unter "Sonstiges".
    """
    categories = [
        "Obst & Gemüse",
        "Fleisch & Wurst",
        "Milcherzeugnisse",
        "Trockenwaren & Gewürze",
        "Sonstiges",
    ]
    default: dict[str, list[str]] = {c: [] for c in categories}
    if not ingredients:
        return default

    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=1500,
            system=(
                "Kategorisiere die folgende Einkaufsliste in genau diese fuenf Kategorien:\n"
                '"Obst & Gemüse", "Fleisch & Wurst", "Milcherzeugnisse", '
                '"Trockenwaren & Gewürze", "Sonstiges".\n'
                "Antworte AUSSCHLIESSLICH mit gueltigem JSON. Format:\n"
                '{"Obst & Gemüse": [...], "Fleisch & Wurst": [...], '
                '"Milcherzeugnisse": [...], "Trockenwaren & Gewürze": [...], '
                '"Sonstiges": [...]}'
            ),
            messages=[{
                "role": "user",
                "content": "Zutaten:\n" + "\n".join(f"- {i}" for i in ingredients),
            }],
        )
        raw = resp.content[0].text.strip() if resp and resp.content else "{}"
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```"))
        data = json.loads(raw)
        result: dict[str, list[str]] = {c: [] for c in categories}
        for cat in categories:
            items = data.get(cat, [])
            if isinstance(items, list):
                result[cat] = [str(i) for i in items]
        return result
    except Exception as e:
        log.warning(
            "categorize_ingredients: Fehler: %s: %s", type(e).__name__, e
        )
        fallback = {c: [] for c in categories}
        fallback["Sonstiges"] = list(ingredients)
        return fallback


def generate_meal_plan_pdf(categorized_ingredients: dict[str, list[str]] | None = None) -> str | None:
    """Erstellt eine ansprechende PDF-Datei mit dem aktuellen Speisenplan.

    Das PDF umfasst vier Bereiche:
    - Seite 1: Titelseite (Cover)
    - Seite 2: Wochenübersicht als Tabelle
    - Seite 3: Einkaufsliste nach Kategorien
    - Seiten 4+: Rezepte (ein Tag pro Seite)

    Args:
        categorized_ingredients: Vorberechnete Kategorisierung der Zutaten.
            Wenn None, werden alle Zutaten unter "Sonstiges" gelistet.

    Returns:
        Absoluter Pfad zur PDF-Datei, oder None bei Fehler.
    """
    if not S.MEAL_PLAN_WEEK:
        return None
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("generate_meal_plan_pdf: PyMuPDF nicht installiert")
        return None
    try:
        pdf_dir = os.path.join(os.path.dirname(__file__), "jarvis_pdfs")
        os.makedirs(pdf_dir, exist_ok=True)
        dates = sorted(S.MEAL_PLAN_WEEK.keys())
        first = datetime.date.fromisoformat(dates[0]) if dates else datetime.date.today()
        last = datetime.date.fromisoformat(dates[-1]) if dates else first
        kw = first.isocalendar()[1]
        year = first.year
        pdf_path = os.path.join(pdf_dir, f"speiseplan_kw{kw:02d}_{year}.pdf")

        PAGE_W, PAGE_H, MARGIN = 595, 842, 50
        # Farben
        COL_DARK_BLUE = (0.05, 0.25, 0.55)
        COL_BLUE = (0.1, 0.4, 0.75)
        COL_GREY = (0.45, 0.45, 0.45)
        COL_BLACK = (0.0, 0.0, 0.0)
        COL_ROW_ALT = (0.94, 0.94, 0.94)
        COL_COVER_BG = (0.9, 0.95, 1.0)

        doc = fitz.open()

        # ------------------------------------------------------------------
        # Seite 1 — Cover
        # ------------------------------------------------------------------
        cover = doc.new_page(width=PAGE_W, height=PAGE_H)
        # Hintergrund
        cover.draw_rect(
            fitz.Rect(0, 0, PAGE_W, PAGE_H),
            color=COL_COVER_BG, fill=COL_COVER_BG,
        )
        # Titel
        cover.insert_textbox(
            fitz.Rect(MARGIN, 280, PAGE_W - MARGIN, 340),
            "Speiseplan",
            fontsize=36, fontname="helvB",
            color=COL_DARK_BLUE, align=1,
        )
        # Datumsbereich
        date_range = f"{first.strftime('%d.%m.')} – {last.strftime('%d.%m.%Y')}"
        cover.insert_textbox(
            fitz.Rect(MARGIN, 350, PAGE_W - MARGIN, 375),
            date_range,
            fontsize=16, fontname="helv",
            color=COL_BLUE, align=1,
        )
        # KW-Info
        cover.insert_textbox(
            fitz.Rect(MARGIN, 382, PAGE_W - MARGIN, 400),
            f"Kalenderwoche {kw} / {year}",
            fontsize=12, fontname="helv",
            color=COL_GREY, align=1,
        )

        # ------------------------------------------------------------------
        # Seite 2 — Wochenübersicht
        # ------------------------------------------------------------------
        overview = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = float(MARGIN)
        overview.insert_textbox(
            fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 26),
            "Wochenübersicht",
            fontsize=18, fontname="helvB", color=COL_DARK_BLUE,
        )
        y += 34
        # Tabellenspalten: Wochentag | Gericht | kcal | KH (g) | Zeit
        col_x = [MARGIN, 145, 355, 415, 470, PAGE_W - MARGIN]
        col_headers = ["Wochentag", "Gericht", "kcal", "KH (g)", "Zeit"]
        row_h = 20
        # Kopfzeile
        overview.draw_rect(
            fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + row_h),
            color=COL_BLUE, fill=COL_BLUE,
        )
        for i, header in enumerate(col_headers):
            overview.insert_textbox(
                fitz.Rect(col_x[i] + 3, y + 3, col_x[i + 1] - 2, y + row_h - 2),
                header,
                fontsize=8.5, fontname="helvB", color=(1, 1, 1),
            )
        y += row_h
        for idx, date_str in enumerate(dates):
            e = S.MEAL_PLAN_WEEK[date_str]
            try:
                d = datetime.date.fromisoformat(date_str)
                day_label = f"{_weekday_de(d.weekday())}, {d.strftime('%d.%m.')}"
            except ValueError:
                day_label = date_str
            dish = e.get("dish", "")
            cook_time = e.get("cook_time_minutes", 45)
            kcal = e.get("kcal_estimate", 0)
            carbs = e.get("carbs_estimate", 0)
            row_color = COL_ROW_ALT if idx % 2 == 1 else (1.0, 1.0, 1.0)
            overview.draw_rect(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + row_h),
                color=row_color, fill=row_color,
            )
            row_values = [
                day_label,
                dish,
                str(kcal) if kcal else "–",
                str(carbs) if carbs else "–",
                f"{cook_time} min",
            ]
            for i, val in enumerate(row_values):
                overview.insert_textbox(
                    fitz.Rect(col_x[i] + 3, y + 3, col_x[i + 1] - 2, y + row_h - 2),
                    val,
                    fontsize=8, fontname="helv", color=COL_BLACK,
                )
            y += row_h

        # ------------------------------------------------------------------
        # Seite 3 — Einkaufsliste
        # ------------------------------------------------------------------
        shopping = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = float(MARGIN)
        shopping.insert_textbox(
            fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 26),
            "Einkaufsliste",
            fontsize=18, fontname="helvB", color=COL_DARK_BLUE,
        )
        y += 34

        # Kategorisierte Zutaten verwenden (oder alle unter Sonstiges)
        if categorized_ingredients is None:
            all_ing: list[str] = []
            seen_norm: set[str] = set()
            for entry in S.MEAL_PLAN_WEEK.values():
                for ing in entry.get("ingredients", []):
                    cleaned = ing.strip()
                    if not cleaned:
                        continue
                    key = _normalize_ingredient(cleaned)
                    if key not in seen_norm:
                        seen_norm.add(key)
                        all_ing.append(cleaned)
            categorized_ingredients = {"Sonstiges": sorted(all_ing)}

        for category, items in categorized_ingredients.items():
            if not items:
                continue
            # Prüfen ob genug Platz für Kategorie-Header
            if y + 20 > PAGE_H - MARGIN:
                shopping = doc.new_page(width=PAGE_W, height=PAGE_H)
                y = float(MARGIN)
            # Kategorie-Header
            shopping.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 16),
                category,
                fontsize=10, fontname="helvB", color=COL_BLUE,
            )
            y += 18
            for item in items:
                if y + 14 > PAGE_H - MARGIN:
                    shopping = doc.new_page(width=PAGE_W, height=PAGE_H)
                    y = float(MARGIN)
                # Checkbox-Quadrat
                shopping.draw_rect(
                    fitz.Rect(MARGIN, y + 2, MARGIN + 8, y + 10),
                    color=COL_GREY,
                )
                shopping.insert_textbox(
                    fitz.Rect(MARGIN + 12, y, PAGE_W - MARGIN, y + 13),
                    item,
                    fontsize=8.5, fontname="helv", color=COL_BLACK,
                )
                y += 14
            y += 6  # Abstand zwischen Kategorien

        # ------------------------------------------------------------------
        # Seiten 4+ — Rezepte (ein Tag pro Seite)
        # ------------------------------------------------------------------
        for date_str in dates:
            e = S.MEAL_PLAN_WEEK[date_str]
            try:
                d = datetime.date.fromisoformat(date_str)
                weekday_label = _weekday_de(d.weekday())
                date_label = d.strftime("%d.%m.%Y")
            except ValueError:
                weekday_label = date_str
                date_label = ""
            dish = e.get("dish", "")
            cook_time = e.get("cook_time_minutes", 45)
            ingredients = e.get("ingredients", [])
            recipe = e.get("recipe", "")
            kcal = e.get("kcal_estimate", 0)
            carbs = e.get("carbs_estimate", 0)

            recipe_page = doc.new_page(width=PAGE_W, height=PAGE_H)
            y = float(MARGIN)

            # Kopfzeile: Wochentag + Datum links, Gericht Mitte, Zeit rechts
            recipe_page.insert_textbox(
                fitz.Rect(MARGIN, y, 180, y + 18),
                f"{weekday_label}, {date_label}",
                fontsize=10, fontname="helvB", color=COL_DARK_BLUE,
            )
            recipe_page.insert_textbox(
                fitz.Rect(180, y, PAGE_W - 110, y + 18),
                dish,
                fontsize=14, fontname="helvB", color=COL_BLACK, align=1,
            )
            recipe_page.insert_textbox(
                fitz.Rect(PAGE_W - 105, y, PAGE_W - MARGIN, y + 18),
                f"{cook_time} min",
                fontsize=10, fontname="helv", color=COL_GREY, align=2,
            )
            y += 26

            # Trennlinie
            recipe_page.draw_line(
                fitz.Point(MARGIN, y), fitz.Point(PAGE_W - MARGIN, y),
                color=COL_BLUE, width=0.5,
            )
            y += 10

            # Zutaten
            recipe_page.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 14),
                "Zutaten",
                fontsize=11, fontname="helvB", color=COL_DARK_BLUE,
            )
            y += 16
            for ing in ingredients:
                if y + 13 > PAGE_H - MARGIN - 30:
                    recipe_page = doc.new_page(width=PAGE_W, height=PAGE_H)
                    y = float(MARGIN)
                recipe_page.insert_textbox(
                    fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 12),
                    f"• {ing}",
                    fontsize=8.5, fontname="helv", color=COL_BLACK,
                )
                y += 13
            y += 8

            # Zubereitung
            if recipe:
                if y + 18 > PAGE_H - MARGIN - 30:
                    recipe_page = doc.new_page(width=PAGE_W, height=PAGE_H)
                    y = float(MARGIN)
                recipe_page.insert_textbox(
                    fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + 14),
                    "Zubereitung",
                    fontsize=11, fontname="helvB", color=COL_DARK_BLUE,
                )
                y += 16
                # Rezept in Schritte aufteilen (Sätze oder Zeilenumbrüche)
                steps_raw = re.split(r"(?<=[.!?])\s+|\n+", recipe.strip())
                steps = [s.strip() for s in steps_raw if s.strip()]
                for idx_s, step in enumerate(steps, 1):
                    step_text = f"{idx_s}. {step}"
                    # Dynamische Höhe schätzen: ca. 12pt pro Zeile, 60 Zeichen pro Zeile
                    est_lines = max(1, len(step_text) // 75 + 1)
                    step_h = est_lines * 12 + 4
                    if y + step_h > PAGE_H - MARGIN - 30:
                        recipe_page = doc.new_page(width=PAGE_W, height=PAGE_H)
                        y = float(MARGIN)
                    recipe_page.insert_textbox(
                        fitz.Rect(MARGIN, y, PAGE_W - MARGIN, y + step_h),
                        step_text,
                        fontsize=8.5, fontname="helv", color=COL_BLACK,
                    )
                    y += step_h + 2

            # Naehrwert-Fusszeile
            if kcal or carbs:
                footer_text = f"ca. {kcal} kcal · {carbs} g KH pro Portion"
                footer_y = PAGE_H - MARGIN - 14
                # Neue Seite wenn Rezept-Text bis in den Footer-Bereich reicht
                if y > footer_y - 5:
                    recipe_page = doc.new_page(width=PAGE_W, height=PAGE_H)
                    footer_y = PAGE_H - MARGIN - 14
                recipe_page.insert_textbox(
                    fitz.Rect(MARGIN, footer_y, PAGE_W - MARGIN, footer_y + 13),
                    footer_text,
                    fontsize=8, fontname="helv", color=COL_GREY, align=1,
                )

        doc.save(pdf_path)
        doc.close()
        log.info("generate_meal_plan_pdf: %s", pdf_path)
        return pdf_path
    except Exception as exc:
        log.warning("generate_meal_plan_pdf: %s: %s", type(exc).__name__, exc)
        return None


def format_meal_plan_telegram(include_today_recipe: bool = True) -> str:
    """Formatiert den aktuellen Plan fuer Telegram.

    Zeigt Übersicht aller Tage + das vollständige Rezept für heute
    (wenn include_today_recipe=True und ein Eintrag für heute vorhanden).
    """
    if not S.MEAL_PLAN_WEEK:
        return "Kein Speisenplan verfuegbar."

    today_str = datetime.date.today().isoformat()
    today_entry = None
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
        marker = " ◄ heute" if date_str == today_str else ""
        lines.append(
            f"{day_label}: {dish} "
            f"({servings} Pers., ca. {cook_time} Min.){marker}"
        )
        if date_str == today_str:
            today_entry = entry

    if include_today_recipe and today_entry:
        dish = today_entry.get("dish", "")
        ingredients = today_entry.get("ingredients", [])
        recipe = today_entry.get("recipe", "")
        lines.append(f"\nRezept heute — {dish}:")
        if ingredients:
            lines.append("Zutaten: " + ", ".join(ingredients))
        if recipe:
            lines.append(f"\nZubereitung: {recipe}")

    market = _preferred_market()
    if market:
        lines.append(f"\nEinkauf empfohlen: {market}")
    lines.append(
        "\nSag mir, wenn du einen Tag aendern moechtest "
        "oder die Zutaten auf die Einkaufsliste uebertragen willst."
    )
    return "\n".join(lines)


def _weekday_de(weekday: int) -> str:
    """Wochentag-Name auf Deutsch (0=Montag .. 6=Sonntag)."""
    names = [
        "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag",
    ]
    return names[weekday] if 0 <= weekday <= 6 else str(weekday)
