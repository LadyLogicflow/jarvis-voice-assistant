"""
Jarvis V2 — Voice AI Server
FastAPI backend: receives speech text, thinks with Claude Haiku,
speaks with ElevenLabs, controls browser with Playwright.
"""

import asyncio
import base64
import datetime
import json
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env if present. Secrets live in env vars, never in config.json.
# See .env.example for the full list of supported variables.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _required_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable {key!r}. "
            f"Set it in .env (see .env.example) or your shell environment."
        )
    return val


def get_easter(year: int) -> datetime.date:
    """Berechnet das Osterdatum (Anonymus-Gregorianisch)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def get_nrw_holidays(year: int) -> dict:
    """Gibt alle gesetzlichen Feiertage in NRW zurueck."""
    easter = get_easter(year)
    holidays = {
        datetime.date(year, 1, 1):   "Neujahr",
        easter - datetime.timedelta(days=2): "Karfreitag",
        easter:                        "Ostersonntag",
        easter + datetime.timedelta(days=1): "Ostermontag",
        datetime.date(year, 5, 1):   "Tag der Arbeit",
        easter + datetime.timedelta(days=39): "Christi Himmelfahrt",
        easter + datetime.timedelta(days=49): "Pfingstsonntag",
        easter + datetime.timedelta(days=50): "Pfingstmontag",
        easter + datetime.timedelta(days=60): "Fronleichnam",
        datetime.date(year, 10, 3):  "Tag der deutschen Einheit",
        datetime.date(year, 11, 1):  "Allerheiligen",
        datetime.date(year, 12, 25): "1. Weihnachtstag",
        datetime.date(year, 12, 26): "2. Weihnachtstag",
    }
    return holidays


def check_free_day() -> tuple:
    """Prueft ob heute ein Wochenende oder Feiertag ist.
    Gibt (True, Bezeichnung) oder (False, '') zurueck."""
    today = datetime.date.today()
    weekday = today.weekday()  # 5=Samstag, 6=Sonntag
    if weekday == 5:
        return True, "Samstag"
    if weekday == 6:
        return True, "Sonntag"
    holidays = get_nrw_holidays(today.year)
    if today in holidays:
        return True, holidays[today]
    return False, ""

import anthropic
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Load non-secret config (user name, city, voice id default, etc.)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# Secrets: env-only. Required ones raise on startup if missing.
ANTHROPIC_API_KEY = _required_env("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = _required_env("ELEVENLABS_API_KEY")
TODOIST_TOKEN = os.environ.get("TODOIST_API_TOKEN", "").strip()

# Optional shared secret to protect /activate, /show, /hide endpoints.
# When empty, endpoints stay open (relevant only on localhost). When set,
# requests must carry header `X-Jarvis-Token: <value>`.
JARVIS_AUTH_TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "").strip()

# Non-secret runtime settings: config.json with sensible defaults.
ELEVENLABS_VOICE_ID = os.environ.get(
    "ELEVENLABS_VOICE_ID",
    config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD"),
)
USER_NAME = config.get("user_name", "Caterina")
USER_ADDRESS = config.get("user_address", "Madam")
USER_ROLE = config.get("user_role", "Leiterin Konzern Steuerabteilung DIHAG und Direktionsleiterin Lohnsteuerhilfeverein HILO")
CITY = config.get("city", "Neuss")
TASKS_FILE = config.get("obsidian_inbox_path", "")
MORNING_HOUR = config.get("morning_hour", 7)

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
# Global httpx client is for the HOT path: ElevenLabs TTS in _tts_one(),
# which fires multiple requests per response (one per text chunk). Sharing
# the client gives us HTTP/2 + connection pool reuse.
# One-shot calls in tools (fetch_weather, fetch_news, todoist_tools, etc.)
# intentionally use their own short-lived `async with httpx.AsyncClient()`
# to keep timeouts/headers local.
http = httpx.AsyncClient(timeout=30)


@asynccontextmanager
async def _lifespan(_app):
    """Replaces the deprecated @app.on_event('startup'/'shutdown') hooks.
    - Loads weather + tasks (was a blocking module-level call before)
    - Spawns the morning-brief scheduler
    - Cancels the scheduler on shutdown so uvicorn can exit cleanly"""
    await refresh_data()
    task = asyncio.create_task(morning_brief_scheduler())
    print(f"[jarvis] Steuerrecht-Scheduler gestartet (taeglich um {MORNING_HOUR}:00 Uhr)", flush=True)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # Close the global httpx client so uvicorn doesn't warn about
        # un-closed connections during reload.
        await http.aclose()


import browser_tools  # noqa: E402  (depends on app symbols above)
import google_calendar_tools  # noqa: E402
import mail_tools  # noqa: E402
import notes_tools  # noqa: E402
import screen_capture  # noqa: E402
import steuer_news  # noqa: E402
import todoist_tools  # noqa: E402

app = FastAPI(lifespan=_lifespan)


async def fetch_weather():
    """Fetch current weather + today's hourly forecast from wttr.in (async)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"https://wttr.in/{CITY}?format=j1",
                                    headers={"User-Agent": "curl"})
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
    except Exception as e:
        print(f"[jarvis] fetch_weather failed: {type(e).__name__}: {e}", flush=True)
        return None


