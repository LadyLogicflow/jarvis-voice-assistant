#!/bin/bash
# Einmalige Einrichtung von JARVIS auf dem Raspberry Pi.
# Aufruf: sudo bash scripts/install-pi.sh
set -euo pipefail

REPO_DIR="/home/pi/jarvis-voice-assistant-master"
SYSTEMD="/etc/systemd/system"

echo "=== JARVIS Pi Setup ==="

# Abhängigkeiten
apt-get install -y git python3 python3-pip

# Git-Credentials (Token in Remote-URL nötig, einmalig setzen):
# git remote set-url origin https://oauth2:<TOKEN>@git.services.cl01.efiniti.de/pandora/user-h1znvt5m/jarvis.git

# Python-Pakete
pip3 install -r "$REPO_DIR/requirements.txt"

# systemd-Units kopieren
chmod +x "$REPO_DIR/scripts/jarvis-update.sh"
cp "$REPO_DIR/scripts/jarvis.service"        "$SYSTEMD/jarvis.service"
cp "$REPO_DIR/scripts/jarvis-update.service" "$SYSTEMD/jarvis-update.service"
cp "$REPO_DIR/scripts/jarvis-update.timer"   "$SYSTEMD/jarvis-update.timer"

systemctl daemon-reload
systemctl enable --now jarvis.service
systemctl enable --now jarvis-update.timer

echo ""
echo "Status prüfen:"
echo "  systemctl status jarvis"
echo "  systemctl list-timers jarvis-update.timer"
echo "  tail -f $REPO_DIR/jarvis.log"
