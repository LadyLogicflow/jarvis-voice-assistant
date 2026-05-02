# JARVIS Voice Assistant — Code Review

> Stand: 2026-05-02 | Reviewer: Irony (Pandora Agent)
> Basis: vollständiger Review aller hochgeladenen Quelldateien + jarvis.log

## Zusammenfassung

Das Projekt funktioniert grundsätzlich, hat aber eine Reihe konkreter Schwachstellen in
Sicherheit, Fehlerbehandlung, Dependency-Management und Dokumentation. Im Log sind
mindestens zwei aktive Bugs erkennbar (Browser-Singleton stirbt, Python 3.9 statt 3.10+).
Die Erweiterungen (Calendar, Mail, Notes, Todoist, Steuer-News) sind sauber in das
Action-System integriert, aber ihre Dependencies fehlen in `requirements.txt`.

Die Findings sind in **6 Milestones** gebündelt, jeder mit konkreten Issues (S/M/L für Aufwand,
P0–P2 für Priorität).

---

## M1 — 🔒 Security & Secrets (Priorität: HOCH)

**Ziel:** Sensible Daten und Angriffsflächen reduzieren.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 1.1 | Secrets in `config.json` durch Umgebungsvariablen / `.env` ersetzen (mit `python-dotenv`); `config.json` nur noch für nicht-sensible Settings | P0 | M |
| 1.2 | Bare `except:` in `get_weather_sync()`, `get_tasks_sync()`, `fetch_news()` durch `except Exception as e:` mit Logging ersetzen | P1 | S |
| 1.3 | AppleScript-Escaping in `notes_tools.py` härten (auch Backticks, NUL-Bytes prüfen) — aktuell nur `"` und `\` | P1 | S |
| 1.4 | `[ACTION:OPEN]` validiert URLs nicht — `webbrowser.open(p)` mit beliebigem User-Input ist riskant. Whitelist auf `http/https`-Schemes | P1 | S |
| 1.5 | `/activate`, `/show`, `/hide` Endpoints haben keine Auth — auf localhost ok, aber bei Port-Forwarding offen. Optional: Token-Header | P2 | S |
| 1.6 | `OAUTHLIB_INSECURE_TRANSPORT=1` in `google-auth.py` nur für localhost-Callback nötig — dokumentieren warum es ok ist | P2 | XS |

---

## M2 — 🐛 Bug Fixes (Priorität: HOCH)

**Ziel:** Aktive Fehler aus dem Log beheben.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 2.1 | **Browser-Singleton stirbt**: `_browser` bleibt gesetzt nachdem User Chromium-Fenster schließt → `BrowserContext.new_page` schlägt fehl. Im Log nachweisbar bei "suche aktuelle Rezepte für Lachs". Fix: vor jedem `new_page()` `is_connected()` prüfen, sonst neu starten | P0 | M |
| 2.2 | **Python 3.9 statt 3.10+ läuft tatsächlich** (Log zeigt `Python/3.9/...`). SETUP.md fordert 3.10+. Dokumentieren wie auf 3.12 upgraden via `brew install python@3.12` und `python3.12 server.py` starten | P0 | S |
| 2.3 | **FastAPI Deprecation**: `@app.on_event("startup")` → durch `lifespan` Context-Manager ersetzen | P1 | S |
| 2.4 | **Fehlende Dependencies in `requirements.txt`**: `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `dateparser`, `pvporcupine` (für wakeword-trigger). Ohne sie crashen Calendar/Wakeword sofort | P0 | S |
| 2.5 | **`launch-session.ps1` fehlt komplett** (clap-trigger.py referenziert sie für Windows). Entweder erstellen oder Windows-Pfad entfernen | P1 | M |
| 2.6 | **`refresh_data()` blockiert Event-Loop** beim Startup (sync, mit `urllib.request`). Auf async umstellen oder in Executor verlagern | P1 | S |
| 2.7 | **`recognition` undefined**: in `frontend/main.js` wird `recognition.start()` aufgerufen ohne Check ob `SpeechRecognition` existiert (Firefox/Safari ohne Chromium → JS-Crash) | P1 | S |
| 2.8 | **Doppelte `refresh_data()`** beim Activate: einmal in `process_message`, einmal beim Boot. Cooldown gegen Doppel-Refresh | P2 | XS |
| 2.9 | **Mail-Action**: `KEINE_MAILS`-Branch wirft alte LLM-Antwort weg, generiert eigene Nachricht — inkonsistent mit anderen Actions | P2 | S |

---

## M3 — 🧹 Code Quality & Refactoring (Priorität: MITTEL)

