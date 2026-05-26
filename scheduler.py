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

import browser_tools  # for fetch_news (politik feed)
import google_calendar_tools
import offer_monitor
from prompt import llm_text, pick_address
import settings as S
import steuer_news
import todoist_tools

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
    result = {
        "temp": c["temp_C"],
        "feels_like": c["FeelsLikeC"],
        "description": c["weatherDesc"][0]["value"],
        "humidity": c["humidity"],
        "wind_kmh": c["windspeedKmph"],
        "forecast_today": [],
    }
    now_hour = datetime.datetime.now().hour
    for h in data["weather"][0]["hourly"]:
        h_hour = int(h["time"]) // 100
        if h_hour > now_hour:
            result["forecast_today"].append({
                "hour": h_hour,
                "temp": h["tempC"],
                "desc": h["weatherDesc"][0]["value"],
                "rain": h.get("chanceofrain", "0"),
            })
    return result


async def fetch_weather() -> Optional[dict]:
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
            l.strip().replace("- [ ]", "").strip()
            for l in lines
            if l.strip().startswith("- [ ]")
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


async def refresh_politik_brief() -> None:
    """Fetch Inland + Wirtschaft news (Tagesschau) and have Claude
    pick max 3 — eine pro Thema, ein aussagefaehiger Satz pro Eintrag.
    Optional eine 4. positive Goodnews wenn die Headlines was Konkret-
    Erfreuliches hergeben.

    Cached per day like the Steuer-Brief."""
    today = datetime.date.today().isoformat()
    if S.POLITIK_BRIEF and S.POLITIK_BRIEF_DATE == today:
        return
    try:
        # Inland + Wirtschaft parallel laden
        inland_task = browser_tools.fetch_news(
            S.POLITIK_NEWS_URL, S.POLITIK_NEWS_NAME
        )
        wirtschaft_task = browser_tools.fetch_news(
            "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
            "Tagesschau Wirtschaft",
        )
        inland_raw, wirtschaft_raw = await asyncio.gather(
            inland_task, wirtschaft_task, return_exceptions=True
        )
        if isinstance(inland_raw, Exception):
            inland_raw = ""
        if isinstance(wirtschaft_raw, Exception):
            wirtschaft_raw = ""
        combined = (
            f"=== POLITIK / INLAND ===\n{inland_raw[:2500]}\n\n"
            f"=== WIRTSCHAFT ===\n{wirtschaft_raw[:2500]}"
        )
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=(
                "Du bist Jarvis. Aus den folgenden Schlagzeilen aus Politik und "
                "Wirtschaft waehle die DREI WICHTIGSTEN aus — Mix aus beiden "
                "Bereichen, abhaengig davon was der Tag hergibt.\n\n"
                "Format STRENG:\n"
                "- Genau 3 Nachrichten, je 1 vollstaendiger aussagefaehiger "
                "Satz (Subjekt + Praedikat + Objekt, mit Punkt am Ende).\n"
                "- KEINE Aufzaehlung mit Bulletpoints, KEINE Nummerierung. "
                "Schreibe die 3 Saetze einfach hintereinander.\n"
                "- Wenn EINE der vorhandenen Headlines klar positiv / "
                "konstruktiv / loesungsorientiert ist (z.B. erfolgreicher "
                "Abschluss, Fortschritt, Hilfsaktion, Erfolg im Sport, "
                "wissenschaftlicher Durchbruch) — bring sie als VIERTEN Satz, "
                "ebenfalls in einem vollstaendigen Satz mit Punkt. "
                "Wenn nichts Positives drin ist: KEINE 4. Meldung erfinden, "
                "sondern bei 3 Saetzen aufhoeren.\n\n"
                "WICHTIG: Beende JEDEN Satz mit einem Punkt. Brich KEINEN "
                "Satz ab. Lieber kuerzer formulieren als unfertige Saetze. "
                "KEINE Begruessung. KEINE direkte Anrede. KEINE Tags."
            ),
            messages=[{"role": "user", "content": combined}],
        )
        out = trim_to_complete_sentences(llm_text(resp).strip())
        S.POLITIK_BRIEF = out
        S.POLITIK_BRIEF_DATE = today
        log.info(f"Politik-Brief: {S.POLITIK_BRIEF[:120]}")
    except Exception as e:
        log.warning(f"refresh_politik_brief failed: {type(e).__name__}: {e}")
        S.POLITIK_BRIEF = ""


