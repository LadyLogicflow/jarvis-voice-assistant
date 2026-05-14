#!/bin/bash
# Wird auf dem Pi ausgefuehrt (via jarvis-deploy.command vom Mac).
# Stoppt Jarvis, zieht Git-Updates, startet Jarvis neu.
set -euo pipefail

REPO="/home/caterina/jarvis-voice-assistant-master"
LOG="$REPO/jarvis.log"

cd "$REPO"

echo "■ Stoppe JARVIS..."
pkill -f 'python.*server.py' || true
sleep 2

echo "■ Hole neueste Aenderungen..."
git pull origin main

echo "■ Starte JARVIS neu..."
nohup .venv/bin/python -u server.py >> "$LOG" 2>&1 &
JARVIS_PID=$!
sleep 3

if kill -0 "$JARVIS_PID" 2>/dev/null; then
    echo "==== JARVIS laeuft (PID $JARVIS_PID) ===="
else
    echo "==== FEHLER: JARVIS gestartet aber gleich beendet ===="
    tail -20 "$LOG"
    exit 1
fi
