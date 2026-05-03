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
    """Fetch Google Calendar events for today only."""
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


async def refresh_politik_brief() -> None:
    """Fetch politik news (Tagesschau Inland) and have Claude condense
    them into 2 sentences. Cached per day like the Steuer-Brief."""
    today = datetime.date.today().isoformat()
    if S.POLITIK_BRIEF and S.POLITIK_BRIEF_DATE == today:
        return
    try:
        raw = await browser_tools.fetch_news(S.POLITIK_NEWS_URL, S.POLITIK_NEWS_NAME)
        resp = await S.ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                f"Du bist Jarvis. Fasse die folgenden Politik-Schlagzeilen in MAXIMAL 2 "
                f"Saetzen zusammen — wie ein Butler, der die Zeitung ueberflogen hat. "
                f"Nur 1-2 wirklich relevante Themen. Sprich {S.USER_ADDRESS} an. "
                f"Keine Tags in eckigen Klammern."
            ),
            messages=[{"role": "user", "content": raw[:3000]}],
        )
        S.POLITIK_BRIEF = resp.content[0].text.strip()
        S.POLITIK_BRIEF_DATE = today
        log.info(f"Politik-Brief: {S.POLITIK_BRIEF[:80]}")
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
            max_tokens=300,
            system=(
                f"Du bist Jarvis, der britisch-hoefliche KI-Butler von {S.USER_NAME}. "
                f"Erstelle einen KURZEN Morgen-Ueberblick ueber neue steuerrechtliche Veroeffentlichungen "
                f"aus BMF-Schreiben, BMF-Pressemitteilungen und BFH-Pressemitteilungen. "
                f"Maximal 3-4 Saetze. Nenne nur was wirklich NEU und relevant ist. "
                f"Ton: praezise, trocken, professionell — kein Smalltalk. "
                f"Sprich {S.USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
            ),
            messages=[{"role": "user", "content": f"Neue Veroeffentlichungen heute:\n\n{raw}"}],
        )
        S.STEUER_BRIEF = resp.content[0].text.strip()
        S.STEUER_BRIEF_DATE = datetime.date.today().isoformat()
        log.info(f"Steuerrecht-Brief: {S.STEUER_BRIEF[:80]}")
    except Exception as e:
        log.warning(f"Steuerrecht-Brief Fehler: {e}")
        S.STEUER_BRIEF = ""


async def morning_brief_scheduler() -> None:
    """Long-running task: fetch the morning brief once per day at
    `S.MORNING_HOUR`."""
    triggered_today = ""
    while True:
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour == S.MORNING_HOUR and triggered_today != today:
            triggered_today = today
            await refresh_steuer_brief()
        await asyncio.sleep(60)
