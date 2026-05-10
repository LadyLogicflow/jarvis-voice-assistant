#!/bin/bash
# Auto-updater: pulls latest changes from git, restarts JARVIS if anything changed.
set -euo pipefail

REPO_DIR="/home/pi/jarvis-voice-assistant-master"
SERVICE="jarvis"
LOG="/home/pi/jarvis-voice-assistant-master/jarvis-update.log"

cd "$REPO_DIR"

git fetch origin main 2>>"$LOG"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') | new commits detected, pulling…" >>"$LOG"
git pull origin main >>"$LOG" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') | restarting $SERVICE" >>"$LOG"
systemctl restart "$SERVICE"
echo "$(date '+%Y-%m-%d %H:%M:%S') | done" >>"$LOG"
