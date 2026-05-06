# CLAUDE.md

Dieses Workspace ist **Jarvis** — ein persoenlicher KI-Assistent mit Sprachsteuerung, Browser-Kontrolle und Doppelklatschen-Trigger.

**Plattform:** macOS (Apple Silicon / Intel). Eine Windows-Variante (PowerShell-Launcher) ist ebenfalls enthalten.

---

## Fuer Claude Code: Setup-Modus

Wenn der Nutzer nach dem Setup fragt oder "Richte Jarvis ein" sagt, folge den Anweisungen in `SETUP.md`. Frage den Nutzer nach seinem Namen, seiner Taetigkeit, und wie er angesprochen werden moechte — diese Infos muessen in den Systemprompt in `server.py` eingetragen werden (ersetze die aktuellen Platzhalter "Julian", "KI-Berater und Automatisierungsexperte", "Sir").

**WICHTIG — Pruefe und installiere zuerst alle Voraussetzungen (macOS):**

1. **Homebrew**: Pruefe ob Homebrew installiert ist (`which brew`). Falls nicht:
   `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`

2. **Python 3.10+**: Pruefe `python3 --version`. Falls < 3.10:
   `brew install python@3.12`

3. **Google Chrome**: Pruefe `ls /Applications/ | grep "Google Chrome"`. Falls nicht installiert: `brew install --cask google-chrome`.

4. **pip Dependencies**: `python3 -m pip install -r requirements.txt`

5. **Playwright Browser**: `python3 -m playwright install chromium`

6. **Mac-Berechtigungen** (manuell vom Nutzer):
   - **Mikrofon**: Systemeinstellungen → Datenschutz & Sicherheit → Mikrofon → Terminal/iTerm aktivieren (fuer wakeword-trigger)
   - **Bildschirmaufnahme**: Systemeinstellungen → Datenschutz & Sicherheit → Bildschirmaufnahme → Terminal/iTerm aktivieren (fuer screen_capture)
   - **Bedienungshilfen**: Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen → Terminal aktivieren (fuer Fenster-Anordnung via AppleScript)

Erst NACHDEM alle Voraussetzungen installiert sind, fahre mit dem Setup in `SETUP.md` fort (API Keys abfragen, .env + config.json erstellen, etc.).

### API-Keys / Secrets — alle in `.env` (nicht in `config.json`)

Seit M1.1 leben sensible Werte in `.env` (gitignored). `.env.example` ist die
verbindliche Liste. Was Claude Code abfragen sollte:

- **`ANTHROPIC_API_KEY`** — Pflicht. https://console.anthropic.com
- **`ELEVENLABS_API_KEY`** — Pflicht. https://elevenlabs.io
- **`TODOIST_API_TOKEN`** — Optional, fuer [ACTION:TASKS/ADDTASK/DONETASK]. https://app.todoist.com/app/settings/integrations/developer
- **`PICOVOICE_ACCESS_KEY`** — Optional, nur fuer `scripts/wakeword-trigger.py`. https://console.picovoice.ai/
- **`JARVIS_AUTH_TOKEN`** — Optional, schuetzt `/activate`, `/show`, `/hide` (relevant nur wenn Port 8340 erreichbar ueber Loopback hinaus)

Google Calendar nutzt OAuth, kein API-Key. Setup-Flow:
1. `credentials.json` aus der Google Cloud Console (OAuth-Client-Secret) ins Projekt-Root legen
2. Einmalig `python3 scripts/google-auth.py` laufen lassen — Browser oeffnet sich, Nutzer autorisiert, Token landet in `token.json`
3. Beide Dateien sind gitignored.

---

## Workspace Structure

```
.
├── CLAUDE.md                  # This file
├── SETUP.md                   # Setup-Anleitung fuer Claude Code
├── CODE_REVIEW.md             # Roadmap (M1-M6 Issues)
├── LICENSE                    # MIT
├── .env                       # Secrets (gitignored)
├── .env.example               # Template fuer .env
├── config.json                # Non-secret settings (gitignored)
├── config.example.json        # Template fuer config.json
├── requirements.txt           # Python Dependencies
├── server.py                  # FastAPI Backend (Claude Haiku + ElevenLabs TTS)
├── browser_tools.py           # Playwright Browser-Steuerung (Mac + Windows)
├── screen_capture.py          # Screenshot + Claude Vision
├── google_calendar_tools.py   # Google Calendar (lesen + anlegen)
├── mail_tools.py              # macOS Mail.app (ungelesene E-Mails)
├── notes_tools.py             # macOS Notes.app (Notiz anlegen)
├── todoist_tools.py           # Todoist (Aufgaben lesen + anlegen + abschliessen)
├── steuer_news.py             # BFH-Pressemitteilungen + Entscheidungen (RSS)
├── frontend/
│   ├── index.html             # Jarvis Web-UI
│   ├── main.js                # Speech Recognition + WebSocket + Audio
│   └── style.css              # Dark Theme mit Orb-Animation
└── scripts/
    ├── wakeword-trigger.py    # 'Jarvis' Wake-Word via Picovoice
    ├── google-auth.py         # Einmalige Google-OAuth-Autorisierung
    ├── launch-session.sh      # macOS Session-Launcher (Terminal + Spotify + Chrome)
    └── launch-session.ps1     # Windows Session-Launcher (PowerShell)
```
