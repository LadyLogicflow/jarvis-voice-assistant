# J.A.R.V.I.S. — Personal AI Voice Assistant

[![CI](https://github.com/LadyLogicflow/jarvis-voice-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/LadyLogicflow/jarvis-voice-assistant/actions/workflows/ci.yml)

> Double-clap. Jarvis wakes up, greets you with the weather and your tasks, answers your questions with dry British wit, controls your browser, and sees your screen.

Built entirely with [Claude Code](https://claude.ai/code) — no code written manually.

---

## Youtube Video

[Demo & Explaination](https://youtu.be/XsceN-hEit4)

---

## Features

### Core (from the original template)
- **Double-Clap Trigger** — Clap twice and your entire workspace launches: Spotify, VS Code, Obsidian, Chrome with Jarvis UI
- **Voice Conversation** — Speak freely with Jarvis through your microphone. He listens, thinks, and responds with voice
- **Sarcastic British Butler** — Jarvis speaks German with the personality of Tony Stark's AI: dry, witty, and always one step ahead
- **Weather & Tasks** — On startup, Jarvis greets you with the current weather and a summary of your open tasks
- **Browser Automation** — "Search for MiroFish" → Jarvis opens a real browser, navigates to the page, reads the content, and summarizes it for you
- **Screen Vision** — "What's on my screen?" → Jarvis takes a screenshot, analyzes it with Claude Vision, and describes what he sees
- **World News** — "What's happening in the world?" → Jarvis fetches current Tagesschau headlines and summarizes them
- **Window Snapping** — All launched apps automatically snap into quadrants on your screen

### Fork extensions (this fork only)
- **Wake-Word Trigger** — Optional Picovoice-powered "Jarvis" wake word (in addition to the double-clap)
- **Google Calendar** — "Was steht heute an?" → Jarvis reads your upcoming events; "Trag morgen 10 Uhr Mandantengespraech ein" → adds the event
- **Mail.app (macOS)** — "Wie sieht mein Posteingang aus?" → Jarvis reports the unread count and the most recent senders
- **Notes.app (macOS)** — "Notiere: Mandant Mueller hat angerufen" → Jarvis creates a timestamped note
- **Todoist** — Aufgaben lesen, anlegen ("Trage Steuererklaerung pruefen ein"), und als erledigt markieren
- **Steuer-News** — Daily morning brief from BFH-Pressemitteilungen + BFH-Entscheidungen RSS feeds, summarized in two sentences
- **NRW-Feiertage / Wochenende-Modus** — Jarvis erkennt Wochenenden und gesetzliche Feiertage in NRW und besteht freundlich auf Erholung
- **Abendmodus** — ab 18:00 Uhr weist Jarvis auf Feierabend hin

---

## Architecture

```
                       You (speak via mic in Chrome)
                                  │
                  Web Speech API → Chrome Browser → WebSocket
                                  │
                          FastAPI Server (local, port 8340)
                                  │
                          Claude Haiku (decides + responds)
                                  │
        ┌──────────────────────┬──┴──┬──────────────────────────────┐
        │                      │     │                              │
   ElevenLabs TTS        Playwright   Claude Vision           Tool Layer
   (speaks back)        (browser)    (screenshot)                  │
                                                                   │
        ┌──────────┬───────────┬───────────┬───────────┬──────────┐│
        │          │           │           │           │          ││
   Google Cal  Mail.app   Notes.app   Todoist API  BFH RSS  Tagesschau RSS
   (OAuth)   (AppleScr.)  (AppleScr.) (REST)      (XML)    (XML)
                                  │
                       Audio → Chrome → You (hear)
```

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Speech Input | Web Speech API (Chrome) | Converts your voice to text |
| Server | FastAPI (Python 3.10+) | Local orchestration — runs on your machine |
| Brain | Claude Haiku (Anthropic) | Thinks, decides, formulates responses |
| Voice | ElevenLabs TTS | Converts text to natural German speech |
| Browser Control | Playwright | Automates a real browser you can see |
| Screen Vision | Claude Vision + Pillow | Screenshots and describes your screen |
| Clap Detection | sounddevice + numpy | Listens for double-clap to launch everything |
| Wake Word | Picovoice Porcupine | Optional "Jarvis" trigger phrase |
| Google Calendar | google-api-python-client + dateparser | Read events; create events from natural-language dates |
| Mail / Notes (macOS) | AppleScript via osascript | Read unread inbox; create notes |
| Todoist | httpx + Todoist REST API | Read / add / complete tasks |
| Steuer-News | httpx + RSS | Daily morning brief from BFH feeds |
| Window Management | PowerShell + Win32 API (Win) / AppleScript (Mac) | Snaps windows into screen quadrants |

---

## Prerequisites

- **Windows 10/11** or **macOS 12+** (an Apple Silicon path is in `SETUP.md`)
- **Python 3.10 or newer** — 3.9 is end-of-life; google-auth and urllib3 will warn,
  and pinned features will start breaking. Recommended: 3.12.
- **Google Chrome**
- **[Claude Code](https://claude.ai/code)** (recommended for setup)

### API Keys Needed

| Service | What For | Cost | Link |
|---------|----------|------|------|
| Anthropic | Claude Haiku (the brain) | ~$0.25 / 1M tokens | [console.anthropic.com](https://console.anthropic.com) |
| ElevenLabs | Voice (text-to-speech) | Free tier: 10k chars/month | [elevenlabs.io](https://elevenlabs.io) |

---

## Quick Start

### Option A: Setup with Claude Code (Recommended)

1. Clone the repo:
   ```bash
   git clone https://github.com/LadyLogicflow/jarvis-voice-assistant.git
   cd jarvis-voice-assistant
   ```

2. Open in VS Code, start Claude Code, and say:
   ```
   Set up Jarvis for me.
   ```

3. Claude Code will ask for your API keys, name, preferences, and configure everything automatically.

### Option B: Manual Setup

1. **Clone and install dependencies:**
   ```bash
   git clone https://github.com/LadyLogicflow/jarvis-voice-assistant.git
   cd jarvis-voice-assistant
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Create `.env`** from the template (this file holds your **secrets**):
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and fill in:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ELEVENLABS_API_KEY=sk_...
   TODOIST_API_TOKEN=...        # optional
   PICOVOICE_ACCESS_KEY=...     # optional, for wakeword-trigger
   ```

3. **Create `config.json`** from the template (non-secret settings only):
   ```bash
   cp config.example.json config.json
   ```
   Then edit `config.json`:
   ```json
   {
     "elevenlabs_voice_id": "YOUR_VOICE_ID",
     "user_name": "Your Name",
     "user_address": "Sir",
     "city": "Hamburg",
     "workspace_path": "C:\\path\\to\\jarvis-voice-assistant",
     "spotify_track": "spotify:track:YOUR_TRACK_ID",
     "browser_url": "https://your-website.com",
     "obsidian_inbox_path": "C:\\path\\to\\obsidian\\inbox",
     "apps": ["obsidian://open"]
   }
   ```
   > Both `.env` and `config.json` are gitignored. Never commit them.

4. **Start Jarvis:**
   ```bash
   python server.py
   ```

5. **Open Chrome** and go to `http://localhost:8340`

6. **Click anywhere** on the page, then speak!

---

## Usage

### Start Jarvis manually
```bash
python server.py
```
Then open `http://localhost:8340` in Chrome.

### Start everything with a double-clap
```bash
python scripts/clap-trigger.py
```
Clap twice → Spotify plays your song, VS Code opens, Obsidian opens, Chrome opens with Jarvis. All windows snap into quadrants.

### Auto-start on Windows login
1. Open Task Scheduler (`Win + R` → `taskschd.msc`)
2. Create Task → Trigger: "At log on"
3. Action: `powershell` with argument:
   ```
   -ExecutionPolicy Bypass -Command "python C:\path\to\scripts\clap-trigger.py"
   ```

---

## What You Can Say

### Core
| Command | What Happens |
|---------|-------------|
| *"Jarvis activate"* | Greeting: weather, tasks, BFH news of the day |
| *"Suche AI-Nachrichten"* | Opens browser, searches DuckDuckGo, summarizes the first result |
| *"Oeffne skool.com"* | Opens the URL (http/https only) in your browser |
| *"Was siehst du auf meinem Bildschirm?"* | Screenshot + Claude Vision description |
| *"Was gibt es Neues in der Welt?"* | Tagesschau RSS, summarized in two sentences |

### Fork extensions
| Command | What Happens |
|---------|-------------|
| *"Zeig mir meinen Kalender"* | Reads the next 7 days from Google Calendar |
| *"Trag morgen 14 Uhr Mandantengespraech ein"* | Creates a Google Calendar event |
| *"Wie sieht mein Posteingang aus?"* | Reads unread mails from Mail.app, summarizes |
| *"Notiere: Mueller hat angerufen"* | Creates a timestamped note in Notes.app |
| *"Was steht heute an?"* | Lists open Todoist tasks (overdue / today / upcoming) |
| *"Trage Steuererklaerung pruefen ein"* | Adds a task to Todoist |
| *"Steuererklaerung ist erledigt"* | Closes the matching Todoist task |
| *"Was gibt es Neues im Steuerrecht?"* | BFH-Pressemitteilungen + -Entscheidungen, summarized |
| *Any question* | Jarvis answers in his sarcastic butler style |

---

## Project Structure

```
jarvis-voice-assistant/
├── server.py              # FastAPI backend — the brain
├── browser_tools.py       # Playwright browser automation
├── screen_capture.py      # Screenshot + Claude Vision
├── .env                   # Your secrets — API keys (gitignored)
├── .env.example           # Template for .env
├── config.json            # Your non-secret settings (gitignored)
├── config.example.json    # Template for config.json
├── requirements.txt       # Python dependencies
├── frontend/
│   ├── index.html         # Jarvis web UI
│   ├── main.js            # Speech recognition + WebSocket + audio
│   └── style.css          # Dark theme with animated orb
├── scripts/
│   ├── clap-trigger.py    # Double-clap detection
│   └── launch-session.ps1 # Launches all apps + window snapping
├── CLAUDE.md              # Instructions for Claude Code
└── SETUP.md               # Detailed setup guide
```

---

## Customization

### Change Jarvis's personality
Edit the system prompt in `server.py` → `build_system_prompt()`. The personality, greeting behavior, and action instructions are all defined there.

### Change which apps launch
Edit `config.json`:
```json
{
  "spotify_track": "spotify:track:YOUR_TRACK_ID",
  "browser_url": "https://your-website.com",
  "apps": ["obsidian://open", "slack://"]
}
```

### Change the voice
Find a voice on [elevenlabs.io](https://elevenlabs.io), copy the Voice ID, and set it in `config.json`:
```json
{
  "elevenlabs_voice_id": "YOUR_VOICE_ID"
}
```

### Change the weather city
```json
{
  "city": "Berlin"
}
```

### Adjust clap sensitivity
In `scripts/clap-trigger.py`:
```python
THRESHOLD = 0.15  # Lower = more sensitive
MAX_GAP = 1.2     # Seconds between claps
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Jarvis doesn't speak | Check if server is running. Kill old process: `taskkill /f /im python.exe` then restart |
| "Connection lost" in browser | Old server still running on port 8340. Kill it and restart |
| Clap not detected | Lower `THRESHOLD` in `clap-trigger.py` (try 0.10) |
| Browser search fails | Run `playwright install chromium` |
| No audio in Chrome | Click anywhere on the page first (Chrome autoplay policy) |
| Jarvis says "Sir planen" instead of "Sie planen" | Update the system prompt grammar rules in `server.py` |

---

## Mac Users

This template is built for Windows. If you're on macOS, clone the repo and tell Claude Code:

```
Convert this project to work on macOS.
```

Claude Code will adapt the PowerShell scripts to shell scripts, adjust paths, and handle macOS-specific differences.

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — Python web framework for the local server
- **[Claude Haiku](https://anthropic.com)** — Fast, affordable AI model (the brain)
- **[ElevenLabs](https://elevenlabs.io)** — Natural text-to-speech (the voice)
- **[Playwright](https://playwright.dev)** — Browser automation
- **[Web Speech API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API)** — Browser-native speech recognition
- **[sounddevice](https://python-sounddevice.readthedocs.io/)** — Audio input for clap detection

---

## Credits

Built by [Julian](https://skool.com/ki-automatisierung) with [Claude Code](https://claude.ai/code).

Inspired by Iron Man's J.A.R.V.I.S. — *"At your service, Sir."*

---

## License

MIT — use it, modify it, build on it. If you build something cool, let me know!
