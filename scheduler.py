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
from prompt import pick_address
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


def get_tasks_sync() -> list[str]:
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
        log.warning(f"get_tasks_sync failed: {type(e).__name__}: {e}")
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
    loop = asyncio.get_event_loop()
    weather, tasks = await asyncio.gather(
        fetch_weather(),
        loop.run_in_executor(None, get_tasks_sync),
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
        out = trim_to_complete_sentences(resp.content[0].text.strip())
        S.POLITIK_BRIEF = out
        S.POLITIK_BRIEF_DATE = today
        log.info(f"Politik-Brief: {S.POLITIK_BRIEF[:120]}")
    except Exception as e:
        log.warning(f"refresh_politik_brief failed: {type(e).__name__}: {e}")
        S.POLITIK_BRIEF = ""


async def refresh_morning_brief_data() -> None:
    """Refresh all the extra data needed for the full morning briefing
    (today's tasks, today's calendar, politik news). Called from the
    activate path before MORNING_BRIEF_UNTIL_HOUR."""
    await asyncio.gather(
        refresh_today_tasks(),
        refresh_today_events(),
        refresh_politik_brief(),
    )


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


async def refresh_steuer_brief() -> None:
    """Fetch steuerrecht news and summarize with Claude. Updates global cache."""
    log.info("Steuerrecht-Brief wird abgerufen...")
    try:
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
        S.STEUER_BRIEF = resp.content[0].text.strip()
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

    # 1. Todoist: offene + ueberfaellige + naechste Woche faellige
    if S.TODOIST_TOKEN and S.TODOIST_TOKEN != "YOUR_TODOIST_API_TOKEN":
        try:
            tasks_text = await todoist_tools.get_tasks(
                S.TODOIST_TOKEN,
                max_tasks=20,
                project_ids=S.TODOIST_PROJECT_IDS or None,
                section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
            )
            if tasks_text and tasks_text != "KEINE_TASKS":
                parts.append(f"TODOIST OFFEN:\n{tasks_text}")
        except Exception as e:
            log.warning(f"weekly outlook: tasks failed: {e}")

    # 2. Google Calendar: Termine bis next_sunday
    try:
        days_ahead = (next_sunday - today).days + 1
        cal_text = await google_calendar_tools.get_events(
            days=max(7, days_ahead), max_results=30,
        )
        if cal_text and cal_text != "KEINE_TERMINE":
            parts.append(f"KALENDER NAECHSTE WOCHE:\n{cal_text}")
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

    # Lass Claude den Brief formulieren
    user_msg = "\n\n".join(parts)
    addr = pick_address()
    sys_prompt = (
        f"Du bist Jarvis. Erstelle aus den folgenden Daten einen knappen "
        f"Wochenausblick fuer {addr} — was kommt naechste Woche, worauf "
        f"sollte sie sich konzentrieren. Maximal 5-6 Saetze, in Prosa "
        f"(keine Aufzaehlung). Hebe 2-3 wichtige Schwerpunkte hervor, "
        f"nicht alle Punkte einzeln. Ton: trocken-butlerhaft. "
        f"Du darfst die Anrede {addr} verwenden. KEINE Tags."
    )
    try:
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return trim_to_complete_sentences(resp.content[0].text.strip())
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


async def morning_brief_scheduler() -> None:
    """Long-running task: fetch the morning brief once per day at or
    after `S.MORNING_HOUR`. Refreshes BOTH the Steuer-Brief and the
    today's-tasks/events/politik caches so the data is hot when Catrin
    activates Jarvis in the morning.

    Uses '>=' on the hour (not '=='): if the Mac was asleep at exactly
    7:00 and the loop wakes up at 7:05, the brief still fires today.
    """
    triggered_today = ""
    while True:
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour >= S.MORNING_HOUR and triggered_today != today:
            triggered_today = today
            try:
                await refresh_steuer_brief()
                await refresh_morning_brief_data()
            except Exception as e:
                log.warning(f"morning_brief_scheduler: refresh failed: "
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
    await refresh_morning_brief_data()
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
    return resp.content[0].text.strip()


async def proactive_briefs_scheduler() -> None:
    """Long-running task: fire each configured slot once per day."""
    triggered: dict[str, str] = {}  # slot "HH:MM" -> ISO date last fired
    while True:
        try:
            now = datetime.datetime.now()
            today = datetime.date.today().isoformat()
            current_hhmm = now.strftime("%H:%M")
            for slot in S.PROACTIVE_BRIEFS_TIMES:
                if current_hhmm != slot:
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
                    log.info(f"proactive {slot}: '{message[:80]}'")
                    await _proactive_handler(message)
                except Exception as e:
                    log.warning(f"proactive {slot} failed: {type(e).__name__}: {e}")
        except Exception as e:
            log.warning(f"proactive scheduler loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(30)