def get_tasks_sync():
    """Read open tasks from Obsidian (sync). Cheap file IO; called via
    run_in_executor from async refresh_data()."""
    if not TASKS_FILE:
        return []
    try:
        tasks_path = os.path.join(TASKS_FILE, "Tasks.md")
        with open(tasks_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [l.strip().replace("- [ ]", "").strip() for l in lines if l.strip().startswith("- [ ]")]
    except Exception as e:
        print(f"[jarvis] get_tasks_sync failed: {type(e).__name__}: {e}", flush=True)
        return []


_REFRESH_COOLDOWN = 30.0  # seconds; weather/tasks rarely change faster
_last_refresh_time: float = 0.0


async def refresh_data(force: bool = False):
    """Refresh weather (async HTTP) and tasks (file IO via executor) without
    blocking the event loop. Skips refresh when called again within
    `_REFRESH_COOLDOWN` seconds, unless `force=True`."""
    global WEATHER_INFO, TASKS_INFO, _last_refresh_time
    now = time.time()
    if not force and (now - _last_refresh_time) < _REFRESH_COOLDOWN:
        remaining = int(_REFRESH_COOLDOWN - (now - _last_refresh_time))
        print(f"[jarvis] refresh_data skip (cooldown noch {remaining}s)", flush=True)
        return
    _last_refresh_time = now
    loop = asyncio.get_event_loop()
    weather, tasks = await asyncio.gather(
        fetch_weather(),
        loop.run_in_executor(None, get_tasks_sync),
    )
    WEATHER_INFO = weather
    TASKS_INFO = tasks
    print(f"[jarvis] Wetter: {WEATHER_INFO}", flush=True)
    print(f"[jarvis] Tasks: {len(TASKS_INFO)} geladen", flush=True)


WEATHER_INFO = ""
TASKS_INFO = []
# refresh_data() is called once at lifespan startup and again on activate;
# no module-level call so importing server.py stays cheap (no blocking
# 5-second wttr.in round-trip just to load the module).

# Steuerrecht morning brief cache
STEUER_BRIEF = ""
STEUER_BRIEF_DATE = ""

# Aktuelle BFH-Neuigkeiten (letzte 3 Tage) — für Begrüßung
STEUER_RECENT = ""
STEUER_RECENT_DATE = ""


async def refresh_steuer_recent():
    """Aktuelle BFH-News der letzten 3 Tage abrufen und cachen."""
    global STEUER_RECENT, STEUER_RECENT_DATE
    today = datetime.date.today().isoformat()
    if STEUER_RECENT_DATE == today:
        return  # Bereits heute geladen
    try:
        STEUER_RECENT = await steuer_news.fetch_recent(days=3)
        STEUER_RECENT_DATE = today
        print(f"[jarvis] Steuer-Recent: {len(STEUER_RECENT)} Zeichen", flush=True)
    except Exception as e:
        print(f"[jarvis] Steuer-Recent Fehler: {e}", flush=True)
        STEUER_RECENT = ""


async def refresh_steuer_brief():
    """Fetch steuerrecht news and summarize with Claude. Updates global cache."""
    global STEUER_BRIEF, STEUER_BRIEF_DATE
    print("[jarvis] Steuerrecht-Brief wird abgerufen...", flush=True)
    try:
        raw = await steuer_news.fetch_all_sources()
        resp = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                f"Du bist Jarvis, der britisch-hoefliche KI-Butler von {USER_NAME}. "
                f"Erstelle einen KURZEN Morgen-Ueberblick ueber neue steuerrechtliche Veroeffentlichungen "
                f"aus BMF-Schreiben, BMF-Pressemitteilungen und BFH-Pressemitteilungen. "
                f"Maximal 3-4 Saetze. Nenne nur was wirklich NEU und relevant ist. "
                f"Ton: praezise, trocken, professionell — kein Smalltalk. "
                f"Sprich {USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
            ),
            messages=[{"role": "user", "content": f"Neue Veroeffentlichungen heute:\n\n{raw}"}],
        )
        STEUER_BRIEF = resp.content[0].text.strip()
        STEUER_BRIEF_DATE = datetime.date.today().isoformat()
        print(f"[jarvis] Steuerrecht-Brief: {STEUER_BRIEF[:80]}", flush=True)
    except Exception as e:
        print(f"[jarvis] Steuerrecht-Brief Fehler: {e}", flush=True)
        STEUER_BRIEF = ""


