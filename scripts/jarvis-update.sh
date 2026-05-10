#!/bin/bash
# Auto-updater: pulls latest changes from git, restarts JARVIS if anything changed.
set -euo pipefail

REPO_DIR="__REPO__"
SERVICE="jarvis"
LOG="__REPO__/jarvis-update.log"

cd "$REPO_DIR"

git fetch origin main 2>>"$LOG"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') | new commits detected, pulling…" >>"$LOG"
git pull origin main >>"$LOG" 2>&1
"__REPO__/.venv/bin/pip" install -r "__REPO__/requirements.txt" >>"$LOG" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') | restarting $SERVICE" >>"$LOG"
systemctl restart "$SERVICE"
echo "$(date '+%Y-%m-%d %H:%M:%S') | done" >>"$LOG"
