#!/usr/bin/env bash
# Jarvis — Launch Session (macOS)

set -e
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE="$(dirname "$SCRIPT_DIR")"

# Load .env if present so JARVIS_AUTH_TOKEN is available to both the
# server we launch and the curl calls below.
if [ -f "$WORKSPACE/.env" ]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    . "$WORKSPACE/.env"
    set +a
fi

# Build curl auth args once (empty when JARVIS_AUTH_TOKEN is unset).
CURL_AUTH=()
if [ -n "${JARVIS_AUTH_TOKEN:-}" ]; then
    CURL_AUTH=(-H "X-Jarvis-Token: $JARVIS_AUTH_TOKEN")
fi

# 1. Jarvis-Server starten (falls nicht läuft) — kein Terminal-Fenster
if ! lsof -i tcp:8340 -sTCP:LISTEN -t &>/dev/null; then
    nohup /usr/bin/python3 "$WORKSPACE/server.py" \
        > "$WORKSPACE/jarvis.log" 2>&1 &
    sleep 3
fi

# 2. Chrome: Jarvis bereits offen? Wake-Signal senden statt Chrome neu starten
if pgrep -f "localhost:8340" > /dev/null 2>&1; then
    # Jarvis läuft bereits — Fenster nach vorne + Wake-Signal an Frontend
    osascript -e 'tell application "Google Chrome" to activate' 2>/dev/null
    curl -s "${CURL_AUTH[@]}" http://localhost:8340/activate > /dev/null 2>&1
    echo "[jarvis] Wake-Signal gesendet."
else
    # Chrome mit Autoplay-Flag starten
    osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null || true
    sleep 1.5

    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
        --autoplay-policy=no-user-gesture-required \
        --app="http://localhost:8340" &>/dev/null &

    # 3. Fenster auf gewünschte Position und Größe setzen + Begrüßung auslösen
    sleep 3
    osascript -e 'tell application "Google Chrome" to set bounds of front window to {2, 30, 1905, 1075}'
    curl -s "${CURL_AUTH[@]}" http://localhost:8340/activate > /dev/null 2>&1

    echo "[jarvis] Session gestartet."
fi
