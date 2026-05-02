# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning loosely follows [SemVer](https://semver.org/).

## [Unreleased]

### Planned (open milestones)
- M3.1 — Split `server.py` into `prompt.py` / `actions.py` / `scheduler.py`
  / `holidays.py`. Deferred until tests (M4) are in place.
- M4 — Tests & CI (pytest, fixtures, GitHub Actions)
- M6 — Features (retry logic, conversations persistence, configurable news,
  cancellable actions, Outlook/IMAP, Linux screen capture)

---

## [0.2.0] — 2026-05-02

Major hardening + refactoring pass driven by `CODE_REVIEW.md`.

### M1 — Security & Secrets

- **M1.1** Secrets moved out of `config.json` into `.env` (python-dotenv)
- **M1.2** Bare `except:` blocks closed; failures now logged
- **M1.3** AppleScript escaping in `notes_tools.add_note` replaced with
  `osascript argv` passing — no injection surface
- **M1.4** `[ACTION:OPEN]` whitelisted to http(s) URLs only
- **M1.5** Optional `JARVIS_AUTH_TOKEN` for `/activate`, `/show`, `/hide`
  endpoints; `launch-session.sh` and `.ps1` send the header automatically
- **M1.6** `OAUTHLIB_INSECURE_TRANSPORT` documented inline

### M2 — Bug Fixes

- **M2.1** Browser singleton now re-launches Chromium when the user closed
  the visible window (previously every following SEARCH crashed)
- **M2.2** Setup docs spell out the Python 3.10+ upgrade path
- **M2.3** Deprecated `@app.on_event` replaced with `lifespan` context
- **M2.4** Missing dependencies added: google-api-python-client, google-auth,
  google-auth-oauthlib, dateparser, pvporcupine
- **M2.5** Created `scripts/launch-session.ps1` (Windows counterpart that
  was referenced but missing)
- **M2.6** `refresh_data()` is async, no longer blocks startup
- **M2.7** `frontend/main.js` no longer crashes in Firefox/Safari
  (SpeechRecognition feature-detect)
- **M2.8** `refresh_data()` cooldown (30 s) prevents double-refresh on activate
- **M2.9** `KEINE_MAILS` / `KEINE_TERMINE` / `KEINE_TASKS` handled uniformly
  via hardcoded butler responses (no extra LLM round-trip)

### M3 — Code Quality

- **M3.2** `print(..., flush=True)` replaced with the `logging` module
  (RotatingFileHandler 10 MB × 3 backups + StreamHandler)
- **M3.3** Return types added to all public functions
- **M3.4** Tunables (`server_port`, `elevenlabs_model`, all cooldowns,
  clap-trigger thresholds) moved into `config.json` with sane defaults
- **M3.5** Inline imports hoisted to module top per PEP 8
- **M3.6** Per-session conversation history capped at 50 messages
- **M3.7** Code-internal docstrings/comments translated to English
  (user-facing strings stay German)
- **M3.8** Global `httpx.AsyncClient` lifecycle documented; closed in
  lifespan teardown

### M5 — Documentation

- **M5.1** README updated with Calendar / Mail / Notes / Todoist /
  Steuer-News features and tech stack
- **M5.2** Repo URLs in README point to `LadyLogicflow` fork
- **M5.3** `config.example.json` covers every config key now in use
- **M5.5** Added `LICENSE` (MIT)
- **M5.6** `CLAUDE.md` documents `.env` workflow + every tool's API key
- **M5.7** Architecture diagram includes the new tool integrations

---

## [0.1.0] — Pre-2026-05-02

Initial fork from Julian Ivanov's
[`jarvis-voice-assistant`](https://github.com/Julian-Ivanov/jarvis-voice-assistant).

Catrin's extensions on top of the original template:

- Google Calendar integration (read events, add events with natural-language
  date parsing)
- macOS Mail.app integration (read unread messages via AppleScript)
- macOS Notes.app integration (create notes via AppleScript)
- Todoist integration (read / add / complete tasks)
- BFH steuerrechtliche News (RSS-aggregated morning brief, daily at
  `morning_hour`)
- macOS launch script (`launch-session.sh`) replacing the original
  PowerShell-only flow