async def refresh_birthday_reminders() -> None:
    """Pruefe Geburtstage der Google-Kontakte in den naechsten 7 Tagen.

    Ergebnis wird in S.BIRTHDAY_REMINDERS gespeichert (Issue #120).
    Format: "Geburtstage diese Woche: • Max Mueller (morgen, 15.06.)"
    Bei nicht verfuegbarer API: stille Degradation (leerer String).
    """
    try:
        import google_contacts_tools
        contacts = await google_contacts_tools.read_all_contacts()
        today = datetime.date.today()
        hits: list[str] = []
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
        S.BIRTHDAY_REMINDERS = (
            "Geburtstage diese Woche: " + ", ".join(hits) if hits else ""
        )
        log.info(f"refresh_birthday_reminders: {len(hits)} Treffer")
    except Exception as e:
        log.warning(f"refresh_birthday_reminders failed: {type(e).__name__}: {e}")
        S.BIRTHDAY_REMINDERS = ""


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
    (today's tasks, today's calendar, politik news, birthday reminders).
    Called from the activate path before MORNING_BRIEF_UNTIL_HOUR.

    refresh_upcoming_deadlines() runs after the first gather because it
    reads S.TODAY_TASKS / S.TODAY_EVENTS which must be populated first."""
    await asyncio.gather(
        refresh_today_tasks(),
        refresh_today_events(),
        refresh_politik_brief(),
        refresh_open_promises(),
        refresh_birthday_reminders(),
    )
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


async def refresh_steuer_brief(raw: Optional[str] = None) -> None:
    """Fetch steuerrecht news and summarize with Claude. Updates global cache.

    Wenn ``raw`` uebergeben wird, wird kein erneuter RSS-Fetch durchgefuehrt
    -- der bereits geholte Text wird direkt verwendet (Issue #91).
    """
    log.info("Steuerrecht-Brief wird abgerufen...")
    try:
        if raw is None:
            raw = await steuer_news.fetch_all_sources()
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
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
            model="claude-haiku-4-5-20251001",
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
    "Liefere ein vollstaendiges Tages-Briefing mit allen verfuegbaren Bloecken: "
    "Wochentag + exaktes Datum, Wetter (nur Maximaltemperatur + Regen ja/nein), "
    "heutige Termine, heutige Aufgaben, Steuerrecht-Schlagzeile (falls vorhanden), "
    "Politik (falls vorhanden). Unter 6 Saetze, fliessende Sprache, Jarvis-Stil. "
    "Keine ACTION-Tags."
)


async def morning_brief_scheduler() -> None:
    """Long-running task: fetch the morning brief once per day at or
    after `S.MORNING_HOUR`. Refreshes BOTH the Steuer-Brief and the
    today's-tasks/events/politik caches, then pushes the brief via the
    registered proactive handler (Telegram + UI if open).

    Uses '>=' on the hour (not '=='): if the server was asleep at exactly
    7:00 and the loop wakes up at 7:05, the brief still fires today.
    """
    triggered_today = ""
    while True:
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour >= S.MORNING_HOUR and triggered_today != today:
            triggered_today = today
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
                    system_prompt = _MORNING_BRIEF_PROMPT.format(addr=addr)
                    today_block = ""
                    if S.TODAY_TASKS:
                        today_block += f"\nHeutige Aufgaben:\n{S.TODAY_TASKS}"
                    if S.TODAY_EVENTS:
                        today_block += f"\nHeutige Termine:\n{S.TODAY_EVENTS}"
                    # Offene Vorhaben (Issue #117) — aus Cache lesen statt
                    # erneuten DB-Call (refresh_morning_brief_data hat bereits
                    # refresh_open_promises() ausgefuehrt und S.OPEN_PROMISES befuellt)
                    if S.OPEN_PROMISES:
                        today_block += f"\n{S.OPEN_PROMISES}"
                    # Geburtstage diese Woche (Issue #120)
                    if S.BIRTHDAY_REMINDERS:
                        today_block += f"\n{S.BIRTHDAY_REMINDERS}"
                    # Supermarkt-Angebote (Issue #122) — nur montags, wenn vorhanden
                    if now.weekday() == 0 and S.WEEKLY_OFFERS:
                        today_block += f"\n{S.WEEKLY_OFFERS}"
                    # Anstehende Fristen (Issue #119)
                    if S.UPCOMING_DEADLINES:
                        today_block += f"\n{S.UPCOMING_DEADLINES}"
                    if S.STEUER_RECENT:
                        today_block += f"\nSteuerrecht aktuell (3 Tage): {S.STEUER_RECENT[:400]}"
                    elif S.STEUER_BRIEF:
                        today_block += f"\nSteuerrecht: {S.STEUER_BRIEF}"
                    if S.POLITIK_BRIEF:
                        today_block += f"\nNachrichten: {S.POLITIK_BRIEF}"
                    if S.WEATHER_INFO:
                        w = S.WEATHER_INFO
                        rain = ""
                        for h in w.get("forecast_today", []):
                            if int(h.get("rain", "0")) >= 40:
                                rain = ", Regen möglich"
                                break
                        today_block += (
                            f"\nWetter: {w.get('temp', '?')} Grad, "
                            f"{w.get('description', '')}{rain}"
                        )
                    user_msg = (
                        f"Datum: {now.strftime('%A, %d.%m.%Y')}, "
                        f"Uhrzeit: {now.strftime('%H:%M')}"
                        + (f"\nAktuelle Tagesdaten:{today_block}" if today_block else "")
                    )
                    resp = await S.ai.messages.create(
                        model="claude-haiku-4-5-20251001",
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
        "an die Mittagspause. KURZ (2-3 Saetze): erst eine trockene Mittagspausen-"
        "Aufforderung im Butler-Stil; dann nenne offene Aufgaben fuer heute (siehe "
        "AKTUELLE DATEN, falls vorhanden) und naechste Termine bis Tagesende. "
        "Wenn keine Aufgaben oder Termine: kurzes Lob im Jarvis-Stil. Keine "
        "ACTION-Tags, alles wird vorgelesen."
    ),
    "16:00": (
        "Du bist Jarvis. Nachmittagsupdate fuer {addr}. KURZ (2-3 Saetze): "
        "ein knapper Status-Check (\"Wie laeuft's?\"-Halbsatz im Butler-Ton), "
        "dann offene Aufgaben fuer heute (siehe AKTUELLE DATEN) und Termine "
        "die noch bis Tagesende anstehen. Keine ACTION-Tags."
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
    user_msg = f"Aktuelle Tagesdaten:{today_block or ' (keine offenen Punkte)'}"
    resp = await S.ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
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

    # Kontext fuer den LLM zusammenstellen
    parts: list[str] = []
    if tasks_done > 0:
        parts.append(f"Erledigte Aufgaben heute: {tasks_done}")
    if open_tasks:
        first_open = open_tasks.splitlines()[0].strip().lstrip("•").strip()
        parts.append(f"Offene Aufgabe: {first_open}")
    if tomorrow_event:
        parts.append(f"Erster Termin morgen: {tomorrow_event}")

    user_content = (
        "Tagesdaten:\n" + "\n".join(parts)
        if parts
        else "Keine besonderen Tagesdaten vorhanden."
    )

    system_prompt = _EVENING_BRIEF_PROMPT.format(addr=addr)
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return trim_to_complete_sentences(llm_text(resp).strip())
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
    triggered_today = ""
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
    triggered: dict[str, str] = {}  # slot "HH:MM" -> ISO date last fired
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            current_hhmm = now.strftime("%H:%M")
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