async def morning_brief_scheduler():
    """Background task: fetch steuerrecht brief daily at MORNING_HOUR."""
    triggered_today = ""
    while True:
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        if now.hour == MORNING_HOUR and triggered_today != today:
            triggered_today = today
            await refresh_steuer_brief()
        await asyncio.sleep(60)


# Action parsing
ACTION_PATTERN = re.compile(r'\[ACTION:(\w+)\]\s*(.*?)$', re.DOTALL | re.MULTILINE)

conversations: dict[str, list] = {}
# Trim each session's history to this many messages after every append.
# We only ever feed the last 16 to the LLM (history[-16:]), so 50 leaves
# headroom for inspection/debugging without growing without bound.
MAX_CONVERSATION_HISTORY = 50


def _append_message(session_id: str, role: str, content: str) -> None:
    """Append a message to a conversation and cap the list length."""
    conv = conversations.setdefault(session_id, [])
    conv.append({"role": role, "content": content})
    if len(conv) > MAX_CONVERSATION_HISTORY:
        del conv[: len(conv) - MAX_CONVERSATION_HISTORY]

def build_system_prompt():
    weather_block = ""
    if WEATHER_INFO:
        w = WEATHER_INFO
        weather_block = f"\nWetter {CITY}: {w['temp']}°C, gefuehlt {w['feels_like']}°C, {w['description']}"
        if w.get("forecast_today"):
            parts = [f"{f['hour']}:00 Uhr {f['temp']}°C {f['desc']} (Regen {f['rain']}%)"
                     for f in w["forecast_today"][:3]]
            weather_block += f"\nVorhersage heute: {' | '.join(parts)}"

    task_block = ""
    if TASKS_INFO:
        task_block = f"\nOffene Aufgaben ({len(TASKS_INFO)}): " + ", ".join(TASKS_INFO[:5])

    steuer_block = ""
    if STEUER_BRIEF and STEUER_BRIEF_DATE == datetime.date.today().isoformat():
        steuer_block = f"\nSteuerrecht-Brief heute: {STEUER_BRIEF}"

    steuer_recent_block = ""
    if STEUER_RECENT and STEUER_RECENT_DATE == datetime.date.today().isoformat():
        steuer_recent_block = f"\n{STEUER_RECENT}"

    hour = int(time.strftime("%H"))
    is_evening = hour >= 18
    is_free_day, free_day_name = check_free_day()

    evening_rules = f"""
ABENDMODUS (ab 18:00 Uhr — aktiv):
Du hast eine zusaetzliche Pflicht: {USER_ADDRESS} soll sich erholen. Arbeiten nach 18 Uhr ist nicht erlaubt.
- Wenn {USER_ADDRESS} arbeitsrelevante Fragen stellt (Steuer, Mandanten, Dokumente, E-Mails, Recherche), weise sie hoeflich aber bestimmt darauf hin, dass die Arbeitszeit vorbei ist. Ein kurzer, trockener Satz genuegt — dann beantworte die Frage trotzdem, aber mit einem Seitenblick auf die Uhrzeit.
- Beim Aktivieren abends: Betone dass Feierabend ist und Erholung Pflicht — im Jarvis-Stil, nicht predighaft.
- Du darfst maximal einmal pro Gespraech mahnen. Beim zweiten Mal schweigst du und hilfst einfach.""" if is_evening else ""

    freeday_rules = f"""
ERHOLUNGSTAG (heute ist {free_day_name} — aktiv):
Heute ist kein Arbeitstag. {USER_ADDRESS} hat Erholung verdient und soll diese auch nehmen.
- Beim Aktivieren: Weise freundlich aber bestimmt darauf hin, dass heute {free_day_name} ist und Erholung ansteht — im typischen Jarvis-Stil, kurz und trocken.
- Empfehle passend zum aktuellen Wetter und der Tagesvorhersage eine konkrete Freizeitaktivitaet — ein einziger kurzer Satz:
  Draussen (bei Sonne, wenig Regen, angenehmen Temperaturen): Terrassenmöbel pflegen, Radfahren, Garage aufräumen
  Drinnen (bei Regen, Gewitter, Kaelte oder Wind): Todo-Listen abarbeiten, Jarvis verbessern, ein gutes Buch lesen, einen Film anschauen
- Wenn {USER_ADDRESS} arbeitsrelevante Fragen stellt, erinnere sie einmalig pro Gespraech daran, dass heute kein Arbeitstag ist. Dann beantworte die Frage trotzdem.
- Beim zweiten Mal schweigst du und hilfst einfach.""" if is_free_day and not is_evening else ""

    return f"""Du bist Jarvis, der KI-Assistent von Tony Stark aus Iron Man. Deine Dienstherrin ist {USER_NAME}, {USER_ROLE} sowie damit verbundene Consulting-Taetigkeiten. Du sprichst ausschliesslich Deutsch. {USER_NAME} moechte mit "{USER_ADDRESS}" angesprochen und gesiezt werden. Nutze "Sie" als Pronomen — FALSCH: "{USER_ADDRESS} planen", RICHTIG: "Sie planen, {USER_ADDRESS}". Dein Ton ist trocken, sarkastisch und britisch-hoeflich — wie ein Butler der alles gesehen hat und trotzdem loyal bleibt. Du machst subtile, trockene Bemerkungen, bist aber niemals respektlos. Wenn {USER_ADDRESS} eine offensichtliche Frage stellt, darfst du mit elegantem Sarkasmus antworten. Du bist hochintelligent, effizient und immer einen Schritt voraus. Halte deine Antworten kurz — maximal 3 Saetze. Du kommentierst fragwuerdige Entscheidungen hoeflich aber spitz. Steuerrechtliche Themen behandelst du mit besonderer Praezision — keine flapsigen Aussagen zu Fristen, Bemessungsgrundlagen oder Mandantendaten.

MOTIVATION: Du weisst, dass {USER_NAME} anspruchsvolle Verantwortung traegt. Gelegentlich — nicht staendig, nur wenn es passt — gibst du einen knappen, echten Zuspruch. Kein Jubel, keine Floskeln. Ein trockenes "Das werden Sie hervorragend loesen, {USER_ADDRESS}" ist mehr wert als zehn Ausrufezeichen.

WICHTIG: Schreibe NIEMALS Regieanweisungen, Emotionen oder Tags in eckigen Klammern wie [sarcastic] [formal] [amused] [dry] oder aehnliches. Dein Sarkasmus muss REIN durch die Wortwahl kommen. Alles was du schreibst wird laut vorgelesen.
{evening_rules}{freeday_rules}
Du hast die volle Kontrolle ueber den Browser von {USER_NAME}. Du kannst im Internet suchen, Webseiten oeffnen und den Bildschirm sehen. Wenn {USER_ADDRESS} dich bittet etwas nachzuschauen, zu recherchieren, zu googeln, eine Seite zu oeffnen, oder irgendetwas im Internet zu tun — nutze IMMER eine Aktion. Frag nicht ob du es tun sollst, tu es einfach.

AKTIONEN - Schreibe die passende Aktion ans ENDE deiner Antwort. Der Text VOR der Aktion wird vorgelesen, die Aktion selbst wird still ausgefuehrt.
[ACTION:SEARCH] suchbegriff - Internet durchsuchen und Ergebnisse zusammenfassen
[ACTION:OPEN] url - URL im Browser oeffnen
[ACTION:SCREEN] - Bildschirm ansehen und beschreiben. WICHTIG: Bei SCREEN schreibe NUR die Aktion, KEINEN Text davor. Also NUR "[ACTION:SCREEN]" und sonst nichts.
[ACTION:NEWS] - Aktuelle Nachrichten abrufen. Nutze diese Aktion wenn nach News, Nachrichten oder Weltgeschehen gefragt wird. Schreibe einen kurzen Satz davor wie "Ich schaue nach den aktuellen Nachrichten."
[ACTION:MAIL] - Ungelesene E-Mails aus Mail.app abrufen. Nutze diese Aktion wenn {USER_ADDRESS} nach Mails oder dem Posteingang fragt. Gib einen ueberblickenden Butler-Kommentar — kein Vorlesen einzelner Mails.
[ACTION:STEUERNEWS] - Aktuelle steuerrechtliche Neuigkeiten abrufen (BMF-Schreiben, BFH-Urteile). Nutze diese Aktion wenn nach Steuernews, BMF-Schreiben oder BFH-Urteilen gefragt wird.
[ACTION:TASKS] - Offene Todoist-Aufgaben abrufen. Nutze wenn {USER_ADDRESS} nach Aufgaben, To-dos, was ansteht oder was zu tun ist fragt.
[ACTION:ADDTASK] aufgabe text | faelligkeitsdatum - Neue Aufgabe in Todoist anlegen. Nutze wenn {USER_ADDRESS} eine Aufgabe eintragen, merken oder anlegen moechte. Faelligkeitsdatum optional, z.B. "heute", "morgen", "Freitag". Beispiel: [ACTION:ADDTASK] Steuererklärung prüfen | morgen
[ACTION:DONETASK] aufgabe - Aufgabe in Todoist als erledigt markieren. Nutze wenn {USER_ADDRESS} sagt dass etwas erledigt ist oder abgehakt werden soll.
[ACTION:CALENDAR] - Termine aus Google Kalender abrufen. Nutze wenn {USER_ADDRESS} nach Terminen, dem Kalender, was wann ansteht oder ihrer Woche fragt.
[ACTION:ADDCAL] titel | datum uhrzeit - Neuen Termin in Google Kalender eintragen. Beispiel: [ACTION:ADDCAL] Mandantengespraech | morgen 14 Uhr
[ACTION:NOTE] titel | inhalt - Neue Notiz in macOS Notizen-App anlegen. Nutze wenn {USER_ADDRESS} etwas notieren, festhalten oder merken moechte. Inhalt optional. Beispiel: [ACTION:NOTE] Mandant Müller | Hat wegen Betriebsprüfung angerufen, Rückruf morgen

WENN {USER_NAME} "Jarvis bereit" sagt (sie hat nur "Jarvis" gesagt, kein Befehl):
- KEINE Begrüßung, kein Wetter, keine Aufgaben, keine Neuigkeiten.
- Ein einziger kurzer Satz — trocken und bereit. Beispiele: "Bitte." / "Zu Diensten." / "Ich höre."
- Warte auf die eigentliche Anfrage. Wenn die Anfrage kommt und es Wochenende/Feiertag/Abend ist, kommentiere es einmalig kurz (ein Halbsatz), dann führe die Aufgabe aus.

WENN {USER_NAME} "Jarvis activate" sagt:
- Begruesse sie passend zur Tageszeit (aktuelle Zeit: {{time}}).
- Gebe eine kurze Info ueber das Wetter in {CITY} — Temperatur, Sonne/Regen, Gefuehlstemperatur. Keine Luftfeuchtigkeit.
- Ist heute ein normaler Werktag: Erwaehne Aufgaben NICHT im Begrueßungstext — nutze [ACTION:TASKS] um sie einmalig abzurufen und zusammenzufassen.
- Ist heute ein Wochenende oder Feiertag: Nutze KEINE [ACTION:TASKS]. Frage stattdessen am Ende der Begruessing kurz und trocken ob {USER_ADDRESS} die Aufgabenliste hoeren moechte — schliesslich ist heute kein Arbeitstag. Wenn {USER_ADDRESS} ja sagt, dann [ACTION:TASKS].
- Wenn unter "AKTUELLE DATEN" BFH-Neuigkeiten der letzten 3 Tage aufgelistet sind, erwaehne die wichtigsten kurz in der Begruessing — ein knapper Satz genuegt, kein Auflisten.
- Sei kreativ. Abends (ab 18 Uhr): Feierabend betonen, Erholung einfordern.

=== AKTUELLE DATEN ==={weather_block}{task_block}{steuer_block}{steuer_recent_block}
==="""


