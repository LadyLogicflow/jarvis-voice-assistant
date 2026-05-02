# Jarvis Setup Guide

Dein persoenlicher KI-Assistent — inspiriert von Iron Mans Jarvis.
Diese Anleitung beschreibt den Mac-Setup. Eine Windows-Variante (PowerShell) ist ebenfalls vorhanden.

**Was du bekommst:**
- Zweimal klatschen → dein komplettes Arbeits-Setup startet
- Jarvis begruesst dich mit Wetter und deinen Aufgaben
- Du sprichst frei mit Jarvis — er antwortet per Stimme
- Jarvis kann deinen Browser steuern (suchen, Seiten oeffnen)
- Jarvis kann deinen Bildschirm sehen und beschreiben

---

## Voraussetzungen (macOS)

- **macOS 12+** (Apple Silicon oder Intel)
- **Google Chrome** (fuer Spracheingabe + Jarvis UI)
- **Homebrew** (Installer: https://brew.sh)
- **Python 3.10 oder neuer** — Apples Standard-Python (3.9.x) reicht NICHT.
  Google-Bibliotheken warnen, urllib3 warnt, kuenftige Updates werden brechen.

  Pruefe deine Version:
  ```bash
  python3 --version
  ```

  Falls < 3.10:
  ```bash
  brew install python@3.12
  # ab jetzt explizit python3.12 verwenden:
  python3.12 -m pip install -r requirements.txt
  python3.12 -m playwright install chromium
  python3.12 server.py
  ```

  Optional `python3` per `~/.zprofile` auf 3.12 zeigen lassen
  (`alias python3=/opt/homebrew/bin/python3.12` oder via `ln -s`),
  damit auch die Skripte unter `scripts/` automatisch die neue Version nehmen.
- **Claude Code** installiert

Pip-Pakete und Browser-Treiber werden automatisch von Claude Code installiert.

---

## Setup starten

Oeffne diesen Ordner in VS Code, starte Claude Code, und sag:

> Richte Jarvis fuer mich ein.

Claude Code fragt dich dann nach:

1. **Dein Name** und wie du angesprochen werden willst (z.B. "Sir", "Chef", Vorname)
2. **Anthropic API Key** — von https://console.anthropic.com (fuer Claude Haiku, das Gehirn)
3. **ElevenLabs API Key** — von https://elevenlabs.io (fuer die Stimme)
4. **Spotify-Song** — Link zum Song der beim Start spielen soll (optional)
5. **Programme** — welche Apps sollen beim Doppelklatschen starten?
6. **Website** — welche Seite soll im Browser aufgehen?
7. **Stadt fuers Wetter** — z.B. Hamburg
8. **Obsidian Vault** — optional, welcher Ordner soll Jarvis kennen?

---

## Was Claude Code fuer dich einrichtet

### 1. Voraussetzungen installieren (macOS)
Claude Code prueft und installiert automatisch:
- **Homebrew** (falls nicht vorhanden)
- **Python 3.10+** (via `brew install python@3.12`)
- **Alle Python-Pakete** (`python3 -m pip install -r requirements.txt`)
- **Playwright Chromium** (`python3 -m playwright install chromium`)

Manuell musst du nur Mac-Berechtigungen freigeben (Systemeinstellungen → Datenschutz & Sicherheit):
- **Mikrofon** (Terminal) — fuer Doppelklatschen-Trigger
- **Bildschirmaufnahme** (Terminal) — fuer "Was siehst du auf meinem Bildschirm?"
- **Bedienungshilfen** (Terminal) — fuer das automatische Anordnen von Fenstern

### 2. .env (Secrets) und config.json (Settings) erstellen

Secrets liegen seit M1.1 in einer **`.env`-Datei** (gitignored), nicht mehr in
`config.json`. Beides wird angelegt:

**`.env`** (kopiere aus `.env.example` und fuelle die Werte ein):
```bash
cp .env.example .env
# Datei oeffnen und Werte einfuegen:
#   ANTHROPIC_API_KEY=sk-ant-...
#   ELEVENLABS_API_KEY=sk_...
#   TODOIST_API_TOKEN=...        (optional)
#   PICOVOICE_ACCESS_KEY=...     (optional, fuer wakeword-trigger)
```

**`config.json`** (kopiere aus `config.example.json`) — enthaelt KEINE Secrets mehr:
```json
{
  "elevenlabs_voice_id": "VOICE_ID",
  "user_name": "Dein Name",
  "user_address": "Sir",
  "user_role": "Deine Rolle",
  "city": "Hamburg",
  "workspace_path": "/Users/DEIN_USER/Downloads/jarvis-voice-assistant-master",
  "spotify_track": "spotify:track:DEIN_TRACK_ID",
  "browser_url": "https://deine-website.com",
  "obsidian_inbox_path": "/Users/DEIN_USER/Documents/Obsidian/inbox",
  "apps": ["obsidian://open"],
  "morning_hour": 7
}
```

> **Sicherheit:** `.env` und `config.json` stehen in `.gitignore` und duerfen
> nie committed werden. Bei Verdacht auf Leak: Tokens in den jeweiligen
> Provider-Dashboards (Anthropic, ElevenLabs, Todoist, Picovoice) rotieren.

### 3. ElevenLabs Stimme
Eine deutsche Stimme auswaehlen und die Voice ID in die Config eintragen. Empfehlung: **Felix Serenitas** (Starter Plan noetig) oder eine der Standard-Stimmen (Free Plan).

### 4. Systemprompt
Der Systemprompt wird in `server.py` automatisch aus der Config generiert. Er enthaelt:
- Jarvis-Persoenlichkeit (trocken, sarkastisch, britisch-hoeflich)
- Siezen mit gewaehlter Anrede
- Wetter- und Aufgaben-Integration
- Browser-Steuerung via Action-Tags
- Screen-Capture-Faehigkeit

---

## Architektur

```
Mikrofon (Chrome) → Web Speech API → WebSocket → FastAPI Server
                                                      ↓
                                                Claude Haiku (denkt)
                                                      ↓
                                    ┌─────────────────┼──────────────────┐
                                    ↓                 ↓                  ↓
                            ElevenLabs TTS     Playwright Browser   Screen Capture
                            (spricht)          (sucht/oeffnet)     (sieht Bildschirm)
                                    ↓
                            Audio → Browser Speaker
```

---

## Starten

### Jarvis manuell starten (macOS)
```
python3 server.py
```
Dann http://localhost:8340 in Chrome oeffnen.

### Alles per Doppelklatschen starten
```
python3 scripts/clap-trigger.py
```
Zweimal klatschen → Spotify, Chrome mit Jarvis und konfigurierte Apps starten automatisch.

### Clap Trigger beim macOS-Login
Ueber **Systemeinstellungen → Allgemein → Anmeldeobjekte → "+"** ein kleines Shell-Skript hinzufuegen:
```bash
#!/usr/bin/env bash
cd /Users/DEIN_USER/Downloads/jarvis-voice-assistant-master
/usr/bin/env python3 scripts/clap-trigger.py &
```
Skript speichern, ausfuehrbar machen (`chmod +x`) und in Anmeldeobjekte aufnehmen.

Alternativ via `launchd`: ein `~/Library/LaunchAgents/com.jarvis.clap.plist` mit `RunAtLoad=true` anlegen.

---

## Was Jarvis kann

- **"Wie ist das Wetter?"** → kennt das aktuelle Wetter
- **"Such nach MiroFish"** → oeffnet Browser, sucht, liest Ergebnisse
- **"Oeffne skool.com"** → oeffnet die Seite im Browser
- **"Was siehst du auf meinem Bildschirm?"** → macht Screenshot, beschreibt was er sieht
- **Jede Frage** → antwortet im Jarvis-Stil mit Stimme

---

## Fehlerbehebung (macOS)

| Problem | Loesung |
|---|---|
| Jarvis spricht nicht | Pruefen ob Server laeuft (`python3 server.py`) und Chrome offen ist |
| "Verbindung verloren" | Alter Server-Prozess laeuft noch → `pkill -f "python3 server.py"` und neu starten |
| Klatschen wird nicht erkannt | Threshold in `clap-trigger.py` anpassen, Mikrofon-Berechtigung pruefen |
| Browser-Suche geht nicht | `python3 -m playwright install chromium` ausfuehren |
| Kein Audio im Browser | Einmal auf die Seite klicken (Chrome Autoplay-Policy) |
| Screenshot ist schwarz | Bildschirmaufnahme-Berechtigung fuer Terminal aktivieren |
| Fenster werden nicht arrangiert | Bedienungshilfen-Berechtigung fuer Terminal aktivieren |
| `pip install` schlaegt fehl | `python3 -m pip install --upgrade pip` und erneut probieren |

---

## Fuer Claude Code: Setup-Anweisungen

Wenn ein Nutzer "Richte Jarvis ein" sagt, fuehre folgende Schritte aus:

**Schritt 0 — Voraussetzungen pruefen und installieren (macOS):**
- Pruefe Homebrew: `which brew`. Falls nicht: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- Pruefe Python 3.10+: `python3 --version`. Falls < 3.10: `brew install python@3.12`
- Pruefe Chrome: `ls /Applications/ | grep "Google Chrome"`. Falls nicht: `brew install --cask google-chrome`
- Installiere Python-Pakete: `python3 -m pip install -r requirements.txt`
- Installiere Playwright: `python3 -m playwright install chromium`
- Weise den Nutzer auf die noetigen Datenschutz-Berechtigungen hin (Mikrofon, Bildschirmaufnahme, Bedienungshilfen)

**Schritt 1 — Nutzerdaten abfragen:**
Frage nach:
- Name (z.B. "Caterina")
- Taetigkeit/Rolle (z.B. "KI-Beraterin und Designerin")
- Gewuenschte Anrede (z.B. "Sir", "Chef", oder einfach Vorname)
- Anthropic API Key (von https://console.anthropic.com)
- ElevenLabs API Key (von https://elevenlabs.io)
- Spotify-Song (Link zum Song der beim Start spielen soll, optional)
- Programme die beim Doppelklatschen starten sollen
- Website die im Browser aufgehen soll
- Stadt fuers Wetter
- Obsidian Vault Pfad (optional)

**Schritt 2 — `.env` und `config.json` erstellen:**
- `.env` aus `.env.example` kopieren und mit den API-Keys (Anthropic, ElevenLabs, optional Todoist/Picovoice) befuellen.
- `config.json` aus `config.example.json` mit den nicht-sensiblen Nutzerdaten anlegen. Setze den `workspace_path` auf den aktuellen Ordnerpfad (`pwd`).
- Beide Dateien sind gitignored.

**Schritt 3 — ElevenLabs Stimme einrichten:**
- Liste verfuegbare Stimmen via ElevenLabs API
- Empfehle eine deutsche Stimme
- Trage die Voice ID in die Config ein

**Schritt 4 — Systemprompt anpassen:**
Oeffne `server.py` und finde die Funktion `build_system_prompt()`. Ersetze:
- Jedes "Julian" → Name des Nutzers (kommt mehrfach vor!)
- "KI-Berater und Automatisierungsexperte" → Taetigkeit/Rolle des Nutzers
- Jedes "Sir" als Anrede → gewuenschte Anrede
- "Hamburg" → Stadt des Nutzers

Ausserdem oben in `server.py` bei den Config-Defaults:
- `USER_NAME = config.get("user_name", "Julian")` → Default-Name
- `CITY = config.get("city", "Hamburg")` → Default-Stadt

**Schritt 5 — Testen:**
- Starte den Server: `python3 server.py`
- Oeffne http://localhost:8340 in Chrome
- Pruefe ob Jarvis spricht und antwortet

**Schritt 6 — Optional: Autostart einrichten (Anmeldeobjekte oder launchd)**

---

## Credits

Template von Julian — [Skool Community](https://skool.com/ki-automatisierung)
Mac-Adaption fuer eine persoenliche Installation.
