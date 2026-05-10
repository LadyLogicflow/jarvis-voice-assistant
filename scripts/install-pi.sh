#!/bin/bash
# Einmalige Einrichtung von JARVIS auf dem Raspberry Pi.
# Aufruf: sudo bash scripts/install-pi.sh
set -euo pipefail

JARVIS_USER="${SUDO_USER:-$(logname)}"
JARVIS_HOME="/home/$JARVIS_USER"
REPO_DIR="$JARVIS_HOME/jarvis-voice-assistant-master"
SYSTEMD="/etc/systemd/system"

echo "=== JARVIS Pi Setup (user: $JARVIS_USER) ==="

# Abhängigkeiten
apt-get install -y git python3 python3-pip

# Python-Pakete
sudo -u "$JARVIS_USER" pip3 install -r "$REPO_DIR/requirements.txt"

# systemd-Units aus Templates generieren (Platzhalter ersetzen)
for f in jarvis.service jarvis-update.service jarvis-update.timer; do
    sed "s|__USER__|$JARVIS_USER|g; s|__HOME__|$JARVIS_HOME|g; s|__REPO__|$REPO_DIR|g" \
        "$REPO_DIR/scripts/$f" > "$SYSTEMD/$f"
done
chmod +x "$REPO_DIR/scripts/jarvis-update.sh"
sed -i "s|__HOME__|$JARVIS_HOME|g; s|__REPO__|$REPO_DIR|g" \
    "$REPO_DIR/scripts/jarvis-update.sh"

systemctl daemon-reload
systemctl enable --now jarvis.service
systemctl enable --now jarvis-update.timer

echo ""
echo "Status prüfen:"
echo "  systemctl status jarvis"
echo "  systemctl list-timers jarvis-update.timer"
echo "  tail -f $REPO_DIR/jarvis.log"