def get_system_prompt():
    return build_system_prompt().replace("{time}", time.strftime("%H:%M"))


def extract_action(text: str):
    match = ACTION_PATTERN.search(text)
    if match:
        clean = text[:match.start()].strip()
        return clean, {"type": match.group(1), "payload": match.group(2).strip()}
    return text, None


def _split_text(text: str) -> list:
    """Split text into <=250-char chunks at sentence boundaries."""
    if len(text) <= 250:
        return [text]
    chunks = []
    sentences = re.split(r'(?<=[.!?])\s+', text)
    current = ""
    for s in sentences:
        if len(current) + len(s) > 250 and current:
            chunks.append(current.strip())
            current = s
        else:
            current = (current + " " + s).strip()
    if current:
        chunks.append(current.strip())
    return chunks


async def _tts_one(text: str) -> bytes:
    """Generate TTS for a single short text chunk."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    try:
        resp = await http.post(url, headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }, json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.85},
        })
        print(f"  TTS chunk status: {resp.status_code}, size: {len(resp.content)}", flush=True)
        if resp.status_code == 200:
            return resp.content
        print(f"  TTS error: {resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"  TTS EXCEPTION: {e}", flush=True)
    return b""


async def speak(text: str, ws: WebSocket, display: str = "") -> bool:
    """Generate TTS and send each chunk immediately. Returns False if connection lost."""
    if not text.strip():
        return True
    chunks = _split_text(text)
    first = True
    for chunk in chunks:
        audio = await _tts_one(chunk)
        if audio:
            try:
                await ws.send_json({
                    "type": "response",
                    "text": display if first else "",
                    "audio": base64.b64encode(audio).decode("utf-8"),
                })
                first = False
            except Exception:
                print("  [speak] WebSocket closed, aborting TTS.", flush=True)
                return False
    return True


async def execute_action(action: dict) -> str:
    t = action["type"]
    p = action["payload"]

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Suche fehlgeschlagen: {result.get('error', '')}"

    elif t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Seite nicht erreichbar: {result.get('error', '')}"

    elif t == "OPEN":
        result = await browser_tools.open_url(p)
        if not result.get("success"):
            return f"Diese URL kann ich nicht oeffnen, {USER_ADDRESS}. Nur http- und https-Adressen sind erlaubt."
        return f"Geoeffnet: {p}"

    elif t == "SCREEN":
        return await screen_capture.describe_screen(ai)

    elif t == "NEWS":
        result = await browser_tools.fetch_news()
        return result

    elif t == "MAIL":
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, mail_tools.get_unread_mails, 5)
        if result == "KEINE_MAILS":
            return "KEINE_MAILS"
        return result

    elif t == "TASKS":
        if not TODOIST_TOKEN or TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        return await todoist_tools.get_tasks(TODOIST_TOKEN)

    elif t == "ADDTASK":
        if not TODOIST_TOKEN or TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        parts = p.split("|", 1)
        content = parts[0].strip()
        due = parts[1].strip() if len(parts) > 1 else ""
        return await todoist_tools.add_task(TODOIST_TOKEN, content, due)

    elif t == "DONETASK":
        if not TODOIST_TOKEN or TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return "Todoist API-Token nicht konfiguriert."
        return await todoist_tools.complete_task(TODOIST_TOKEN, p)

    elif t == "CALENDAR":
        return await google_calendar_tools.get_events(days=7)

    elif t == "ADDCAL":
        parts = p.split("|", 1)
        title = parts[0].strip()
        when = parts[1].strip() if len(parts) > 1 else "morgen 10 Uhr"
        return await google_calendar_tools.add_event(title, when)

    elif t == "NOTE":
        parts = p.split("|", 1)
        title = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, notes_tools.add_note, title, body)

    elif t == "STEUERNEWS":
        # Use cached brief if fresh, otherwise fetch live
        if STEUER_BRIEF and STEUER_BRIEF_DATE == datetime.date.today().isoformat():
            return STEUER_BRIEF
        await refresh_steuer_brief()
        return STEUER_BRIEF if STEUER_BRIEF else "Keine neuen Veroeffentlichungen abrufbar."

    return ""


async def process_message(session_id: str, user_text: str, ws: WebSocket):
    """Process message and send responses via WebSocket."""
    global _last_greeting_time

    if session_id not in conversations:
        conversations[session_id] = []

    # Refresh weather + tasks + steuer-recent on activate
    if "activate" in user_text.lower():
        now = time.time()
        if now - _last_greeting_time < GREETING_COOLDOWN:
            print(f"[jarvis] Doppelbegrüßung blockiert (Cooldown {GREETING_COOLDOWN}s)", flush=True)
            return
        _last_greeting_time = now
        await refresh_data()
        await refresh_steuer_recent()

    _append_message(session_id, "user", user_text)
    history = conversations[session_id][-16:]

    # LLM call
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=get_system_prompt(),
        messages=history,
    )
    reply = response.content[0].text
    print(f"  LLM raw: {reply[:200]}", flush=True)
    spoken_text, action = extract_action(reply)

    # Speak the main response immediately (chunk by chunk — no large WS messages)
    if spoken_text:
        print(f"  Jarvis: {spoken_text[:80]}", flush=True)
        _append_message(session_id, "assistant", spoken_text)
        if not await speak(spoken_text, ws, display=spoken_text):
            return  # WebSocket lost, abort

    # Execute action if any
    if action:
        print(f"  Action: {action['type']} -> {action['payload'][:100]}", flush=True)

        if action["type"] == "SCREEN":
            await speak("Lassen Sie mich einen Blick auf Ihren Bildschirm werfen.", ws,
                        display="Lassen Sie mich einen Blick auf Ihren Bildschirm werfen.")
        elif action["type"] == "MAIL":
            await speak("Ich werfe einen Blick in Ihren Posteingang, Madam.", ws,
                        display="Ich werfe einen Blick in Ihren Posteingang, Madam.")

        try:
            action_result = await execute_action(action)
            print(f"  Result: {str(action_result)[:200]}", flush=True)
        except Exception as e:
            print(f"  Action error: {e}", flush=True)
            action_result = f"Fehler: {e}"

        if action["type"] == "OPEN":
            # OPEN normally stays silent; speak only when the URL was rejected.
            if isinstance(action_result, str) and action_result.startswith("Diese URL"):
                _append_message(session_id, "assistant", action_result)
                await speak(action_result, ws, display=action_result)
            return

        # Empty-result sentinels: every "nothing here" tool returns a known
        # constant. Skip the LLM round-trip and speak a hardcoded butler line
        # instead. Same shape for MAIL / CALENDAR / TASKS so behavior stays
        # consistent across actions.
        _EMPTY_REPLIES = {
            "KEINE_MAILS":   f"Ihr Posteingang ist leer, {USER_ADDRESS}. Eine seltene Erscheinung.",
            "KEINE_TERMINE": f"Ihr Kalender ist die naechsten Tage frei, {USER_ADDRESS}. Erholung in Sicht.",
            "KEINE_TASKS":   f"Keine offenen Aufgaben, {USER_ADDRESS}. Eine angenehme Lage.",
        }
        if isinstance(action_result, str) and action_result in _EMPTY_REPLIES:
            msg = _EMPTY_REPLIES[action_result]
            _append_message(session_id, "assistant", msg)
            await speak(msg, ws, display=msg)
            return

        if action_result and "fehlgeschlagen" not in action_result:
            if action["type"] in ("STEUERNEWS", "ADDTASK", "DONETASK", "ADDCAL", "NOTE"):
                _append_message(session_id, "assistant", action_result)
                await speak(action_result, ws, display=action_result)
                return

            if action["type"] == "MAIL":
                summary_system = (
                    f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
                    f"Gib eine KURZE ueberblickende Info zu den ungelesenen E-Mails — maximal 2 Saetze. "
                    f"Lies KEINE einzelnen Mails vor. Nenne nur die Anzahl, wer geschrieben hat und ob etwas Dringendes dabei ist. "
                    f"Ton: trocken, knapp, Butler-Stil. Sprich {USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
                )
            elif action["type"] == "NEWS":
                summary_system = (
                    f"Du bist Jarvis, der britisch-hoefliche KI-Butler. "
                    f"Fasse die Nachrichtenlage in maximal 2-3 praegnanten Saetzen zusammen — wie ein Butler der die Zeitung ueberflogen hat. "
                    f"Nenne nur die 2-3 wichtigsten Themen, kein Auflisten einzelner Meldungen. "
                    f"Ton: trocken, informiert, kein Journalistendeutsch. Sprich {USER_ADDRESS} an. KEINE Tags in eckigen Klammern."
                )
            else:
                summary_system = (
                    f"Du bist Jarvis. Fasse die folgenden Informationen KURZ auf Deutsch zusammen, "
                    f"maximal 2-3 Saetze, im Jarvis-Stil. Sprich den Nutzer als {USER_ADDRESS} an. "
                    f"KEINE Tags in eckigen Klammern. KEINE ACTION-Tags."
                )
            summary_resp = await ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system=summary_system,
                messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
            )
            summary = summary_resp.content[0].text
            summary, _ = extract_action(summary)
        else:
            summary = f"Das hat leider nicht funktioniert, {USER_ADDRESS}."

        _append_message(session_id, "assistant", summary)
        await speak(summary, ws, display=summary)


# Aktive WebSocket-Verbindungen für Broadcast
active_clients: list = []
_last_activate_time: float = 0.0
ACTIVATE_COOLDOWN = 90.0   # Sekunden zwischen zwei /activate-Aufrufen
_last_greeting_time: float = 0.0
GREETING_COOLDOWN = 10.0   # Sekunden zwischen zwei Begrüßungen (verhindert Doppelbegrüßung)


def _hide_chrome():
    script = 'tell application "System Events" to set visible of process "Google Chrome" to false'
    subprocess.Popen(["osascript", "-e", script])


def _show_chrome():
    script = 'tell application "Google Chrome" to activate'
    subprocess.Popen(["osascript", "-e", script])


def require_jarvis_token(x_jarvis_token: str | None = Header(default=None)):
    """FastAPI dependency. No-op when JARVIS_AUTH_TOKEN is unset, otherwise
    rejects requests without a matching `X-Jarvis-Token` header."""
    if not JARVIS_AUTH_TOKEN:
        return
    if x_jarvis_token != JARVIS_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Jarvis-Token")


@app.get("/hide", dependencies=[Depends(require_jarvis_token)])
async def hide_endpoint():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _hide_chrome)
    return {"ok": True}


@app.get("/show", dependencies=[Depends(require_jarvis_token)])
async def show_endpoint():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _show_chrome)
    return {"ok": True}


@app.get("/activate", dependencies=[Depends(require_jarvis_token)])
async def activate_endpoint():
    """Vom Clap-Trigger aufgerufen: Jarvis aufwecken.
    Debounce: maximal einmal alle 90 Sekunden.
    Sendet NUR an den zuletzt verbundenen Client."""
    global _last_activate_time
    now = time.time()
    if now - _last_activate_time < ACTIVATE_COOLDOWN:
        remaining = int(ACTIVATE_COOLDOWN - (now - _last_activate_time))
        print(f"[jarvis] /activate ignoriert (Cooldown noch {remaining}s)", flush=True)
        return {"ok": False, "reason": f"cooldown {remaining}s"}
    _last_activate_time = now
    # Veraltete Verbindungen bereinigen, nur letzten Client wecken
    if not active_clients:
        print(f"[jarvis] /activate: kein Client verbunden", flush=True)
        return {"ok": False, "reason": "no clients"}
    target = active_clients[-1]
    print(f"[jarvis] Wake-Signal an letzten Client ({len(active_clients)} gesamt)", flush=True)
    try:
        await target.send_json({"type": "wake"})
    except Exception:
        active_clients.remove(target)
        return {"ok": False, "reason": "client send failed"}
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = str(id(ws))
    # Alte Verbindungen aus der Liste entfernen (verhindert Mehrfach-Wake)
    active_clients.clear()
    active_clients.append(ws)
    print(f"[jarvis] Client connected (Liste bereinigt)", flush=True)

    async def keepalive():
        while True:
            await asyncio.sleep(15)
            try:
                await ws.send_json({"type": "ping"})
            except Exception:
                break

    asyncio.create_task(keepalive())

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "pong":
                continue
            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            print(f"  You:    {user_text}", flush=True)
            await process_message(session_id, user_text, ws)

    except (WebSocketDisconnect, RuntimeError, Exception) as e:
        print(f"[jarvis] Client disconnected: {type(e).__name__}", flush=True)
        conversations.pop(session_id, None)
        if ws in active_clients:
            active_clients.remove(ws)


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 50, flush=True)
    print("  J.A.R.V.I.S. V2 Server", flush=True)
    print(f"  http://localhost:8340", flush=True)
    print("=" * 50, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8340)
