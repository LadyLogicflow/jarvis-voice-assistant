#!/usr/bin/env bash
# Jarvis — Launch Session (macOS)

set -e

# When invoked from macOS Shortcuts the PATH is minimal — extend it so
# Homebrew binaries (osascript is /usr/bin, but lsof / curl / pgrep
# can be missing on Apple Silicon depending on user profile setup).
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE="$(dirname "$SCRIPT_DIR")"

# Pick the right Python: prefer the project's venv (has all packages
# + a recent enough Python). Fall back to /opt/homebrew/bin/python3 or
# system /usr/bin/python3.
if [ -x "$WORKSPACE/.venv/bin/python" ]; then
    JARVIS_PY="$WORKSPACE/.venv/bin/python"
elif [ -x /opt/homebrew/bin/python3 ]; then
    JARVIS_PY=/opt/homebrew/bin/python3
elif [ -x /usr/local/bin/python3 ]; then
    JARVIS_PY=/usr/local/bin/python3
else
    JARVIS_PY=/usr/bin/python3
fi

# Read the JARVIS_AUTH_TOKEN value from .env without sourcing the file.
# (Sourcing breaks when a value contains shell-special chars like &, $, ;.)
read_env_value() {
    local key="$1"
    [ -f "$WORKSPACE/.env" ] || return 0
    awk -v k="$key" '
        $0 ~ "^[ \\t]*"k"=" {
            sub("^[ \\t]*"k"=", "")
            # Strip optional surrounding single or double quotes.
            sub("^[\"\x27]", "")
            sub("[\"\x27]$", "")
            print
            exit
        }' "$WORKSPACE/.env"
}

JARVIS_AUTH_TOKEN="$(read_env_value JARVIS_AUTH_TOKEN)"

# Build curl auth args once (empty when JARVIS_AUTH_TOKEN is unset).
CURL_AUTH=()
if [ -n "${JARVIS_AUTH_TOKEN:-}" ]; then
    CURL_AUTH=(-H "X-Jarvis-Token: $JARVIS_AUTH_TOKEN")
fi

# 1. Jarvis-Server starten (falls nicht läuft) — kein Terminal-Fenster
if ! lsof -i tcp:8340 -sTCP:LISTEN -t &>/dev/null; then
    cd "$WORKSPACE"
    nohup "$JARVIS_PY" "$WORKSPACE/server.py" \
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
