"""
Background data fetching and morning brief scheduler.

Refreshes weather + tasks (`refresh_data`), the BFH 3-day digest
(`refresh_steuer_recent`), and the morning Steuerrecht brief
(`refresh_steuer_brief`). `morning_brief_scheduler` is the long-running
asyncio task that triggers the brief at MORNING_HOUR.

Mutates `settings.WEATHER_INFO` / `TASKS_INFO` / `STEUER_*` directly
so other modules can read them via `settings as S`.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import time

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import google_calendar_tools
import health_tools
from holidays import check_free_day
import jarvis_quotes
import offer_monitor
from prompt import llm_text, pick_address
import settings as S
import steuer_news
import todoist_tools
import weather_tools

log = S.log

_last_refresh_time: float = 0.0


async def _fetch_weather_once() -> dict:
    """One try at wttr.in; tenacity wraps the retry loop above us."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"https://wttr.in/{S.CITY}?format=j1",
            headers={"User-Agent": "curl"},
        )
        resp.raise_for_status()
        data = resp.json()
    c = data["current_condition"][0]
    today = data["weather"][0]
    result = {
        "temp": c["temp_C"],
        "feels_like": c["FeelsLikeC"],
        "description": c["weatherDesc"][0]["value"],
        "humidity": c["humidity"],
        "wind_kmh": c["windspeedKmph"],
        "max_temp": today.get("maxtempC", c["temp_C"]),
        "min_temp": today.get("mintempC", c["temp_C"]),
        "day_description": today.get("weatherDesc", [{}])[0].get("value", c["weatherDesc"][0]["value"]),
        "forecast_today": [],
    }
    # Alle Stunden des Tages (nicht nur zukuenftige) fuer Tagesbogen-Darstellung
    for h in today["hourly"]:
        result["forecast_today"].append({
            "hour": int(h["time"]) // 100,
            "temp": h["tempC"],
            "desc": h["weatherDesc"][0]["value"],
            "rain": h.get("chanceofrain", "0"),
        })
    return result


async def fetch_weather() -> dict | None:
    """Fetch wttr.in weather with one retry on transient failure.
    Returns None when both attempts fail."""
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type((httpx.HTTPError, KeyError, ValueError)),
            reraise=True,
        ):
            with attempt:
                return await _fetch_weather_once()
    except Exception as e:
        log.warning(f"fetch_weather failed (after retries): {type(e).__name__}: {e}")
        return None


