"""
Central configuration + runtime state + shared clients.

Imports config.json and .env exactly once at import time. Other modules
that need config values do `from settings import CITY` for static
constants, but for *mutable* state (WEATHER_INFO, TASKS_INFO,
STEUER_BRIEF, ...) they should use `import settings as S` and read
`S.WEATHER_INFO` so they see updates.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os

import anthropic
import httpx
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# .env + config.json loading
# ---------------------------------------------------------------------------
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _required_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable {key!r}. "
            f"Set it in .env (see .env.example) or your shell environment."
        )
    return val


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)


# ---------------------------------------------------------------------------
# Logging — module-level so first import wires it up for everything else.
# ---------------------------------------------------------------------------
_LOG_PATH = os.path.join(os.path.dirname(__file__), "jarvis.log")
_LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_log_handlers = [
    logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    ),
    logging.StreamHandler(),
]
for _h in _log_handlers:
    _h.setFormatter(logging.Formatter(_LOG_FMT))
logging.basicConfig(level=logging.INFO, handlers=_log_handlers, force=True)
log = logging.getLogger("jarvis")


# ---------------------------------------------------------------------------
# Secrets (env-only). Required ones raise on startup if missing.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = _required_env("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = _required_env("ELEVENLABS_API_KEY")
TODOIST_TOKEN = os.environ.get("TODOIST_API_TOKEN", "").strip()
JARVIS_AUTH_TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "").strip()
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "").strip()


# ---------------------------------------------------------------------------
# Non-secret runtime settings (from config.json with sane defaults).
# ---------------------------------------------------------------------------
ELEVENLABS_VOICE_ID = os.environ.get(
    "ELEVENLABS_VOICE_ID",
    config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD"),
)
USER_NAME = config.get("user_name", "Caterina")
USER_ADDRESS = config.get("user_address", "Madam")
USER_ROLE = config.get(
    "user_role",
    "Leiterin Konzern Steuerabteilung DIHAG und Direktionsleiterin Lohnsteuerhilfeverein HILO",
)
CITY = config.get("city", "Neuss")
TASKS_FILE = config.get("obsidian_inbox_path", "")
MORNING_HOUR = config.get("morning_hour", 7)

SERVER_PORT = int(config.get("server_port", 8340))
ELEVENLABS_MODEL = config.get("elevenlabs_model", "eleven_turbo_v2_5")
GREETING_COOLDOWN = float(config.get("greeting_cooldown", 10.0))
ACTIVATE_COOLDOWN = float(config.get("activate_cooldown", 90.0))
REFRESH_COOLDOWN = float(config.get("refresh_cooldown", 30.0))
CALENDAR_DAYS = int(config.get("calendar_days", 7))
NEWS_URL = config.get(
    "news_url",
    "https://www.tagesschau.de/infoservices/alle-meldungen-100~rss2.xml",
)
NEWS_SOURCE_NAME = config.get("news_source_name", "Tagesschau")

# Morning brief: full briefing (weather, today's events/tasks, Steuer/Politik)
# is delivered on Activate before this hour. After it, a short greeting only.
MORNING_BRIEF_UNTIL_HOUR = int(config.get("morning_brief_until_hour", 11))
POLITIK_NEWS_URL = config.get(
    "politik_news_url",
    "https://www.tagesschau.de/inland/index~rss2.xml",
)
POLITIK_NEWS_NAME = config.get("politik_news_name", "Tagesschau Inland")

# Address pool — Jarvis randomly varies how he calls Catrin so the
# greeting doesn't sound canned.
USER_ADDRESS_POOL = config.get("user_address_pool", ["Madam", "Catrin", "Caterina"])

# Proactive briefs: Jarvis self-triggers updates at these times of day
# (HH:MM, 24h). Configurable; 12:30 lunch reminder, 16:00 afternoon
# check-in, 18:00 Feierabend hint.
PROACTIVE_BRIEFS_TIMES = config.get("proactive_briefs_times", ["12:30", "16:00", "18:00"])

PERSIST_HISTORY = bool(config.get("persist_conversations", True))
HISTORY_PATH = os.path.join(os.path.dirname(__file__), ".jarvis_history.json")

# Todoist project / section scoping (M-???). Keeps the task list focused
# on Catrin's three relevant areas instead of every project she has.
# Each entry is a Todoist project_id; HILO additionally restricts to a
# section_id so only the personal-tasks-for-Catrin section comes through.
TODOIST_PROJECTS = config.get("todoist_projects", {})
TODOIST_PROJECT_IDS = [
    pid for key in ("hilo", "dihag", "privat")
    if (pid := TODOIST_PROJECTS.get(key))
]
TODOIST_SECTIONS_PER_PROJECT = {}
if TODOIST_PROJECTS.get("hilo") and TODOIST_PROJECTS.get("hilo_section"):
    TODOIST_SECTIONS_PER_PROJECT[TODOIST_PROJECTS["hilo"]] = [TODOIST_PROJECTS["hilo_section"]]

# Mail backend ("applescript" = macOS Mail.app | "imap" = cross-platform).
MAIL_BACKEND = config.get("mail_backend", "applescript")
IMAP_HOST = config.get("imap_host", "")
IMAP_USER = config.get("imap_user", "")
IMAP_PORT = int(config.get("imap_port", 993))
IMAP_SSL = bool(config.get("imap_ssl", True))
IMAP_FOLDER = config.get("imap_folder", "INBOX")


# ---------------------------------------------------------------------------
# Shared clients (singletons).
# ---------------------------------------------------------------------------
ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
# Global httpx client for the HOT path: ElevenLabs TTS in tts._tts_post(),
# which fires multiple requests per response. Sharing the client gives us
# HTTP/2 + connection pool reuse. One-shot calls in tools intentionally
# spin up their own short-lived `async with httpx.AsyncClient()`.
http = httpx.AsyncClient(timeout=30)


# ---------------------------------------------------------------------------
# Mutable runtime state. Other modules MUST access these via
# `import settings as S; S.WEATHER_INFO` so they see updates.
# ---------------------------------------------------------------------------
WEATHER_INFO: dict | None = None
TASKS_INFO: list[str] = []  # Obsidian Tasks.md
STEUER_BRIEF: str = ""
STEUER_BRIEF_DATE: str = ""
STEUER_RECENT: str = ""
STEUER_RECENT_DATE: str = ""

# Morning-brief state. Refreshed on activate before MORNING_BRIEF_UNTIL_HOUR.
TODAY_TASKS: str = ""        # Todoist tasks due today + overdue
TODAY_EVENTS: str = ""        # Google Calendar events for today
POLITIK_BRIEF: str = ""       # Tagesschau Inland summary
POLITIK_BRIEF_DATE: str = ""