**Ziel:** Wartbarkeit und Lesbarkeit verbessern, ohne Verhalten zu ändern.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 3.1 | **`server.py` aufteilen** (690 Zeilen): `prompt.py` (build_system_prompt), `actions.py` (execute_action), `scheduler.py` (morning_brief), `holidays.py` (NRW-Feiertage). `server.py` nur noch FastAPI-Routing + WebSocket | P1 | L |
| 3.2 | **Logging-Modul statt `print(..., flush=True)`** verwenden — mit Levels (INFO/WARN/ERROR), Rotation, Format. `jarvis.log` ist aktuell unstrukturiert | P1 | M |
| 3.3 | **Type Hints flächendeckend** ergänzen (`def get_events(days: int = 7) -> str:` ist Anfang) | P2 | M |
| 3.4 | **Magic Numbers in Config**: `GREETING_COOLDOWN`, `ACTIVATE_COOLDOWN`, `THRESHOLD`, Port `8340`, ElevenLabs-Model `eleven_turbo_v2_5` | P2 | S |
| 3.5 | **Imports konsolidieren**: `import urllib.request` und `import asyncio` mitten in Funktionen → nach oben | P2 | XS |
| 3.6 | **Conversations-Cleanup**: `conversations[session_id]` wächst pro Session monoton bis Disconnect — Limit auf letzte N + Cleanup älterer Sessions nach Inaktivität | P2 | S |
| 3.7 | **Konsistente Sprache** in Docstrings/Kommentaren: aktuell Mix aus Deutsch und Englisch. Empfehlung: Code-Kommentare und Docstrings auf Englisch (Standard), User-Strings auf Deutsch | P2 | M |
| 3.8 | **Globale `httpx.AsyncClient`** in `server.py` definiert, aber Tools spinnen eigene auf — entweder zentralen Client teilen oder global wegnehmen | P2 | S |

---

## M4 — 🧪 Tests & CI (Priorität: MITTEL)

**Ziel:** Regressionen früh fangen.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 4.1 | **Pytest-Setup** mit Fixtures für httpx-Mock und Anthropic-Mock | P1 | M |
| 4.2 | **Unit-Tests** für `get_easter()`, `check_free_day()`, `extract_action()`, `_split_text()` (pure Funktionen, einfach testbar) | P1 | M |
| 4.3 | **Integrationstest** für `execute_action(SEARCH/NEWS/CALENDAR/...)` mit gemockten Backends | P2 | L |
| 4.4 | **GitHub Actions Workflow**: bei Push Pytest + Linter (ruff/black) ausführen | P2 | S |

---

## M5 — 📚 Dokumentation (Priorität: MITTEL)

**Ziel:** Doku auf Stand der Erweiterungen bringen.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 5.1 | **README.md aktualisieren**: Calendar, Mail, Notes, Todoist, Steuer-News in Feature-Liste, Architektur und Tech-Stack ergänzen | P1 | M |
| 5.2 | **README.md Repo-URL** korrigieren — aktuell `Julian-Ivanov/jarvis-voice-assistant`, sollte auf Catrins Fork zeigen | P1 | XS |
| 5.3 | **`config.example.json` ergänzen** um `todoist_api_token`, `picovoice_access_key`, `user_role`, `morning_hour` | P1 | S |
| 5.4 | **CHANGELOG.md** anlegen mit Versionierung der Erweiterungen | P2 | S |
| 5.5 | **LICENSE-Datei** anlegen (README sagt MIT, aber keine LICENSE-Datei vorhanden) | P2 | XS |
| 5.6 | **CLAUDE.md erweitern** um Hinweise zu den neuen Tools (welche API-Keys nötig sind) | P2 | S |
| 5.7 | **Architektur-Diagramm** um neue Tools erweitern | P2 | S |

---

## M6 — ✨ Features & UX (Priorität: NIEDRIG)

**Ziel:** Komfort und Robustheit.

| # | Issue | Prio | Aufwand |
|---|-------|------|---------|
| 6.1 | **Retry-Logik** für Anthropic/ElevenLabs/Wetter-Calls (z.B. `tenacity`) — ein Netzwerkhicke darf Jarvis nicht stumm lassen | P1 | M |
| 6.2 | **Conversations persistieren** (SQLite oder JSON) — aktuell weg bei Disconnect | P2 | M |
| 6.3 | **News-Quelle konfigurierbar** statt fest Tagesschau RSS | P2 | S |
| 6.4 | **Action-Cancel** via WebSocket-Message ("Stop Jarvis") | P3 | M |
| 6.5 | **Kalender-Tage konfigurierbar** (aktuell hardcoded `days=7`) | P3 | XS |
| 6.6 | **Outlook/andere Mail-Clients** statt nur Mail.app — über IMAP-Adapter | P3 | L |
| 6.7 | **Linux-Support** für `screen_capture` (PIL `ImageGrab` braucht xdisplay-Setup) | P3 | M |

---

## Empfohlene Reihenfolge

1. **M1 + M2 zuerst** (Sicherheit + akute Bugs, ~1–2 Tage)
2. **M5 parallel** (Doku-Updates sind günstig und befreien Kopf)
3. **M3** (Refactoring) — sobald Tests stehen
4. **M4** (Tests) — am besten *vor* M3, damit Refactoring sicher ist
5. **M6** (Features) als letzte Iteration

---

## Hinweise zum Log

`jarvis.log` zeigt einen typischen Tagesablauf. Auffällig:
- **Browser-Crash bei `[ACTION:SEARCH] aktuelle Rezepte Lachs`** — siehe Issue 2.1
- **Sehr viele `/activate ignoriert (Cooldown noch ...)`** — Cooldown von 90s greift oft, evtl. zu lang oder Frontend triggert zu oft
- **Wetter zeigt englische Beschreibungen** ("Thundery outbreaks in nearby") — wttr.in unterstützt `lang=de` Parameter, könnte konfigurierbar werden