def read_obsidian_tasks_sync() -> list[str]:
    """Read open tasks from Obsidian (sync). Cheap file IO; called via
    run_in_executor from async refresh_data()."""
    if not S.TASKS_FILE:
        return []
    try:
        tasks_path = os.path.join(S.TASKS_FILE, "Tasks.md")
        with open(tasks_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [
            line.strip().replace("- [ ]", "").strip()
            for line in lines
            if line.strip().startswith("- [ ]")
        ]
    except Exception as e:
        log.warning(f"read_obsidian_tasks_sync failed: {type(e).__name__}: {e}")
        return []


async def refresh_data(force: bool = False) -> None:
    """Refresh weather (async HTTP) and tasks (file IO via executor) without
    blocking the event loop. Skips refresh when called again within
    `S.REFRESH_COOLDOWN` seconds, unless `force=True`."""
    global _last_refresh_time
    now = time.time()
    if not force and (now - _last_refresh_time) < S.REFRESH_COOLDOWN:
        remaining = int(S.REFRESH_COOLDOWN - (now - _last_refresh_time))
        log.info(f"refresh_data skip (cooldown noch {remaining}s)")
        return
    _last_refresh_time = now
    loop = asyncio.get_running_loop()
    weather, tasks = await asyncio.gather(
        fetch_weather(),
        loop.run_in_executor(None, read_obsidian_tasks_sync),
    )
    S.WEATHER_INFO = weather
    S.TASKS_INFO = tasks
    log.info(f"Wetter: {S.WEATHER_INFO}")
    log.info(f"Tasks: {len(S.TASKS_INFO)} geladen")
    # Person-Enrichment: Aufgaben auf bekannte Kontakte scannen
    try:
        import person_enrichment
        n = person_enrichment.enrich_from_texts(S.TASKS_INFO, "Aufgabe")
        if n:
            log.info(f"person_enrichment: {n} Profile aus Aufgaben aktualisiert")
    except Exception as e:
        log.debug(f"person_enrichment tasks failed: {e}")


async def refresh_today_tasks() -> None:
    """Fetch Todoist tasks due today / overdue, scoped to Catrin's
    HILO/DIHAG/Privat projects. Stored in S.TODAY_TASKS as plain text."""
    if not S.TODOIST_TOKEN:
        S.TODAY_TASKS = ""
        return
    try:
        # Reuse get_tasks (already filters by user + projects). It returns
        # all open tasks; we further filter to today/overdue inline.
        full = await todoist_tools.get_tasks(
            S.TODOIST_TOKEN,
            max_tasks=50,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )
        if full == "KEINE_TASKS":
            S.TODAY_TASKS = ""
            return
        today = datetime.date.today().isoformat()
        # The lines look like '• content (heute)' or '• content ⚠ überfällig'.
        keep = [
            line for line in full.splitlines()
            if "(heute)" in line or "überfällig" in line
        ]
        S.TODAY_TASKS = "\n".join(keep)
    except Exception as e:
        log.warning(f"refresh_today_tasks failed: {type(e).__name__}: {e}")
        S.TODAY_TASKS = ""


async def refresh_today_events() -> None:
    """Fetch Google Calendar events for today only. The lines are
    stored raw — prompt.build_system_prompt() annotates them with
    fresh time deltas at each prompt build so they're always current."""
    try:
        # get_events returns events for the next N days; we filter today
        # ourselves so we don't have to widen the API.
        full = await google_calendar_tools.get_events(days=1, max_results=20)
        if full == "KEINE_TERMINE":
            S.TODAY_EVENTS = ""
            return
        # The text looks like:
        #   "Kalender — naechste N Termine:\n• Sun 03.05. 14:00 — ..."
        # Filter the bullet lines whose date matches today.
        today = datetime.date.today()
        today_short = today.strftime("%d.%m.")
        keep = [
            line for line in full.splitlines()
            if line.startswith("•") and today_short in line
        ]
        S.TODAY_EVENTS = "\n".join(keep)
        # Person-Enrichment: Kalender-Events auf bekannte Kontakte scannen
        try:
            import person_enrichment
            n = person_enrichment.enrich_from_texts(keep, "Termin")
            if n:
                log.info(f"person_enrichment: {n} Profile aus Kalender-Events aktualisiert")
        except Exception as e:
            log.debug(f"person_enrichment events failed: {e}")
    except Exception as e:
        log.warning(f"refresh_today_events failed: {type(e).__name__}: {e}")
        S.TODAY_EVENTS = ""


def trim_to_complete_sentences(text: str) -> str:
    """Defense-in-depth: wenn der LLM-Output mitten im Satz endet,
    schneide bis zum letzten vollstaendigen Satz. Verhindert
    'abgeschnittene Nachrichten'-Symptom unabhaengig von max_tokens."""
    text = text.strip()
    if not text:
        return text
    if text[-1] in ".!?":
        return text
    # Find last sentence-ender; if there's none, return as-is
    last_dot = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    if last_dot < len(text) // 2:
        # Less than half is a complete sentence — keep everything,
        # the result is already mostly fragmentary
        return text
    return text[: last_dot + 1].strip()


def _is_round_birthday(age: int) -> bool:
    """Returns True when `age` qualifies as a 'round' birthday.

    Rule (Issue #144):
    - 70 and above: every 5 years (70, 75, 80, ...)
    - Below 70: every 10 years (10, 20, 30, 40, 50, 60)
    """
    if age >= 70:
        return age % 5 == 0
    return age % 10 == 0


async def refresh_birthday_reminders() -> None:
    """Pruefe Geburtstage der Google-Kontakte in den naechsten 7 Tagen.

    Ergebnis wird in S.BIRTHDAY_REMINDERS gespeichert (Issue #120).
    Format: "Geburtstage diese Woche: • Max Mueller (morgen, 15.06.)"

    Freitags (Issue #144): zus\xe4tzlich runde Geburtstage berechnen und
    in S.BIRTHDAY_ROUND speichern.
    Format: "Runde Geburtstage diese Woche: • Max M\xfcller (50, Freitag 06.06.)"

    Bei nicht verfuegbarer API: stille Degradation (leerer String).
    """
    try:
        import google_contacts_tools
        contacts = await google_contacts_tools.read_all_contacts()
        today = datetime.date.today()
        is_friday = today.weekday() == 4  # 0=Mon, 4=Fri

        hits: list[str] = []
        round_hits: list[str] = []

        _WEEKDAYS_DE = [
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag",
        ]

        for c in contacts:
            if not c.birthday:
                continue
            bm = c.birthday.get("month")
            bd = c.birthday.get("day")
            if not bm or not bd:
                continue
            try:
                next_bd = datetime.date(today.year, bm, bd)
            except ValueError:
                continue
            if next_bd < today:
                try:
                    next_bd = datetime.date(today.year + 1, bm, bd)
                except ValueError:
                    continue
            delta = (next_bd - today).days
            if 0 <= delta <= 7:
                date_str = next_bd.strftime("%d.%m.")
                if delta == 0:
                    when = "heute"
                elif delta == 1:
                    when = "morgen"
                else:
                    when = f"in {delta} Tagen"
                hits.append(f"• {c.name} ({when}, {date_str})")

                # Runde Geburtstage (freitags): nur wenn Geburtsjahr bekannt
                if is_friday:
                    birth_year = c.birthday.get("year")
                    if birth_year:
                        age = next_bd.year - birth_year
                        if age > 0 and _is_round_birthday(age):
                            weekday_label = _WEEKDAYS_DE[next_bd.weekday()]
                            round_hits.append(
                                f"• {c.name} ({age}, {weekday_label} {date_str})"
                            )

        S.BIRTHDAY_REMINDERS = (
            "Geburtstage diese Woche: " + ", ".join(hits) if hits else ""
        )
        if is_friday and round_hits:
            S.BIRTHDAY_ROUND = "Runde Geburtstage diese Woche: " + ", ".join(round_hits)
        else:
            S.BIRTHDAY_ROUND = ""
        log.info(
            f"refresh_birthday_reminders: {len(hits)} Treffer, "
            f"{len(round_hits)} runde (freitags={is_friday})"
        )
    except Exception as e:
        log.warning(f"refresh_birthday_reminders failed: {type(e).__name__}: {e}")
        S.BIRTHDAY_REMINDERS = ""
        S.BIRTHDAY_ROUND = ""


async def refresh_open_promises() -> None:
    """Lade offene Vorhaben aus der DB und speichere als S.OPEN_PROMISES
    (Issue #117). Wird beim Morgen-Briefing und bei Activate aufgerufen."""
    try:
        import promise_tracker
        block = await promise_tracker.format_promises_block(max_age_days=3)
        S.OPEN_PROMISES = block
        log.info(f"refresh_open_promises: {len(S.OPEN_PROMISES)} Zeichen")
    except Exception as e:
        log.warning(f"refresh_open_promises failed: {type(e).__name__}: {e}")
        S.OPEN_PROMISES = ""


async def refresh_pending_followups() -> None:
    """Lade ausstehende Mail-Follow-ups und speichere als S.PENDING_FOLLOWUPS."""
    try:
        import followup_tracker
        followup_tracker.prune_old(max_age_days=14)
        S.PENDING_FOLLOWUPS = followup_tracker.format_followups_block(max_age_days=7)
        if S.PENDING_FOLLOWUPS:
            log.info(f"refresh_pending_followups: {len(S.PENDING_FOLLOWUPS)} Zeichen")
    except Exception as e:
        log.warning(f"refresh_pending_followups failed: {type(e).__name__}: {e}")
        S.PENDING_FOLLOWUPS = ""


# Issue #119 -- Fristen-Bewusstsein ----------------------------------------

_DEADLINE_KEYWORDS = ["frist", "abgabe", "deadline", "faellig", "ablauf"]


def get_upcoming_deadlines(days: int = 3) -> str:
    """Prueffe anstehende Fristen der naechsten `days` Tage.

    Quellen: S.TODAY_EVENTS (Kalender), S.TODAY_TASKS (Todoist),
    feste Steuerfristen (31.05, 31.07, 31.10, 10.01).

    Returns:
        Formatierter Text mit Frist-Hinweisen oder leerer String.
    """
    today = datetime.date.today()
    lines: list[str] = []

    # Kalender-Events aus S.TODAY_EVENTS
    try:
        for line in (S.TODAY_EVENTS or "").splitlines():
            low = line.lower()
            if not any(kw in low for kw in _DEADLINE_KEYWORDS):
                continue
            m = re.search(r"(\d{2})\.(\d{2})\.", line)
            if not m:
                continue
            try:
                ev = datetime.date(today.year, int(m.group(2)), int(m.group(1)))
                delta = (ev - today).days
                if 0 <= delta <= days:
                    lines.append(_deadline_hint(line.strip().lstrip("•").strip(), delta))
            except ValueError:
                pass
    except Exception as exc:
        log.warning(f"get_upcoming_deadlines calendar: {exc}")

    # Todoist-Aufgaben aus S.TODAY_TASKS
    try:
        for line in (S.TODAY_TASKS or "").splitlines():
            low = line.lower()
            if not any(kw in low for kw in _DEADLINE_KEYWORDS):
                continue
            title = line.strip().lstrip("•").strip()
            if "ueberfaellig" in low or "⚠" in line:
                lines.append(f"Ueberfaellige Frist: {title}")
            else:
                lines.append(f"Frist heute: {title}")
    except Exception as exc:
        log.warning(f"get_upcoming_deadlines todoist: {exc}")

    # Feste Steuerfristen
    for month, day, label in [
        (5, 31, "Abgabefrist Steuererklaerung (31. Mai)"),
        (7, 31, "Abgabefrist Steuererklaerung (31. Juli)"),
        (10, 31, "Abgabefrist Steuererklaerung (31. Oktober)"),
        (1, 10, "Lohnsteuer-Anmeldung (10. Januar)"),
    ]:
        try:
            fixed = datetime.date(today.year, month, day)
            delta = (fixed - today).days
            if 0 <= delta <= days:
                lines.append(_deadline_hint(label, delta))
        except Exception:
            pass

    if not lines:
        return ""
    return "Anstehende Fristen:\n" + "\n".join(f"• {ln}" for ln in lines)


def _deadline_hint(title: str, delta_days: int) -> str:
    """Hilfsfunktion: lesbarer Frist-Hinweis anhand Restlaufzeit."""
    if delta_days == 0:
        return f"Heute laeuft ab: {title}"
    if delta_days == 1:
        return f"Morgen laeuft ab: {title}"
    if delta_days == 2:
        return f"Uebermorgen laeuft ab: {title}"
    return f"In {delta_days} Tagen laeuft ab: {title}"


async def refresh_upcoming_deadlines() -> None:
    """Berechne anstehende Fristen und speichere als S.UPCOMING_DEADLINES.

    Muss NACH refresh_today_tasks() + refresh_today_events() laufen,
    da es S.TODAY_TASKS / S.TODAY_EVENTS als Eingabe nutzt (Issue #119).
    """
    try:
        S.UPCOMING_DEADLINES = get_upcoming_deadlines(days=3)
        log.info(f"refresh_upcoming_deadlines: {len(S.UPCOMING_DEADLINES)} Zeichen")
    except Exception as exc:
        log.warning(f"refresh_upcoming_deadlines failed: {type(exc).__name__}: {exc}")
        S.UPCOMING_DEADLINES = ""


# ---------------------------------------------------------------------------

async def refresh_morning_brief_data() -> None:
    """Refresh all the extra data needed for the full morning briefing
    (today's tasks, today's calendar, birthday reminders).
    Called from the activate path before MORNING_BRIEF_UNTIL_HOUR.

    refresh_upcoming_deadlines() runs after the first gather because it
    reads S.TODAY_TASKS / S.TODAY_EVENTS which must be populated first."""
    await asyncio.gather(
        refresh_today_tasks(),
        refresh_today_events(),
        refresh_open_promises(),
        refresh_birthday_reminders(),
        refresh_pending_followups(),
    )
    # Second pass: deadline check needs tasks + events already in S.*

    await refresh_upcoming_deadlines()


async def refresh_steuer_recent() -> None:
    """Fetch and cache the last 3 days of BFH news."""
    today = datetime.date.today().isoformat()
    if S.STEUER_RECENT_DATE == today:
        return
    try:
        S.STEUER_RECENT = await steuer_news.fetch_recent(days=3)
        S.STEUER_RECENT_DATE = today
        log.info(f"Steuer-Recent: {len(S.STEUER_RECENT)} Zeichen")
    except Exception as e:
        log.warning(f"Steuer-Recent Fehler: {e}")
        S.STEUER_RECENT = ""


async def refresh_steuer_brief(raw: str | None = None) -> None:
    """Fetch steuerrecht news and summarize with Claude. Updates global cache.

    Wenn ``raw`` uebergeben wird, wird kein erneuter RSS-Fetch durchgefuehrt
    -- der bereits geholte Text wird direkt verwendet (Issue #91).
    """
    log.info("Steuerrecht-Brief wird abgerufen...")
    try:
        if raw is None:
            raw = await steuer_news.fetch_all_sources()
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=600,
            system=(
                f"Du bist Jarvis, der britisch-hoefliche KI-Butler von {S.USER_NAME}. "
                f"Erstelle einen KURZEN Ueberblick ueber neue steuerrechtliche Veroeffentlichungen "
                f"aus BMF-Schreiben, BMF-Pressemitteilungen und BFH-Pressemitteilungen. "
                f"Maximal 3-4 Saetze. Nenne nur was wirklich NEU und relevant ist. "
                f"Ton: praezise, trocken, professionell — kein Smalltalk. "
                f"WICHTIG: KEINE Begruessung wie 'Guten Morgen' oder 'Guten Tag', KEINE "
                f"direkte Anrede. Schreibe NUR die Sachzusammenfassung. Dieser Text wird "
                f"spaeter zitiert. KEINE Tags in eckigen Klammern."
            ),
            messages=[{"role": "user", "content": f"Neue Veroeffentlichungen heute:\n\n{raw}"}],
        )
        S.STEUER_BRIEF = llm_text(resp).strip()
        S.STEUER_BRIEF_DATE = datetime.date.today().isoformat()
        log.info(f"Steuerrecht-Brief: {S.STEUER_BRIEF[:80]}")
    except Exception as e:
        log.warning(f"Steuerrecht-Brief Fehler: {e}")
        S.STEUER_BRIEF = ""


