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
   - **Mikrofon**: Systemeinstellungen → Datenschutz & Sicherheit → Mikrofon → Terminal/iTerm aktivieren (fuer clap-trigger)
   - **Bildschirmaufnahme**: Systemeinstellungen → Datenschutz & Sicherheit → Bildschirmaufnahme → Terminal/iTerm aktivieren (fuer screen_capture)
   - **Bedienungshilfen**: Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen → Terminal aktivieren (fuer Fenster-Anordnung via AppleScript)

Erst NACHDEM alle Voraussetzungen installiert sind, fahre mit dem Setup in `SETUP.md` fort (API Keys abfragen, config.json erstellen, etc.).

---

## Workspace Structure

```
.
├── CLAUDE.md              # This file
├── SETUP.md               # Setup-Anleitung fuer Claude Code
├── config.json            # Persoenliche Config (gitignored)
├── config.example.json    # Template mit Platzhaltern
├── requirements.txt       # Python Dependencies
├── server.py              # FastAPI Backend (Claude Haiku + ElevenLabs TTS)
├── browser_tools.py       # Playwright Browser-Steuerung (Mac + Windows)
├── screen_capture.py      # Screenshot + Claude Vision
├── frontend/
│   ├── index.html         # Jarvis Web-UI
│   ├── main.js            # Speech Recognition + WebSocket + Audio
│   └── style.css          # Dark Theme mit Orb-Animation
└── scripts/
    ├── clap-trigger.py    # Doppelklatschen-Erkennung (cross-platform)
    ├── launch-session.sh  # macOS Session-Launcher (Terminal + Spotify + Chrome)
    └── launch-session.ps1 # Windows Session-Launcher (PowerShell)
```