async def build_weekly_outlook() -> str:
    """Sammle die wichtigsten offenen Punkte fuer die NAECHSTE Woche
    aus allen Quellen, lasse Claude einen kurzen Sprach-Brief
    formulieren. Wird sowohl vom Sonntag-Scheduler als auch von der
    Action WEEKLY_OUTLOOK genutzt."""
    today = datetime.date.today()
    next_monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
    next_sunday = next_monday + datetime.timedelta(days=6)

    parts: list[str] = []

    # 1. Todoist: ALLE offenen + ueberfaelligen Tasks (nicht nur 20)
    if S.TODOIST_TOKEN and S.TODOIST_TOKEN != "YOUR_TODOIST_API_TOKEN":
        try:
            tasks_text = await todoist_tools.get_tasks(
                S.TODOIST_TOKEN,
                max_tasks=50,
                project_ids=S.TODOIST_PROJECT_IDS or None,
                section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
            )
            if tasks_text and tasks_text != "KEINE_TASKS":
                parts.append(f"TODOIST OFFEN:\n{tasks_text}")
        except Exception as e:
            log.warning(f"weekly outlook: tasks failed: {e}")

    # 2. Google Calendar: alle Termine der naechsten 7 Tage (mindestens —
    # bei Sonntag-Trigger sind das die naechsten 7 Tage Mo-So). max_results
    # hoch genug damit Catrin's Vollkalender reinpasst.
    try:
        days_ahead = max(7, (next_sunday - today).days + 1)
        cal_text = await google_calendar_tools.get_events(
            days=days_ahead, max_results=80,
        )
        if cal_text and cal_text != "KEINE_TERMINE":
            parts.append(f"KALENDER NAECHSTE 7 TAGE:\n{cal_text}")
    except Exception as e:
        log.warning(f"weekly outlook: calendar failed: {e}")

    # 3. persons_db: offene Punkte
    try:
        import persons_db
        persons_open: list[str] = []
        for prof in persons_db.all_profiles():
            for pt in prof.open_points[-3:]:
                persons_open.append(f"{prof.name}: {pt}")
        if persons_open:
            parts.append("OFFENE PUNKTE MIT PERSONEN:\n" + "\n".join(persons_open))
    except Exception as e:
        log.warning(f"weekly outlook: persons_db failed: {e}")

    if not parts:
        return ""

    # Lass Claude den Brief formulieren — Catrin will eine vollstaendige
    # Auflistung von Terminen + Aufgaben, keine Prosa-Zusammenfassung.
    user_msg = "\n\n".join(parts)
    addr = pick_address()
    sys_prompt = (
        f"Du bist Jarvis. Aus den folgenden Daten baust Du einen "
        f"Wochenausblick fuer {addr}. STRENGES FORMAT:\n\n"
        f"1. Eine kurze Eroeffnung (1 Satz), z.B. \"Naechste Woche, "
        f"{addr}, stehen folgende Punkte an.\"\n\n"
        f"2. Block 'TERMINE': nenne JEDEN einzelnen Termin der naechsten "
        f"7 Tage als eigenen Halbsatz mit Datum/Wochentag + Uhrzeit + "
        f"Titel. Keine Auswahl, keine Zusammenfassung — alle nennen.\n\n"
        f"3. Block 'AUFGABEN': nenne JEDE offene Aufgabe einzeln. "
        f"Ueberfaellige zuerst, dann nach Faelligkeitsdatum sortiert. "
        f"Keine Auswahl — alle nennen.\n\n"
        f"4. Wenn 'OFFENE PUNKTE MIT PERSONEN' vorhanden sind: kurzer "
        f"Block dazu, max 1 Satz pro Person.\n\n"
        f"5. Optional 1-2 Saetze am Schluss mit dem Schwerpunkt der Woche.\n\n"
        f"Ton: trocken-butlerhaft, klar, vollstaendig. KEINE eckigen "
        f"Klammern, KEINE Tags. Beende JEDEN Satz mit einem Punkt."
    )
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=1500,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return trim_to_complete_sentences(llm_text(resp).strip())
    except Exception as e:
        log.warning(f"weekly outlook generation failed: {type(e).__name__}: {e}")
        return ""


async def weekly_outlook_scheduler() -> None:
    """Sonntag 18:00: triggere den Wochenausblick automatisch via
    den proactive-broadcaster (Mac-UI-Push, sofern verbunden)."""
    triggered_for_week = ""  # ISO week (z.B. "2026-W18")
    while True:
        now = datetime.datetime.now()
        # Sonntag = weekday 6, 18 Uhr ist der Trigger
        iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        if (now.weekday() == 6
                and now.hour >= 18
                and triggered_for_week != iso_week):
            triggered_for_week = iso_week
            log.info("weekly_outlook_scheduler: triggering Sunday-evening outlook")
            try:
                text = await build_weekly_outlook()
                if text and _proactive_handler is not None:
                    await _proactive_handler(text)
            except Exception as e:
                log.warning(f"weekly_outlook_scheduler failed: "
                            f"{type(e).__name__}: {e}")
        await asyncio.sleep(300)  # alle 5 Min reicht


async def refresh_offers() -> None:
    """Fetch supermarket offers and cache in S.WEEKLY_OFFERS (Issue #122).
    Called Monday-only from morning_brief_scheduler."""
    try:
        S.WEEKLY_OFFERS = await offer_monitor.format_offers_block(
            S.OFFER_WATCHLIST, S.OFFER_PLZ
        )
        log.info(f"refresh_offers: {len(S.WEEKLY_OFFERS)} Zeichen")
    except Exception as e:
        log.warning(f"refresh_offers failed: {type(e).__name__}: {e}")
        S.WEEKLY_OFFERS = ""


_MORNING_BRIEF_PROMPT = (
    "Du bist Jarvis. Guten-Morgen-Briefing fuer {addr}. "
    "STRENGE LAENGENVORGABE: maximal 3 Saetze gesamt. "
    "Inhalt (nach Prioritaet, alles in diese 3 Saetze packen): "
    "Wochentag + exaktes Datum + Wetter (Maximaltemperatur + Regen ja/nein); "
    "wichtigster Termin / wichtigste Aufgabe heute falls vorhanden; "
    "falls 'Abendessen heute' in den Tagesdaten steht, erwaehne den Gerichtsnamen kurz (ein Halbsatz); "
    "Geburtstag-Hinweis falls vorhanden (runde Geburtstage bevorzugt). "
    "Steuerrecht-Schlagzeile nur wenn Platz bleibt. "
    "Keine Nachrichten / Politik. "
    "Falls Gesundheitsdaten vorhanden: in EINEM der 3 Saetze trockenen "
    "Jarvis-Kommentar einbauen (Vortagsvergleich nutzen wenn verfuegbar). "
    "Fliessende Sprache, butler-typisch trocken. Keine ACTION-Tags."
)

_MORNING_BRIEF_PROMPT_WEEKEND = (
    "Du bist Jarvis. Guten-Morgen-Briefing fuer {addr} — heute ist {day_label}. "
    "Kein Arbeitsmodus. Kein Steuerrecht. Keine Aufgaben. Keine Nachrichten. "
    "STRENGE LAENGENVORGABE: maximal 3 Saetze. "
    "Inhalt: Datum + Wochentag erwaehnen, Wetter mit einem konkreten "
    "Freizeitvorschlag ('18 Grad, kein Regen — guter Tag fuer einen laengeren "
    "Spaziergang.'), Geburtstage falls vorhanden kurz erwaehnen. "
    "Gesundheitsdaten falls vorhanden in einen Satz einbauen "
    "(Vortagsvergleich nutzen wenn verfuegbar, Ton wohlwollend). "
    "Ton: entspannt, leicht humorvoll, butler-typisch trocken. Keine ACTION-Tags."
)


async def morning_brief_scheduler() -> None:
    """Long-running task: fetch the morning brief once per day at or
    after `S.MORNING_HOUR`. Refreshes BOTH the Steuer-Brief and the
    today's-tasks/events caches, then pushes the brief via the
    registered proactive handler (Telegram + UI if open).

    Uses '>=' on the hour (not '=='): if the server was asleep at exactly
    7:00 and the loop wakes up at 7:05, the brief still fires today.
    """
    # Pre-fill guard so a restart after the brief hour does not re-fire.
    _now_init = datetime.datetime.now()
    _is_weekend_init = _now_init.weekday() in (5, 6)
    _brief_hour_init = 9 if _is_weekend_init else S.MORNING_HOUR
    triggered_today = (
        datetime.date.today().isoformat() if _now_init.hour >= _brief_hour_init else ""
    )
    while True:
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        # Issue #145: Wochenende — Morgen-Briefing erst um 09:00
        _is_weekend = now.weekday() in (5, 6)
        _brief_hour = 9 if _is_weekend else S.MORNING_HOUR
        if now.hour >= _brief_hour and triggered_today != today:
            triggered_today = today
            # Issue #145: Tages-Aktivitaetslog zuruecksetzen
            import activity_log as _al
            _al.reset()
            try:
                gather_tasks = [
                    refresh_data(force=True),
                    refresh_steuer_brief(),
                    refresh_steuer_recent(),
                    refresh_morning_brief_data(),
                ]
                # Angebote nur montags laden (Issue #122)
                if now.weekday() == 0:
                    gather_tasks.append(refresh_offers())
                await asyncio.gather(*gather_tasks)
            except Exception as e:
                log.warning(f"morning_brief_scheduler: refresh failed: "
                            f"{type(e).__name__}: {e}")
            if _proactive_handler is None:
                log.info("morning_brief_scheduler: no handler registered, skipping send")
            else:
                try:
                    addr = pick_address()
                    is_free, day_label = check_free_day(datetime.date.today())
                    if is_free:
                        system_prompt = _MORNING_BRIEF_PROMPT_WEEKEND.format(
                            addr=addr, day_label=day_label
                        )
                    else:
                        system_prompt = _MORNING_BRIEF_PROMPT.format(addr=addr)
                    today_block = ""
                    if not is_free:
                        # Arbeitsblöcke nur unter der Woche
                        if S.TODAY_TASKS:
                            today_block += f"\nHeutige Aufgaben:\n{S.TODAY_TASKS}"
                        if S.TODAY_EVENTS:
                            today_block += f"\nHeutige Termine:\n{S.TODAY_EVENTS}"
                        if S.OPEN_PROMISES:
                            today_block += f"\n{S.OPEN_PROMISES}"
                        if S.PENDING_FOLLOWUPS:
                            today_block += f"\n{S.PENDING_FOLLOWUPS}"
                        if now.weekday() == 0 and S.WEEKLY_OFFERS:
                            today_block += f"\n{S.WEEKLY_OFFERS}"
                        if S.UPCOMING_DEADLINES:
                            today_block += f"\n{S.UPCOMING_DEADLINES}"
                        if S.STEUER_RECENT:
                            today_block += f"\nSteuerrecht aktuell (3 Tage): {S.STEUER_RECENT[:400]}"
                        elif S.STEUER_BRIEF:
                            today_block += f"\nSteuerrecht: {S.STEUER_BRIEF}"
                    # Abendessen heute (aus Speiseplan)
                    if not is_free:
                        _today_str = datetime.date.today().isoformat()
                        _meal = S.MEAL_PLAN_WEEK.get(_today_str, {}).get("dish", "")
                        if _meal:
                            today_block += f"\nAbendessen heute: {_meal}"
                    # Geburtstage + Gesundheit immer
                    if S.BIRTHDAY_REMINDERS:
                        today_block += f"\n{S.BIRTHDAY_REMINDERS}"
                    if S.BIRTHDAY_ROUND:
                        today_block += f"\n{S.BIRTHDAY_ROUND}"
                    if S.WEATHER_INFO:
                        w = S.WEATHER_INFO
                        rain = ""
                        for h in w.get("forecast_today", []):
                            if int(h.get("rain", "0")) >= 40:
                                rain = ", Regen möglich"
                                break
                        max_t = w.get("max_temp", w.get("temp", "?"))
                        min_t = w.get("min_temp", w.get("temp", "?"))
                        day_desc = w.get("day_description", w.get("description", ""))
                        today_block += (
                            f"\nWetter heute: {day_desc}, "
                            f"max. {max_t} Grad (min. {min_t} Grad){rain}"
                        )
                    # Morgens: Vortages-Daten bevorzugen (heute 7am ~ 0 kcal)
                    _health_src = S.HEALTH_INFO_PREV if S.HEALTH_INFO_PREV else S.HEALTH_INFO
                    if _health_src:
                        health_block = health_tools.format_for_brief(
                            _health_src, S.ACTIVITY_GOAL_KCAL
                        )
                        if health_block:
                            _label = "Gesundheitsdaten gestern" if S.HEALTH_INFO_PREV else "Gesundheitsdaten"
                            today_block += f"\n{_label}:\n{health_block}"
                    # Echtzeit-Wetter Neuss via Open-Meteo (Issue #199)
                    _live_weather = await weather_tools.get_weather_neuss()
                    if _live_weather:
                        today_block += f"\nAktuelles Wetter Neuss (live): {_live_weather}"
                    # Issue #200: optionales Film-Motivationszitat mit Cooldown
                    motivation_q = jarvis_quotes.quote_maybe("motivation_film", 0.3)
                    if motivation_q:
                        today_block += f"\n{motivation_q}"
                    user_msg = (
                        f"Datum: {now.strftime('%A, %d.%m.%Y')}, "
                        f"Uhrzeit: {now.strftime('%H:%M')}"
                        + (f"\nAktuelle Tagesdaten:{today_block}" if today_block else "")
                    )
                    resp = await S.ai.messages.create(
                        model=S.HAIKU_MODEL,
                        max_tokens=600,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_msg}],
                    )
                    brief = llm_text(resp).strip()
                    if brief:
                        log.info(f"morning_brief_scheduler: sending brief: {brief[:80]!r}")
                        await _proactive_handler(brief)
                except Exception as e:
                    log.warning(f"morning_brief_scheduler: send failed: "
                                f"{type(e).__name__}: {e}")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Proactive briefs (issue #46): Jarvis self-triggers updates at the times in
# settings.PROACTIVE_BRIEFS_TIMES. Each slot fires at most once per day.
# Server.py registers a callback that knows how to push to active clients
# (we keep zero coupling from scheduler to the WebSocket layer).
# ---------------------------------------------------------------------------

# Slot-type system prompts. The scheduler picks the one that matches the
# closest configured time; falls back to the generic short update prompt.
_PROACTIVE_PROMPTS = {
    "12:30": (
        "Du bist Jarvis. Es ist Mittag. Der Nutzer arbeitet. Erinnere {addr} "
        "an die Mittagspause. 3-4 Saetze: erst eine trockene Mittagspausen-"
        "Aufforderung im Butler-Stil; dann nenne ALLE offenen Aufgaben fuer heute "
        "(siehe AKTUELLE DATEN — PFLICHT, auch wenn die Liste lang ist) und naechste "
        "Termine bis Tagesende. Wenn keine Aufgaben oder Termine: kurzes Lob im "
        "Jarvis-Stil. Keine ACTION-Tags, alles wird vorgelesen."
    ),
    "16:00": (
        "Du bist Jarvis. Nachmittagsupdate fuer {addr}. KURZ (2-3 Saetze): "
        "ein knapper Status-Check im Butler-Ton, "
        "dann offene Aufgaben fuer heute (siehe AKTUELLE DATEN) und Termine "
        "die noch bis Tagesende anstehen. "
        "Falls Gesundheitsdaten vorhanden und Bewegungsring unter 50 Prozent: "
        "eine trockene Aufforderung zu einer kurzen Aktivitaet einbauen — "
        "konkret und witzig, nicht predighaft "
        "('Ihr Bewegungsring sieht aus als haette er aufgegeben. "
        "Fuenfzehn Minuten frische Luft wuerden da Wunder wirken.'). "
        "Bei Verbesserung gegenueber gestern: kurz positiv vermerken. "
        "Keine ACTION-Tags."
    ),
    "18:00": (
        "Du bist Jarvis. Es ist 18 Uhr — Feierabend-Erinnerung fuer {addr}. "
        "KURZ (2-3 Saetze): trocken-bestimmt auf Feierabend hinweisen "
        "(\"Erholung ist Pflicht\"-Tonalitaet), dann erwaehne kurz noch offene "
        "Aufgaben (warten bis morgen) und ob heute Abend noch ein Termin "
        "ansteht. Keine ACTION-Tags."
    ),
}

_DEFAULT_PROACTIVE_PROMPT = (
    "Du bist Jarvis. Knappes Tages-Update fuer {addr}: 1-2 Saetze, offene "
    "Aufgaben heute + verbleibende Termine. Keine ACTION-Tags."
)

_proactive_handler = None


def register_proactive_handler(fn) -> None:
    """server.py registers its broadcaster here so scheduler stays
    decoupled from the WebSocket layer."""
    global _proactive_handler
    _proactive_handler = fn


async def _generate_proactive_message(slot: str) -> str:
    """Refresh today's data and ask Claude for the spoken update."""
    await asyncio.gather(refresh_data(), refresh_morning_brief_data())
    system_prompt = _PROACTIVE_PROMPTS.get(slot, _DEFAULT_PROACTIVE_PROMPT).format(
        addr=pick_address(),
    )
    today_block = ""
    if S.TODAY_TASKS:
        today_block += f"\nHeutige Aufgaben:\n{S.TODAY_TASKS}"
    if S.TODAY_EVENTS:
        today_block += f"\nHeutige Termine:\n{S.TODAY_EVENTS}"
    if S.OPEN_PROMISES:
        today_block += f"\n{S.OPEN_PROMISES}"
    if S.PENDING_FOLLOWUPS:
        today_block += f"\n{S.PENDING_FOLLOWUPS}"
    if S.UPCOMING_DEADLINES:
        today_block += f"\n{S.UPCOMING_DEADLINES}"
    _health_src = S.HEALTH_INFO_PREV if S.HEALTH_INFO_PREV else S.HEALTH_INFO
    if _health_src:
        health_block = health_tools.format_for_brief(_health_src, S.ACTIVITY_GOAL_KCAL)
        if health_block:
            _label = "Gesundheitsdaten gestern" if S.HEALTH_INFO_PREV else "Gesundheitsdaten"
            today_block += f"\n{_label}:\n{health_block}"
    user_msg = f"Aktuelle Tagesdaten:{today_block or ' (keine offenen Punkte)'}"
    resp = await S.ai.messages.create(
        model=S.HAIKU_MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    return llm_text(resp).strip()


async def memory_reindex_scheduler() -> None:
    """Long-running task: täglich um 03:00 Uhr den Vektorspeicher neu aufbauen.

    Läuft im Hintergrund — wenn ChromaDB/sentence-transformers nicht
    installiert sind, loggt memory_search.reindex_all() lediglich einen
    Hinweis und kehrt sofort zurück.
    """
    import memory_search
    triggered_today = ""
    _REINDEX_HOUR = 3  # 03:00 Uhr
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            if now.hour >= _REINDEX_HOUR and triggered_today != today:
                triggered_today = today
                log.info("memory_reindex_scheduler: Starte nächtlichen Reindex …")
                try:
                    await memory_search.reindex_all()
                except Exception as e:
                    log.warning(
                        f"memory_reindex_scheduler: reindex_all fehlgeschlagen: "
                        f"{type(e).__name__}: {e}"
                    )
        except Exception as e:
            log.warning(f"memory_reindex_scheduler loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(60)



# ---------------------------------------------------------------------------
# Abschluss-Ritual (Issue #121): Tagesabschluss-Briefing um EVENING_HOUR.
# Fires at most once per day and only when JARVIS was actually used today
# (at least one user interaction tracked via session history).
# ---------------------------------------------------------------------------

_EVENING_BRIEF_PROMPT = (
    "Du bist Jarvis. Tagesabschluss-Briefing fuer {addr}. "
    "Ton: warm, anerkennend, leicht butlerhaft — so wie am Ende eines langen Arbeitstages. "
    "Pflicht-Elemente in dieser Reihenfolge, ALLE in fliessender Sprache ohne Aufzaehlung:\n"
    "1. Kurze Bilanz: wie viele Aufgaben erledigt (falls > 0), "
    "wie viele Mails beantwortet (falls bekannt).\n"
    "2. Falls noch offene Aufgaben vorhanden: EINE hervorheben "
    "('Ein offener Punkt bleibt: ...').\n"
    "3. Falls ein Termin morgen frueh: knapp erwaehnen.\n"
    "4. Abschluss-Satz im Jarvis-Stil: 'Gute Arbeit heute — schoenen Abend, {addr}.' "
    "(oder aequivalent, leicht variiert).\n"
    "NICHT mehr als 4-5 Saetze gesamt. KEINE ACTION-Tags."
)


def _format_jarvis_actions(summary: dict) -> str:
    """Formt die Tages-Aktivitaets-Zusammenfassung in einen lesbaren Text.

    Args:
        summary: Rueckgabewert von activity_log.get_daily_summary().

    Returns:
        Kommaseparierter Satz der Aktionen oder leerer String wenn nichts
        protokolliert wurde.
    """
    parts = []
    if summary["mail_triage"] > 0:
        n = summary["mail_triage"]
        parts.append(f"{n} Mail{'s' if n != 1 else ''} sortiert")
    if summary["followup_saved"] > 0:
        n = summary["followup_saved"]
        parts.append(f"{n} Follow-up{'s' if n != 1 else ''} vorgemerkt")
    if summary["followup_resolved"] > 0:
        n = summary["followup_resolved"]
        parts.append(f"{n} Antwort{'en' if n != 1 else ''} erkannt")
    if summary["contact_enriched"] > 0:
        n = summary["contact_enriched"]
        parts.append(f"{n} Kontakt{'e' if n != 1 else ''} aktualisiert")
    for name in summary["draft_created"]:
        parts.append(f"Geburtstagsentwurf fuer {name} vorbereitet")
    for ev in summary["calendar_added"]:
        parts.append(f"Termin '{ev}' eingetragen")
    if not parts:
        return ""
    return ", ".join(parts[:-1]) + (" und " + parts[-1] if len(parts) > 1 else parts[0])


async def build_evening_brief() -> str:
    """Sammle die Tageszusammenfassung und formuliere das Abschluss-Ritual.

    Returns:
        Formulierter Text fuer das Abschluss-Briefing, oder leerer String
        bei Fehler.
    """
    addr = pick_address()

    # 1. Erledigte Aufgaben heute (aus Zaehler in settings)
    tasks_done = S.TASKS_COMPLETED_TODAY

    # 2. Noch offene Aufgaben (aus Cache — refresh_today_tasks wurde vorher
    #    aufgerufen, sodass der Cache aktuell ist)
    open_tasks = S.TODAY_TASKS  # bereits gefiltert: nur heute/ueberfaellig

    # 3. Erster Termin morgen
    tomorrow_event = ""
    try:
        events_text = await google_calendar_tools.get_events(days=2, max_results=20)
        if events_text and events_text != "KEINE_TERMINE":
            tomorrow = datetime.date.today() + datetime.timedelta(days=1)
            tomorrow_short = tomorrow.strftime("%d.%m.")
            for line in events_text.splitlines():
                if line.startswith("•") and tomorrow_short in line:
                    tomorrow_event = line.strip().lstrip("•").strip()
                    break
    except Exception as e:
        log.warning(
            f"build_evening_brief: calendar fetch failed: {type(e).__name__}: {e}"
        )

    # 4. JARVIS Eigenleistungs-Log (Issue #145)
    import activity_log as _al
    _jarvis_summary = _al.get_daily_summary()
    _jarvis_actions_text = _format_jarvis_actions(_jarvis_summary)

    # Kontext fuer den LLM zusammenstellen
    parts: list[str] = []
    if tasks_done > 0:
        parts.append(f"Erledigte Aufgaben heute: {tasks_done}")
    if open_tasks:
        first_open = open_tasks.splitlines()[0].strip().lstrip("•").strip()
        parts.append(f"Offene Aufgabe: {first_open}")
    if tomorrow_event:
        parts.append(f"Erster Termin morgen: {tomorrow_event}")

    # JARVIS-Eigenleistung: entweder konkrete Aktionen oder Selbstironie-Hinweis
    if _jarvis_actions_text:
        parts.append(f"JARVIS hat heute autonom folgendes erledigt: {_jarvis_actions_text}")
        jarvis_instruction = (
            "Baue die JARVIS-Eigenleistung als natuerlichen Satz ein "
            "(z.B. 'Heute habe ich 14 Mails sortiert und einen Geburtstagsentwurf vorbereitet.'). "
            "Ton: leicht stolz, aber diskret butlerhaft."
        )
    else:
        jarvis_instruction = (
            "JARVIS hat heute nichts autonom erledigt. "
            "Formuliere eine kurze selbstironische Bemerkung im Butler-Stil "
            "(trocken-britisch, ein Satz). "
            "Beispiel: 'Ich bin heute weitgehend dekorativer Natur gewesen.'"
        )

    user_content = (
        "Tagesdaten:\n" + "\n".join(parts)
        if parts
        else "Keine besonderen Tagesdaten vorhanden."
    )
    user_content += f"\n\nHinweis fuer JARVIS-Eigenleistungs-Satz: {jarvis_instruction}"

    system_prompt = _EVENING_BRIEF_PROMPT.format(addr=addr)
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        brief_text = trim_to_complete_sentences(llm_text(resp).strip())
        # Issue #199: JARVIS-Abschlusszitat im Marvel-Stil (Closing-Quote)
        _closing = jarvis_quotes.quote("closing")
        if _closing and brief_text:
            brief_text = brief_text + " " + _closing
        # Issue #200: optionales Film-Abschlusszitat mit Cooldown (30 % Chance)
        _closing_film = jarvis_quotes.quote_maybe("closing_film", 0.3)
        if _closing_film and brief_text:
            brief_text = brief_text + " " + _closing_film
        return brief_text
    except Exception as e:
        log.warning(
            f"build_evening_brief: LLM call failed: {type(e).__name__}: {e}"
        )
        return ""


async def evening_brief_scheduler() -> None:
    """Long-running task: deliver the Abschluss-Ritual once per day at
    S.EVENING_HOUR, but only if JARVIS was actually used today.

    Uses the same '>=' pattern as morning_brief_scheduler so the brief
    still fires if the loop woke up a few minutes after EVENING_HOUR:00.
    """
    _now_init = datetime.datetime.now()
    triggered_today = (
        datetime.date.today().isoformat() if _now_init.hour >= S.EVENING_HOUR else ""
    )
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            if now.hour >= S.EVENING_HOUR and triggered_today != today:
                triggered_today = today
                # Only fire when JARVIS was used today.
                from conversation import conversations
                used_today = bool(conversations) or S.TASKS_COMPLETED_TODAY > 0
                if not used_today:
                    log.info(
                        "evening_brief_scheduler: JARVIS nicht genutzt heute, "
                        "Abschluss-Briefing wird uebersprungen"
                    )
                elif _proactive_handler is None:
                    log.info(
                        "evening_brief_scheduler: kein Handler registriert, skip"
                    )
                else:
                    log.info(
                        "evening_brief_scheduler: Abschluss-Ritual wird erstellt"
                    )
                    try:
                        # Refresh open tasks so the brief is current.
                        await refresh_today_tasks()
                        brief = await build_evening_brief()
                        if brief:
                            log.info(
                                f"evening_brief_scheduler: sending: {brief[:80]!r}"
                            )
                            await _proactive_handler(brief)
                    except Exception as e:
                        log.warning(
                            f"evening_brief_scheduler: failed: "
                            f"{type(e).__name__}: {e}"
                        )
        except Exception as e:
            log.warning(
                f"evening_brief_scheduler loop error: {type(e).__name__}: {e}"
            )
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Abend-Zusammenfassung (Issue #164): taeglich 20:30 — was hat JARVIS heute
# gelernt? Quellen: Mail-Intelligence, Google Calendar, Todoist.
# ---------------------------------------------------------------------------

_EVENING_SUMMARY_PROMPT = (
    "Du bist Jarvis. Erstelle eine knappe Abendzusammenfassung fuer {addr}. "
    "Ton: trocken-butlerhaft, sachlich, leicht ironisch wenn passend. "
    "Format: fliessendes Deutsch, keine Listen, keine Markdown-Formatierung, "
    "keine eckigen Klammern. Text wird vorgelesen (TTS), also natuerliche Saetze. "
    "Maximal 5-7 Saetze insgesamt. "
    "Inhalt (nur was vorhanden ist): "
    "Was war heute wichtig laut E-Mail-Postfach (Kerninhalte kurz nennen); "
    "welche Termine haben stattgefunden; "
    "wie viele Aufgaben waren erledigt oder offen. "
    "Falls alle Quellen leer sind: einen kurzen natuerlichen Satz wie "
    "'Heute war es ruhig — die Postfaecher haben nichts Neues gebracht, "
    "der Kalender war frei.' "
    "KEINE Begruessung wie 'Guten Abend'. Starte direkt mit dem Inhalt. "
    "Beende JEDEN Satz mit einem Punkt."
)


async def generate_evening_summary(detailed: bool = False) -> str:
    """Erstelle eine Zusammenfassung des heutigen Tages aus drei Quellen.

    Quellen: Mail-Intelligence (letzte 24h), Google Calendar (heute),
    Todoist (erledigte + offene Aufgaben). Alle Quellen werden mit
    graceful degradation behandelt — schlaegt eine Quelle fehl, werden
    die anderen trotzdem verarbeitet.

    Args:
        detailed: Falls True, werden bis zu 20 Mail-Eintraege einbezogen
            (statt 5) und main_info-Felder angehaengt. Kanal-Routing und
            Kalender/Todoist-Ausgabe sind identisch — nur die Mail-Tiefe
            aendert sich. Zukuenftig erweiterbar.

    Returns:
        Formulierter Text fuer TTS oder leerer String bei Fehler.
    """
    import mail_intelligence

    parts: list[str] = []

    # 1. Mail-Intelligence: Wissen aus den letzten 24h
    try:
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None,
            lambda: mail_intelligence.get_recent_knowledge(days=1, limit=20),
        )
        if rows:
            # Kerninformationen extrahieren: Absender + Betreff + Hauptaussage
            mail_lines: list[str] = []
            limit = 20 if detailed else 5
            for r in rows[:limit]:
                sender = r.get("sender_name") or r.get("sender_email", "")
                subject = r.get("subject", "")
                main_info = r.get("main_info", "")
                if subject or main_info:
                    entry = f"{sender}: {subject}" if sender else subject
                    if main_info and detailed:
                        entry += f" — {main_info[:120]}"
                    mail_lines.append(entry)
            if mail_lines:
                parts.append("MAILS (letzte 24h):\n" + "\n".join(mail_lines))
    except Exception as e:
        log.warning(f"generate_evening_summary: mail_intelligence failed: {type(e).__name__}: {e}")

    # 2. Kalender: Termine die heute stattgefunden haben.
    # time_min=today_start stellt sicher dass auch Termine vor 20:30 enthalten
    # sind (get_events verwendet sonst 'jetzt' als Untergrenze).
    try:
        import re as _re
        today = datetime.date.today()
        today_short = today.strftime("%d.%m.")
        # UTC-aware damit Google Calendar API kein HTTP 400 zurueckgibt
        today_start = datetime.datetime.combine(
            today, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        today_end = today_start + datetime.timedelta(days=1)

        cal_events: list[str] = []

        # 2a. Google Calendar (primaer)
        cal_text = await google_calendar_tools.get_events(
            days=1, max_results=30, time_min=today_start
        )
        if cal_text and cal_text != "KEINE_TERMINE":
            _today_re = _re.compile(r"^•\s+\w+\s+" + _re.escape(today_short))
            cal_events.extend(
                line.strip() for line in cal_text.splitlines()
                if _today_re.match(line.strip())
            )

        # 2b. DIHAG-Kalender (Microsoft/ICS), falls konfiguriert
        if S.MICROSOFT_CALENDAR_ICS_URL:
            import microsoft_calendar_tools
            ms_text = await microsoft_calendar_tools.get_events(
                days=1, time_min=today_start, time_max=today_end
            )
            if ms_text and not ms_text.startswith(
                ("Keine DIHAG", "DIHAG-Kalender nicht", "DIHAG-Kalender konnte")
            ):
                for line in ms_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("•"):
                        cal_events.append(stripped)

        if cal_events:
            parts.append("TERMINE HEUTE:\n" + "\n".join(cal_events))
    except Exception as e:
        log.warning(f"generate_evening_summary: calendar failed: {type(e).__name__}: {e}")

    # 3. Todoist: erledigte + offene Aufgaben heute
    try:
        tasks_done = S.TASKS_COMPLETED_TODAY
        open_tasks_text = S.TODAY_TASKS  # cache aus letztem Refresh

        task_parts: list[str] = []
        if tasks_done > 0:
            task_parts.append(f"Erledigt: {tasks_done}")
        if open_tasks_text:
            open_lines = [
                line.strip() for line in open_tasks_text.splitlines()
                if line.strip()
            ]
            if open_lines:
                task_parts.append("Noch offen: " + "; ".join(open_lines[:5]))
        if task_parts:
            parts.append("AUFGABEN:\n" + "\n".join(task_parts))
    except Exception as e:
        log.warning(f"generate_evening_summary: todoist failed: {type(e).__name__}: {e}")

    # 4. PDF-Analysen des Tages (via E-Mail-Trigger)
    try:
        import pdf_tools as _pt
        pdf_results = _pt.pop_daily_pdf_results()
        if pdf_results:
            parts.append("PDF-ANALYSEN HEUTE:\n" + "\n".join(pdf_results))
    except Exception as e:
        log.warning(f"generate_evening_summary: pdf_results failed: {type(e).__name__}: {e}")

    addr = pick_address()
    system_prompt = _EVENING_SUMMARY_PROMPT.format(addr=addr)
    user_content = (
        "Tagesdaten:\n\n" + "\n\n".join(parts)
        if parts
        else "Keine Tagesdaten verfuegbar."
    )
    max_tokens = 700 if detailed else 500

    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return trim_to_complete_sentences(llm_text(resp).strip())
    except Exception as e:
        log.warning(
            f"generate_evening_summary: LLM call failed: {type(e).__name__}: {e}"
        )
        return ""


async def evening_summary_scheduler() -> None:
    """Long-running task: taeglich um 20:30 Uhr Abendzusammenfassung senden.

    Kanal-Logik:
    - Orb verbunden (aktive WebSocket-Sitzung): PENDING_PROACTIVE setzen
      und per Sprachausgabe fragen ("Haben Sie einen Moment?"). Die
      bestehende PROACTIVE_DELIVER-Action liefert die Zusammenfassung dann
      aus. Catrin kann ablehnen -> Telegram-Fallback (bereits in
      _broadcast_proactive implementiert).
    - Orb nicht verbunden: direkt per Telegram senden.

    Verwendet das '>=' Muster aus morning_brief_scheduler: wenn der
    Server um genau 20:30 schlaeft und um 20:35 aufwacht, wird die
    Zusammenfassung trotzdem gesendet.
    """
    _TRIGGER_HHMM = "20:30"
    _now_init = datetime.datetime.now()
    triggered_today = (
        datetime.date.today().isoformat()
        if _now_init.strftime("%H:%M") >= _TRIGGER_HHMM
        else ""
    )
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            current_hhmm = now.strftime("%H:%M")

            if current_hhmm >= _TRIGGER_HHMM and triggered_today != today:
                triggered_today = today
                log.info("evening_summary_scheduler: 20:30 Trigger — Abendzusammenfassung")
                try:
                    # Aufgaben-Cache aktualisieren bevor Zusammenfassung generiert wird
                    await refresh_today_tasks()
                    summary = await generate_evening_summary()
                    if not summary:
                        log.info("evening_summary_scheduler: leere Zusammenfassung, kein Push")
                    elif _proactive_handler is not None:
                        log.info(
                            f"evening_summary_scheduler: sende via proactive handler: "
                            f"{summary[:80]!r}"
                        )
                        await _proactive_handler(summary)
                    else:
                        # Kein Orb-Handler registriert — direkt per Telegram
                        log.info(
                            "evening_summary_scheduler: kein proactive_handler, "
                            "sende direkt per Telegram"
                        )
                        try:
                            import telegram_bot
                            await telegram_bot.send_user_text(summary)
                        except Exception as e:
                            log.warning(
                                f"evening_summary_scheduler: Telegram failed: "
                                f"{type(e).__name__}: {e}"
                            )
                except Exception as e:
                    log.warning(
                        f"evening_summary_scheduler: failed: "
                        f"{type(e).__name__}: {e}"
                    )
        except Exception as e:
            log.warning(
                f"evening_summary_scheduler loop error: {type(e).__name__}: {e}"
            )
        await asyncio.sleep(60)


_PROMISE_FOLLOWUP_SLOT = "16:00"


async def _build_promise_followup_suffix() -> str:
    """Prueft ob eine ueberfaellige Versprechen-Nachfrage gesendet werden soll.

    Gibt einen fertigen Satz zurueck der an die proaktive Nachricht angehaengt
    werden kann, oder einen leeren String wenn keine Nachfrage noetig ist.
    Markiert das heutige Datum wenn eine Nachfrage generiert wird.
    """
    try:
        import promise_tracker
        if await promise_tracker.was_followup_sent_today():
            return ""
        promise = await promise_tracker.get_oldest_overdue_promise(min_age_days=2)
        if promise is None:
            return ""
        age = promise["age_label"]
        text = promise["text"]
        suffix = (
            f" Uebrigens — Sie wollten {age} noch: {text}. "
            f"Ist das inzwischen erledigt?"
        )
        await promise_tracker.mark_followup_sent_today()
        log.info(f"promise_tracker: Nachfrage generiert fuer promise #{promise['id']}")
        return suffix
    except Exception as e:
        log.warning(
            f"_build_promise_followup_suffix failed: {type(e).__name__}: {e}"
        )
        return ""


async def proactive_briefs_scheduler() -> None:
    """Long-running task: fire each configured slot once per day.

    Uses '>=' on the HH:MM string (not '=='): if the Mac was asleep at
    exactly the configured time and the loop wakes up a few minutes later,
    the slot still fires today — same approach as morning_brief_scheduler.

    At the _PROMISE_FOLLOWUP_SLOT (16:00) a promise followup question is
    appended when there is an overdue open promise and no followup was sent
    today yet (Issue #124).
    """
    # Pre-fill all slots already past today so a restart doesn't re-fire them.
    _now_init = datetime.datetime.now()
    _today_init = datetime.date.today().isoformat()
    _hhmm_init = _now_init.strftime("%H:%M")
    triggered: dict[str, str] = {
        slot: _today_init
        for slot in S.PROACTIVE_BRIEFS_TIMES
        if _hhmm_init >= slot
    }
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            current_hhmm = now.strftime("%H:%M")
            # Issue #145: Proaktive Briefs (12:30, 16:00, 18:00) an Wochenenden
            # nicht senden — Abend-Briefing laeuft separat via evening_brief_scheduler.
            if now.weekday() in (5, 6):
                await asyncio.sleep(30)
                continue
            for slot in S.PROACTIVE_BRIEFS_TIMES:
                if current_hhmm < slot:
                    continue
                if triggered.get(slot) == today:
                    continue
                triggered[slot] = today
                if _proactive_handler is None:
                    log.info(f"proactive {slot}: no handler registered, skipping")
                    continue
                log.info(f"proactive {slot}: generating message")
                try:
                    message = await _generate_proactive_message(slot)
                    # Issue #124: at the followup slot append a promise
                    # followup question (max once per day, min 2 days old).
                    if slot == _PROMISE_FOLLOWUP_SLOT:
                        followup = await _build_promise_followup_suffix()
                        if followup:
                            message = message + followup
                    log.info(f"proactive {slot}: '{message[:80]}'")
                    await _proactive_handler(message)
                except Exception as e:
                    log.warning(f"proactive {slot} failed: {type(e).__name__}: {e}")
        except Exception as e:
            log.warning(f"proactive scheduler loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Bring!-Monitor (Issue #123): alle 15 Minuten neue Einkaufsartikel pruefen
# und Angebots-Treffer proaktiv melden.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Speiseplanung (Issue #125): Wochenplan donnerstags + taegliche Rezepterinnerung
# ---------------------------------------------------------------------------

async def meal_plan_scheduler() -> None:
    """Long-running task: jeden Donnerstag um 09:00 Uhr Speisenplan generieren.

    Ablauf:
    1. Frage an Catrin senden (Telegram + Orb): besondere Wuensche?
    2. Bis zu 15 Minuten auf Antwort warten (S.MEAL_PLAN_AWAITING_WISHES).
    3. Plan mit Wuenschen (oder ohne, falls keine Antwort) generieren und senden.

    Startup-Guard (Issue #179): Falls beim Start bereits ein Plan fuer
    die aktuelle ISO-Woche im Cache liegt (erkannt via
    meal_plan.get_generated_week()), wird triggered_for_week vorbelegt
    und der Donnerstag-Trigger fuer diese Woche uebersprungen.
    """
    import meal_plan as _mp_guard
    _startup_week = _mp_guard.get_generated_week()
    if _startup_week:
        log.info(
            f"meal_plan_scheduler: Plan fuer {_startup_week!r} bereits im Cache — "
            "Donnerstag-Trigger fuer diese Woche wird uebersprungen"
        )
    triggered_for_week = _startup_week  # ISO-Woche ("2026-W18") als Dedup-Guard
    _TRIGGER_WEEKDAY = 3   # Donnerstag
    _TRIGGER_TIME = "09:00"
    _WISHES_TIMEOUT = 900   # 15 Minuten

    while True:
        try:
            now = datetime.datetime.now()
            iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
            current_hhmm = now.strftime("%H:%M")

            if (now.weekday() == _TRIGGER_WEEKDAY
                    and current_hhmm >= _TRIGGER_TIME
                    and triggered_for_week != iso_week):
                # triggered_for_week is set BEFORE the attempt. If sending the
                # question fails, we do NOT retry this week (fail-once-per-week).
                # This prevents duplicate questions if the server restarts mid-day.
                triggered_for_week = iso_week
                log.info("meal_plan_scheduler: Donnerstag-Trigger — Wunsch-Abfrage")
                try:
                    import meal_plan as _mp
                    import telegram_bot

                    # Wuensche zuruecksetzen und auf Antwort warten
                    S.MEAL_PLAN_WISHES = ""
                    S.MEAL_PLAN_WISHES_EVENT.clear()
                    S.MEAL_PLAN_AWAITING_WISHES = True
                    question = (
                        "Ich erstelle gleich den Speiseplan fuer naechste Woche. "
                        "Hast du besondere Wuensche?"
                    )
                    if _proactive_handler:
                        await _proactive_handler(question)
                    else:
                        await telegram_bot.send_user_text(question)

                    # Auf Antwort warten (max. 15 Minuten) — event-basiert statt
                    # polling, damit keine Antwort verloren geht (Issue #170).
                    try:
                        await asyncio.wait_for(
                            S.MEAL_PLAN_WISHES_EVENT.wait(),
                            timeout=_WISHES_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        pass
                    S.MEAL_PLAN_AWAITING_WISHES = False
                    S.MEAL_PLAN_WISHES_EVENT.clear()

                    wishes = S.MEAL_PLAN_WISHES.strip()
                    if wishes:
                        log.info(f"meal_plan_scheduler: Wuensche empfangen: '{wishes[:80]}'")
                    else:
                        log.info("meal_plan_scheduler: keine Wuensche, generiere ohne")

                    plan = await _mp.generate_meal_plan(wishes=wishes)
                    if plan:
                        # include_today_recipe=False: plan covers next week, not today.
                        text = _mp.format_meal_plan_telegram(include_today_recipe=False)
                        if _proactive_handler:
                            await _proactive_handler(text)
                        else:
                            await telegram_bot.send_user_text(text)
                        log.info("meal_plan_scheduler: Plan gesendet")
                    else:
                        log.warning("meal_plan_scheduler: Plan-Generierung lieferte leeres Ergebnis")

                    # Issue #204: Donnerstags-Vorratscheck — fast_leer und leer
                    # Artikel auf Bring! setzen und Catrin benachrichtigen.
                    try:
                        import pantry as _pantry
                        import bring_tools as _bt
                        fast_leer = _pantry.get_items_by_status("fast_leer")
                        leer = _pantry.get_items_by_status("leer")
                        low = leer + fast_leer
                        if low:
                            names = ", ".join(low)
                            if S.BRING_EMAIL and S.BRING_PASSWORD:
                                await _bt.bring_add_items(low)
                            hint = (
                                f"Vorratscheck: Diese Stammzutaten sollten nachgekauft werden: "
                                f"{names}"
                            )
                            if _proactive_handler:
                                await _proactive_handler(hint)
                            else:
                                await telegram_bot.send_user_text(hint)
                            log.info(
                                f"meal_plan_scheduler: Vorratscheck — {len(low)} Artikel gemeldet"
                            )
                    except Exception as _pe:
                        log.warning(f"meal_plan_scheduler: Vorratscheck failed: {_pe}")
                except BaseException as e:
                    # BaseException (not just Exception) catches CancelledError so
                    # the flag is never left stuck True after task cancellation.
                    S.MEAL_PLAN_AWAITING_WISHES = False
                    S.MEAL_PLAN_WISHES_EVENT.clear()
                    log.warning(
                        f"meal_plan_scheduler: Fehler: {type(e).__name__}: {e}"
                    )
                    if not isinstance(e, Exception):
                        raise  # Re-raise CancelledError so asyncio can clean up
        except Exception as e:
            log.warning(f"meal_plan_scheduler loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(60)


async def meal_plan_reminder_scheduler() -> None:
    """Long-running task: taeglich um S.MEAL_PLAN_REMINDER_TIME Uhr (Standard
    17:30) das heutige Rezept per Telegram versenden.

    Sendet nur wenn ein Speisenplan vorhanden ist und ein Eintrag fuer
    heute existiert. Kein Push wenn quiet hours aktiv.
    """
    _now_init = datetime.datetime.now()
    triggered_today = (
        datetime.date.today().isoformat()
        if _now_init.strftime("%H:%M") >= S.MEAL_PLAN_REMINDER_TIME
        else ""
    )

    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            current_hhmm = now.strftime("%H:%M")

            if (current_hhmm >= S.MEAL_PLAN_REMINDER_TIME
                    and triggered_today != today):
                triggered_today = today
                log.info("meal_plan_reminder_scheduler: taegliche Rezept-Erinnerung")
                try:
                    import meal_plan as _mp
                    import telegram_bot
                    recipe_text = await _mp.get_today_recipe()
                    if recipe_text:
                        await telegram_bot.send_user_text(recipe_text)
                        log.info(
                            "meal_plan_reminder_scheduler: Rezept per Telegram gesendet"
                        )
                    else:
                        log.info(
                            "meal_plan_reminder_scheduler: kein Rezept fuer heute "
                            "— kein Push"
                        )
                except Exception as e:
                    log.warning(
                        f"meal_plan_reminder_scheduler: Fehler: "
                        f"{type(e).__name__}: {e}"
                    )
        except Exception as e:
            log.warning(
                f"meal_plan_reminder_scheduler loop error: {type(e).__name__}: {e}"
            )
        await asyncio.sleep(60)


_bring_known_items: set[str] = set()
_BRING_MONITOR_INTERVAL = 15 * 60  # 15 Minuten in Sekunden


async def bring_monitor_scheduler() -> None:
    """Long-running task: alle 15 Minuten die Bring!-Liste pruefen.

    Vergleicht die aktuelle Liste mit den bekannten Eintraegen. Neue
    Artikel werden gegen S.WEEKLY_OFFERS geprueft; bei Treffer wird eine
    proaktive Benachrichtigung ueber _proactive_handler gesendet.

    Wird nur gestartet wenn BRING_EMAIL und BRING_PASSWORD konfiguriert
    sind. Fehler werden geloggt und uebersprungen — kein Crash.
    """
    global _bring_known_items

    if not S.BRING_EMAIL or not S.BRING_PASSWORD:
        log.info("bring_monitor_scheduler: BRING_EMAIL/PASSWORD fehlt — Scheduler inaktiv")
        return

    log.info("bring_monitor_scheduler: gestartet (alle 15 Minuten)")
    while True:
        try:
            import bring_tools
            current_items = await bring_tools.bring_get_items()
            current_set = {item.strip() for item in current_items if item.strip()}

            if _bring_known_items:
                # Neue Artikel = in current aber nicht im letzten bekannten Stand
                new_items = list(current_set - _bring_known_items)
                if new_items and S.WEEKLY_OFFERS and _proactive_handler is not None:
                    offer_hint = await bring_tools.bring_check_offers(new_items)
                    if offer_hint:
                        msg = (
                            f"Neue Artikel auf der Einkaufsliste: "
                            f"{', '.join(new_items)}. {offer_hint}."
                        )
                        log.info(f"bring_monitor_scheduler: Angebots-Treffer: {msg[:120]}")
                        try:
                            await _proactive_handler(msg)
                        except Exception as e:
                            log.warning(
                                f"bring_monitor_scheduler: _proactive_handler failed: "
                                f"{type(e).__name__}: {e}"
                            )

            _bring_known_items = current_set

        except Exception as e:
            log.warning(f"bring_monitor_scheduler: {type(e).__name__}: {e}")

        await asyncio.sleep(_BRING_MONITOR_INTERVAL)


# ---------------------------------------------------------------------------
# Geburtstags-Entwurf-Scheduler (Issue #144): freitags 08:00 runde
# Geburtstage als IMAP-Entwurf anlegen und per Telegram melden.
# ---------------------------------------------------------------------------

_BIRTHDAY_DRAFT_CONGRATULATION_PROMPT = """\
Du bist Jarvis und schreibst im Namen von Catrin Essberger eine \
Geburtstagsglueckwunsch-Mail.
Empfaenger: {name}, wird {age} Jahre alt.
Beziehung: {funktion} (falls leer: foermlich-professionell).
Kurz, persoenlich, 2-3 Saetze. Kein generisches 'Herzlichen Glueckwunsch \
zum Geburtstag' als Ersteinstieg.
Ton: {tone}.
Betreff: bitte auch vorschlagen.
Antwortformat: JSON {{"subject": "...", "body": "..."}}"""


async def birthday_draft_scheduler() -> None:
    """Long-running task: jeden Freitag um 08:00 runde Geburtstage als
    IMAP-Entwurf ablegen und Catrin per Telegram benachrichtigen.

    Voraussetzungen:
    - S.BIRTHDAY_ROUND wurde von refresh_birthday_reminders() befuellt
    - mail_actions.MAIL_MONITOR_ACCOUNTS hat mindestens einen Eintrag
    - birthday_drafts.py verhindert doppelte Entwuerfe pro Kontakt + Jahr

    Kein automatisches Senden — Catrin gibt manuell frei.
    """
    triggered_for_week: str = ""  # ISO-Woche als Dedup-Guard
    _TRIGGER_WEEKDAY = 4   # Freitag
    _TRIGGER_TIME = "08:00"

    while True:
        try:
            now = datetime.datetime.now()
            iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
            current_hhmm = now.strftime("%H:%M")

            if (now.weekday() == _TRIGGER_WEEKDAY
                    and current_hhmm >= _TRIGGER_TIME
                    and triggered_for_week != iso_week):
                triggered_for_week = iso_week
                log.info("birthday_draft_scheduler: Freitag-Trigger — runde Geburtstage pruefen")

                try:
                    await _process_birthday_drafts(now)
                except Exception as e:
                    log.warning(
                        f"birthday_draft_scheduler: _process_birthday_drafts failed: "
                        f"{type(e).__name__}: {e}"
                    )
        except Exception as e:
            log.warning(f"birthday_draft_scheduler loop error: {type(e).__name__}: {e}")

        await asyncio.sleep(60)


async def _process_birthday_drafts(now: datetime.datetime) -> None:
    """Bearbeite S.BIRTHDAY_ROUND: erstelle IMAP-Entwuerfe fuer alle
    runden Geburtstage bei denen noch kein Entwurf existiert."""
    import email.utils as _email_utils
    import re as _re
    from email.message import EmailMessage as _EmailMessage

    import birthday_drafts
    import mail_actions
    import persons_db

    round_str = S.BIRTHDAY_ROUND
    if not round_str:
        log.info("birthday_draft_scheduler: S.BIRTHDAY_ROUND leer — nichts zu tun")
        return

    # Format: "Runde Geburtstage diese Woche: • Max Mueller (50, Freitag 06.06.)"
    # Extrahiere Eintraege: Name, Alter, Datum
    entries_raw = round_str.replace("Runde Geburtstage diese Woche:", "").strip()
    # Trenne an "• " (Bullet), ueberspringe leere
    entry_list = [e.strip() for e in entries_raw.split("•") if e.strip()]

    if not entry_list:
        log.info("birthday_draft_scheduler: keine Eintraege in BIRTHDAY_ROUND")
        return

    # Sicherstellen dass mindestens ein IMAP-Konto konfiguriert ist
    if not S.MAIL_MONITOR_ACCOUNTS:
        log.warning("birthday_draft_scheduler: keine MAIL_MONITOR_ACCOUNTS — Abbruch")
        return

    account = S.MAIL_MONITOR_ACCOUNTS[0]
    current_year = now.year

    for entry in entry_list:
        # Parsen: "Max Mueller (50, Freitag 06.06.)"
        m = _re.match(r"^(.+?)\s*\((\d+),\s*\w+\s+(\d{2}\.\d{2}\.)\),?$", entry)
        if not m:
            log.warning(f"birthday_draft_scheduler: Kann Eintrag nicht parsen: {entry!r}")
            continue

        name = m.group(1).strip()
        age = int(m.group(2))

        # Doppelt-Schutz
        if birthday_drafts.was_draft_created(name, current_year):
            log.info(f"birthday_draft_scheduler: Entwurf fuer {name!r} ({current_year}) bereits vorhanden — skip")
            continue

        # E-Mail-Adresse aus persons_db oder google_contacts_tools holen
        email_addr = _find_email_for_name(name)
        if not email_addr:
            log.info(f"birthday_draft_scheduler: keine E-Mail fuer {name!r} — skip")
            continue

        # Funktion aus persons_db fuer den Ton
        funktion = ""
        profiles = persons_db.search_by_name(name)
        if profiles:
            funktion = profiles[0].funktion or ""

        # Ton bestimmen
        funktion_lower = funktion.lower()
        if any(kw in funktion_lower for kw in ("kollege", "kollegin", "freund", "freundin")):
            tone = "kollegial-herzlich"
        else:
            tone = "foermlich-professionell"

        # Glueckwunsch per Haiku generieren
        prompt = _BIRTHDAY_DRAFT_CONGRATULATION_PROMPT.format(
            name=name,
            age=age,
            funktion=funktion or "nicht angegeben",
            tone=tone,
        )
        try:
            resp = await S.ai.messages.create(
                model=S.HAIKU_MODEL,
                max_tokens=400,
                system="Du bist Jarvis. Antworte NUR mit JSON, kein Praeamble.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw_json = resp.content[0].text.strip() if resp.content else ""
            # JSON aus der Antwort extrahieren
            import json as _json
            # Manchmal gibt das Modell Markdown-Codeblock
            json_text = _re.sub(r"```(?:json)?\s*", "", raw_json).strip(" `\n")
            draft_data = _json.loads(json_text)
            subject = draft_data.get("subject", f"Alles Gute zum {age}. Geburtstag, {name}")
            body = draft_data.get("body", "")
        except Exception as e:
            log.warning(
                f"birthday_draft_scheduler: LLM-Fehler fuer {name!r}: "
                f"{type(e).__name__}: {e}"
            )
            continue

        # IMAP-Entwurf ablegen (neues Schreiben, kein Reply — kein Re:-Prefix)
        from_addr = account.get("user", "")
        _msg = _EmailMessage()
        _msg["From"] = from_addr
        _msg["To"] = email_addr
        _msg["Subject"] = subject
        _msg["Date"] = _email_utils.formatdate(localtime=True)
        _msg["Message-ID"] = _email_utils.make_msgid()
        _msg.set_content(body)
        msg_bytes = bytes(_msg)
        # Entwurfs-Ordner aus Account-Config
        drafts_folder = account.get("drafts_folder", "Drafts")
        ok, detail = await _append_birthday_draft(account, msg_bytes, drafts_folder)

        if ok:
            birthday_drafts.mark_draft_created(name, current_year, subject)
            log.info(
                f"birthday_draft_scheduler: Entwurf fuer {name!r} in "
                f"{detail!r} gespeichert"
            )
            import activity_log as _al
            _al.log_action("draft_created", name)
            # Telegram-Benachrichtigung
            try:
                import telegram_bot
                tg_msg = (
                    f"Entwurf fuer {name} ({age}) erstellt — "
                    f"liegt als Entwurf im Entwurfsordner."
                )
                await telegram_bot.send_user_text(tg_msg)
            except Exception as e:
                log.warning(
                    f"birthday_draft_scheduler: Telegram-Benachrichtigung fehlgeschlagen: "
                    f"{type(e).__name__}: {e}"
                )
        else:
            log.warning(
                f"birthday_draft_scheduler: IMAP-Append fuer {name!r} fehlgeschlagen: "
                f"{detail}"
            )


def _find_email_for_name(name: str) -> str:
    """Sucht die primaere E-Mail-Adresse eines Kontakts anhand des Namens.

    Prueft zuerst persons_db, dann gibt einen leeren String zurueck wenn
    keine Adresse bekannt ist.

    Args:
        name: Anzeigename des Kontakts.

    Returns:
        E-Mail-Adresse als String oder leerer String wenn nicht gefunden.
    """
    import persons_db

    profiles = persons_db.search_by_name(name)
    if profiles:
        email = profiles[0].primary_email
        if email:
            return email
        if profiles[0].secondary_emails:
            return profiles[0].secondary_emails[0]
    return ""


async def _append_birthday_draft(
    account: dict, msg_bytes: bytes, preferred_folder: str
) -> tuple[bool, str]:
    """Lege Entwurf in den konfigurierten Drafts-Ordner ab.

    Versucht zuerst den bevorzugten Ordner, faellt dann auf die bekannten
    Alternativ-Namen aus mail_actions.DRAFTS_FOLDER_GUESSES zurueck.

    Args:
        account: Account-Dict aus S.MAIL_MONITOR_ACCOUNTS.
        msg_bytes: RFC822-Bytes des Entwurfs.
        preferred_folder: Konfigurierter Drafts-Ordner (z.B. "Drafts").

    Returns:
        Tuple (success, folder_name_or_error).
    """
    import mail_actions

    account_name = account.get("name", "default")

    # Preferred folder zuerst versuchen, dann Fallbacks
    from mail_actions import DRAFTS_FOLDER_GUESSES
    candidates = [preferred_folder] + [
        f for f in DRAFTS_FOLDER_GUESSES if f != preferred_folder
    ]

    try:
        import asyncio as _asyncio
        import aioimaplib

        cls = aioimaplib.IMAP4_SSL if account["ssl"] else aioimaplib.IMAP4
        client = cls(host=account["host"], port=account["port"], timeout=30)
        await _asyncio.wait_for(client.wait_hello_from_server(), timeout=30)
        resp = await _asyncio.wait_for(
            client.login(account["user"], account["password"]), timeout=30
        )
        if getattr(resp, "result", None) != "OK":
            return False, f"LOGIN rejected for {account['user']!r}"

        last_err = "kein Drafts-Folder gefunden"
        for folder in candidates:
            try:
                result = await client.append(msg_bytes, mailbox=folder)
                result_code = getattr(result, "result", None) or (
                    result[0] if isinstance(result, tuple) and result else None
                )
                if result_code == "OK":
                    log.info(f"_append_birthday_draft[{account_name}] -> {folder!r}")
                    try:
                        await client.logout()
                    except Exception:
                        pass
                    return True, folder
                last_err = f"folder={folder!r} code={result_code}"
            except Exception as exc:
                last_err = f"folder={folder!r} {type(exc).__name__}: {exc}"
                continue

        try:
            await client.logout()
        except Exception:
            pass
        return False, last_err

    except Exception as e:
        log.warning(
            f"_append_birthday_draft[{account_name}]: {type(e).__name__}: {e}"
        )
        return False, f"{type(e).__name__}: {e}"
